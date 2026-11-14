"""
unit-tests for the progress bars (flipjump/utils/progress.py) and their hooks in the assembler and the loader.

the contract: off a terminal the output is exactly PrintTimer's (`  stage:  1.234s`), on a terminal the line shows
the progress and ends the same way, the reported progress only moves forward inside [0, 1], and turning the bars on
changes nothing in what is assembled.
"""

import inspect
import io
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flipjump import flipjump_quickstart
from flipjump.fjm.fjm_consts import FJMVersion
from flipjump.fjm.fjm_reader import read_fjm_header
from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.utils import progress as progress_module
from flipjump.utils.progress import ProgressBar
from tests.unit.unit_utils import CAT_PROGRAM, native_engine_required


class _Terminal(io.StringIO):
    """a captured stream that says it is an interactive terminal"""

    def isatty(self) -> bool:
        return True


def _terminal(monkeypatch: pytest.MonkeyPatch) -> _Terminal:
    """stdout, as a terminal (patched in the test itself: pytest's capture re-sets sys.stdout after the fixtures)"""
    stream = _Terminal()
    monkeypatch.setattr('sys.stdout', stream)
    monkeypatch.setattr(progress_module, '_REDRAW_SECONDS', 0)  # draw every update
    _terminal_width(monkeypatch, 120)  # (not the width of the terminal the tests run in)
    return stream


def _terminal_width(monkeypatch: pytest.MonkeyPatch, columns: int) -> None:
    monkeypatch.setattr(shutil, 'get_terminal_size', lambda: os.terminal_size((columns, 24)))


def _screen_row(output: str) -> str:
    """what a one-row terminal shows after `output`: '\r' returns to column 0, characters overwrite"""
    row: List[str] = []
    column = 0
    for char in output.rstrip('\n'):
        if char == '\r':
            column = 0
            continue
        if column < len(row):
            row[column] = char
        else:
            row.append(char)
        column += 1
    return ''.join(row)


