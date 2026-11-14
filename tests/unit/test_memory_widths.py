"""
unit-tests for the memory widths: every power-of-two width from 8 up assembles, is written, and runs - the widths up
to 64 on the native engine, the wider ones on the python loops. What 1.6.0 added (the table pool, its pins, the
compact op stream) is run at w=64 and w=1024 too, and at the edges: addresses that fit no 63-bit slot, a pool above
2^64, a segment past word 2^64 (the .fjm's w-bit segment fields), a word that does not fit its width.
"""

from pathlib import Path
import io
import struct
import sys
from typing import Any, List, Optional

import pytest

from flipjump import assemble, run
from flipjump.assembler.inner_classes.table_pool import BlockPool
from flipjump.fjm.fjm_consts import (
    FJMVersion,
    is_supported_memory_width,
    pack_segments,
    segment_field_size,
    unpack_segments,
)
from flipjump.fjm.fjm_reader import Reader, read_fjm_header
from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.interpreter.io_devices.StandardIO import StandardIO
from flipjump.utils.exceptions import FlipJumpException, FlipJumpFlatMemoryException
from tests.unit.pool_fuzzer import fuzz_case
from tests.unit.relocation_proof import prove_relocation
from tests.unit.unit_utils import build_and_run, pool_stats
from flipjump.assembler.fj_parser import parse_macro_tree
from flipjump.utils.functions import get_file_tuples

WIDTHS = [16, 32, 64, 128, 256, 1024]
WIDE = [64, 128, 1024]  # the default width, the first one past the native engine, and a far one

# three xors of two hexes: six tables through two jump words. It prints 0x35 ^ 0x6a = 5f.
XOR_PROGRAM = """
stl.startup
    rep(3, i) hex.xor 2, a, b
    hex.print_as_digit 2, a, 0
    stl.loop
a: hex.vec 2, 0x35
b: hex.vec 2, 0x6a
"""

# the whole hex library's tables (stl.startup_and_init_all), then an add, a multiply and a compare
MATH_PROGRAM = """
stl.startup_and_init_all
    hex.add 4, a, b
    hex.mul 4, r, a, b
    hex.cmp 4, r, b, LT, EQ, GT
LT: stl.output '<'
    ;done
EQ: stl.output '='
    ;done
GT: stl.output '>'
done:
    hex.print_uint 4, r, 1, 1
    stl.loop
a: hex.vec 4, 0x00F5
b: hex.vec 4, 0x0031
r: hex.vec 4
"""

# a pointer reads the cell `d` raw: its word must not be pinned (pin_all_but names it)
POINTER_PROGRAM = """
stl.startup_and_init_all
    hex.set d, 6
    hex.xor e, d
    hex.set v, 0xD
    hex.xor_hex_from_ptr v, p
    hex.print_as_digit v, 1
    stl.loop
d: hex.hex
e: hex.hex
v: hex.hex
p: hex.vec w/4, d
"""

# no stl: prints 'a', jumps to the segment at HIGH which prints 'b' and jumps back, prints 'c'
SEGMENT_PROGRAM = """
def outbit b < IO {
    IO + b;
}
def outc c {
    rep(8, i) outbit (c >> i) & 1
}
    ;start
IO: ;0
start:
    outc 'a'
    ;far
back:
    outc 'c'
end:
    ;end
segment HIGH
far:
    outc 'b'
    ;back
"""

# two 256-bit numbers, kept in a segment past word 2^64, are added and the sum is printed
ADD_PROGRAM = """
stl.startup_and_init_all
    hex.add 64, a, b
    hex.print_uint 64, a, 1, 0
    stl.loop
segment 1 << 200
a: hex.vec 64, FIRST
b: hex.vec 64, SECOND
"""


def _address_id(value: object) -> str:
    """a short test id for an address: 2^n, or ~2^n for one just below 2^n"""
    if not isinstance(value, int) or value < 1 << 16:
        return str(value)
    return f'2^{value.bit_length() - 1}' if value & (value - 1) == 0 else f'~2^{value.bit_length()}'


def _output(fjm_path: Path) -> bytes:
    io_device = FixedIO(b'')
    fjm_run.run(fjm_path, io_device=io_device, print_time=False)
    return io_device.get_output()


def _assemble(tmp_path: Path, source: str, memory_width: int, use_stl: bool = True, **kwargs: object) -> Path:
    fj_path, fjm_path = tmp_path / 'p.fj', tmp_path / 'p.fjm'
    fj_path.write_text(source)
    options: Any = kwargs
    assemble([fj_path], fjm_path, memory_width=memory_width, print_time=False, use_stl=use_stl, **options)
    return fjm_path


# --- which widths there are ---


