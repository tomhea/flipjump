"""
the progress bar of a long stage (assembling, loading a program).

`ProgressBar` is a drop-in for `PrintTimer`: it prints the same `  stage:  1.234s` line when the stage ends. Only on
an interactive terminal it also shows the stage's progress while it runs, on that one line:

      loading memory:  ##############----------------  48%  0:03 left
"""

import sys
from time import monotonic, time
from typing import Any, Optional

_REDRAW_SECONDS = 0.1
_BAR_WIDTH = 30

# the bar on screen, if any (see clear_progress_line)
_shown_bar: Optional['ProgressBar'] = None


def clear_progress_line() -> None:
    """erase the progress bar on screen (if one is), so that other output starts on a clean line"""
    if _shown_bar is not None:
        _shown_bar.clear()


class ProgressBar:
    """
    the progress line of one stage. usage:
    with ProgressBar('  loading memory:  ', print_time=True) as bar:
        ... bar.update(fraction_done) ...
    `active` says whether the bar is drawn (print_time, on a terminal): a caller may skip reporting when it is not.
    """

    def __init__(self, init_message: str, *, print_time: bool = True):
        self.init_message = init_message
        self.print_time = print_time
        self.active = print_time and sys.stdout.isatty()
        self._start_time = 0.0
        self._last_draw = 0.0
        self._drawn_width = 0

    def __enter__(self) -> 'ProgressBar':
        global _shown_bar
        self._start_time = time()
        if self.print_time:
            print(self.init_message, end='', flush=True)
        if self.active:
            _shown_bar = self
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        global _shown_bar
        if not self.print_time:
            return
        elapsed = f'{time() - self._start_time:.3f}s'
        if self.active:
            _shown_bar = None
            elapsed = '\r' + (self.init_message + elapsed).ljust(self._drawn_width)
        print(elapsed, flush=True)

    def update(self, fraction: float) -> None:
        """report the stage's progress, a fraction in [0, 1] (drawn at most ~10 times a second)"""
        now = monotonic()
        if not self.active or now - self._last_draw < _REDRAW_SECONDS:
            return
        self._last_draw = now
        fraction = min(max(fraction, 0.0), 1.0)
        full = int(fraction * _BAR_WIDTH)
        line = f'{self.init_message}{"#" * full}{"-" * (_BAR_WIDTH - full)} {int(fraction * 100):3d}%'
        elapsed = time() - self._start_time
        if fraction >= 0.02 and elapsed >= 1:
            left = int(elapsed * (1 - fraction) / fraction)
            line += f'  {left // 60}:{left % 60:02d} left'
        sys.stdout.write('\r' + line.ljust(self._drawn_width))
        sys.stdout.flush()
        self._drawn_width = len(line)

    def clear(self) -> None:
        """erase the bar's line (it is drawn again at the next update)"""
        if self._drawn_width:
            sys.stdout.write('\r' + ' ' * self._drawn_width + '\r')
            sys.stdout.flush()
            self._drawn_width = 0