def test_off_a_terminal_it_prints_in_print_timers_format(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(ValueError):
        with ProgressBar('  stage:  ') as bar:
            assert not bar.active
            bar.update(0.5)
            raise ValueError('the stage failed')  # the time is printed all the same
    assert re.fullmatch(r'  stage:  \d+\.\d{3}s\n', capsys.readouterr().out)


def test_on_a_terminal_it_draws_a_bar_and_ends_with_the_time(monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = _terminal(monkeypatch)
    with ProgressBar('  stage:  ') as bar:
        assert bar.active
        bar._start_time -= 75  # long enough to show the time left
        bar.update(0.75)
    frames = terminal.getvalue().split('\r')
    assert any(' 75%' in frame and '0:25 left' in frame and '#' * 22 in frame for frame in frames)
    shown = _screen_row(terminal.getvalue())  # the bar is overwritten, all of it
    assert re.fullmatch(r'  stage:  \d+\.\d{3}s *', shown) and terminal.getvalue().endswith('\n')


def test_show_progress_false_prints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = _terminal(monkeypatch)
    with ProgressBar('  stage:  ', show_progress=False) as bar:
        assert not bar.active
        bar.update(1)
    assert terminal.getvalue() == ''


def test_safe_print_erases_the_bar_prints_and_draws_it_again(monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = _terminal(monkeypatch)
    with ProgressBar('  parsing:  ') as bar:
        bar.update(0.5)
        progress_module.safe_print('a warning')
        rows = terminal.getvalue().split('\n')
        assert _screen_row(rows[0]).rstrip() == 'a warning'  # on the bar's row, with nothing of the bar left
        assert _screen_row(rows[1]).startswith('  parsing:  ###############---------------  50%')  # drawn again below
    assert progress_module._shown_stage is None
    progress_module.safe_print('no bar is shown')  # with no bar, it is print()
    assert terminal.getvalue().endswith('no bar is shown\n')


def test_safe_print_before_the_first_update_frees_the_stage_name_too(monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = _terminal(monkeypatch)
    with ProgressBar('  labels resolve:  '):
        progress_module.safe_print('a warning')
        rows = terminal.getvalue().split('\n')
        assert _screen_row(rows[0]).rstrip() == 'a warning'  # the stage's name is erased from that row
        assert _screen_row(rows[1]).startswith('  labels resolve:  ------------------------------   0%')
    assert re.fullmatch(r'  labels resolve:  \d+\.\d{3}s *', _screen_row(terminal.getvalue().split('\n')[1]))


def test_off_a_terminal_safe_print_ends_the_stage_line_and_names_the_stage_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with ProgressBar('  labels resolve:  '):
        progress_module.safe_print('a warning')
    assert re.fullmatch(r'  labels resolve:  \na warning\n  labels resolve:  \d+\.\d{3}s\n', capsys.readouterr().out)


@pytest.mark.parametrize('columns, bar_width', [(120, 30), (60, 22), (48, 10), (47, 0), (30, 0)])
def test_the_bar_is_as_wide_as_the_terminal_has_room_for(
    monkeypatch: pytest.MonkeyPatch, columns: int, bar_width: int
) -> None:
    """the longest line (`100%  999:59 left`) leaves the last column free; under 10 columns of bar, no bar"""
    terminal = _terminal(monkeypatch)
    _terminal_width(monkeypatch, columns)
    with ProgressBar('  loading memory:  ') as bar:
        assert bar.active == (bar_width > 0) and (bar.get_active_update_function() is None) == (bar_width == 0)
        bar._start_time -= 10_000_000  # so long that the time left is at its longest, 999:59
        bar.update(0.5)
        row = _screen_row(terminal.getvalue().split('\n')[-1])
    if bar_width:
        half = bar_width // 2
        assert row == '  loading memory:  ' + '#' * half + '-' * (bar_width - half) + '  50%  999:59 left'
        assert len(row) <= columns - 1
    else:
        assert '#' not in terminal.getvalue() and terminal.getvalue().startswith('  loading memory:  ')


class _Recorder:
    """records every progress update a hook reports"""

    def __init__(self) -> None:
        self.values: List[float] = []

    def __call__(self, value: float) -> None:
        self.values.append(value)


def _assert_moves_forward_within_0_1(values: List[float]) -> None:
    assert values, 'no progress was reported'
    assert all(0.0 <= value <= 1.0 for value in values)
    assert all(later >= earlier for earlier, later in zip(values, values[1:]))


def test_the_assembler_phases_report_forward_progress_and_assemble_the_same_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _terminal(monkeypatch)
    # every phase's bar on, through a fake terminal; the output must be byte-identical to a quiet assembly
    updates: List[List[float]] = []
    real_update = vars(ProgressBar)['update']  # the original, called by the patch

    def recording_update(self: ProgressBar, done: float) -> None:
        if getattr(self, '_recorded', None) is None:
            self._recorded = []  # type: ignore[attr-defined]
            updates.append(self._recorded)  # type: ignore[attr-defined]
        self._recorded.append(done)  # type: ignore[attr-defined]
        real_update(self, done)

    monkeypatch.setattr(ProgressBar, 'update', recording_update)
    loud = tmp_path / 'loud.fjm'
    flipjump_quickstart.assemble([CAT_PROGRAM], loud, show_progress=True, fjm_version=FJMVersion.CompressedVersion)
    quiet = tmp_path / 'quiet.fjm'
    flipjump_quickstart.assemble([CAT_PROGRAM], quiet, show_progress=False, fjm_version=FJMVersion.CompressedVersion)
    assert loud.read_bytes() == quiet.read_bytes()
    assert len(updates) == 4  # parsing, macro resolve, labels resolve, create binary
    for values in updates:
        _assert_moves_forward_within_0_1(values)


@native_engine_required
def test_the_loading_bar_moves_forward(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _terminal(monkeypatch)
    fjm_path = tmp_path / 'cat.fjm'
    flipjump_quickstart.assemble([CAT_PROGRAM], fjm_path, show_progress=False, fjm_version=FJMVersion.CompressedVersion)
    recorder = _Recorder()
    with ProgressBar('  loading memory:  ') as bar:
        real_update = bar.update

        def update(done: float) -> None:
            recorder(done)
            real_update(done)

        bar.update = update  # type: ignore[method-assign, assignment]
        fjm_run._load_native(fjm_path, read_fjm_header(fjm_path), None, False, bar)
    _assert_moves_forward_within_0_1(recorder.values)
    assert recorder.values[-1] == 1.0


# the functions that took print_time in flipjump 1.5: they take it still, as their show_progress
_TOOK_PRINT_TIME = [
    ('flipjump_quickstart', name)
    for name in (
        'assemble',
        'run',
        'debug',
        'run_test_output',
        'assemble_and_run',
        'assemble_and_debug',
        'assemble_and_run_test_output',
    )
] + [('assembler', 'assemble'), ('fjm_run', 'run')]


@pytest.mark.parametrize('module_name, function_name', _TOOK_PRINT_TIME)
def test_print_time_is_still_taken_where_flipjump_1_5_took_it(module_name: str, function_name: str) -> None:
    from flipjump.assembler import assembler

    module = {'flipjump_quickstart': flipjump_quickstart, 'assembler': assembler, 'fjm_run': fjm_run}[module_name]
    parameters = inspect.signature(getattr(module, function_name)).parameters
    assert parameters['print_time'].default is None and 'show_progress' in parameters


def test_print_time_is_show_progress(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fjm_path = tmp_path / 'cat.fjm'
    cases: List[Tuple[Dict[str, Any], bool]] = [
        ({'show_progress': False}, False),  # the control: show_progress=False is silent
        ({'show_progress': True}, True),
        ({'print_time': True}, True),
        ({'print_time': False}, False),
        ({'show_progress': False, 'print_time': True}, True),  # print_time, when given, is show_progress
    ]
    for kwargs, shown in cases:
        flipjump_quickstart.assemble([CAT_PROGRAM], fjm_path, **kwargs)
        fjm_run.run(fjm_path, io_device=FixedIO(b''), **kwargs)
        out = capsys.readouterr().out
        assert ('  parsing:' in out and '  loading memory:' in out) == shown, kwargs
        assert ('  parsing:' in out or '  loading memory:' in out) == shown, kwargs


def test_the_progress_bar_is_shown_by_show_progress_only() -> None:
    with pytest.raises(TypeError):
        ProgressBar('  stage:  ', print_time=False)  # type: ignore[call-arg]


def test_labels_resolve_reports_its_share_of_ops_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a tiny chunk, so even a small program reports many times: up to exactly 1.0 when every op is resolved
    from flipjump.assembler import assembler

    monkeypatch.setattr(assembler, '_PROGRESS_CHUNK', 4)
    recorder = _Recorder()
    real_labels_resolve = assembler.labels_resolve

    def labels_resolve(*args: Any, **kwargs: Any) -> None:
        kwargs['on_progress'] = recorder
        real_labels_resolve(*args, **kwargs)

    monkeypatch.setattr(assembler, 'labels_resolve', labels_resolve)
    flipjump_quickstart.assemble([CAT_PROGRAM], tmp_path / 'cat.fjm', show_progress=False)
    _assert_moves_forward_within_0_1(recorder.values)
    assert len(recorder.values) > 10 and recorder.values[0] < 0.5 and recorder.values[-1] == 1.0


def test_a_warning_during_a_stage_gets_its_own_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """the pins are resolved inside the labels-resolve stage, and may warn there (off a terminal here)"""
    from flipjump.assembler.inner_classes.table_pool import BlockPool

    fj_path = tmp_path / 'p.fj'
    fj_path.write_text('stl.startup\n    hex.xor x, y\n    stl.loop\nx: hex.hex 3\ny: hex.hex 5\n')
    pool = BlockPool(64, pin_all_but=['nowhere'])
    flipjump_quickstart.assemble(
        [fj_path], tmp_path / 'p.fjm', memory_width=64, warning_as_errors=False, show_progress=True, table_pool=pool
    )
    out = capsys.readouterr().out
    warning = r'Warning: pin_all_but names labels the program does not define: nowhere'
    assert re.search(rf'^  labels resolve:  \n{warning}\n  labels resolve:  \d+\.\d{{3}}s$', out, re.MULTILINE), out
