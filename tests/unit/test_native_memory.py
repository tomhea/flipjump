"""
unit-tests for the native engine's memory (_fjcore.Memory).

the key property pinned here is the memory footprint at scale: declaring huge segments
(like prime_sieve's half-the-address-space reserve) must
not allocate memory upfront - pages are allocated lazily, so the footprint scales with the
words actually touched, not with the declared segment sizes.

also pins the flat-storage mode selection: the configurable span limit (constructor
parameter / FLIPJUMP_FLAT_MAX_WORDS env var / 2^23-word default), the paged fallback on a
failed flat-array allocation, and the storage_mode observability ('flat'/'paged').
"""

from typing import Any

import pytest

try:
    from flipjump.interpreter import _fjcore  # type: ignore[attr-defined]
except ImportError:
    _fjcore = None

from flipjump.utils.exceptions import IOReadOnEOF

pytestmark = pytest.mark.skipif(_fjcore is None, reason='the native engine (_fjcore) is not built')


@pytest.fixture(autouse=True)
def clean_flat_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # the flat-mode env knobs must not leak into (or from) these tests
    monkeypatch.delenv('FLIPJUMP_NO_FLAT', raising=False)
    monkeypatch.delenv('FLIPJUMP_FLAT_MAX_WORDS', raising=False)
    monkeypatch.delenv('FLIPJUMP_TEST_FLAT_ALLOC_FAIL', raising=False)


def _unexpected_io(*args: object) -> bool:
    raise AssertionError('the program must not perform IO')


def _looping_memory(**kwargs: Any) -> Any:
    """a w=32 memory holding one op at address 0: a harmless flip (word 4) + jump-to-self."""
    memory = _fjcore.Memory(32, **kwargs)
    memory.add_segment(0, 8)
    memory.set_words(0, [128, 0])
    return memory


def _run_to_looping(memory: Any) -> None:
    cause, op_count, _, _, _ = memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)
    assert cause == _fjcore.TERM_LOOPING
    assert op_count == 1


def test_huge_segment_is_not_allocated_upfront() -> None:
    # half the w=32 address space (2^26 words = 512MB if dense at 8B/word)
    memory = _fjcore.Memory(32)
    memory.add_segment(0, 1 << 26)
    assert memory.allocated_bytes < 1 << 20  # nothing touched - under 1MB


def test_footprint_scales_with_touched_memory_at_scale() -> None:
    # ~1M words of data/code spread over a 2^26-word segment: the footprint must stay
    # near the touched size, far below dense.
    memory = _fjcore.Memory(32)
    memory.add_segment(0, 1 << 26)

    touched_words = 1_000_000
    stride = (1 << 26) // touched_words
    for i in range(0, touched_words, 4096):  # touch one word in each region
        memory.set_word(i * stride, 0xDEADBEEF & 0xFFFFFFFF)

    # each touch allocates at most one 128KB page
    assert memory.allocated_bytes < 256 * (1 << 20)  # well below the 512MB dense equivalent
    assert memory.get_word(0) == 0xDEADBEEF & 0xFFFFFFFF


def test_dense_data_footprint_is_proportional() -> None:
    # a contiguous 1M-word program: footprint ~8MB (8B/word) + page table
    memory = _fjcore.Memory(32)
    memory.add_segment(0, 1 << 20)
    memory.set_words(0, list(range(1 << 20)))
    assert memory.allocated_bytes < 12 * (1 << 20)
    assert memory.get_word(12345) == 12345


def test_get_set_word_roundtrip_and_masking() -> None:
    memory = _fjcore.Memory(16)
    memory.add_segment(0, 1024)
    memory.set_word(7, 0x12345)  # masked to 16 bits
    assert memory.get_word(7) == 0x2345
    assert memory.get_word(8) == 0


def test_storage_mode_is_none_before_the_first_run() -> None:
    assert _looping_memory().storage_mode is None


def test_storage_mode_flat_for_a_compact_program() -> None:
    memory = _looping_memory()
    _run_to_looping(memory)
    assert memory.storage_mode == 'flat'


