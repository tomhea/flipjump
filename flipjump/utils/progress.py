"""
A progress bar for a long stage. It prints a `  stage:  1.234s` line at the end, and on a terminal it also draws
`  loading memory:  ##############----------------  48%  0:03 left` while it runs.

The bar is drawn with a carriage return, spaces and ASCII only (no ANSI escape codes, no cursor moves), which every
common terminal handles alike: Windows' cmd, PowerShell and Windows Terminal, macOS' Terminal and iTerm, the Linux
terminals, and the IDEs' consoles. It is drawn only when the output is a terminal, and only when the line fits its
width (a wrapped line could not be redrawn in place).
"""

import shutil
import sys
from time import monotonic, time
from typing import Any, Callable, Optional

_REDRAW_SECONDS = 0.1
_BAR_WIDTH = 30
_MIN_BAR_WIDTH = 10
# what follows the bar at its longest, ` 100%  999:59 left`, and one free column (some terminals wrap at the last one)
_AFTER_BAR_WIDTH = len(' 100%  999:59 left') + 1

_shown_stage: Optional['ProgressBar'] = None  # the stage whose line is on screen now


def safe_print(*values: Any, **print_options: Any) -> None:
    """print(), also while a stage's line is on screen: the line is freed first, and the stage is shown again
    after."""
    stage = _shown_stage
    if stage is not None:
        stage.free_the_line()
    print(*values, **print_options)
    if stage is not None:
        stage.show_again()


class ProgressBar:
    """
    A progress bar for a long stage: `with ProgressBar('  stage:  ') as bar: bar.update(fraction)`.
    It is drawn only on a terminal (`active`).
    """

    def __init__(self, init_message: str, *, show_progress: bool = True):
        self.init_message = init_message
        self.show_progress = show_progress
        # the bar is as wide as the terminal has room for, measured once; with no room for one, only the time is shown
        self._bar_width = min(_BAR_WIDTH, shutil.get_terminal_size().columns - len(init_message) - _AFTER_BAR_WIDTH)
        self.active = show_progress and sys.stdout.isatty() and self._bar_width >= _MIN_BAR_WIDTH
        self._start_time = 0.0
        self._last_draw = 0.0
        self._drawn_width = 0  # how much of the line is drawn (on a terminal)
        self._fraction = 0.0  # the last progress reported

    def __enter__(self) -> 'ProgressBar':
        global _shown_stage
        self._start_time = time()
        if self.show_progress:
            print(self.init_message, end='', flush=True)
            self._drawn_width = len(self.init_message)
            _shown_stage = self
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        global _shown_stage
        if not self.show_progress:
            return
        _shown_stage = None
        elapsed = f'{time() - self._start_time:.3f}s'
        if self.active:
            elapsed = '\r' + (self.init_message + elapsed).ljust(self._drawn_width)
        print(elapsed, flush=True)

    def get_active_update_function(self) -> Optional[Callable[[float], None]]:
        """Return `update`, or None if the bar is not drawn - so a stage can skip its progress bookkeeping."""
        return self.update if self.active else None

    def update(self, fraction: float) -> None:
        """report the stage's progress, a fraction in [0, 1] (drawn at most ~10 times a second)"""
        self._fraction = min(max(fraction, 0.0), 1.0)
        now = monotonic()
        if self.active and now - self._last_draw >= _REDRAW_SECONDS:
            self._last_draw = now
            self.redraw()

    def redraw(self) -> None:
        """draw the bar now, at the last progress reported"""
        if not self.active:
            return
        fraction = self._fraction
        full = int(fraction * self._bar_width)
        line = f'{self.init_message}{"#" * full}{"-" * (self._bar_width - full)} {int(fraction * 100):3d}%'
        elapsed = time() - self._start_time
        if fraction >= 0.02 and elapsed >= 1:
            left = min(int(elapsed * (1 - fraction) / fraction), 999 * 60 + 59)
            line += f'  {left // 60}:{left % 60:02d} left'
        sys.stdout.write('\r' + line.ljust(self._drawn_width))
        sys.stdout.flush()
        self._drawn_width = len(line)

    def clear(self) -> None:
        """On a terminal, erase the stage's line (it is drawn again at the next update)."""
        if self.active and self._drawn_width:
            sys.stdout.write('\r' + ' ' * self._drawn_width + '\r')
            sys.stdout.flush()
            self._drawn_width = 0

    def free_the_line(self) -> None:
        """Make the stage's line free for other output: erase it on a terminal; elsewhere, where nothing can be
        erased, end it."""
        if self.active:
            self.clear()
        else:
            print(flush=True)

    def show_again(self) -> None:
        """Show the stage again after other output: the bar on a terminal, the stage's name elsewhere."""
        if self.active:
            self.redraw()
        else:
            print(self.init_message, end='', flush=True)
