"""
unit-tests for the live progress bars (flipjump/utils/progress.py) and their hooks in the assembler and
the loader.

the contract: off a terminal the output is exactly PrintTimer's (`  stage:  1.234s`), on a terminal the
line animates and ends the same way, the reported progress only moves forward inside [0, 1], and turning
the bars on changes nothing in what is assembled.
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

    encoding = 'utf-8'

    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def progress_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ('FLIPJUMP_PROGRESS', 'TERM', 'FLIPJUMP_NO_NATIVE'):
        monkeypatch.delenv(var, raising=False)
    # no color, on every platform: the screen tests count characters, and escape codes have no width.
    # (on POSIX a fake terminal gets color, on Windows it can't - test_color_* cover the color itself)
    monkeypatch.setenv('NO_COLOR', '1')


def test_off_a_terminal_it_prints_what_print_timer_printed() -> None:
    stream = io.StringIO()
    with ProgressBar('  stage:  ', stream=stream, total=10) as bar:
        assert not bar.active
        bar.update(5, stage='half')
    text = stream.getvalue()
    assert text.startswith('  stage:  ') and text.endswith('s\n')
    assert '\r' not in text and '%' not in text
    float(text[len('  stage:  ') : -2])  # noqa: E203 - just the seconds


def test_off_a_terminal_a_stage_that_raises_prints_what_print_timer_printed() -> None:
    stream = io.StringIO()
    with pytest.raises(ValueError):
        with ProgressBar('  stage:  ', stream=stream, total=10):
            raise ValueError('the stage failed')
    assert re.fullmatch(r'  stage:  \d+\.\d{3}s\n', stream.getvalue()), stream.getvalue()


def test_on_a_terminal_it_draws_a_bar_and_ends_with_the_time() -> None:
    stream = _Terminal()
    with ProgressBar('  stage:  ', stream=stream, total=4) as bar:
        assert bar.active
        bar.update(1, stage='first')
        bar.update(3, stage='second')  # a new stage always redraws
    text = stream.getvalue()
    frames = text.split('\r')
    assert any(' 75%' in frame and 'second' in frame for frame in frames)
    last = frames[-1]
    assert last.startswith('  stage:  ') and last.rstrip().endswith('s') and '%' not in last


def test_a_bar_without_a_total_spins() -> None:
    stream = _Terminal()
    with ProgressBar('  stage:  ', stream=stream) as bar:
        bar.update(0, stage='working')
    assert '%' not in stream.getvalue() and 'working' in stream.getvalue()


def test_the_bars_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FLIPJUMP_PROGRESS', '0')
    assert not ProgressBar('x', stream=_Terminal()).active


def test_print_time_false_prints_nothing() -> None:
    stream = _Terminal()
    with ProgressBar('  stage:  ', print_time=False, stream=stream, total=1) as bar:
        assert not bar.active
        bar.update(1)
    assert stream.getvalue() == ''


def test_without_unicode_it_draws_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    class AsciiTerminal(_Terminal):
        encoding = 'ascii'

    stream = AsciiTerminal()
    with ProgressBar('  stage:  ', stream=stream, total=2) as bar:
        bar.update(1, stage='half')
    stream.getvalue().encode('ascii')  # nothing that an ascii console can't print
    assert '#' in stream.getvalue()


class _Recorder:
    """a ProgressBar stand-in that records every progress update the hooks report"""

    def __init__(self) -> None:
        self.values: List[float] = []

    def __call__(self, value: float) -> None:
        self.values.append(value)


def _assert_moves_forward_within_0_1(values: List[float]) -> None:
    assert values, 'no progress was reported'
    assert all(0.0 <= value <= 1.0 for value in values)
    # forward, up to float rounding of the nested slices
    assert all(later >= earlier - 1e-9 for earlier, later in zip(values, values[1:]))


def test_the_assembler_phases_report_forward_progress_and_assemble_the_same_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # every phase's bar on, through a fake terminal; the output must be byte-identical to a quiet assembly
    quiet = tmp_path / 'quiet.fjm'
    flipjump_quickstart.assemble([CAT_PROGRAM], quiet, print_time=False)

    updates: List[List[float]] = []
    real_update = ProgressBar.update

    def recording_update(self: ProgressBar, done: float, **kwargs: Any) -> None:
        if getattr(self, '_recorded', None) is None:
            self._recorded = []  # type: ignore[attr-defined]
            updates.append(self._recorded)  # type: ignore[attr-defined]
        self._recorded.append(done)  # type: ignore[attr-defined]
        real_update(self, done, **kwargs)

    monkeypatch.setattr(progress_module, 'progress_enabled', lambda stream=None: True)
    monkeypatch.setattr(ProgressBar, 'update', recording_update)
    monkeypatch.setattr('sys.stdout', _Terminal())
    loud = tmp_path / 'loud.fjm'
    flipjump_quickstart.assemble([CAT_PROGRAM], loud, print_time=True, fjm_version=FJMVersion.CompressedVersion)
    quiet_compressed = tmp_path / 'quiet3.fjm'
    flipjump_quickstart.assemble(
        [CAT_PROGRAM], quiet_compressed, print_time=False, fjm_version=FJMVersion.CompressedVersion
    )

    assert loud.read_bytes() == quiet_compressed.read_bytes()
    assert len(updates) >= 4  # parsing, macro resolve, labels resolve, create binary
    for values in updates:
        _assert_moves_forward_within_0_1(values)


@native_engine_required
def test_the_loading_bar_moves_forward(tmp_path: Path) -> None:
    fjm_path = tmp_path / 'cat.fjm'
    flipjump_quickstart.assemble([CAT_PROGRAM], fjm_path, print_time=False, fjm_version=FJMVersion.CompressedVersion)
    recorder = _Recorder()
    stream = _Terminal()
    with ProgressBar('  loading memory:  ', stream=stream, total=1.0) as bar:
        real_update = bar.update

        def update(done: float, **kwargs: Any) -> None:
            recorder(done)
            real_update(done, **kwargs)

        bar.update = update  # type: ignore[method-assign]
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


@pytest.mark.parametrize('stage', ['', 'compressing'])
@pytest.mark.parametrize('columns', [200, 100, 60, 40, 24])
def test_the_finished_line_leaves_nothing_of_the_bar_on_screen(
    monkeypatch: pytest.MonkeyPatch, stage: str, columns: int
) -> None:
    monkeypatch.setattr(progress_module, '_terminal_columns', lambda stream: columns)
    stream = _Terminal()
    with ProgressBar('  labels resolve:  ', stream=stream, total=1.0) as bar:
        bar._start -= 75  # long enough to show the time left
        bar.update(0.6, stage=stage)
    frames = stream.getvalue().split('\r')
    assert all(len(frame) < columns for frame in frames[:-1])  # a drawn bar never wraps (the last line is PrintTimer's)
    shown = _screen_row(stream.getvalue())
    assert shown.rstrip() == frames[-1].rstrip() and shown.rstrip().endswith('s')


def test_clear_progress_line_erases_the_bar_before_other_output() -> None:
    stream = _Terminal()
    with ProgressBar('  parsing:  ', stream=stream, total=1.0) as bar:
        bar.update(0.5, stage='half')
        progress_module.clear_progress_line()
        assert _screen_row(stream.getvalue()).strip() == ''


_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')


def test_color_is_drawn_and_has_no_width(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('NO_COLOR')
    monkeypatch.setattr(progress_module, '_enable_windows_vt', lambda stream: True)
    monkeypatch.setattr(progress_module, '_terminal_columns', lambda stream: 70)
    stream = _Terminal()
    with ProgressBar('  stage:  ', stream=stream, total=1.0) as bar:
        bar.update(0.5, stage='half')
    frames = stream.getvalue().split('\r')
    assert any(_ESCAPE.search(frame) for frame in frames)
    assert all(len(_ESCAPE.sub('', frame)) < 70 for frame in frames[:-1])
    assert _screen_row(_ESCAPE.sub('', stream.getvalue())).rstrip() == frames[-1].rstrip()


@pytest.mark.parametrize('no_color, colored', [('1', False), ('', True)])
def test_no_color_turns_the_color_off_only_when_set(
    monkeypatch: pytest.MonkeyPatch, no_color: str, colored: bool
) -> None:
    monkeypatch.setenv('NO_COLOR', no_color)
    monkeypatch.setattr(progress_module, '_enable_windows_vt', lambda stream: True)
    stream = _Terminal()
    with ProgressBar('  stage:  ', stream=stream, total=1.0) as bar:
        bar.update(0.5)
    assert bool(_ESCAPE.search(stream.getvalue())) == colored
