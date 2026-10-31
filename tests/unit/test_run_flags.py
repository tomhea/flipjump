"""
unit-tests for what a .fjm carries about how to run it, and for loading it.

the header's run flags and io mode (`fj --flat-memory` / `--io-mode MODE` compile them in; `--[no-]flat-memory`
and `--io` override them at run time), the flat window the CLI derives from them, the error a forced flat
memory gives when it cannot be allocated, and the lean native load (the .fjm's bytes straight into the
engine, no python object per word) - pinned against the Reader's memory, word for word.
"""

import functools
import lzma
import random
import struct
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

import pytest

from flipjump import assemble_run_according_to_cmd_line_args
from flipjump import flipjump_cli, flipjump_quickstart
from flipjump.fjm.fjm_consts import (
    FJMVersion,
    IO_MODE_PC,
    IO_MODE_STANDARD,
    RUN_FLAG_FLAT_MEMORY,
    _LZMA_FORMAT,
    _lzma_compression_filters,
    _header_base_size,
)
from flipjump.fjm.fjm_reader import Reader, read_fjm_header
from flipjump.fjm.fjm_writer import Writer
from flipjump.interpreter import fjm_run, run_defaults
from flipjump.interpreter.io_devices.StandardIO import StandardIO
from flipjump.utils.exceptions import FlipJumpReadFjmException, FlipJumpWriteFjmException
from flipjump.utils.progress import ProgressBar
from tests.unit.unit_utils import (
    MINIMAL_STARTUP,
    assemble_to_path,
    high_segment_program,
    native_engine_required,
    patch_run_header,
    spy_engine_runs,
)

try:
    from flipjump.interpreter import _fjcore  # type: ignore[attr-defined]
except ImportError:
    _fjcore = None


pytestmark = pytest.mark.usefixtures('clean_flat_mode_env')


def _write_simple(
    path: Path, version: FJMVersion = FJMVersion.NormalVersion, *, run_flags: int = 0, io_mode: Union[int, str] = 0
) -> Path:
    writer = Writer(path, 16, version, run_flags=run_flags, io_mode=io_mode)
    writer.add_simple_segment_with_data(0, [0, 0])
    writer.write_to_file()
    return path


# --- the header ---


def test_the_flags_are_two_u32s_after_the_base_header(tmp_path: Path) -> None:
    # `u32 run_flags; u32 io_mode` and a reserved u32: the 12 header-extension bytes
    path = _write_simple(tmp_path / 'x.fjm', run_flags=RUN_FLAG_FLAT_MEMORY, io_mode=IO_MODE_PC)
    raw = path.read_bytes()
    assert struct.unpack_from('<LLL', raw, _header_base_size) == (RUN_FLAG_FLAT_MEMORY, IO_MODE_PC, 0)
    header = read_fjm_header(path)
    assert (header.run_flags, header.io_mode) == (RUN_FLAG_FLAT_MEMORY, IO_MODE_PC)
    reader = Reader(path)
    assert (reader.run_flags, reader.io_mode) == (RUN_FLAG_FLAT_MEMORY, IO_MODE_PC)


def test_the_flags_default_to_zero(tmp_path: Path) -> None:
    header = read_fjm_header(_write_simple(tmp_path / 'x.fjm'))
    assert (header.run_flags, header.io_mode) == (0, IO_MODE_STANDARD)


def test_a_base_version_file_has_no_flags(tmp_path: Path) -> None:
    header = read_fjm_header(_write_simple(tmp_path / 'x.fjm', FJMVersion.BaseVersion))
    assert (header.run_flags, header.io_mode) == (0, 0)
    for run_flags, io_mode in ((1, 0), (0, 1)):
        with pytest.raises(FlipJumpWriteFjmException):
            Writer(tmp_path / 'y.fjm', 16, FJMVersion.BaseVersion, run_flags=run_flags, io_mode=io_mode)


