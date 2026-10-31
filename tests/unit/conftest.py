"""shared fixtures for the unit-tests."""

import pytest

from flipjump.fjm import fjm_reader
from flipjump.interpreter import fjm_run
from flipjump.interpreter.fjm_run import TerminationStatistics
from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.utils.classes import RunStatistics


@pytest.fixture
def clean_flat_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """unset the environment variables that pick the engine and its flat-memory window."""
    for var in ('FLIPJUMP_NO_NATIVE', 'FLIPJUMP_NO_FLAT', 'FLIPJUMP_FLAT_MAX_WORDS', 'FLIPJUMP_TEST_FLAT_ALLOC_FAIL'):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(params=['fast-python', 'native', 'featured'])
def engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """parametrizes a test over all three run-loops: the pure-python fast loop, the native
    engine, and the featured loop (the slower one used for tracing/breakpoints/profiling)."""
    param = str(request.param)
    if param == 'native':
        if fjm_run._fjcore is None:  # type: ignore[attr-defined]
            pytest.skip('the native engine (_fjcore) is not built')
        monkeypatch.delenv('FLIPJUMP_NO_NATIVE', raising=False)
    else:
        monkeypatch.setattr(fjm_run, '_fjcore', None)  # both python loops run with native disabled
        if param == 'featured':
            # route the fast loop through the featured loop, so `engine`-parametrized tests
            # exercise the trace/breakpoint/profile run-loop too (run() already attached the
            # ReaderDeviceMemory hook on the non-native path, which the featured loop reuses)
            real_featured = fjm_run._run_featured

            def via_featured(
                mem: fjm_reader.Reader, io_device: IODevice, statistics: RunStatistics
            ) -> TerminationStatistics:
                return real_featured(mem, io_device, statistics, None, False)

            monkeypatch.setattr(fjm_run, '_run_fast', via_featured)
    return param