def test_a_memory_width_is_a_power_of_two_from_8_up_to_what_the_header_holds() -> None:
    assert [w for w in range(0, 70) if is_supported_memory_width(w)] == [8, 16, 32, 64]
    assert all(is_supported_memory_width(1 << bits) for bits in range(3, 16))
    assert not is_supported_memory_width(1 << 16) and not is_supported_memory_width(4)


@pytest.mark.parametrize('memory_width', WIDTHS + [32768])
def test_a_program_runs_the_same_at_every_width(tmp_path: Path, memory_width: int) -> None:
    fjm_path = _assemble(tmp_path, XOR_PROGRAM, memory_width)
    assert read_fjm_header(fjm_path).memory_width == memory_width
    assert _output(fjm_path) == b'5f'


@pytest.mark.parametrize('memory_width', WIDE)
@pytest.mark.parametrize('version', list(FJMVersion))
def test_every_fjm_version_holds_every_width(tmp_path: Path, memory_width: int, version: FJMVersion) -> None:
    fjm_path = _assemble(tmp_path, XOR_PROGRAM, memory_width, fjm_version=version)
    reader = Reader(fjm_path)
    assert reader.memory_width == memory_width and reader.version == version
    assert _output(fjm_path) == b'5f'


def test_the_native_engine_runs_the_widths_up_to_64_and_the_python_loops_the_rest(tmp_path: Path) -> None:
    for memory_width in WIDE:
        fjm_path = _assemble(tmp_path, XOR_PROGRAM, memory_width)
        statistics = run(fjm_path, io_device=FixedIO(b''), print_time=False, print_termination=False)
        native = fjm_run.is_native_engine_active() and memory_width <= fjm_run.NATIVE_MAX_MEMORY_WIDTH
        assert (statistics.storage_mode is not None) == native


def test_flat_memory_is_refused_above_the_native_engines_widths(tmp_path: Path) -> None:
    fjm_path = _assemble(tmp_path, XOR_PROGRAM, 128)
    with pytest.raises(FlipJumpFlatMemoryException, match='runs memory widths up to 64; this program is w=128'):
        fjm_run.run(fjm_path, io_device=FixedIO(b''), print_time=False, flat_required=True)


# --- the table pool at every width ---


@pytest.mark.parametrize('memory_width', WIDTHS)
def test_a_pool_moves_the_tables_and_pins_their_words_at_every_width(tmp_path: Path, memory_width: int) -> None:
    reference, plain_ops, _ = build_and_run(XOR_PROGRAM, tmp_path, None, name='plain', memory_width=memory_width)
    pool = BlockPool(memory_width)
    output, ops, _ = build_and_run(XOR_PROGRAM, tmp_path, pool, memory_width=memory_width)
    assert output == reference == b'5f'
    assert pool.moved == 6 and len(pool.pinned) == 2 and pool.unpins == {}
    assert ops < plain_ops  # an arm flips a table's index, not its whole address


@pytest.mark.parametrize('memory_width', WIDE)
def test_the_whole_hex_library_runs_the_same_with_a_pool(tmp_path: Path, memory_width: int) -> None:
    reference, plain_ops, _ = build_and_run(MATH_PROGRAM, tmp_path, None, name='plain', memory_width=memory_width)
    pool = BlockPool(memory_width)
    output, ops, _ = build_and_run(MATH_PROGRAM, tmp_path, pool, memory_width=memory_width)
    stats = pool_stats(pool)
    assert output == reference == b'>0x3846'  # (0xF5 + 0x31) * 0x31
    assert pool.moved > 20 and stats['allocated'] == pool.moved and pool.pinned and ops < plain_ops


@pytest.mark.parametrize('memory_width', WIDE)
def test_a_pointer_program_pins_all_but_the_cells_it_names(tmp_path: Path, memory_width: int) -> None:
    reference, _, _ = build_and_run(POINTER_PROGRAM, tmp_path, None, name='plain', memory_width=memory_width)
    unpinned = BlockPool(memory_width)  # the cells are not named: nothing is pinned, the tables still move
    assert build_and_run(POINTER_PROGRAM, tmp_path, unpinned, name='raw', memory_width=memory_width)[0] == reference
    assert unpinned.reads_words_raw and unpinned.moved and not unpinned.pinned
    pool = BlockPool(memory_width, pin_all_but=[('d', 1)])
    output, _, _ = build_and_run(POINTER_PROGRAM, tmp_path, pool, memory_width=memory_width)
    assert output == reference == b'B' and pool.pinned and pool.unpins == {}


@pytest.mark.parametrize('memory_width', WIDE)
def test_the_relocation_proof_holds_at_every_width(tmp_path: Path, memory_width: int) -> None:
    fj_path = tmp_path / 'math.fj'
    fj_path.write_text(MATH_PROGRAM)
    macros = parse_macro_tree(get_file_tuples([str(fj_path)], no_stl=False), memory_width, True)
    pool = BlockPool(memory_width)
    proof = prove_relocation(memory_width, macros, pool)
    assert proof.mismatches == [] and pool.moved