def test_flat_max_words_parameter_limits_the_flat_window() -> None:
    memory = _looping_memory(flat_max_words=4)  # the segment ends at word 8, above the limit
    _run_to_looping(memory)
    assert memory.storage_mode == 'hybrid'  # words 0..3 flat, the rest page-backed


def test_flat_max_words_parameter_can_raise_the_limit() -> None:
    # a segment ending beyond the default 2^23-word limit still runs flat with a raised limit
    memory = _fjcore.Memory(32, flat_max_words=1 << 25)
    memory.add_segment(0, 8)
    memory.set_words(0, [128, 0])
    memory.add_segment((1 << 24) - 8, 8)
    _run_to_looping(memory)
    assert memory.storage_mode == 'flat'


def test_flat_max_words_env_var_limits_the_flat_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FLIPJUMP_FLAT_MAX_WORDS', '4')
    memory = _looping_memory()
    _run_to_looping(memory)
    assert memory.storage_mode == 'hybrid'


def test_flat_max_words_parameter_overrides_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FLIPJUMP_FLAT_MAX_WORDS', '4')
    memory = _looping_memory(flat_max_words=1 << 23)
    _run_to_looping(memory)
    assert memory.storage_mode == 'flat'


def test_flat_allocation_failure_falls_back_to_paged(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # a failed flat allocation warns to stderr and falls back to paged (the run still succeeds)
    monkeypatch.setenv('FLIPJUMP_TEST_FLAT_ALLOC_FAIL', '1')
    memory = _looping_memory()
    _run_to_looping(memory)
    assert memory.storage_mode == 'paged'
    assert 'FLIPJUMP_NO_FLAT' in capfd.readouterr().err  # the actionable warning was printed


def test_reinitializing_a_memory_does_not_crash() -> None:
    # __init__ can be called again on a live object - the old allocations must be released
    memory = _looping_memory()
    _run_to_looping(memory)  # builds the flat array
    memory.__init__(16)
    memory.add_segment(0, 8)
    memory.set_word(3, 0x123)
    assert memory.get_word(3) == 0x123


def test_set_words_beyond_the_flat_span_raises() -> None:
    memory = _looping_memory()
    _run_to_looping(memory)  # flat storage is built now
    assert memory.storage_mode == 'flat'
    with pytest.raises(ValueError):
        memory.set_words(6, [1, 2, 3])  # words 6..8, beyond the 8-word span


def test_no_flat_env_var_forces_paged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FLIPJUMP_NO_FLAT', '1')
    memory = _looping_memory()
    _run_to_looping(memory)
    assert memory.storage_mode == 'paged'


def test_negative_last_ops_length_behaves_like_no_ring() -> None:
    memory = _looping_memory()
    cause, op_count, _, last_ops, _ = memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF, last_ops_length=-1)
    assert cause == _fjcore.TERM_LOOPING
    assert last_ops == []


def test_set_words_near_the_address_space_top_raises() -> None:
    # a wrapping (start + count overflows 64 bits) range must be rejected, not write low pages
    memory = _fjcore.Memory(32)
    memory.add_segment(0, 8)
    with pytest.raises(ValueError):
        memory.set_words((1 << 64) - 2, [1, 2, 3])


def test_huge_flat_limit_overflow_falls_back_to_paged() -> None:
    # a raised limit whose byte-size overflows size_t warns + falls back to paged (not a crash)
    memory = _fjcore.Memory(32, flat_max_words=1 << 62)
    memory.add_segment(0, 8)
    memory.set_words(0, [128, 0])
    memory.add_segment(1 << 61, 8)
    _run_to_looping(memory)
    assert memory.storage_mode == 'paged'


def test_run_with_no_segments_raises() -> None:
    # no segments -> no first op at address 0 -> error, not a silent paged run
    memory = _fjcore.Memory(32)
    with pytest.raises(ValueError):
        memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)


def test_run_with_only_a_far_segment_raises() -> None:
    # a garbage-stop program whose only segment is above the flat window has no first op at
    # address 0 - impossible for a valid program, so it raises (not a silent paged run)
    memory = _fjcore.Memory(32, flat_max_words=4)
    memory.add_segment(100, 8)
    with pytest.raises(ValueError):
        memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)


# ---------------------------------------- self-modifying ops (the flat run-loop's rare paths)


