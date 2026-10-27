"""
unit-tests for the program's own run defaults in the python API (flipjump/interpreter/run_defaults.py, used by
flipjump_quickstart.run / debug and by the `fj` command line alike): the io device and the flat-memory window
the .fjm's header asks for, the assemble_and_* helpers compiling them in, and the package's exports.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import flipjump
from flipjump import flipjump_quickstart
from flipjump.fjm.fjm_consts import IO_MODE_NAMES, IO_MODE_PC, RUN_FLAG_FLAT_MEMORY
from flipjump.fjm.fjm_reader import read_fjm_header
from flipjump.interpreter.io_devices.cli_devices import IO_MODES
from flipjump.interpreter.io_devices.StandardIO import StandardIO
from flipjump.interpreter.run_defaults import FLAT_MEMORY_AUTO_MAX_WORDS
from flipjump.utils.exceptions import FlipJumpFlatMemoryException
from tests.unit.unit_utils import (
    MINIMAL_STARTUP,
    assemble_to_path,
    high_segment_program,
    native_engine_required,
    patch_run_header,
    spy_engine_runs,
)

pytestmark = pytest.mark.usefixtures('clean_flat_mode_env')


def test_every_compiled_io_mode_names_a_real_io_device() -> None:
    # the header's io modes and the --io devices are two tables; a mode with no device would break `fj --run`
    assert set(IO_MODE_NAMES.values()) <= set(IO_MODES)


def test_the_package_exports_the_run_header_api() -> None:
    for name in ('IO_MODE_STANDARD', 'IO_MODE_PC', 'RUN_FLAG_FLAT_MEMORY', 'FjmHeader', 'read_fjm_header'):
        assert hasattr(flipjump, name) and name in flipjump.__all__


@pytest.mark.parametrize('io_mode, expected', [(0, 'standard'), (IO_MODE_PC, 'pc')])
def test_run_without_a_device_uses_the_compiled_io_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, io_mode: int, expected: str
) -> None:
    fjm_path = assemble_to_path(high_segment_program(1 << 10), tmp_path, memory_width=32)
    patch_run_header(fjm_path, io_mode=io_mode)
    modes: List[str] = []

    def make(mode: str) -> Any:
        modes.append(mode)
        return StandardIO(False)

    monkeypatch.setattr(flipjump_quickstart, 'make_io_device', make)
    flipjump_quickstart.run(fjm_path, print_time=False, print_termination=False)
    assert modes == [expected]
    # an explicit device always wins: the compiled mode is not even looked at
    flipjump_quickstart.run(fjm_path, io_device=StandardIO(False), print_time=False, print_termination=False)
    assert modes == [expected]


@native_engine_required
def test_run_follows_the_compiled_flat_memory_flag_and_the_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    top = FLAT_MEMORY_AUTO_MAX_WORDS  # a span over the automatic limit
    span = top + 4
    fjm_path = assemble_to_path(high_segment_program(top), tmp_path, memory_width=32)
    seen = spy_engine_runs(monkeypatch)

    def run(**kwargs: Any) -> Tuple[Optional[int], bool, Optional[str]]:
        flipjump_quickstart.run(
            fjm_path, io_device=StandardIO(False), print_time=False, print_termination=False, **kwargs
        )
        return seen[-1]

    assert run() == (None, False, 'hybrid')
    assert run(flat_memory=True) == (span, True, 'flat')
    assert run(run_flags=RUN_FLAG_FLAT_MEMORY) == (span, True, 'flat')
    patch_run_header(fjm_path, run_flags=RUN_FLAG_FLAT_MEMORY)
    assert run() == (span, True, 'flat')
    assert run(run_flags=0) == (None, False, 'hybrid')
    assert run(flat_max_words=4) == (4, False, 'hybrid')


@native_engine_required
def test_a_small_program_runs_flat_from_the_api_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fjm_path = assemble_to_path(high_segment_program(1 << 10), tmp_path, memory_width=32)
    seen = spy_engine_runs(monkeypatch)
    flipjump_quickstart.run(fjm_path, io_device=StandardIO(False), print_time=False, print_termination=False)
    assert seen == [((1 << 10) + 4, False, 'flat')]


@native_engine_required
def test_a_failed_compiled_flat_memory_says_how_to_run_it_from_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fjm_path = assemble_to_path(high_segment_program(FLAT_MEMORY_AUTO_MAX_WORDS), tmp_path, memory_width=32)
    patch_run_header(fjm_path, run_flags=RUN_FLAG_FLAT_MEMORY)
    monkeypatch.setenv('FLIPJUMP_TEST_FLAT_ALLOC_FAIL', '1')
    with pytest.raises(
        FlipJumpFlatMemoryException, match=r'(?s)compiled into the \.fjm.*Running it with --run-flags 0 might fix that'
    ):
        flipjump_quickstart.run(fjm_path, io_device=StandardIO(False), print_time=False, print_termination=False)
    patch_run_header(fjm_path, run_flags=0)
    with pytest.raises(
        FlipJumpFlatMemoryException, match=r'(?s)--run-flags turns on flat memory.*Running it with --run-flags 0'
    ):
        flipjump_quickstart.run(
            fjm_path, io_device=StandardIO(False), print_time=False, print_termination=False, run_flags=1
        )
    with pytest.raises(FlipJumpFlatMemoryException) as no_advice:  # asked for directly: no flag to drop
        flipjump_quickstart.run(
            fjm_path,
            io_device=StandardIO(False),
            print_time=False,
            print_termination=False,
            flat_max_words=FLAT_MEMORY_AUTO_MAX_WORDS + 4,
            flat_required=True,
        )
    assert not str(no_advice.value).endswith('\n') and 'might fix' not in str(no_advice.value)


@pytest.mark.parametrize('run_flags', [-1, 2, 1 << 32])
def test_the_api_rejects_bad_run_flags(tmp_path: Path, run_flags: int) -> None:
    fjm_path = assemble_to_path(high_segment_program(1 << 10), tmp_path, memory_width=32)
    with pytest.raises(ValueError, match='bad run flags'):
        flipjump_quickstart.run(
            fjm_path, io_device=StandardIO(False), print_time=False, print_termination=False, run_flags=run_flags
        )


def test_a_device_the_api_created_is_closed_and_a_given_one_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fjm_path = assemble_to_path(high_segment_program(1 << 10), tmp_path, memory_width=32)
    closed: List[str] = []

    class Device(StandardIO):
        def __init__(self, name: str) -> None:
            super().__init__(False)
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    monkeypatch.setattr(flipjump_quickstart, 'make_io_device', lambda mode: Device('created'))
    flipjump_quickstart.run(fjm_path, print_time=False, print_termination=False)
    flipjump_quickstart.run(fjm_path, io_device=Device('given'), print_time=False, print_termination=False)
    assert closed == ['created']


@pytest.mark.parametrize(
    'run_flags, io_mode', [(RUN_FLAG_FLAT_MEMORY, 0), (0, IO_MODE_PC), (RUN_FLAG_FLAT_MEMORY, IO_MODE_PC)]
)
@pytest.mark.parametrize('helper', ['assemble_and_run', 'assemble_and_debug', 'assemble_and_run_test_output'])
def test_the_assemble_and_helpers_compile_the_flags_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str, run_flags: int, io_mode: int
) -> None:
    fj_path = tmp_path / 'p.fj'
    fj_path.write_text(MINIMAL_STARTUP + '\nstartup\nloop: ;loop\n')
    headers: List[Any] = []
    real_debug = flipjump_quickstart.debug

    def debug(fjm_path: Path, *args: Any, **kwargs: Any) -> Any:
        headers.append(read_fjm_header(fjm_path))
        kwargs['io_device'] = StandardIO(False)  # the compiled mode is pc: no window in a test
        return real_debug(fjm_path, *args, **kwargs)

    monkeypatch.setattr(flipjump_quickstart, 'debug', debug)
    monkeypatch.setattr(flipjump_quickstart, 'run_test_output', _run_test_output_through(debug))
    flags = {'run_flags': run_flags, 'io_mode': io_mode}  # unequal pairs: a swap of the two is caught
    kwargs: Dict[str, Any] = {'use_stl': False, 'memory_width': 32, 'print_time': False, 'print_termination': False}
    if helper == 'assemble_and_run_test_output':
        getattr(flipjump_quickstart, helper)([fj_path], b'', b'', **kwargs, **flags)
    else:
        getattr(flipjump_quickstart, helper)([fj_path], **kwargs, **flags)
    assert [(h.run_flags, h.io_mode) for h in headers] == [(run_flags, io_mode)]


def _run_test_output_through(debug: Any) -> Any:
    """run_test_output, routed through the spying debug (it calls fjm_run directly otherwise)"""

    def run_test_output(fjm_path: Path, fixed_input: bytes, expected_output: bytes, **kwargs: Any) -> bool:
        debug(fjm_path, None, print_time=False, print_termination=False)
        return True

    return run_test_output
