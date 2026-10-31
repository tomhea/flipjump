"""
end-to-end unit-tests for the high-level API (flipjump/flipjump_quickstart.py).

assembles and runs the small no-stl hello program across the fjm-versions and memory-widths,
and checks the run_test_output success/failure contract.
"""

import inspect
from pathlib import Path
from typing import Any, Dict, Set

import pytest

from flipjump import assemble_and_run, assemble_and_run_test_output, flipjump_quickstart, run_test_output
from flipjump.assembler import assembler
from flipjump.fjm.fjm_writer import Writer
from flipjump.utils.exceptions import FlipJumpAssemblerException
from flipjump.utils.functions import load_debugging_labels
from flipjump.fjm.fjm_consts import FJMVersion
from flipjump.utils.classes import TerminationCause
from tests.unit.unit_utils import HELLO_NO_STL, HELLO_WORLD_OUTPUT, assemble_to_path


@pytest.mark.parametrize('fjm_version', list(FJMVersion))
@pytest.mark.parametrize('memory_width', [32, 64])
def test_assemble_and_run_hello(fjm_version: FJMVersion, memory_width: int) -> None:
    assert assemble_and_run_test_output(
        [HELLO_NO_STL],
        b'',
        HELLO_WORLD_OUTPUT,
        memory_width=memory_width,
        use_stl=False,
        fjm_version=fjm_version,
        should_raise_assertion_error=True,
        show_progress=False,
        print_termination=False,
    )


def test_run_test_output_failure_returns_false(tmp_path: Path) -> None:
    fjm_path = assemble_to_path(HELLO_NO_STL.read_text(), tmp_path)
    assert not run_test_output(
        fjm_path,
        b'',
        b'WRONG OUTPUT',
        should_raise_assertion_error=False,
        show_progress=False,
        print_termination=False,
    )


def test_run_test_output_failure_raises(tmp_path: Path) -> None:
    fjm_path = assemble_to_path(HELLO_NO_STL.read_text(), tmp_path)
    with pytest.raises(AssertionError):
        run_test_output(
            fjm_path,
            b'',
            b'WRONG OUTPUT',
            should_raise_assertion_error=True,
            show_progress=False,
            print_termination=False,
        )


def test_assemble_returns_the_labels(tmp_path: Path) -> None:
    source = tmp_path / 'p.fj'
    source.write_text('a: ;a\nb: a+3;b\nns n {\n  c: ;a\n}\n')
    labels = flipjump_quickstart.assemble([source], tmp_path / 'p.fjm', use_stl=False, show_progress=False)
    assert {name: labels[name] for name in ('a', 'b', 'n.c')} == {'a': 0, 'b': 2 * 64, 'n.c': 4 * 64}
    writer = Writer(tmp_path / 'q.fjm', 32, FJMVersion.NormalVersion)
    labels = assembler.assemble([('f1', source)], 32, writer, show_progress=False)
    assert {name: labels[name] for name in ('a', 'b', 'n.c')} == {'a': 0, 'b': 2 * 32, 'n.c': 4 * 32}


def test_the_assembler_takes_the_memory_width_from_the_writer(tmp_path: Path) -> None:
    source = tmp_path / 'p.fj'
    source.write_text('a: ;a\nb: a+3;b\n')
    for width in (16, 32):
        labels = assembler.assemble(
            [('f1', source)],
            fjm_writer=Writer(tmp_path / 'w.fjm', width, FJMVersion.NormalVersion),
            show_progress=False,
        )
        assert labels['b'] == 2 * width
    with pytest.raises(FlipJumpAssemblerException, match="differs from the writer's memory-width 16"):
        assembler.assemble([('f1', source)], 32, Writer(tmp_path / 'x.fjm', 16, FJMVersion.NormalVersion))
    with pytest.raises(TypeError, match='needs fjm_writer'):
        assembler.assemble([('f1', source)], 32)


def test_assemble_returns_the_debugging_labels_with_a_debugging_file(tmp_path: Path) -> None:
    source = tmp_path / 'p.fj'
    source.write_text('stl.startup\nloop: ;loop\n')
    debugging_file = tmp_path / 'p.fjd'
    plain = flipjump_quickstart.assemble([source], tmp_path / 'a.fjm', show_progress=False)
    debugged = flipjump_quickstart.assemble(
        [source], tmp_path / 'b.fjm', show_progress=False, debugging_file_path=debugging_file
    )
    assert debugged == load_debugging_labels(debugging_file)
    assert plain.items() <= debugged.items() and 'loop' in plain
    # the control: the debugging-only labels are there, and only with the debugging file
    assert any(':start:' in name for name in debugged) and not any(':start:' in name for name in plain)


def test_assemble_and_run_returns_looping() -> None:
    statistics = assemble_and_run([HELLO_NO_STL], use_stl=False, show_progress=False, print_termination=False)
    assert statistics.termination_cause == TerminationCause.Looping


# {helper: (the functions it combines, the parameters it owns itself)}: the paths of the temporary files it
# creates, and run_test_output's FixedIO device
_OWN_FILES = {'output_fjm_path', 'debugging_file_path', 'fjm_path', 'debugging_file'}
_COMBINED = {
    'assemble_and_run': (('assemble', 'run'), _OWN_FILES),
    'assemble_and_debug': (('assemble', 'debug'), _OWN_FILES),
    'assemble_and_run_test_output': (('assemble', 'run_test_output'), _OWN_FILES),
    'run_test_output': (('run',), _OWN_FILES | {'io_device'}),
}


def _defaults(function_name: str) -> Dict[str, Any]:
    signature = inspect.signature(getattr(flipjump_quickstart, function_name))
    return {name: parameter.default for name, parameter in signature.parameters.items()}


def _not_forwarded(helper_name: str) -> Set[str]:
    """the parameters of the helper's halves that the helper lacks, or takes with none of its halves' defaults"""
    halves, owned = _COMBINED[helper_name]
    helper = _defaults(helper_name)
    halves_defaults = [_defaults(half) for half in halves]
    return {
        name
        for name in set().union(*halves_defaults) - owned
        if name not in helper
        or helper[name] not in [defaults[name] for defaults in halves_defaults if name in defaults]
    }


@pytest.mark.parametrize('helper_name', sorted(_COMBINED))
def test_a_combined_helper_takes_every_parameter_of_its_halves(helper_name: str) -> None:
    assert _not_forwarded(helper_name) == set()


def test_the_check_sees_a_parameter_a_helper_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    signature = inspect.signature(flipjump_quickstart.assemble_and_run)

    def reduced(*args: Any, **kwargs: Any) -> Any:
        return flipjump_quickstart.assemble_and_run(*args, **kwargs)

    setattr(
        reduced,
        '__signature__',
        signature.replace(parameters=[p for p in signature.parameters.values() if p.name != 'table_pool']),
    )
    monkeypatch.setattr(flipjump_quickstart, 'assemble_and_run', reduced)
    assert _not_forwarded('assemble_and_run') == {'table_pool'}