def _self_modifying_memory(**kwargs: Any) -> Any:
    """a w=32 program whose op A gets its jump word rewritten between two executions.
    flow: entry -> A -> B (flips A's jump) -> A -> C (loops).
    5 ops; afterwards A's jump word (word 5) is 320 and data word 16 reads 0b101.

    Ops are placed at ip=0/128/256/320 (word_address 0/4/8/10) to stay outside w=32's
    input range (38, 102] (an ip there would trigger unexpected read_bit calls).
    B flips bit 6 of word 5 (A's jump word): 256 ^ 64 = 320 = C.
    """
    memory = _fjcore.Memory(32, **kwargs)
    memory.add_segment(0, 18)
    #                    flip  jump
    memory.set_words(0, [512, 128])  # entry (ip=0):   flip data bit 512, jump to A (128)
    memory.set_words(4, [513, 256])  # A     (ip=128): flip data bit 513, jump to B (->320=C)
    memory.set_words(8, [166, 128])  # B     (ip=256): flip bit 6 of word 5 (A's jump: 256->320)
    memory.set_words(10, [514, 320])  # C     (ip=320): flip data bit 514, loop
    return memory


def test_a_rewritten_jump_word_dispatches_correctly() -> None:
    # the flat loop reads each op's jump word after its flip - a jump word rewritten by an
    # earlier op (the wflip dispatch idiom) must be honored on the next execution.
    memory = _self_modifying_memory()
    cause, op_count, _, _, _ = memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)
    assert cause == _fjcore.TERM_LOOPING
    assert op_count == 5  # entry, A, B, A, C
    assert memory.storage_mode == 'flat'
    assert memory.get_word(5) == 320  # A's rewritten jump word (word_address 5 = ip 128+w)
    assert memory.get_word(16) == 0b101  # bits 512+514 once; 513 twice (A ran twice) = net 0


# --------------------------------------------------- w=64 flat storage (magic gap sentinel)


def _looping_memory_w64(**kwargs: Any) -> Any:
    """a w=64 memory holding one op at address 0: a harmless flip (word 4) + jump-to-self."""
    memory = _fjcore.Memory(64, **kwargs)
    memory.add_segment(0, 8)
    memory.set_words(0, [256, 0])
    return memory


def _self_modifying_memory_w64(**kwargs: Any) -> Any:
    """the w=64 mirror of _self_modifying_memory: entry -> A -> B (flips A's jump) -> A -> C.
    ips 0/256/512/640 stay outside w=64's input range (71, 199]. B flips bit 7 of word 5
    (A's jump word): 512 ^ 128 = 640 = C. afterwards word 5 is 640 and word 16 reads 0b101."""
    memory = _fjcore.Memory(64, **kwargs)
    memory.add_segment(0, 18)
    #                     flip  jump
    memory.set_words(0, [1024, 256])  # entry (ip=0):   flip data bit 1024, jump to A (256)
    memory.set_words(4, [1025, 512])  # A     (ip=256): flip data bit 1025, jump to B (->640=C)
    memory.set_words(8, [327, 256])  # B     (ip=512): flip bit 7 of word 5 (A's jump: 512->640)
    memory.set_words(10, [1026, 640])  # C     (ip=640): flip data bit 1026, loop
    return memory


def _run_self_modifying_w64(memory: Any) -> None:
    cause, op_count, _, _, _ = memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)
    assert cause == _fjcore.TERM_LOOPING
    assert op_count == 5  # entry, A, B, A, C
    assert memory.get_word(5) == 640  # A's rewritten jump word
    assert memory.get_word(16) == 0b101  # bits 1024+1026 once; 1025 twice (A ran twice) = net 0


def test_w64_compact_program_runs_flat() -> None:
    memory = _looping_memory_w64()
    _run_to_looping(memory)
    assert memory.storage_mode == 'flat'


def test_w64_flat_matches_paged_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    flat = _self_modifying_memory_w64()
    _run_self_modifying_w64(flat)
    assert flat.storage_mode == 'flat'

    monkeypatch.setenv('FLIPJUMP_NO_FLAT', '1')
    paged = _self_modifying_memory_w64()
    _run_self_modifying_w64(paged)
    assert paged.storage_mode == 'paged'


