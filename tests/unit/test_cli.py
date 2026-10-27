"""
unit-tests for the command-line interface (flipjump/flipjump_cli.py).

drives the public assemble_run_according_to_cmd_line_args entry-point with argument lists,
and checks get_version's defaulting/validation logic.
"""

import re
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from flipjump import assemble_run_according_to_cmd_line_args
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.utils.exceptions import FlipJumpParsingException
from flipjump.fjm.fjm_consts import FJMVersion
from flipjump.fjm.fjm_reader import Reader, read_fjm_header
from flipjump.interpreter import fjm_run
from flipjump.flipjump_cli import get_version, parse_arguments
from flipjump.interpreter.run_defaults import FLAT_MEMORY_AUTO_MAX_WORDS
from tests.unit.unit_utils import (
    HELLO_NO_STL,
    PROGRAMS_DIR,
    assemble_to_path,
    high_segment_program,
    spy_engine_runs,
)


def _write_hello(tmp_path: Path) -> Path:
    fj_path = tmp_path / 'hello.fj'
    fj_path.write_text(HELLO_NO_STL.read_text())
    return fj_path


def test_cli_assemble_only(tmp_path: Path) -> None:
    fj_path = _write_hello(tmp_path)
    out_path = tmp_path / 'out.fjm'
    assemble_run_according_to_cmd_line_args(
        cmd_line_args=['--asm', '-o', str(out_path), '--no_stl', '-w', '32', '-s', str(fj_path)]
    )
    assert out_path.is_file()
    assert Reader(out_path).memory_width == 32


def test_cli_run_only(tmp_path: Path) -> None:
    fjm_path = assemble_to_path(HELLO_NO_STL.read_text(), tmp_path)
    assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', str(fjm_path)])


def test_cli_assemble_and_run(tmp_path: Path) -> None:
    fj_path = _write_hello(tmp_path)
    assemble_run_according_to_cmd_line_args(cmd_line_args=['--no_stl', '-s', str(fj_path)])


from tests.unit.unit_utils import native_engine_required  # noqa: E402


@native_engine_required
def test_cli_flat_max_words_flag_is_plumbed_to_the_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # the flag parses into flat_max_words and the run completes; its storage-mode effect
    # (flat_max_words=4 -> 'hybrid') is covered by test_native_memory.py.
    args, _ = parse_arguments(cmd_line_args=['--run', '--flat-max-words', '4', 'x.fjm'])
    assert args.flat_max_words == 4
    fjm_path = assemble_to_path(HELLO_NO_STL.read_text(), tmp_path, memory_width=32)
    assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '--flat-max-words', '4', str(fjm_path)])
    assert 'Finished by looping' in capsys.readouterr().out


