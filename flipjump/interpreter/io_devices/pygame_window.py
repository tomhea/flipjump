"""
the pygame-ce window of the interactive io-devices, and the devices that use it.

PygameWindow owns the one SDL window: it queues key events (io_devices/README.md's keycodes), presents frames,
toggles fullscreen on F11, and raises KeyboardInterrupt when closed (a clean end of the run). PcIO.interactive()
puts the screen and the keyboard on one window. SDL delivers keys only to an open window: the screen opens it on
init_screen, ensure_open_for_input a small one for a keyboard alone. Drive it from the main thread (macOS/SDL).
pygame-ce only (`pip install flipjump[io]`): upstream pygame shares its module name, so `_import_pygame` checks IS_CE.
"""

import os
import time
import warnings
from collections import deque
from itertools import chain
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.interpreter.io_devices.KeyboardIO import KeyboardIO, KeyEventSource, ScriptedKeyEventSource
from flipjump.interpreter.io_devices.ScreenIO import InMemoryScreen
from flipjump.utils.exceptions import IODeviceException

# some of the keycodes delivered to the program (io_devices/README.md, "Keycodes")
KEYCODE_LEFT_CTRL, KEYCODE_LEFT_SHIFT, KEYCODE_LEFT_ALT, KEYCODE_LEFT_GUI = 0x80, 0x81, 0x82, 0x83
KEYCODE_RIGHT_CTRL, KEYCODE_RIGHT_SHIFT, KEYCODE_RIGHT_ALT, KEYCODE_RIGHT_GUI = 0x84, 0x85, 0x86, 0x87
KEYCODE_RIGHT, KEYCODE_LEFT, KEYCODE_DOWN, KEYCODE_UP = 0xD7, 0xD8, 0xD9, 0xDA

# pygame's event.key is SDL2's keycode: a key's ASCII code if it has one (never 'A'-'Z'), else its USB HID usage ID
# with bit 30 set. The program gets ASCII as is, the modifiers (HID 0xE0-0xE7) as 0x80-0x87, the rest as 0x88 + HID.
SDL_SCANCODE_KEY = 1 << 30
HID_KEYCODE_BASE = 0x88

# THE key table: SDL keycode -> the byte delivered to the program
KEYCODES: Dict[int, int] = {
    **{code: code for code in (0x08, 0x09, 0x0D, 0x1B, *range(0x20, ord('A')), *range(ord('Z') + 1, 0x7F))},
    0x7F: HID_KEYCODE_BASE + 0x4C,  # Delete: SDL gives it ASCII DEL, the program its HID code
    **{SDL_SCANCODE_KEY | usage: KEYCODE_LEFT_CTRL + usage - 0xE0 for usage in range(0xE0, 0xE8)},
    # Caps Lock, F1-F12, Print Screen .. Page Up (0x39-0x4B), End .. keypad '.' (0x4D-0x63), Menu, F13-F24
    **{
        SDL_SCANCODE_KEY | usage: HID_KEYCODE_BASE + usage
        for usage in [*range(0x39, 0x4C), *range(0x4D, 0x64), 0x65, *range(0x68, 0x74)]
    },
}

INPUT_WINDOW_SIZE = (320, 240)  # the window `ensure_open_for_input` opens for a keyboard with no screen
# how tall a program's window opens (the width follows its aspect): SCALED alone picks the largest integer scale
# that fits the desktop, near-fullscreen for a small surface. The user can still drag-resize, or F11.
WINDOW_HEIGHT = 480


def _import_pygame() -> Any:
    try:
        # pygame prints a banner to stdout (the program's output channel) on import, unless this is set first
        os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
        import pygame
    except ImportError as import_error:
        raise IODeviceException(
            'the interactive window needs the pygame-ce package, which is not installed. '
            'install it with `pip install pygame-ce`, or `pip install flipjump[io]` to pull it '
            'in as a flipjump extra.'
        ) from import_error
    if not getattr(pygame, 'IS_CE', 0):
        raise IODeviceException(
            'the interactive window requires pygame-ce, but the installed `pygame` module is '
            'upstream pygame %s. they share a module name and cannot coexist; run '
            '`pip uninstall -y pygame && pip install pygame-ce`.' % pygame.version.ver
        )
    return pygame


