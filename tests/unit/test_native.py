"""
unit-tests for flipjump.native: the public names of the native engine.
"""

import importlib
import sys

import pytest

from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.BrokenIO import BrokenIO
from flipjump.utils.exceptions import IOReadOnEOF

needs_the_engine = pytest.mark.skipif(fjm_run._fjcore is None, reason='the native engine (_fjcore) is not built')

PUBLIC_NAMES = ['Memory', 'TERM_EOF', 'TERM_LOOPING', 'TERM_MEMORY_ERROR', 'TERM_NULL_IP', 'cpu_calibrate']


@needs_the_engine
def test_every_public_name_is_the_engines_own() -> None:
    native = importlib.import_module('flipjump.native')

    assert native.__all__ == PUBLIC_NAMES
    for name in PUBLIC_NAMES:
        assert getattr(native, name) is getattr(fjm_run._fjcore, name)


@needs_the_engine
def test_a_program_runs_on_the_public_memory() -> None:
    native = importlib.import_module('flipjump.native')
    memory = native.Memory(64)
    memory.add_segment(0, 8)
    memory.set_words(0, [256, 0])  # one op: flip the first bit of word 4, and jump to itself
    io_device = BrokenIO()

    cause, op_count, _, _, _ = memory.run(io_device.read_bit, io_device.write_bit, IOReadOnEOF)

    assert cause == native.TERM_LOOPING
    assert op_count == 1
    assert memory.get_word(4) == 1


def test_without_the_engine_the_import_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fjm_run, '_fjcore', None)
    monkeypatch.delitem(sys.modules, 'flipjump.native', raising=False)

    with pytest.raises(ImportError, match='the native engine is not built in this flipjump install'):
        importlib.import_module('flipjump.native')