def test_w64_flat_gap_touch_is_a_memory_error() -> None:
    # words 8..15 lie between the two segments - flipping into them must report a memory
    # error at the gap word's bit address, exactly like the paged path does.
    memory = _fjcore.Memory(64)
    memory.add_segment(0, 8)
    memory.add_segment(16, 8)
    memory.set_words(0, [9 * 64, 128])  # flip a bit of gap word 9
    cause, _, error_address, _, _ = memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)
    assert memory.storage_mode == 'flat'
    assert cause == _fjcore.TERM_MEMORY_ERROR
    assert error_address == 9 * 64


def test_w64_word_holding_the_garbage_magic_is_real_data() -> None:
    # the magic gap-fill value is a legal 64-bit program word; an in-segment word holding
    # it must read back and flip like any other value (the segment list disambiguates).
    magic = _fjcore.FLAT_GARBAGE_MAGIC
    memory = _fjcore.Memory(64)
    memory.add_segment(0, 16)
    memory.set_words(0, [8 * 64, 0])  # flip bit 0 of word 8 (which holds the magic), loop
    memory.set_word(8, magic)
    cause, op_count, _, _, _ = memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)
    assert memory.storage_mode == 'flat'
    assert cause == _fjcore.TERM_LOOPING
    assert op_count == 1
    assert memory.get_word(8) == magic ^ 1


def test_w64_untouched_in_segment_word_reads_zero() -> None:
    memory = _looping_memory_w64()
    _run_to_looping(memory)
    assert memory.storage_mode == 'flat'
    assert memory.get_word(6) == 0  # in-segment, never written


def test_w64_far_segment_makes_hybrid() -> None:
    # sieve-style programs reserve far/huge segments - the low window still runs flat
    memory = _looping_memory_w64()
    memory.add_segment(1 << 50, 8)
    _run_to_looping(memory)
    assert memory.storage_mode == 'hybrid'
    assert memory.get_word(6) == 0  # low in-segment untouched word, via the flat window


def test_w64_api_write_to_a_gap_survives_the_flat_build() -> None:
    # device/debugger pokes outside the declared segments are page-backed in flat mode
    # (set_word/get_word route by segment membership), so they behave exactly as in
    # paged mode - including data written before the storage decision.
    memory = _fjcore.Memory(64)
    memory.add_segment(0, 8)
    memory.add_segment(16, 8)
    memory.set_words(0, [256, 0])  # a looping op
    memory.set_word(9, 0x77)  # gap word, written pre-run (lands in a page)
    _run_to_looping(memory)
    assert memory.storage_mode == 'flat'
    assert memory.get_word(9) == 0x77  # survived the flat build, page-backed
    memory.set_word(9, 0x78)  # and still writable post-build
    assert memory.get_word(9) == 0x78


# ------------------------------------------ hybrid storage (low flat window + paged far)

FAR = 1 << 26  # a word address far above the default 2^23-word flat window (w=32: < 2^27)


def test_hybrid_low_code_flips_far_data() -> None:
    # the sieve shape: compact code low, a data table far away. the code runs through the
    # flat window; the flip lands in the far segment through the paged machinery.
    memory = _fjcore.Memory(32)
    memory.add_segment(0, 8)
    memory.add_segment(FAR, 8)
    #                    flip      jump
    # ip=128 (not 64): aligned ips inside w=32's input range (38, 102] would trigger IO
    memory.set_words(0, [FAR * 32, 128])  # ip=0:   flip bit 0 of the far word, jump on
    memory.set_words(4, [224, 128])  # ip=128: flip bit 0 of word 7, loop
    cause, op_count, _, _, _ = memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)
    assert cause == _fjcore.TERM_LOOPING
    assert op_count == 2
    assert memory.storage_mode == 'hybrid'
    assert memory.get_word(FAR) == 1
    assert memory.get_word(7) == 1


