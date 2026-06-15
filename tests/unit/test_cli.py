"""
unit-tests for the command-line interface (flipjump/flipjump_cli.py).

drives the public assemble_run_according_to_cmd_line_args entry-point with argument lists,
and checks get_version's defaulting/validation logic.
"""

from pathlib import Path

import pytest

from flipjump import assemble_run_according_to_cmd_line_args
from flipjump.fjm.fjm_consts import FJMVersion
from flipjump.fjm.fjm_reader import Reader
from flipjump.flipjump_cli import get_version, parse_arguments
from tests.unit.unit_utils import HELLO_NO_STL, assemble_to_path


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


def test_cli_io_default_is_standard() -> None:
    args, _ = parse_arguments(cmd_line_args=['prog.fjm'])
    assert args.io == 'standard'


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
