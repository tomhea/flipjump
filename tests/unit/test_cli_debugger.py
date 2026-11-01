"""
unit-tests for the CLI debugger: the terminal command prompt (show_message / ask_for_command)
and an end-to-end breakpoint session (break -> read memory -> step -> continue, and the
exit/EOF -> keyboard-interrupt path) driven through scripted stdin lines.
"""

from pathlib import Path
from typing import Iterator, List

import pytest

from flipjump.interpreter import fjm_run
from flipjump.interpreter.debugging import user_queries
from flipjump.interpreter.debugging.breakpoints import get_breakpoint_handler
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.utils.classes import TerminationCause
from tests.unit.unit_utils import assemble_to_path

LOOP_PROGRAM = """
stl.startup
flipper+dbit;
my_loop_label:
;my_loop_label

flipper: hex.hex 0
"""


def feed_answers(monkeypatch: pytest.MonkeyPatch, answers: List[str]) -> None:
    answers_iterator: Iterator[str] = iter(answers)

    def fake_input(prompt: str = '') -> str:
        try:
            return next(answers_iterator)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr('builtins.input', fake_input)


class TestPromptFunctions:
    def test_show_message_prints_body_and_title(self, capsys: pytest.CaptureFixture[str]) -> None:
        user_queries.show_message('the body', 'The Title')
        captured = capsys.readouterr().out
        assert 'The Title' in captured
        assert 'the body' in captured

    def test_command_is_read_and_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        feed_answers(monkeypatch, ['  skip 0x10  '])
        assert user_queries.ask_for_command('> ') == 'skip 0x10'

    def test_command_returns_none_on_eof(self, monkeypatch: pytest.MonkeyPatch) -> None:
        feed_answers(monkeypatch, [])
        assert user_queries.ask_for_command('> ') is None

    def test_empty_command_is_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        feed_answers(monkeypatch, [''])
        assert user_queries.ask_for_command('> ') == ''


class TestDebuggerEndToEnd:
    def run_with_breakpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: List[str]
    ) -> 'tuple[fjm_run.TerminationStatistics, object]':
        fjm_path = assemble_to_path(LOOP_PROGRAM, tmp_path, use_stl=True, with_debug=True)
        breakpoint_handler = get_breakpoint_handler(
            tmp_path / 'debug.fjd', None, None, breakpoint_contains_labels={'my_loop_label'}
        )
        assert breakpoint_handler.breakpoints, 'breakpoint was not resolved from the debug file'
        feed_answers(monkeypatch, answers)
        statistics = fjm_run.run(
            fjm_path, io_device=FixedIO(b''), show_progress=False, breakpoint_handler=breakpoint_handler
        )
        return statistics, breakpoint_handler

    def test_break_step_continue_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        statistics, _ = self.run_with_breakpoint(tmp_path, monkeypatch, ['s', 'ca'])
        assert statistics.termination_cause == TerminationCause.Looping
        captured = capsys.readouterr().out
        assert 'program break' in captured
        assert 'step' in captured

    def test_break_read_memory_then_continue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # read takes its target on the same line now
        statistics, _ = self.run_with_breakpoint(tmp_path, monkeypatch, ['read 0', 'continue all'])
        assert statistics.termination_cause == TerminationCause.Looping
        assert 'memory[0x0]' in capsys.readouterr().out

    def test_help_command_lists_the_commands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        statistics, _ = self.run_with_breakpoint(tmp_path, monkeypatch, ['h', 'c*'])
        assert statistics.termination_cause == TerminationCause.Looping
        captured = capsys.readouterr().out
        assert 'continue all' in captured and 'read' in captured

    def test_skip_hex_count(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 'skip 0x2' arms next_break at op_counter + 2; the program loops to termination after
        statistics, _ = self.run_with_breakpoint(tmp_path, monkeypatch, ['skip 0x2', 'ca'])
        assert statistics.termination_cause == TerminationCause.Looping

    def test_unknown_command_reprompts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        statistics, _ = self.run_with_breakpoint(tmp_path, monkeypatch, ['nonsense', 'continue'])
        assert statistics.termination_cause == TerminationCause.Looping
        assert 'unknown command' in capsys.readouterr().out

    def test_break_on_eof_is_a_keyboard_interrupt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # no scripted answers: the prompt hits EOF -> exit -> keyboard-interrupt termination
        statistics, _ = self.run_with_breakpoint(tmp_path, monkeypatch, [])
        assert statistics.termination_cause == TerminationCause.KeyboardInterrupt

    def test_explicit_exit_is_a_keyboard_interrupt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        statistics, _ = self.run_with_breakpoint(tmp_path, monkeypatch, ['exit'])
        assert statistics.termination_cause == TerminationCause.KeyboardInterrupt