@pytest.mark.slow
@pytest.mark.parametrize('memory_width', [128, 1024])
def test_random_programs_run_the_same_with_every_pool_at_the_wide_widths(tmp_path: Path, memory_width: int) -> None:
    assert fuzz_case(4200 + memory_width, memory_width, tmp_path) == []


# --- addresses at the edges ---

# (memory width, the high segment's address): above the packed slot's 32-bit jump, at the 63-bit slot's limit and
# past it, near the top of a 64-bit memory, and - at the wide widths - past 2^64
HIGH_SEGMENTS = [
    (64, 1 << 40),
    (64, 1 << 62),
    (64, 1 << 63),
    (64, (1 << 64) - (1 << 20)),
    (128, 1 << 63),
    (128, 1 << 64),
    (128, 1 << 70),
    (1024, 1 << 64),
    (1024, 1 << 73),
]


@pytest.mark.parametrize('memory_width, high', HIGH_SEGMENTS, ids=_address_id)
def test_a_segment_at_a_high_address(tmp_path: Path, memory_width: int, high: int) -> None:
    fjm_path = _assemble(tmp_path, SEGMENT_PROGRAM.replace('HIGH', hex(high)), memory_width, use_stl=False)
    starts = [start * memory_width for start, _, _, _ in read_fjm_header(fjm_path).segments]
    assert high in starts and _output(fjm_path) == b'abc'


# past word 2^64: a version 4 file holds these in w-bit segment fields
SEGMENTS_PAST_WORD_2_64 = [
    (128, 1 << 71),
    (128, (1 << 128) - (1 << 20)),
    (1024, 1 << 74),
    (1024, 1 << 1000),
    (1024, (1 << 1024) - (1 << 20)),
]


@pytest.mark.parametrize('memory_width, high', SEGMENTS_PAST_WORD_2_64, ids=_address_id)
def test_version_4_holds_a_segment_anywhere_in_a_wide_memory(tmp_path: Path, memory_width: int, high: int) -> None:
    fjm_path = _assemble(tmp_path, SEGMENT_PROGRAM.replace('HIGH', hex(high)), memory_width, use_stl=False)
    starts = [start * memory_width for start, _, _, _ in read_fjm_header(fjm_path).segments]
    assert high in starts and _output(fjm_path) == b'abc'


@pytest.mark.parametrize('version', [version for version in FJMVersion if version.value < 4])
def test_the_older_versions_refuse_a_segment_past_word_2_64_by_name(tmp_path: Path, version: FJMVersion) -> None:
    source = SEGMENT_PROGRAM.replace('HIGH', hex(1 << 71))
    with pytest.raises(FlipJumpException, match=r'version \d of the \.fjm format holds segments below word 2\^64'):
        _assemble(tmp_path, source, 128, use_stl=False, fjm_version=version)


@pytest.mark.parametrize('memory_width, field_bytes', [(8, 8), (32, 8), (64, 8), (128, 16), (1024, 128)])
def test_a_segment_field_is_a_u64_and_in_version_4_w_bits_above_w_64(memory_width: int, field_bytes: int) -> None:
    for version in FJMVersion:
        expected = field_bytes if version == FJMVersion.CompressedSegmentsVersion else 8
        assert segment_field_size(version, memory_width) == expected


