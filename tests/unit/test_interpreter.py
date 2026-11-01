"""
unit-tests for the interpreter run-loop (flipjump/interpreter/fjm_run.py).

covers a real output program, each termination cause (Looping / NullIP / RuntimeMemoryError
/ EOF), the input path (via a hand-built .fjm and via the stl cat program), and the
last-ops debugging deque.
"""

from pathlib import Path

import pytest

from flipjump.fjm.fjm_consts import FJMVersion
from flipjump.fjm.fjm_writer import Writer
from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.utils.classes import TerminationCause
from flipjump import assemble_and_run
from tests.unit.unit_utils import (
    CAT_PROGRAM,
    HELLO_NO_STL,
    HELLO_WORLD_OUTPUT,
    INFINITE_LOOP_PROGRAM,
    MINIMAL_STARTUP,
    assemble_to_path,
    run_source,
)

from tests.unit.unit_utils import native_engine_required


def test_hello_world_output(tmp_path: Path) -> None:
    statistics, io_device = run_source(HELLO_NO_STL.read_text(), tmp_path)
    assert statistics.termination_cause == TerminationCause.Looping
    assert io_device.get_output(allow_incomplete_output=True) == HELLO_WORLD_OUTPUT


def test_looping_termination(tmp_path: Path) -> None:
    statistics, _ = run_source(INFINITE_LOOP_PROGRAM, tmp_path)
    assert statistics.termination_cause == TerminationCause.Looping


def test_null_ip_termination(tmp_path: Path) -> None:
    statistics, _ = run_source(MINIMAL_STARTUP + '\nstartup\n;0\n', tmp_path)
    assert statistics.termination_cause == TerminationCause.NullIP


def test_runtime_memory_error_termination(tmp_path: Path) -> None:
    statistics, _ = run_source(MINIMAL_STARTUP + '\nstartup\n;0x4000\n', tmp_path, memory_width=16)
    assert statistics.termination_cause == TerminationCause.RuntimeMemoryError
    assert statistics.memory_error_address is not None


def test_eof_termination_handbuilt(tmp_path: Path) -> None:
    # w=8: op0 (at bit 0) jumps to bit 16; op1 (at bit 16) spans the input address
    # (3w + #w = 28), so the interpreter reads input - and an empty FixedIO raises EOF.
    fjm_path = tmp_path / 'eof.fjm'
    writer = Writer(fjm_path, 8, FJMVersion.NormalVersion)
    writer.add_simple_segment_with_data(0, [0, 16, 0, 16])
    writer.write_to_file()

    statistics = fjm_run.run(fjm_path, io_device=FixedIO(b''), show_progress=False)
    assert statistics.termination_cause == TerminationCause.EOF


def test_cat_program_echoes_input() -> None:
    io_device = FixedIO(b'AB')
    statistics = assemble_and_run(
        [CAT_PROGRAM], use_stl=True, io_device=io_device, show_progress=False, print_termination=False
    )
    assert statistics.termination_cause == TerminationCause.EOF
    assert io_device.get_output(allow_incomplete_output=True) == b'AB'


def test_last_ops_addresses_deque(tmp_path: Path) -> None:
    fjm_path = assemble_to_path(INFINITE_LOOP_PROGRAM, tmp_path)

    statistics = fjm_run.run(fjm_path, io_device=FixedIO(b''), show_progress=False, last_ops_debugging_list_length=3)
    assert statistics.last_ops_addresses is not None
    assert len(statistics.last_ops_addresses) <= 3

    statistics_none = fjm_run.run(fjm_path, io_device=FixedIO(b''), show_progress=False)
    assert statistics_none.last_ops_addresses is None


@native_engine_required
def test_native_run_reports_flat_storage_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('FLIPJUMP_NO_NATIVE', raising=False)
    monkeypatch.delenv('FLIPJUMP_NO_FLAT', raising=False)
    statistics, _ = run_source(INFINITE_LOOP_PROGRAM, tmp_path, memory_width=32)
    assert statistics.storage_mode == 'flat'


@native_engine_required
def test_run_flat_max_words_limits_the_flat_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('FLIPJUMP_NO_NATIVE', raising=False)
    fjm_path = assemble_to_path(INFINITE_LOOP_PROGRAM, tmp_path, memory_width=32)
    statistics = fjm_run.run(fjm_path, io_device=FixedIO(b''), show_progress=False, flat_max_words=4)
    assert statistics.termination_cause == TerminationCause.Looping
    assert statistics.storage_mode == 'hybrid'  # words 0..3 flat, the rest page-backed


def test_python_run_storage_mode_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FLIPJUMP_NO_NATIVE', '1')
    statistics, _ = run_source(INFINITE_LOOP_PROGRAM, tmp_path, memory_width=32)
    assert statistics.storage_mode is None