@native_engine_required
def test_cli_non_silent_run_reports_run_statistics(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fjm_path = assemble_to_path(HELLO_NO_STL.read_text(), tmp_path, memory_width=32)
    assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', str(fjm_path)])
    out = capsys.readouterr().out
    assert 'Finished by looping' in out
    assert 'ops executed' in out


def test_cli_mutually_exclusive_asm_run(tmp_path: Path) -> None:
    fj_path = _write_hello(tmp_path)
    with pytest.raises(SystemExit):
        assemble_run_according_to_cmd_line_args(cmd_line_args=['-a', '-r', str(fj_path)])


def test_cli_invalid_width(tmp_path: Path) -> None:
    fj_path = _write_hello(tmp_path)
    out_path = tmp_path / 'out.fjm'
    with pytest.raises(SystemExit):
        assemble_run_according_to_cmd_line_args(
            cmd_line_args=['--asm', '-o', str(out_path), '--no_stl', '-w', '7', str(fj_path)]
        )


def test_cli_missing_file(tmp_path: Path) -> None:
    out_path = tmp_path / 'out.fjm'
    with pytest.raises(SystemExit):
        assemble_run_according_to_cmd_line_args(
            cmd_line_args=['--asm', '-o', str(out_path), '--no_stl', str(tmp_path / 'does_not_exist.fj')]
        )


# -D overrides a declaration, it does not make one: so every program here declares the constant it overrides
# (one that does not is test_cli_define_of_an_undeclared_constant_is_refused's)
DEFINE_PROG = """GREET = 0x41
stl.startup
stl.output_char GREET
stl.loop
"""

NAMESPACED_PROG = """ns a {
ns b {
GREET = 0x41
}
}
stl.startup
stl.output_char a.b.GREET
stl.loop
"""


def _assemble_with(tmp_path: Path, name: str, source: str, *defines: str) -> bytes:
    fj_path = tmp_path / f'{name}.fj'
    fj_path.write_text(source)
    out_path = tmp_path / f'{name}.fjm'
    args = ['--asm', '-o', str(out_path), '-w', '32']
    for define in defines:
        args += ['-D', define]
    assemble_run_according_to_cmd_line_args(cmd_line_args=args + [str(fj_path)])
    return out_path.read_bytes()


def test_cli_define_reaches_the_assembled_binary(tmp_path: Path) -> None:
    # two defines give two binaries, and the same define twice the same binary (the control: else "they differ"
    # could be true of any two runs). all three assemble in one process, so the later ones hit the stl-prefix
    # cache: a cache that served a stale override would fail the first assert
    a = _assemble_with(tmp_path, 'a', DEFINE_PROG, 'GREET = 0x41')
    b = _assemble_with(tmp_path, 'b', DEFINE_PROG, 'GREET = 0x58')
    again = _assemble_with(tmp_path, 'again', DEFINE_PROG, 'GREET = 0x41')
    assert a != b, 'the -D value did not reach the binary'
    assert a == again, 'the assembler is not deterministic - the difference above proves nothing'


def test_cli_define_overrides_the_programs_own_declaration(tmp_path: Path) -> None:
    # overriding 0x41 with 0x58 assembles exactly what writing 0x58 does; the inequality is the control
    overridden = _assemble_with(tmp_path, 'ov', DEFINE_PROG, 'GREET = 0x58')
    written = _assemble_with(tmp_path, 'wr', DEFINE_PROG.replace('0x41', '0x58'))
    untouched = _assemble_with(tmp_path, 'un', DEFINE_PROG)
    assert overridden == written, '-D did not replace the declaration'
    assert overridden != untouched, 'the two sources are identical - the equality proves nothing'


def test_cli_define_addresses_a_namespaced_constant_by_its_full_name(tmp_path: Path) -> None:
    # a constant declared in `ns a { ns b {` is a.b.GREET; the bare name names another (here undeclared) constant
    overridden = _assemble_with(tmp_path, 'ns_ov', NAMESPACED_PROG, 'a.b.GREET = 0x58')
    written = _assemble_with(tmp_path, 'ns_wr', NAMESPACED_PROG.replace('0x41', '0x58'))
    assert overridden == written

    with pytest.raises(FlipJumpParsingException):
        _assemble_with(tmp_path, 'ns_bare', NAMESPACED_PROG, 'GREET = 0x58')


def test_cli_define_of_an_undeclared_constant_is_refused(tmp_path: Path) -> None:
    # a define that meets no declaration is a typo
    bare_prog = DEFINE_PROG.replace('GREET = 0x41', '').replace('GREET', '0x41')
    assert 'GREET' not in bare_prog
    with pytest.raises(FlipJumpParsingException):
        _assemble_with(tmp_path, 'undeclared', bare_prog, 'GREET = 0x41')


def test_cli_define_of_a_parser_builtin_is_refused(tmp_path: Path) -> None:
    # `w` is set by -w, not declared by the program. the message is asserted: overriding `w` breaks the build in
    # several ways at once, so the exception alone would pass without the builtin's own refusal
    with pytest.raises(FlipJumpParsingException) as excinfo:
        _assemble_with(tmp_path, 'width', DEFINE_PROG, 'w = 64')
    assert 'override of non-defined constant' in str(excinfo.value), str(excinfo.value)


def test_cli_define_value_may_use_the_builtin_width(tmp_path: Path) -> None:
    # the defines file is read before the stl (see test_ptr_cell_bits.py), so a define's value may use the parser
    # builtin `w` (32 here: w + 33 is 65), but not an stl constant such as `dw` (write 2*w)
    computed = _assemble_with(tmp_path, 'computed', DEFINE_PROG, 'GREET = w + 33')
    literal = _assemble_with(tmp_path, 'literal', DEFINE_PROG, 'GREET = 65')
    assert computed == literal


def test_cli_define_is_repeatable_and_may_use_an_earlier_define(tmp_path: Path) -> None:
    # -D appends, in order: a later define sees an earlier one's overridden value, not the declared one
    source = 'BASE = 0\n' + DEFINE_PROG
    chained = _assemble_with(tmp_path, 'chained', source, 'BASE = 0x40', 'GREET = BASE + 1')
    literal = _assemble_with(tmp_path, 'lit2', source, 'BASE = 0x40', 'GREET = 0x41')
    assert chained == literal


def test_cli_define_without_a_value_is_refused(tmp_path: Path) -> None:
    # a bare -D NAME is a typo, not an empty definition
    fj_path = tmp_path / 'defines.fj'
    fj_path.write_text(DEFINE_PROG)
    with pytest.raises(SystemExit):
        assemble_run_according_to_cmd_line_args(
            cmd_line_args=['--asm', '-o', str(tmp_path / 'out.fjm'), '-w', '32', '-D', 'GREET', str(fj_path)]
        )


def test_cli_define_with_a_malformed_name_is_refused(tmp_path: Path) -> None:
    # the name must be a (possibly dotted) identifier: else the error would point at the generated defines file
    for bad in ('2GREET', 'a..b', 'a.', 'GREET GREET'):
        with pytest.raises(SystemExit):
            _assemble_with(tmp_path, 'bad', DEFINE_PROG, bad + ' = 1')


def test_cli_define_repeated_for_one_name_still_needs_a_declaration(tmp_path: Path) -> None:
    # a define does not satisfy itself: a misspelled -D given twice is still refused
    with pytest.raises(FlipJumpParsingException):
        _assemble_with(tmp_path, 'twice', DEFINE_PROG, 'TYPO = 12', 'TYPO = 16')


def test_cli_define_repeated_for_a_declared_name_takes_the_last(tmp_path: Path) -> None:
    # the control of the test above: on a declared name the last -D wins, as two lines of one file would
    twice = _assemble_with(tmp_path, 'lastwins', DEFINE_PROG, 'GREET = 0x58', 'GREET = 0x41')
    once = _assemble_with(tmp_path, 'onceonly', DEFINE_PROG, 'GREET = 0x41')
    assert twice == once


def test_cli_define_refusal_names_the_defines_file(tmp_path: Path) -> None:
    # the error points at the -D, not at the user's program (a position that exists there, and is wrong)
    fj_path = tmp_path / 'prog.fj'
    fj_path.write_text(DEFINE_PROG)
    with pytest.raises(FlipJumpParsingException) as excinfo:
        assemble_run_according_to_cmd_line_args(
            cmd_line_args=['--asm', '-o', str(tmp_path / 'out.fjm'), '-w', '32', '-D', 'NOPE = 5', str(fj_path)]
        )
    # the reported file, not any substring: the temp directory is itself named "..__prog.fj__temp_directory"
    message = str(excinfo.value)
    reported = re.search(r'in file (.+?) \(line', message)
    assert reported is not None, message
    assert Path(reported.group(1)).name == '_defines.fj', reported.group(1)


def _no_error(message: str) -> None:
    raise AssertionError(f'unexpected error: {message}')


def test_get_version_default_with_outfile() -> None:
    assert get_version(None, True, _no_error) == FJMVersion.CompressedVersion


def test_get_version_default_without_outfile() -> None:
    assert get_version(None, False, _no_error) == FJMVersion.NormalVersion


def test_get_version_explicit() -> None:
    assert get_version(2, False, _no_error) == FJMVersion.RelativeJumpVersion


def test_get_version_invalid_calls_error() -> None:
    def raise_error(message: str) -> None:
        raise SystemExit(message)

    with pytest.raises(SystemExit):
        get_version(99, False, raise_error)


def test_cli_invalid_flat_max_words_rejected(tmp_path: Path) -> None:
    fjm_path = assemble_to_path(HELLO_NO_STL.read_text(), tmp_path)
    with pytest.raises(SystemExit):
        assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', '--flat-max-words', '0', str(fjm_path)])


def test_cli_invalid_io_mode_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # --io only accepts the registered mode names; rejected at parse time (no pygame needed)
    fjm_path = assemble_to_path(HELLO_NO_STL.read_text(), tmp_path)
    with pytest.raises(SystemExit):
        assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', '--io', 'hologram', str(fjm_path)])
    assert 'argument --io' in capsys.readouterr().err  # argparse rejected it, before any assemble/run


def test_cli_io_mode_parameters_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # mode parameters (whitespace-separated after the name) parse, but no current mode takes any
    fjm_path = assemble_to_path(HELLO_NO_STL.read_text(), tmp_path)
    with pytest.raises(SystemExit):
        assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', '--io', 'standard loud', str(fjm_path)])
    assert 'no parameters' in capsys.readouterr().err  # the factory's message reaches the user


def test_cli_io_default_is_the_programs_io_mode() -> None:
    # no --io: the io mode compiled into the .fjm decides (standard unless it was assembled with --io-mode)
    args, _ = parse_arguments(cmd_line_args=['prog.fjm'])
    assert args.io is None


def test_cli_no_output_flag_is_gone(tmp_path: Path) -> None:
    # --no_output was dropped: the standard device is built only by make_io_device()
    fjm_path = assemble_to_path(HELLO_NO_STL.read_text(), tmp_path)
    with pytest.raises(SystemExit):
        assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', '--no_output', str(fjm_path)])


@pytest.mark.parametrize('flag', ['-V', '--flipjump_version'])
def test_cli_flipjump_version_flag(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    # -V / --flipjump_version print the package version and exit 0, before the required `files` positional.
    from flipjump import __version__

    with pytest.raises(SystemExit) as exc_info:
        parse_arguments(cmd_line_args=[flag])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f'flipjump {__version__}'


# the span of a high_segment_program decides the flat window (the native engine's default window is 2^23
# words; the CLI's automatic limit is 2^24)
_SMALL_TOP, _BIG_TOP = 1 << 23, FLAT_MEMORY_AUTO_MAX_WORDS


def test_the_header_span_is_the_highest_segment_end(tmp_path: Path) -> None:
    fjm_path = assemble_to_path(high_segment_program(_SMALL_TOP), tmp_path, memory_width=32)
    expected = max(seg.segment_start + seg.segment_length for seg in Reader(fjm_path).memory_segments)
    assert read_fjm_header(fjm_path).span_words == expected == _SMALL_TOP + 4


def test_cli_flat_memory_flag_parses_and_excludes_flat_max_words() -> None:
    args, _ = parse_arguments(cmd_line_args=['--run', '--flat-memory', 'x.fjm'])
    assert args.flat_memory and args.flat_max_words is None
    with pytest.raises(SystemExit):
        parse_arguments(cmd_line_args=['--run', '--flat-memory', '--flat-max-words', '4', 'x.fjm'])


def _run_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, top: int, *runs: List[str]
) -> List[Tuple[Optional[int], Optional[str]]]:
    """run the program (span top + 4) once per extra-argument list; -> [(flat_max_words passed, storage mode)]"""
    fjm_path = assemble_to_path(high_segment_program(top), tmp_path, memory_width=32)
    seen = spy_engine_runs(monkeypatch)
    for extra in runs:
        assemble_run_according_to_cmd_line_args(cmd_line_args=['--run', '-s', *extra, str(fjm_path)])
    return [(words, mode) for words, _required, mode in seen]


@native_engine_required
@pytest.mark.usefixtures('clean_flat_mode_env')
def test_cli_small_span_runs_flat_without_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # span 2^23 + 4 <= 2^24: automatic --flat-memory (the engine default alone would be 'hybrid');
    # an explicit --flat-max-words still wins
    seen = _run_modes(tmp_path, monkeypatch, _SMALL_TOP, [], ['--flat-max-words', '4'])
    assert seen == [(_SMALL_TOP + 4, 'flat'), (4, 'hybrid')]


@native_engine_required
@pytest.mark.usefixtures('clean_flat_mode_env')
def test_cli_big_span_needs_the_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # span 2^24 + 4 > 2^24: no automatic window (the engine default -> 'hybrid'); --flat-memory -> 'flat'
    seen = _run_modes(tmp_path, monkeypatch, _BIG_TOP, [], ['--flat-memory'])
    assert seen == [(None, 'hybrid'), (_BIG_TOP + 4, 'flat')]


# hexlib programs whose tables are declared relocatable (hex.exact_xor's and its kin's, the bit shifts')
_HEXLIB = PROGRAMS_DIR / 'hexlib_tests'
_POOLED_PROGRAMS = {
    'and': [_HEXLIB / '2params' / 'and.fj'],
    'basic_math': [_HEXLIB / 'basics1' / 'basic_math.fj'],
    'shl_bit': [_HEXLIB / 'basics2' / 'shift_utils.fj', _HEXLIB / 'basics2' / 'shl_bit.fj'],
    'shr_bit': [_HEXLIB / 'basics2' / 'shift_utils.fj', _HEXLIB / 'basics2' / 'shr_bit.fj'],
}


def _cli_assemble(tmp_path: Path, name: str, files: List[Path], *options: str) -> Path:
    out_path = tmp_path / f'{name}.fjm'
    assemble_run_according_to_cmd_line_args(
        cmd_line_args=['--asm', '-s', '-o', str(out_path), *options, *(str(path) for path in files)]
    )
    return out_path


def _output_and_ops(fjm_path: Path) -> Tuple[bytes, int]:
    io_device = FixedIO(b'')
    statistics = fjm_run.run(fjm_path, io_device=io_device, print_time=False)
    return io_device.get_output(allow_incomplete_output=True), statistics.op_counter


@pytest.mark.parametrize('program', sorted(_POOLED_PROGRAMS))
def test_cli_pool_tables_runs_the_same_program_in_fewer_ops(tmp_path: Path, program: str) -> None:
    files = _POOLED_PROGRAMS[program]
    plain = _cli_assemble(tmp_path, 'plain', files)
    pooled = _cli_assemble(tmp_path, 'pooled', files, '--pool-tables')
    assert pooled.read_bytes() != plain.read_bytes()  # the pool moved tables
    (plain_output, plain_ops), (pooled_output, pooled_ops) = _output_and_ops(plain), _output_and_ops(pooled)
    assert plain_output and pooled_output == plain_output
    assert pooled_ops < plain_ops


def test_cli_pool_tables_runs_a_program_that_reads_through_pointers_the_same(tmp_path: Path) -> None:
    # a pointer may read any word raw, so the pool pins none (assembler.unpin): the tables still move
    files = [PROGRAMS_DIR / 'concept_checks' / 'hex_ptr.fj']
    plain, pooled = _cli_assemble(tmp_path, 'plain', files), _cli_assemble(tmp_path, 'pooled', files, '--pool-tables')
    assert pooled.read_bytes() != plain.read_bytes()
    assert _output_and_ops(pooled)[0] == _output_and_ops(plain)[0] != b''


def test_cli_pool_tables_keeps_inline_the_tables_of_a_program_that_fills_the_memory(tmp_path: Path) -> None:
    # prime_sieve reserves the upper half of memory after its code: no room for its tables, so it is assembled without
    # the pool - the binary is the plain one, byte for byte
    plain = _cli_assemble(tmp_path, 'plain', [PROGRAMS_DIR / 'prime_sieve.fj'])
    pooled = _cli_assemble(tmp_path, 'pooled', [PROGRAMS_DIR / 'prime_sieve.fj'], '--pool-tables')
    assert pooled.read_bytes() == plain.read_bytes()