@pytest.mark.parametrize('memory_width', [64, 128, 1024])
def test_the_segment_table_packs_and_unpacks(memory_width: int) -> None:
    version = FJMVersion.CompressedSegmentsVersion
    top_word = (1 << memory_width) // memory_width
    segments = [(0, 2, 0, 2), (top_word - 4, 4, 2, 2), (top_word // 2, 6, 4, 0)]
    table = pack_segments(segments, version, memory_width)
    assert len(table) == len(segments) * 4 * segment_field_size(version, memory_width)
    assert unpack_segments(table, version, memory_width) == segments
    if memory_width == 64:  # up to w=64 the table is four u64 a segment, in every version
        assert table == b''.join(struct.pack('<QQQQ', *segment) for segment in segments)


# (memory width, pool_base): the pool's addresses (table places, block bases, pinned words' flips) at and past what a
# 63-bit slot and a 64-bit array hold, and past word 2^64
HIGH_POOLS = [
    (64, 1 << 40),
    (64, 1 << 62),
    (64, 1 << 63),
    (64, (1 << 64) - (1 << 20)),
    (128, 1 << 63),
    (128, 1 << 64),
    (128, 1 << 70),
    (1024, 1 << 64),
    (1024, 1 << 73),
    (128, 1 << 100),
    (1024, 1 << 80),
    (1024, 1 << 1000),
]


@pytest.mark.parametrize('memory_width, pool_base', HIGH_POOLS, ids=_address_id)
def test_a_pool_at_a_high_address(tmp_path: Path, memory_width: int, pool_base: int) -> None:
    reference, plain_ops, _ = build_and_run(XOR_PROGRAM, tmp_path, None, name='plain', memory_width=memory_width)
    pool = BlockPool(memory_width, pool_base)
    output, ops, fjm_path = build_and_run(XOR_PROGRAM, tmp_path, pool, memory_width=memory_width)
    assert output == reference == b'5f' and ops < plain_ops
    assert pool.moved == 6 and len(pool.pinned) == 2 and min(pool.pinned.values()) == pool_base
    assert all(site.address >= pool_base for site in pool.table_sites)
    segments = [start * memory_width for start, _, _, _ in read_fjm_header(fjm_path).segments]
    assert sum(start >= pool_base for start in segments) == 6  # each moved table is a segment


@pytest.mark.parametrize('memory_width', WIDE)
def test_a_pool_right_below_the_top_of_the_memory(tmp_path: Path, memory_width: int) -> None:
    pool = BlockPool(memory_width, (1 << memory_width) - (1 << 20))
    assert build_and_run(XOR_PROGRAM, tmp_path, pool, memory_width=memory_width)[0] == b'5f' and pool.moved == 6


# --- a word that does not fit its width ---


@pytest.mark.parametrize('memory_width', [16, 64, 128, 1024])
@pytest.mark.parametrize('op', [';1 << W', '1 << W;', ';0 - 64'], ids=['jump', 'flip', 'negative'])
def test_a_word_that_does_not_fit_the_width_is_refused_by_name(tmp_path: Path, memory_width: int, op: str) -> None:
    source = f'    ;start\nIO: ;0\nstart:\n    {op.replace("W", str(memory_width))}\n'
    with pytest.raises(FlipJumpException, match=f'Not enough space with the {memory_width}-bits memory-width'):
        _assemble(tmp_path, source, memory_width, use_stl=False)


def _cli(tmp_path: Path, memory_width: str, *options: str) -> Optional[bytes]:
    from flipjump import assemble_run_according_to_cmd_line_args

    fj_path, fjm_path = tmp_path / 'cli.fj', tmp_path / 'cli.fjm'
    fj_path.write_text(XOR_PROGRAM)
    arguments: List[str] = [str(fj_path), '--asm', '-o', str(fjm_path), '-w', memory_width, '--silent', *options]
    assemble_run_according_to_cmd_line_args(cmd_line_args=arguments)
    return _output(fjm_path)


@pytest.mark.parametrize('memory_width', ['8', '128', '1024'])
def test_the_cli_takes_every_width(tmp_path: Path, memory_width: str) -> None:
    if memory_width == '8':  # (a 256-bit memory holds no stl program: the width is taken, the program does not fit)
        with pytest.raises(FlipJumpException, match='Not enough space with the 8-bits memory-width'):
            _cli(tmp_path, memory_width)
        return
    assert _cli(tmp_path, memory_width) == b'5f'
    assert _cli(tmp_path, memory_width, '--pool-tables') == b'5f'


@pytest.mark.parametrize('memory_width', ['0', '12', '100', '65536', 'x', '-64'])
def test_the_cli_refuses_what_is_no_width(tmp_path: Path, memory_width: str) -> None:
    with pytest.raises(SystemExit):
        _cli(tmp_path, memory_width)


# --- end to end at w=1024 ---


def test_an_addition_runs_end_to_end_at_w_1024(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """the cli assembles and runs ADD_PROGRAM at w=1024: the .fjm (with a segment past word 2^64), the python
    engine, and the printed sum of two 256-bit numbers"""
    from flipjump import assemble_run_according_to_cmd_line_args

    printed = io.StringIO()
    monkeypatch.setattr(sys.modules[StandardIO.__module__], 'stdout', printed)  # where the program prints
    a = 0xFEDCBA9876543210_0123456789ABCDEF_FFFFFFFFFFFFFFFF_00000000FFFFFFFF
    b = 0x0123456789ABCDE0_FEDCBA9876543210_0000000000000001_FFFFFFFF00000001
    fj_path, fjm_path = tmp_path / 'add.fj', tmp_path / 'add.fjm'
    fj_path.write_text(ADD_PROGRAM.replace('FIRST', hex(a)).replace('SECOND', hex(b)))
    assemble_run_according_to_cmd_line_args(cmd_line_args=[str(fj_path), '-o', str(fjm_path), '-w', '1024', '--silent'])
    assert printed.getvalue() == hex((a + b) % (1 << 256))
    header = read_fjm_header(fjm_path)
    assert header.memory_width == 1024 and 1 << 200 in [start * 1024 for start, _, _, _ in header.segments]