@pytest.mark.parametrize(
    'flags', [{'run_flags': 2}, {'run_flags': 3}, {'run_flags': 1 << 31}, {'io_mode': 2}, {'io_mode': 'hologram'}]
)
def test_the_writer_rejects_bad_flags(tmp_path: Path, flags: Any) -> None:
    with pytest.raises(FlipJumpWriteFjmException, match='bad'):
        Writer(tmp_path / 'x.fjm', 16, FJMVersion.NormalVersion, **flags)


@pytest.mark.parametrize('offset, value', [(0, 2), (0, 0x80000000), (4, 2), (4, 0xFFFFFFFF)])
def test_the_reader_rejects_bad_flags(tmp_path: Path, offset: int, value: int) -> None:
    # offset 0 is run_flags, 4 is io_mode: anything but the defined values is a bad-flags error
    path = _write_simple(tmp_path / 'x.fjm')
    raw = bytearray(path.read_bytes())
    struct.pack_into('<L', raw, _header_base_size + offset, value)
    path.write_bytes(bytes(raw))
    for read in (read_fjm_header, Reader):
        with pytest.raises(FlipJumpReadFjmException, match='bad flags'):
            read(path)


# --- the CLI: compiling the flags in ---


def _looping_source() -> str:
    return MINIMAL_STARTUP + '\nstartup\nloop: ;loop\n'


def test_cli_compiles_the_flags_into_the_fjm(tmp_path: Path) -> None:
    fj_path = tmp_path / 'p.fj'
    fj_path.write_text(_looping_source())
    out = tmp_path / 'out.fjm'
    assemble_run_according_to_cmd_line_args(
        cmd_line_args=['--asm', '-s', '--no_stl', '-o', str(out), '--io-mode', 'pc', '--flat-memory', str(fj_path)]
    )
    header = read_fjm_header(out)
    assert (header.run_flags, header.io_mode) == (RUN_FLAG_FLAT_MEMORY, IO_MODE_PC)
    for off in ([], ['--no-flat-memory']):  # the control: without --flat-memory, nothing is compiled in
        args = ['--asm', '-s', '--no_stl', '-o', str(out), *off, str(fj_path)]
        assemble_run_according_to_cmd_line_args(cmd_line_args=args)
        assert read_fjm_header(out).run_flags == 0