class PygameWindow:
    """owns the SDL window and the live key-event queue. opened on the first ensure_open*."""

    def __init__(self, *, title: str = 'FlipJump'):
        self._pygame = _import_pygame()
        self._title = title
        self._screen_surface = None
        self._icon: Any = None  # the last icon surface, re-applied when the window is re-created
        self.key_events: Deque[Tuple[bool, int]] = deque()  # (is_down, keycode)
        self.closed = False

    @property
    def is_open(self) -> bool:
        return self._screen_surface is not None

    def ensure_open(self, width: int, height: int) -> None:
        """open (or resize) the window for a width x height logical surface."""
        if self._screen_surface is not None and self._screen_surface.get_size() == (width, height):
            return  # already open at this size - recreating the window would flicker
        pg = self._pygame
        fullscreen = False
        if self._screen_surface is not None:
            # a new size: a new window. A second SCALED set_mode crashes pygame-ce 2.5 (an access violation), so
            # the display is closed first - after taking its events; the title, icon and fullscreen carry over.
            self.pump_events()
            fullscreen = bool(pg.display.is_fullscreen())
            pg.display.quit()
        elif pg.display.get_init() and pg.display.get_surface() is not None:
            pg.display.quit()  # another PygameWindow's display is still up: the same SCALED re-create crash
        pg.display.init()
        pg.display.set_caption(self._title)
        # SCALED scales the small logical surface up to the window (also fullscreen); RESIZABLE: drag-resize
        mode_flags = pg.SCALED | (pg.FULLSCREEN if fullscreen else pg.RESIZABLE)
        self._screen_surface = pg.display.set_mode((width, height), mode_flags)
        if self._icon is not None:
            self._apply_icon(self._icon)
        if not fullscreen:  # the width follows the program's aspect: scaled, never stretched
            self._resize_window(max(1, round(WINDOW_HEIGHT * width / height)), WINDOW_HEIGHT)

    def _os_window(self) -> Any:
        """the OS window of the display surface: `from_display_module` is pygame-ce's only handle to it (its
        deprecation, in favour of building on `pygame.Window`, is silenced)."""
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            return self._pygame.window.Window.from_display_module()

    def _resize_window(self, width: int, height: int) -> None:
        """size the OS window (SCALED keeps upscaling the surface); a video driver that refuses keeps its size."""
        try:
            self._os_window().size = (width, height)
        except self._pygame.error:
            pass

    def set_title(self, title: str) -> None:
        """set the caption, now and whenever `ensure_open` (re)creates the window"""
        self._title = title
        if self._screen_surface is not None:
            self._pygame.display.set_caption(title)

    def set_icon(self, width: int, height: int, colours: Sequence[Tuple[int, int, int]]) -> None:
        """set the window icon from row-major RGB colours (opaque: the icon has no transparency)."""
        pg = self._pygame
        pg.display.init()
        self._icon = pg.image.frombuffer(bytes(chain.from_iterable(colours)), (width, height), 'RGB').copy()
        self._apply_icon(self._icon)

    def _apply_icon(self, surface: Any) -> None:
        # through the live window: `display.set_icon` only works before `set_mode`, and the icon comes later
        try:
            self._os_window().set_icon(surface)
        except self._pygame.error:
            self._pygame.display.set_icon(surface)

    def ensure_open_for_input(self) -> None:
        """open a small window (if none is open) so SDL delivers key events to a keyboard with no screen."""
        if not self.is_open:
            self.ensure_open(*INPUT_WINDOW_SIZE)

    def pump_events(self) -> None:
        """take the pending window events: queue key transitions, F11 toggles fullscreen, a close raises
        KeyboardInterrupt (a clean end of the run)."""
        if self.closed or self._screen_surface is None:
            return
        pg = self._pygame
        for event in pg.event.get():
            if event.type == pg.QUIT:
                self.closed = True
                raise KeyboardInterrupt
            if event.type not in (pg.KEYDOWN, pg.KEYUP):
                continue
            if event.key == pg.K_F11:
                if event.type == pg.KEYDOWN:
                    try:
                        pg.display.toggle_fullscreen()
                    except pg.error:
                        pass  # some video drivers (e.g. the headless dummy) can't toggle
            elif event.key in KEYCODES:
                self.key_events.append((event.type == pg.KEYDOWN, KEYCODES[event.key]))

    def show_black(self) -> None:
        """fill the window black and show it (init_screen's black screen)."""
        assert self._screen_surface is not None
        self._screen_surface.fill((0, 0, 0))
        self._pygame.display.flip()

    def draw(self, width: int, height: int, rgb_pixels: List[Tuple[int, int, int]]) -> None:
        """blit a full frame given as row-major RGB tuples, and present it (`draw_indexed` is ~30x faster)."""
        if self._screen_surface is not None:
            self._blit(self._pygame.image.frombuffer(bytes(chain.from_iterable(rgb_pixels)), (width, height), 'RGB'))

    def draw_indexed(self, width: int, height: int, indices: bytes, palette: List[Tuple[int, int, int]]) -> None:
        """blit a frame given as palette indices, and present it: SDL expands the palette, not python."""
        if self._screen_surface is not None:
            frame_surface = self._pygame.image.frombuffer(indices, (width, height), 'P')
            frame_surface.set_palette(palette)
            self._blit(frame_surface)

    def _blit(self, frame_surface: Any) -> None:
        assert self._screen_surface is not None
        self._screen_surface.blit(frame_surface, (0, 0))
        self._pygame.display.flip()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._pygame.display.quit()


