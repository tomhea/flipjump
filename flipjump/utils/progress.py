"""
the live progress bar of a long stage (assembling, loading a program).

`ProgressBar` is a drop-in for `PrintTimer`: it prints the same `  stage:  1.234s` line when the
stage ends, so a log, a pipe or a test sees exactly what it always saw. Only on an interactive
terminal does the line come alive while the stage runs:

      loading memory:  ███████████████▋░░░░░░░░░░░░░░  52%  0:01 · 0:01 left  decompressing

The bar is drawn on one line (carriage return, no scrolling), fitted to the terminal's width, and
redrawn at most ~12 times a second, so a caller may report progress from a hot loop as long as it
does so every few thousand items (`active` tells it whether to bother at all). Anything else that
prints while a bar is up should call `clear_progress_line()` first. `FLIPJUMP_PROGRESS=0` turns the
bars off; a non-empty `NO_COLOR` keeps them but drops the color.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
from time import monotonic, time
from typing import Any, Dict, Optional, TextIO, Tuple

_REDRAW_SECONDS = 0.08
_BAR_WIDTH = 30  # cells, at most; a narrow terminal gets a shorter bar
_MIN_BAR_WIDTH = 8
_PARTIALS = ' ▏▎▍▌▋▊▉'  # eighths of a cell, for a smooth leading edge
_FULL, _EMPTY = '█', '░'
_ASCII_FULL, _ASCII_EMPTY = '#', '-'
_SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
_ASCII_SPINNER = '|/-\\'

_COLOR_BAR = '\x1b[36m'  # cyan
_COLOR_DIM = '\x1b[2m'
_COLOR_RESET = '\x1b[0m'

# the bar currently on screen, if any (see clear_progress_line)
_shown_bar: Optional['ProgressBar'] = None
# the console modes changed to show color: {handle: original mode}. Restored when the bar ends (and at
# exit, in case it never does), so the program's own output is never interpreted as escapes.
_changed_console_modes: Dict[int, int] = {}
_atexit_registered = False


def _restore_console_modes() -> None:
    try:
        import ctypes

        kernel32 = getattr(ctypes, 'windll').kernel32
        for handle, mode in _changed_console_modes.items():
            kernel32.SetConsoleMode(handle, mode)
    except Exception:  # noqa: BLE001 - best effort
        return
    finally:
        _changed_console_modes.clear()


def _enable_windows_vt(stream: TextIO) -> bool:
    """turn on ANSI escape processing in a Windows console (off by default in the classic one); see
    _changed_console_modes. @return: whether escapes will render"""
    global _atexit_registered
    if os.name != 'nt':
        return True
    try:
        import ctypes
        import msvcrt

        # getattr: these exist only on Windows, which a type-checker on another platform does not know
        kernel32 = getattr(ctypes, 'windll').kernel32
        handle = getattr(msvcrt, 'get_osfhandle')(stream.fileno())
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_virtual_terminal_processing = 0x0004
        if mode.value & enable_virtual_terminal_processing:
            return True
        if not kernel32.SetConsoleMode(handle, mode.value | enable_virtual_terminal_processing):
            return False
        if not _atexit_registered:
            _atexit_registered = True
            atexit.register(_restore_console_modes)
        _changed_console_modes.setdefault(handle, mode.value)
        return True
    except Exception:  # noqa: BLE001 - any failure just means no color
        return False


def _can_encode(stream: TextIO, text: str) -> bool:
    try:
        text.encode(stream.encoding or 'ascii')
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _terminal_columns(stream: TextIO) -> int:
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, ValueError, OSError):
        return shutil.get_terminal_size((100, 24)).columns


def _format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f'{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'
    return f'{seconds // 60}:{seconds % 60:02d}'


def progress_enabled(stream: Optional[TextIO] = None) -> bool:
    """would a progress bar be drawn on this stream? (an interactive terminal, not turned off)"""
    stream = stream if stream is not None else sys.stdout
    if os.environ.get('FLIPJUMP_PROGRESS', '1') == '0' or os.environ.get('TERM') == 'dumb':
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def clear_progress_line() -> None:
    """erase the progress bar on screen (if one is), so that other output starts on a clean line; the
    bar is drawn again below it at its next update"""
    bar = _shown_bar
    if bar is not None:
        bar.clear()


class ProgressBar:
    """
    the progress line of one stage. usage:

        with ProgressBar('  loading memory:  ', print_time=True, total=n) as bar:
            for i in ...:
                ...
                if bar.active and i % 4096 == 0:
                    bar.update(i, stage='decompressing')

    `update(done)` takes ABSOLUTE progress in units of `total` (set it later with `set_total` if it
    is only known once the stage has started). Without a total the bar is a spinner. When the stage
    ends, the line is replaced by the stage's time, exactly as PrintTimer prints it.
    """

    def __init__(
        self,
        init_message: str,
        *,
        print_time: bool = True,
        total: Optional[float] = None,
        stream: Optional[TextIO] = None,
    ) -> None:
        self.init_message = init_message
        self.print_time = print_time
        self.total = total
        self.stream = stream if stream is not None else sys.stdout
        self.active = print_time and progress_enabled(self.stream)
        self.done = 0.0
        self.stage = ''
        self._start_wall = 0.0
        self._start = 0.0
        self._last_draw = 0.0
        self._drawn_width = 0
        self._spin = 0
        self._unicode = self.active and _can_encode(self.stream, _FULL + _EMPTY + _PARTIALS + _SPINNER + '·')
        self._color = self.active and not os.environ.get('NO_COLOR') and _enable_windows_vt(self.stream)

    def __enter__(self) -> 'ProgressBar':
        global _shown_bar
        self._start_wall = time()
        self._start = monotonic()
        if self.print_time:
            if self.active:
                _shown_bar = self
                self._draw(force=True)
            else:
                print(self.init_message, end='', flush=True, file=self.stream)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        global _shown_bar
        if not self.print_time:
            return
        elapsed = f'{time() - self._start_wall:.3f}s'
        if self.active:
            if _shown_bar is self:
                _shown_bar = None
            line = self.init_message + elapsed
            padding = ' ' * max(0, self._drawn_width - len(line))
            print('\r' + line + padding, file=self.stream, flush=True)
            if self._color:
                _restore_console_modes()
        else:
            print(elapsed, file=self.stream, flush=True)

    def set_total(self, total: Optional[float]) -> None:
        """change the total (None: a spinner, for a part whose size is unknown)"""
        self.total = total
        self._draw(force=True)

    def update(self, done: float, *, stage: Optional[str] = None) -> None:
        """report the absolute progress (in units of `total`), and optionally the current sub-stage"""
        self.done = done
        if stage is not None and stage != self.stage:
            self.stage = stage
            self._draw(force=True)
            return
        self._draw()

    def clear(self) -> None:
        """erase the bar's line (it is drawn again at the next update)"""
        if self.active and self._drawn_width:
            self.stream.write('\r' + ' ' * self._drawn_width + '\r')
            self.stream.flush()
            self._drawn_width = 0

    def _bar(self, fraction: float, width: int) -> str:
        if self._unicode:
            eighths = int(fraction * width * 8)
            full, partial = divmod(eighths, 8)
            filled = _FULL * full + (_PARTIALS[partial] if full < width and partial else '')
            empty = _EMPTY * (width - len(filled))
        else:
            full = int(fraction * width)
            filled, empty = _ASCII_FULL * full, _ASCII_EMPTY * (width - full)
        if self._color:
            return f'{_COLOR_BAR}{filled}{_COLOR_DIM}{empty}{_COLOR_RESET}'
        return filled + empty

    def _line(self, elapsed: float, columns: int) -> Tuple[str, int]:
        """the bar's part of the line, and its visible width: the whole line stays under `columns`, so
        it never wraps (a wrapped line would leave a stale copy at every redraw)"""
        room = columns - 1 - len(self.init_message)
        dot = '·' if self._unicode else '-'
        if self.total:
            fraction = min(1.0, max(0.0, self.done / self.total))
            percent = f' {int(fraction * 100):3d}%'
            timing = f'  {_format_seconds(elapsed)}'
            if fraction >= 0.02 and elapsed >= 1:
                timing += f' {dot} {_format_seconds(elapsed * (1 - fraction) / fraction)} left'
            if room - len(percent) - len(timing) < _MIN_BAR_WIDTH:
                timing = ''  # a narrow terminal: drop the times before the bar
            bar_width = min(_BAR_WIDTH, room - len(percent) - len(timing))
            if bar_width < _MIN_BAR_WIDTH:
                plain = percent.strip()[: max(0, room)]
                return plain, len(plain)
            plain_body = '#' * bar_width + percent + timing
            body = self._bar(fraction, bar_width) + percent + timing
        else:
            spinner = _SPINNER if self._unicode else _ASCII_SPINNER
            self._spin = (self._spin + 1) % len(spinner)
            plain_body = body = f'{spinner[self._spin]} {_format_seconds(elapsed)}'[: max(0, room)]
        stage = f'  {self.stage}' if self.stage else ''
        stage = stage[: max(0, room - len(plain_body))]
        visible = len(plain_body) + len(stage)
        if self._color and stage:
            stage = f'{_COLOR_DIM}{stage}{_COLOR_RESET}'
        return body + stage, visible

    def _draw(self, *, force: bool = False) -> None:
        if not self.active or not self.print_time:
            return
        now = monotonic()
        if not force and now - self._last_draw < _REDRAW_SECONDS:
            return
        self._last_draw = now
        columns = _terminal_columns(self.stream)
        if columns - 1 <= len(self.init_message):
            return  # no room for a bar at all; the stage's time is still printed at the end
        body, visible = self._line(now - self._start, columns)
        width = len(self.init_message) + visible
        padding = ' ' * max(0, self._drawn_width - width)
        self._drawn_width = width
        self.stream.write('\r' + self.init_message + body + padding)
        self.stream.flush()