def test_hybrid_far_gap_touch_is_a_memory_error() -> None:
    # touching out-of-segment memory above the flat window errors through the paged
    # access checks, exactly like a pure-paged run.
    memory = _fjcore.Memory(32)
    memory.add_segment(0, 8)
    memory.add_segment(FAR, 8)
    memory.set_words(0, [(FAR + 100) * 32, 128])
    memory.set_words(4, [224, 128])
    cause, _, error_address, _, _ = memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)
    assert memory.storage_mode == 'hybrid'
    assert cause == _fjcore.TERM_MEMORY_ERROR
    assert error_address == (FAR + 100) * 32


def test_hybrid_segment_straddling_the_cut() -> None:
    # one segment crossing the flat window: its low part lives in flat, its high part is
    # page-backed - flips and reads must agree on both sides.
    memory = _fjcore.Memory(32, flat_max_words=4)
    memory.add_segment(0, 8)
    memory.set_words(0, [192, 0])  # flip bit 0 of word 6 (above the cut, in-segment), loop
    cause, op_count, _, _, _ = memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)
    assert cause == _fjcore.TERM_LOOPING
    assert op_count == 1
    assert memory.storage_mode == 'hybrid'
    assert memory.get_word(6) == 1


def test_hybrid_op_at_the_cut_boundary() -> None:
    # an op whose two words straddle the cut (flip word below, jump word above) takes the
    # slow per-word lanes and must execute exactly like anywhere else.
    memory = _fjcore.Memory(32, flat_max_words=5)
    memory.add_segment(0, 8)
    memory.set_words(0, [224, 128])  # ip=0:   flip bit 0 of word 7, jump to ip 128
    memory.set_words(4, [193, 128])  # ip=128: words 4|5 straddle the cut; flip word 6 bit 1, loop
    cause, op_count, _, _, _ = memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)
    assert cause == _fjcore.TERM_LOOPING
    assert op_count == 2
    assert memory.storage_mode == 'hybrid'
    assert memory.get_word(7) == 1
    assert memory.get_word(6) == 2


def test_hybrid_far_code_executes() -> None:
    # code above the cut runs through the slow lanes - correct, just not fast
    memory = _fjcore.Memory(32)
    memory.add_segment(0, 8)
    memory.add_segment(FAR, 8)
    memory.set_words(0, [224, FAR * 32])  # ip=0: flip bit 0 of word 7, jump to the far op
    memory.set_words(FAR, [225, FAR * 32])  # far op: flip bit 1 of word 7, loop
    cause, op_count, _, _, _ = memory.run(_unexpected_io, _unexpected_io, IOReadOnEOF)
    assert cause == _fjcore.TERM_LOOPING
    assert op_count == 2
    assert memory.storage_mode == 'hybrid'
    assert memory.get_word(7) == 0b11


def test_hybrid_device_pokes_route_correctly() -> None:
    memory = _fjcore.Memory(32)
    memory.add_segment(0, 8)
    memory.add_segment(16, 8)
    memory.add_segment(FAR, 8)
    memory.set_words(0, [128, 0])
    _run_to_looping(memory)
    assert memory.storage_mode == 'hybrid'
    memory.set_word(3, 7)  # below the cut, in-segment -> the flat window
    memory.set_word(9, 5)  # below the cut, gap -> page-backed (device-only memory)
    memory.set_word(FAR + 2, 9)  # above the cut -> page-backed
    assert memory.get_word(3) == 7
    assert memory.get_word(9) == 5
    assert memory.get_word(FAR + 2) == 9


def test_cpu_calibrate_is_deterministic_and_well_formed() -> None:
    # the L1 throughput yardstick: for a fixed iteration count the checksum is deterministic
    # (pure uint64 arithmetic, endianness-independent) - same on every arch, so the work can't be
    # optimized away and a mismatch would flag a miscompiled calibration. ops = 4 lanes x iters.
    result = _fjcore.cpu_calibrate(1_000_000)
    assert result['iterations'] == 1_000_000
    assert result['ops'] == 4_000_000
    assert result['seconds'] > 0.0
    assert _fjcore.cpu_calibrate(1_000_000)['checksum'] == result['checksum']  # deterministic


def test_cpu_calibrate_rejects_degenerate_args() -> None:
    with pytest.raises(ValueError):
        _fjcore.cpu_calibrate(0)  # iterations must be >= 1