class WindowKeyEventSource(KeyEventSource):
    """live key events read from a PygameWindow (pumps the window before every poll)."""

    def __init__(self, window: PygameWindow):
        self._window = window

    def next_due_event(self, tic: int) -> Optional[Tuple[bool, int]]:
        self._window.pump_events()
        return self._window.key_events.popleft() if self._window.key_events else None


class InteractiveScreen(InMemoryScreen):
    """the InMemoryScreen command stream, presented into a PygameWindow (its own unless given a shared one).
    it takes no input: a live keyboard reads the same window through a WindowKeyEventSource."""

    def __init__(self, *, frames_dir: Optional[Path] = None, window: Optional[PygameWindow] = None):
        super().__init__(frames_dir=frames_dir)
        self.window = window if window is not None else PygameWindow()
        # perf_counter() at the previous present, for the max-fps cap; -inf: the first present never waits (the
        # clock's starting point is undefined, so 0.0 could hold the first frame)
        self._last_flush = float('-inf')

    def _init_screen(self, width: int, height: int, bpp: int) -> None:
        super()._init_screen(width, height, bpp)
        self.window.ensure_open(width, height)
        self.window.show_black()

    def _set_window_title(self, title: str) -> None:
        super()._set_window_title(title)
        self.window.set_title(title)

    def _set_window_icon(self, width: int, height: int, colours: List[Tuple[int, int, int]]) -> None:
        super()._set_window_icon(width, height, colours)
        self.window.set_icon(width, height, colours)

    def _present(self) -> None:
        # the set_max_fps cap first, so the frame is recorded (its frame_hashes time) when it is shown; the window
        # is pumped meanwhile (keys still queue, and closing it still ends the run)
        if self.max_fps:
            due = self._last_flush + 1.0 / self.max_fps
            while (remaining := due - time.perf_counter()) > 0:
                time.sleep(min(remaining, 0.005))
                self.window.pump_events()
        self._last_flush = time.perf_counter()
        super()._present()
        if self.bpp <= 8:  # SDL expands the indices (`last_frame_rgb` is lazy, so no per-pixel RGB list is built)
            self.window.draw_indexed(self.width, self.height, bytes(self.pixel_indices), self.palette)
        else:  # RGB, or more than 256 colours: SDL's 8-bit surfaces cannot hold the palette
            self.window.draw(self.width, self.height, self.last_frame_rgb)
        self.window.pump_events()


class PcIO(IODevice):
    """the 'pc' io-device: read_bit from the keyboard, write_bit to the screen. interactive() puts both on one
    real window; headless() (or __init__) builds it from explicit components, e.g. scripted keys + PNG frames."""

    def __init__(self, screen: InMemoryScreen, keyboard: KeyboardIO):
        self._screen = screen
        self._keyboard = keyboard

    @property
    def outputs_live(self) -> bool:
        return True  # shown live as frames (or PNGs): the termination summary must not dump the command bytes

    @classmethod
    def interactive(cls) -> 'PcIO':
        """a real window: live keys in, a scaled screen out. The screen opens it on init_screen (live keys from
        then on: a pc program initializes its screen up front)."""
        window = PygameWindow()
        return cls(InteractiveScreen(window=window), KeyboardIO(WindowKeyEventSource(window)))

    @classmethod
    def headless(cls, events_file: Path, frames_dir: Path) -> 'PcIO':
        """no window: a scripted keyboard in, PNG frames out (deterministic replays / CI)."""
        return cls(InMemoryScreen(frames_dir=frames_dir), KeyboardIO(ScriptedKeyEventSource.from_file(events_file)))

    def end_of_run(self) -> None:
        self._screen.end_of_run()  # the screen's output must not end inside a command
        self._keyboard.end_of_run()

    def close(self) -> None:
        window = getattr(self._screen, 'window', None)
        if window is not None:
            window.close()

    def read_bit(self) -> bool:
        return self._keyboard.read_bit()

    def write_bit(self, bit: bool) -> None:
        self._screen.write_bit(bit)

    def write_byte(self, byte: int) -> None:
        self._screen.write_byte(byte)

    def get_output(self, *, allow_incomplete_output: bool = False) -> bytes:
        return self._screen.get_output(allow_incomplete_output=allow_incomplete_output)