@pytest.mark.parametrize('value', ['1', '0', 'PC', 'hologram'])
def test_cli_io_mode_takes_a_name_only(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        flipjump_cli.parse_arguments(cmd_line_args=['--asm', '-o', 'o.fjm', '--io-mode', value, 'a.fj'])
    assert 'invalid choice' in capsys.readouterr().err
    for name in ('standard', 'pc'):  # the control: the names parse
        args, _ = flipjump_cli.parse_arguments(cmd_line_args=['--asm', '-o', 'o.fjm', '--io-mode', name, 'a.fj'])
        assert args.io_mode == name


@pytest.mark.parametrize('io_mode, expected', [('pc', IO_MODE_PC), (IO_MODE_PC, IO_MODE_PC), ('standard', 0), (0, 0)])
def test_the_api_takes_the_io_mode_by_name_or_number(tmp_path: Path, io_mode: Any, expected: int) -> None:
    assert read_fjm_header(_write_simple(tmp_path / 'w.fjm', io_mode=io_mode)).io_mode == expected
    fj_path = tmp_path / 'p.fj'
    fj_path.write_text(_looping_source())
    flipjump_quickstart.assemble([fj_path], tmp_path / 'q.fjm', use_stl=False, print_time=False, io_mode=io_mode)
    assert read_fjm_header(tmp_path / 'q.fjm').io_mode == expected


@pytest.mark.parametrize('option', ['--flags', '--run-flags'])
def test_there_is_no_flags_option(option: str) -> None:
    with pytest.raises(SystemExit):
        flipjump_cli.parse_arguments(cmd_line_args=['--asm', '-o', 'o.fjm', option, '1', 'a.fj'])


# --- the CLI: running by the compiled flags ---


def _spy_io_modes(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """record the io mode the CLI builds its device for (and give it a standard device, no window)"""
    modes: List[str] = []

    def make(mode: str) -> Any:
        modes.append(mode)
        return StandardIO(False)

    monkeypatch.setattr(flipjump_cli, 'make_io_device', make)
    return modes


def test_the_compiled_io_mode_picks_the_device_and_io_overrides_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fjm_path = assemble_to_path(_looping_source(), tmp_path)
    for io_mode, extra, expected in (
        (IO_MODE_STANDARD, [], 'standard'),
        (IO_MODE_PC, [], 'pc'),
        (IO_MODE_PC, ['--io', 'standard'], 'standard'),
        (IO_MODE_STANDARD, ['--io', 'pc'], 'pc'),
    ):
        patch_run_header(fjm_path, io_mode=io_mode)
        modes = _spy_io_modes(monkeypatch)
        assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', *extra, str(fjm_path)])
        assert modes == [expected], (io_mode, extra)


_BIG_TOP = run_defaults.FLAT_MEMORY_AUTO_MAX_WORDS


def _run_and_spy(
    fjm_path: Path, monkeypatch: pytest.MonkeyPatch, extra: List[str]
) -> Tuple[Optional[int], bool, Optional[str]]:
    """run once; -> (flat_max_words passed, flat_required passed, storage mode)"""
    with monkeypatch.context() as patch:
        seen = spy_engine_runs(patch)
        assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', *extra, str(fjm_path)])
    return seen[0]


@native_engine_required
def test_the_compiled_flat_memory_runs_flat_and_the_flat_memory_flags_override_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a span over the automatic limit: without the flag the engine's default window ('hybrid')
    fjm_path = assemble_to_path(high_segment_program(_BIG_TOP), tmp_path, memory_width=32)
    span = _BIG_TOP + 4
    assert _run_and_spy(fjm_path, monkeypatch, []) == (None, False, 'hybrid')
    assert _run_and_spy(fjm_path, monkeypatch, ['--flat-memory']) == (span, True, 'flat')
    assert _run_and_spy(fjm_path, monkeypatch, ['--no-flat-memory']) == (None, False, 'hybrid')
    patch_run_header(fjm_path, run_flags=RUN_FLAG_FLAT_MEMORY)
    assert _run_and_spy(fjm_path, monkeypatch, []) == (span, True, 'flat')
    assert _run_and_spy(fjm_path, monkeypatch, ['--flat-memory']) == (span, True, 'flat')
    assert _run_and_spy(fjm_path, monkeypatch, ['--no-flat-memory']) == (None, False, 'hybrid')
    # the explicit window options still win over the compiled flag
    assert _run_and_spy(fjm_path, monkeypatch, ['--flat-max-words', '4']) == (4, False, 'hybrid')


@native_engine_required
@pytest.mark.parametrize(
    'compiled, extra, cause',
    [
        (RUN_FLAG_FLAT_MEMORY, [], 'this program asks for flat memory (compiled into the .fjm with --flat-memory)'),
        (0, ['--flat-memory'], '--flat-memory was given'),
        (RUN_FLAG_FLAT_MEMORY, ['--flat-memory'], '--flat-memory was given'),
    ],
)
def test_a_forced_flat_memory_that_cannot_be_allocated_says_how_to_run_anyway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    compiled: int,
    extra: List[str],
    cause: str,
) -> None:
    fjm_path = assemble_to_path(high_segment_program(_BIG_TOP), tmp_path, memory_width=32)
    patch_run_header(fjm_path, run_flags=compiled)
    monkeypatch.setenv('FLIPJUMP_TEST_FLAT_ALLOC_FAIL', '1')
    with pytest.raises(SystemExit) as exit_info:
        assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', *extra, str(fjm_path)])
    message = str(exit_info.value.code)
    assert message.startswith(f'flipjump: {cause}: ')
    assert 'Running it with --no-flat-memory might fix that, at a cost of speed.' in message
    assert f'{_BIG_TOP + 4:,} words' in message
    # the control: the advice works - the same run with --no-flat-memory falls back to paged memory
    assert _run_and_spy(fjm_path, monkeypatch, ['--no-flat-memory'])[1:] == (False, 'paged')


@native_engine_required
def test_an_unforced_flat_window_still_falls_back_to_paged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # the automatic window (a small span, no flag) was not asked for: a failed allocation runs paged
    fjm_path = assemble_to_path(high_segment_program(1 << 10), tmp_path, memory_width=32)
    monkeypatch.setenv('FLIPJUMP_TEST_FLAT_ALLOC_FAIL', '1')
    assert _run_and_spy(fjm_path, monkeypatch, [])[2] == 'paged'
    assert 'running paged' in capfd.readouterr().err


# --- the lean native load ---


def _lean_load(fjm_path: Path, flat_max_words: Optional[int] = None) -> Any:
    with ProgressBar('', print_time=False) as progress:
        return fjm_run._load_native(fjm_path, read_fjm_header(fjm_path), flat_max_words, False, progress)


def _assert_loaded_like_the_reader(fjm_path: Path, flat_max_words: Optional[int] = None, core: Any = None) -> None:
    reader = Reader(fjm_path)
    core = _lean_load(fjm_path, flat_max_words) if core is None else core
    mismatches = [address for address, value in reader.memory.items() if core.get_word(address) != value]
    assert not mismatches, f'{len(mismatches)} words differ, first at {hex(mismatches[0])}'
    for start, end in reader.zeros_boundaries:  # the long reserved tails the Reader leaves implicit
        assert core.get_word(start) == 0 and core.get_word(end - 1) == 0


_TWO_SEGMENTS = MINIMAL_STARTUP + '''
startup
loop: ;loop
a: ;a
b: a+3;b
segment 0x4000
c: ;loop
d: c+5;c
reserve 2000 * w
'''


@native_engine_required
@pytest.mark.parametrize('version', list(FJMVersion))
@pytest.mark.parametrize('memory_width', [16, 32, 64])  # (w=8 has 32 words: too small for this program)
def test_the_lean_load_equals_the_reader(tmp_path: Path, version: FJMVersion, memory_width: int) -> None:
    # every version (v2/v3 store the jump words relative to their address) and every width
    fjm_path = assemble_to_path(_TWO_SEGMENTS, tmp_path, memory_width=memory_width, fjm_version=version)
    _assert_loaded_like_the_reader(fjm_path)


@native_engine_required
@pytest.mark.parametrize('flat_max_words', [None, 4, 0x4001])
def test_the_lean_load_equals_the_reader_in_every_storage_mode(tmp_path: Path, flat_max_words: Optional[int]) -> None:
    # 'flat', and two 'hybrid' windows - one cutting into the second segment: the words above the
    # window must land in the paged part
    fjm_path = assemble_to_path(_TWO_SEGMENTS, tmp_path, memory_width=32, fjm_version=FJMVersion.CompressedVersion)
    _assert_loaded_like_the_reader(fjm_path, flat_max_words)


@native_engine_required
def test_the_lean_load_check_catches_a_wrong_word(tmp_path: Path) -> None:
    # the negative control of the checks above: the checker itself must reject an image with one bit off
    fjm_path = assemble_to_path(_TWO_SEGMENTS, tmp_path, memory_width=32, fjm_version=FJMVersion.CompressedVersion)
    core = _lean_load(fjm_path)
    _assert_loaded_like_the_reader(fjm_path, core=core)
    address = Reader(fjm_path).memory_segments[1].segment_start + 1  # a relative jump word
    core.set_word(address, core.get_word(address) ^ 1)
    with pytest.raises(AssertionError, match='1 words differ'):
        _assert_loaded_like_the_reader(fjm_path, core=core)


@native_engine_required
@pytest.mark.parametrize('version', list(FJMVersion))
def test_the_lean_load_equals_the_reader_at_w8(tmp_path: Path, version: FJMVersion) -> None:
    # w=8 memory is 32 words: a bare looping op and a second segment, no startup
    fjm_path = assemble_to_path(
        'a: ;a\nb: a+3;b\nsegment 16*w\nc: b+1;c\n', tmp_path, memory_width=8, fjm_version=version
    )
    _assert_loaded_like_the_reader(fjm_path)


# --- damaged or malformed files: the lean load must reject what the Reader rejects ---


def _raw_fjm(width: int, segments: List[Tuple[int, int, int, int]], data: bytes, version: int = 1) -> bytes:
    raw = struct.pack('<HHQQ', ord('F') + (ord('J') << 8), width, version, len(segments))
    raw += struct.pack('<LLL', 0, 0, 0)
    for segment in segments:
        raw += struct.pack('<QQQQ', *segment)
    return raw + data


def _compressed_looping_fjm(tmp_path: Path) -> Path:
    return assemble_to_path(_TWO_SEGMENTS, tmp_path, memory_width=32, fjm_version=FJMVersion.CompressedVersion)


def _both_readers_reject(path: Path, match: str) -> None:
    with pytest.raises(FlipJumpReadFjmException, match=match):
        Reader(path)
    if _fjcore is not None:
        with pytest.raises(FlipJumpReadFjmException, match=match):
            _lean_load(path)


@pytest.mark.parametrize('damage', ['cut the last byte', 'cut in the middle', 'empty data'])
def test_damaged_compressed_data_is_rejected(tmp_path: Path, damage: str) -> None:
    path = _compressed_looping_fjm(tmp_path)
    raw = path.read_bytes()
    data_start = _header_base_size + 12 + 32 * len(read_fjm_header(path).segments)
    compressed = raw[data_start:]
    damaged = {
        'cut the last byte': compressed[:-1],
        'cut in the middle': compressed[: len(compressed) // 2],
        'empty data': b'',
    }[damage]
    path.write_bytes(raw[:data_start] + damaged)
    _both_readers_reject(path, 'damaged')


def test_streams_and_junk_after_the_compressed_data_decode_like_lzma_decompress(tmp_path: Path) -> None:
    from flipjump.fjm.fjm_reader import _decompress_in_chunks

    payload = bytes(range(256)) * 9000  # > 2 chunks
    stream = lzma.compress(payload, format=_LZMA_FORMAT, filters=_lzma_compression_filters(64, 6))
    for data in (stream, stream + stream, stream + b'junk' * 300_000, stream + stream + b'\x00junk'):
        assert _decompress_in_chunks(data, None) == lzma.decompress(
            data, format=_LZMA_FORMAT, filters=[{"id": lzma.FILTER_LZMA2}]
        )


def test_data_that_is_not_whole_words_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / 'x.fjm'
    path.write_bytes(_raw_fjm(16, [(0, 2, 0, 2)], struct.pack('<2H', 0, 0) + b'\x00'))
    with pytest.raises(FlipJumpReadFjmException, match="can't unpack"):
        Reader(path)
    if _fjcore is not None:
        with pytest.raises(FlipJumpReadFjmException, match='whole number'):
            _lean_load(path)


def test_data_longer_than_its_segment_is_rejected(tmp_path: Path) -> None:
    # segment (0, 2) holding 4 words of data would spill into the gap before segment (4, 2)
    path = tmp_path / 'x.fjm'
    path.write_bytes(_raw_fjm(8, [(0, 2, 0, 4), (4, 2, 4, 2)], bytes([24, 0, 0, 0, 0, 2])))
    _both_readers_reject(path, 'exceeds its segment-length')


def test_overlapping_segments_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / 'x.fjm'
    path.write_bytes(_raw_fjm(8, [(0, 4, 0, 4), (2, 2, 0, 0)], bytes([0, 0, 0, 0])))
    _both_readers_reject(path, 'overlap')


@native_engine_required
def test_a_segment_past_the_address_space_is_a_bad_file(tmp_path: Path) -> None:
    path = tmp_path / 'x.fjm'
    path.write_bytes(_raw_fjm(8, [(0, 2, 0, 2), ((1 << 64) - 2, 4, 0, 0)], bytes([0, 0])))
    with pytest.raises(FlipJumpReadFjmException):
        _lean_load(path)


@native_engine_required
def test_a_failed_decide_storage_stays_undecided(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FLIPJUMP_TEST_FLAT_ALLOC_FAIL', '1')
    core = _fjcore.Memory(32, flat_required=True)
    core.add_segment(0, 8)
    for _ in range(2):  # the second attempt fails the same way - it does not quietly run paged
        with pytest.raises(MemoryError):
            core.decide_storage()
    assert core.storage_mode is None


@native_engine_required
def test_set_words_bytes_on_an_uninitialized_memory_raises() -> None:
    with pytest.raises(RuntimeError, match='never initialized'):
        _fjcore.Memory.__new__(_fjcore.Memory).set_words_bytes(0, b'', 0, 0)


@native_engine_required
def test_set_words_bytes_reads_little_endian_words_and_restores_relative_jumps() -> None:
    core = _fjcore.Memory(16)
    core.add_segment(0x100, 4)
    data = struct.pack('<6H', 0xAAAA, 1, 2, 0x10, 0xFFFF, 0x20)
    core.set_words_bytes(0x100, data, 2, 4, True)
    # words 2..5 of the data; odd words get + (address * w)
    assert [core.get_word(0x100 + i) for i in range(4)] == [
        2,
        (0x10 + 0x101 * 16) & 0xFFFF,
        0xFFFF,
        (0x20 + 0x103 * 16) & 0xFFFF,
    ]
    with pytest.raises(ValueError):
        core.set_words_bytes(0x100, data, 4, 3, False)  # words 4..6 of a 6-word buffer


@native_engine_required
def test_decide_storage_with_flat_required_raises_memory_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FLIPJUMP_TEST_FLAT_ALLOC_FAIL', '1')
    core = _fjcore.Memory(32, flat_required=True)
    core.add_segment(0, 8)
    with pytest.raises(MemoryError):
        core.decide_storage()
    relaxed = _fjcore.Memory(32)
    relaxed.add_segment(0, 8)
    relaxed.decide_storage()  # the default: warn and run paged
    assert relaxed.storage_mode == 'paged'


# --- the writer's chunked compression ---


@pytest.mark.parametrize('size', [0, 1, 999, (1 << 20) + 7, 3 * (1 << 20)])
def test_chunked_compression_equals_one_shot(tmp_path: Path, size: int) -> None:
    # the compression is fed in chunks (to show its progress); its bytes must be exactly lzma.compress's
    data = bytes((i * 2654435761 >> 13) & 0xFF if i % 7 else 0 for i in range(size))
    writer = Writer(tmp_path / 'x.fjm', 32, FJMVersion.CompressedVersion)
    chunked = writer._compress_data(data)
    one_shot = lzma.compress(data, format=_LZMA_FORMAT, filters=_lzma_compression_filters(64, writer.lzma_preset))
    assert chunked == one_shot


@native_engine_required
def test_the_environment_window_wins_over_the_automatic_one_and_the_run_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fjm_path = assemble_to_path(high_segment_program(1 << 10), tmp_path, memory_width=32)
    monkeypatch.setenv('FLIPJUMP_FLAT_MAX_WORDS', '4')
    assert _run_and_spy(fjm_path, monkeypatch, []) == (None, False, 'hybrid')
    patch_run_header(fjm_path, run_flags=RUN_FLAG_FLAT_MEMORY)
    assert _run_and_spy(fjm_path, monkeypatch, []) == (None, False, 'hybrid')
    # ...but an explicit --flat-memory still wins over the environment
    assert _run_and_spy(fjm_path, monkeypatch, ['--flat-memory'])[1:] == (True, 'flat')


@native_engine_required
def test_the_automatic_window_includes_a_span_of_exactly_2_24(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fjm_path = assemble_to_path(high_segment_program(_BIG_TOP - 4), tmp_path, memory_width=32)
    assert read_fjm_header(fjm_path).span_words == _BIG_TOP
    assert _run_and_spy(fjm_path, monkeypatch, []) == (_BIG_TOP, False, 'flat')


def test_version_0_with_flags_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fj_path = tmp_path / 'p.fj'
    fj_path.write_text(_looping_source())
    for flags in (['--io-mode', 'pc'], ['--flat-memory']):
        with pytest.raises(SystemExit):
            assemble_run_according_to_cmd_line_args(
                cmd_line_args=[
                    '--asm',
                    '-s',
                    '--no_stl',
                    '-v',
                    '0',
                    '-o',
                    str(tmp_path / 'o.fjm'),
                    *flags,
                    str(fj_path),
                ]
            )
        assert 'version 0' in capsys.readouterr().err
    # the control: the default io mode, given explicitly, fits version 0
    assemble_run_according_to_cmd_line_args(
        cmd_line_args=['--asm', '-s', '--no_stl', '-v', '0', '-o', str(tmp_path / 'o.fjm'), '--io-mode', 'standard']
        + [str(fj_path)]
    )


def test_assemble_and_run_in_one_command_uses_the_compiled_io_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fj_path = tmp_path / 'p.fj'
    fj_path.write_text(_looping_source())
    modes = _spy_io_modes(monkeypatch)
    assemble_run_according_to_cmd_line_args(cmd_line_args=['-s', '--no_stl', '--io-mode', 'pc', str(fj_path)])
    assert modes == ['pc']


@native_engine_required
def test_assemble_and_run_in_one_command_compiles_and_runs_flat_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fj_path = tmp_path / 'p.fj'
    fj_path.write_text(_looping_source())
    out = tmp_path / 'o.fjm'
    for extra, required in (([], False), (['--flat-memory'], True)):  # a small span runs flat either way
        with monkeypatch.context() as patch:
            seen = spy_engine_runs(patch)
            assemble_run_according_to_cmd_line_args(
                cmd_line_args=['-s', '--no_stl', '-o', str(out), *extra, str(fj_path)]
            )
        assert [required_ for _, required_, _ in seen] == [required]
        assert read_fjm_header(out).run_flags == (RUN_FLAG_FLAT_MEMORY if required else 0)


@functools.lru_cache(maxsize=None)
def _random_letters(size: int) -> bytes:
    """`size` random letters of ABCD (deterministic; cached: making them takes seconds)"""
    generator = random.Random(11)  # nosec B311 - deterministic test data, not security
    return bytes(generator.choice(b'ABCD') for _ in range(size))


def test_a_damaged_later_stream_contributes_nothing() -> None:
    # lzma.decompress drops a later stream that fails mid-way. The second stream must be LONGER THAN A
    # CHUNK compressed, so the damage is met after part of it was already decoded - else nothing tests it.
    from flipjump.fjm.fjm_reader import _DECOMPRESS_CHUNK, _decompress_in_chunks

    filters = _lzma_compression_filters(64, 0)
    first = lzma.compress(b'first' * 30, format=_LZMA_FORMAT, filters=filters)
    second = bytearray(lzma.compress(_random_letters(6_000_000), format=_LZMA_FORMAT, filters=filters))
    assert len(second) > _DECOMPRESS_CHUNK
    second[len(second) * 9 // 10] ^= 0xFF
    data = first + bytes(second)
    expected = lzma.decompress(data, format=_LZMA_FORMAT, filters=[{"id": lzma.FILTER_LZMA2}])
    assert expected == b'first' * 30
    for expected_size in (0, len(expected), 10_000_000):
        assert bytes(_decompress_in_chunks(data, None, expected_size)) == expected


@pytest.mark.parametrize('data_start_words', [1 << 60, 1 << 37, 1 << 28, 1_500_000_000])
def test_a_huge_data_offset_is_rejected_without_allocating_it(tmp_path: Path, data_start_words: int) -> None:
    # the decompression buffer may only grow with the decoded data, never to what a damaged segment table
    # claims: a ~1.9 MB stream (1,500,000,000 words = 12 GB claimed, inside an "8192x the compressed size"
    # bound) must be rejected like the Reader rejects it, with the buffer's peak bounded by the real data
    import tracemalloc

    from flipjump.fjm.fjm_reader import _DECOMPRESS_CHUNK, read_fjm_raw

    data = _random_letters(6_000_000)
    stream = lzma.compress(data, format=_LZMA_FORMAT, filters=_lzma_compression_filters(128, 0))
    assert len(stream) > _DECOMPRESS_CHUNK
    path = tmp_path / 'x.fjm'
    path.write_bytes(_raw_fjm(64, [(0, 2, 0, 2), (100, 2, data_start_words, 2)], stream, version=3))
    tracemalloc.start()
    try:
        with pytest.raises(FlipJumpReadFjmException, match='exceeds data pool length'):
            read_fjm_raw(path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # the buffer starts at 16x the compressed size and then grows with the data: tens of MB here, where
    # trusting the claimed offset would be gigabytes
    limit = 16 * len(stream) + 4 * len(data) + 3 * len(stream)
    assert peak < limit, f'{peak:,} bytes allocated for {len(data):,} bytes of data'
    _both_readers_reject(path, 'exceeds data pool length')


@native_engine_required
def test_a_file_that_changes_during_the_load_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from flipjump.fjm import fjm_reader

    fjm_path = assemble_to_path(_TWO_SEGMENTS, tmp_path, memory_width=32)
    real_read_raw = fjm_reader.read_fjm_raw

    def read_raw_of_another_file(path: Path, on_progress: Any = None) -> Any:
        header, data = real_read_raw(path, on_progress)
        header.segments = header.segments[:1]
        return header, data

    monkeypatch.setattr(fjm_reader, 'read_fjm_raw', read_raw_of_another_file)
    with pytest.raises(FlipJumpReadFjmException, match='changed while'):
        _lean_load(fjm_path)


@native_engine_required
@pytest.mark.parametrize('value', ['0', 'many'])
def test_an_env_window_the_engine_ignores_does_not_cancel_the_run_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    fjm_path = assemble_to_path(high_segment_program(_BIG_TOP), tmp_path, memory_width=32)
    patch_run_header(fjm_path, run_flags=RUN_FLAG_FLAT_MEMORY)
    monkeypatch.setenv('FLIPJUMP_FLAT_MAX_WORDS', value)
    assert _run_and_spy(fjm_path, monkeypatch, []) == (_BIG_TOP + 4, True, 'flat')


def test_the_decompression_buffer_grows_with_highly_compressible_data() -> None:
    # 50 MB of zeros compress ~7000x - far past the buffer's 16x start - under a huge claim: the buffer
    # must grow to exactly the data, and its peak stay near the data, not the claim
    import tracemalloc

    from flipjump.fjm.fjm_reader import _decompress_in_chunks

    data = bytes(50_000_000)
    stream = lzma.compress(data, format=_LZMA_FORMAT, filters=_lzma_compression_filters(64, 6))
    assert len(stream) * 16 < len(data)
    tracemalloc.start()
    try:
        decoded = _decompress_in_chunks(stream, None, 1 << 40)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert decoded == data
    assert peak < 3 * len(data), f'{peak:,} bytes allocated for {len(data):,} bytes of data'


def test_the_segment_table_is_read_in_one_read(tmp_path: Path) -> None:
    # a read per segment is thousands of small reads for a big program - seconds on a slow filesystem
    from flipjump.fjm.fjm_reader import _read_header

    writer = Writer(tmp_path / 'x.fjm', 16, FJMVersion.NormalVersion)
    for start in range(0, 2000, 4):
        writer.add_simple_segment_with_data(start, [0, 0])
    writer.write_to_file()

    class CountingFile:
        def __init__(self, handle: Any) -> None:
            self.handle = handle
            self.reads: List[int] = []

        def read(self, size: int = -1) -> bytes:
            self.reads.append(size)
            return bytes(self.handle.read(size))

        def __getattr__(self, name: str) -> Any:
            return getattr(self.handle, name)

    with open(tmp_path / 'x.fjm', 'rb') as handle:
        counting = CountingFile(handle)
        header = _read_header(counting, tmp_path / 'x.fjm')  # type: ignore[arg-type]
    assert len(header.segments) == 500
    assert counting.reads.count(500 * 32) == 1 and len(counting.reads) <= 3  # base header, extension, the table


@pytest.mark.parametrize('missing', [1, 31, 32])
def test_a_truncated_segment_table_is_rejected(tmp_path: Path, missing: int) -> None:
    path = _write_simple(tmp_path / 'x.fjm')
    raw = path.read_bytes()
    table_end = _header_base_size + 12 + 32
    path.write_bytes(raw[: table_end - missing])
    _both_readers_reject(path, "can't unpack")
