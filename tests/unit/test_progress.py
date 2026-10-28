"""
unit-tests for the progress bars (flipjump/utils/progress.py) and their hooks in the assembler and the loader.

the contract: off a terminal the output is exactly PrintTimer's (`  stage:  1.234s`), on a terminal the line shows
the progress and ends the same way, the reported progress only moves forward inside [0, 1], and turning the bars on
changes nothing in what is assembled.
"""

import io
import re
from pathlib import Path
from typing import Any, List

import pytest

from flipjump import flipjump_quickstart
from flipjump.fjm.fjm_consts import FJMVersion
from flipjump.fjm.fjm_reader import read_fjm_header
from flipjump.interpreter import fjm_run
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
    return stream


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


def test_print_time_false_prints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = _terminal(monkeypatch)
    with ProgressBar('  stage:  ', print_time=False) as bar:
        assert not bar.active
        bar.update(1)
    assert terminal.getvalue() == ''


def test_clear_progress_line_erases_the_bar_before_other_output(monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = _terminal(monkeypatch)
    with ProgressBar('  parsing:  ') as bar:
        bar.update(0.5)
        progress_module.clear_progress_line()
        assert _screen_row(terminal.getvalue()).strip() == ''
    assert progress_module._shown_bar is None


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
    real_update = ProgressBar.update

    def recording_update(self: ProgressBar, done: float) -> None:
        if getattr(self, '_recorded', None) is None:
            self._recorded = []  # type: ignore[attr-defined]
            updates.append(self._recorded)  # type: ignore[attr-defined]
        self._recorded.append(done)  # type: ignore[attr-defined]
        real_update(self, done)

    monkeypatch.setattr(ProgressBar, 'update', recording_update)
    loud = tmp_path / 'loud.fjm'
    flipjump_quickstart.assemble([CAT_PROGRAM], loud, print_time=True, fjm_version=FJMVersion.CompressedVersion)
    quiet = tmp_path / 'quiet.fjm'
    flipjump_quickstart.assemble([CAT_PROGRAM], quiet, print_time=False, fjm_version=FJMVersion.CompressedVersion)
    assert loud.read_bytes() == quiet.read_bytes()
    assert len(updates) == 4  # parsing, macro resolve, labels resolve, create binary
    for values in updates:
        _assert_moves_forward_within_0_1(values)


@native_engine_required
def test_the_loading_bar_moves_forward(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _terminal(monkeypatch)
    fjm_path = tmp_path / 'cat.fjm'
    flipjump_quickstart.assemble([CAT_PROGRAM], fjm_path, print_time=False, fjm_version=FJMVersion.CompressedVersion)
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
    flipjump_quickstart.assemble([CAT_PROGRAM], tmp_path / 'cat.fjm', print_time=False)
    _assert_moves_forward_within_0_1(recorder.values)
    assert len(recorder.values) > 10 and recorder.values[0] < 0.5 and recorder.values[-1] == 1.0
