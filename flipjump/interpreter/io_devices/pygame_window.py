"""
the pygame/SDL window host shared by the interactive io-devices, and the two devices that
use it.

PygameWindow owns the single SDL window and its event pump: it captures live key events
(into a queue), presents frames, toggles fullscreen on F11, and raises KeyboardInterrupt
when the window is closed (a clean run-termination). It is a *neutral* resource - the
interactive screen draws to it and the keyboard reads keys from it, but neither depends
on the other. PcIO.interactive() wires both onto the one window it creates, so it shows
the frames and captures the keys.

SDL only delivers key events to a focusable window, so capturing live keys needs one open.
In `pc` the screen opens+sizes the window on the program's init-screen command; for a window
with no screen to size it (e.g. standalone keyboard tests), ensure_open_for_input opens a
small default window.

keycodes delivered to the fj program (one byte) are io_devices/README.md's; KEYCODES below is the one
table. Keys not in it are not delivered; F11 is captured for the fullscreen toggle and is never delivered.

pygame-ce is an optional dependency: `pip install flipjump[io]`. the window must be
driven from the main thread (a macOS/SDL requirement) - the interpreter already runs
there. works on Windows, Linux and macOS; tests run headless with SDL's dummy driver.

PYGAME-CE ONLY, AND CHECKED AT IMPORT. upstream `pygame` and `pygame-ce` install under the SAME
module name, so having the wrong one is silent: everything here would import and run. The two
diverge on newer API (`pygame.Window`) and ship different SDL builds, so "it happens to work"
is not the same as "it is the tested configuration". `_import_pygame` requires `pygame.IS_CE`,
which only pygame-ce defines, and names the fix if it is missing.
"""

import os
import time
import warnings
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.interpreter.io_devices.KeyboardIO import KeyboardIO, KeyEventSource, ScriptedKeyEventSource
from flipjump.interpreter.io_devices.ScreenIO import InMemoryScreen
from flipjump.utils.exceptions import IODeviceException

# the >=0x80 keycodes (io_devices/README.md, "Keycodes")
KEYCODE_LEFT_CTRL = 0x80
KEYCODE_LEFT_SHIFT = 0x81
KEYCODE_LEFT_ALT = 0x82
KEYCODE_LEFT_GUI = 0x83
KEYCODE_RIGHT_CTRL = 0x84
KEYCODE_RIGHT_SHIFT = 0x85
KEYCODE_RIGHT_ALT = 0x86
KEYCODE_RIGHT_GUI = 0x87
KEYCODE_RIGHT = 0xD7
KEYCODE_LEFT = 0xD8
KEYCODE_DOWN = 0xD9
KEYCODE_UP = 0xDA

# SDL2 gives a key with a character its ASCII code, and every other key its USB HID usage ID (SDL's
# "scancode") with bit 30 set. pygame's event.key is that SDL keycode.
SDL_SCANCODE_KEY = 1 << 30
HID_KEYCODE_BASE = 0x88  # a key outside ASCII and the modifiers is sent as 0x88 + its HID usage ID

# THE key table: SDL keycode -> the byte delivered to the program (io_devices/README.md, "Keycodes").
KEYCODES: Dict[int, int] = {
    0x08: 0x08,  # Backspace
    0x09: 0x09,  # Tab
    0x0D: 0x0D,  # Enter
    0x1B: 0x1B,  # Esc
    # the printable keys, unshifted: Space, punctuation, '0'-'9', 'a'-'z' (SDL never gives 'A'-'Z')
    **{code: code for code in range(0x20, 0x7F) if not ord('A') <= code <= ord('Z')},
    0x7F: HID_KEYCODE_BASE + 0x4C,  # Delete: SDL gives it ASCII DEL; the table's Delete is HID 0x4C (0xD4)
    # the modifiers, HID 0xE0-0xE7 -> 0x80-0x87
    SDL_SCANCODE_KEY | 0xE0: KEYCODE_LEFT_CTRL,
    SDL_SCANCODE_KEY | 0xE1: KEYCODE_LEFT_SHIFT,
    SDL_SCANCODE_KEY | 0xE2: KEYCODE_LEFT_ALT,
    SDL_SCANCODE_KEY | 0xE3: KEYCODE_LEFT_GUI,  # Windows / Command
    SDL_SCANCODE_KEY | 0xE4: KEYCODE_RIGHT_CTRL,
    SDL_SCANCODE_KEY | 0xE5: KEYCODE_RIGHT_SHIFT,
    SDL_SCANCODE_KEY | 0xE6: KEYCODE_RIGHT_ALT,
    SDL_SCANCODE_KEY | 0xE7: KEYCODE_RIGHT_GUI,
    # 0x88 + HID: Caps Lock (0x39), F1-F12 (0x3A-0x45), Print Screen, Scroll Lock, Pause (0x46-0x48),
    # Insert, Home, Page Up (0x49-0x4B), End, Page Down (0x4D, 0x4E; Delete is 0x7F above),
    # Right, Left, Down, Up (0x4F-0x52), Num Lock (0x53), keypad / * - + Enter (0x54-0x58),
    # keypad 1-9, 0, . (0x59-0x63), Menu (0x65, "Application"), F13-F24 (0x68-0x73)
    **{
        SDL_SCANCODE_KEY | usage: HID_KEYCODE_BASE + usage
        for usage in [*range(0x39, 0x4C), *range(0x4D, 0x64), 0x65, *range(0x68, 0x74)]
    },
}

# the size of the standalone window opened for a live keyboard that has no interactive screen
# (`ensure_open_for_input`); a program that sends init_screen opens through `ensure_open` instead
INPUT_WINDOW_SIZE = (320, 240)

# how tall a program's window OPENS, in screen pixels; the width follows the program's aspect
# ratio. `pg.SCALED` alone picks the largest integer scale that fits the desktop, which for a
# small logical surface is a near-fullscreen window. The upscale is nearest-neighbour; the user
# can still drag-resize or F11 - SCALED | RESIZABLE stay on.
WINDOW_HEIGHT = 480


def _import_pygame() -> Any:
    try:
        # pygame prints its version banner to stdout on import, which is the program's own output
        # channel; this is pygame's supported way to silence it, and must be set before the import
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
            # a new size: start a new window. SDL cannot re-create a SCALED window's renderer in place
            # (a second set_mode with SCALED crashes pygame-ce 2.5 with an access violation), so the
            # display is closed first. Its pending events are taken first (keys queue; a close ends the
            # run here), and the title, the icon and fullscreen carry over to the new window.
            self.pump_events()
            fullscreen = bool(pg.display.is_fullscreen())
            pg.display.quit()
        elif pg.display.get_init() and pg.display.get_surface() is not None:
            pg.display.quit()  # another PygameWindow's display is still up: the same SCALED re-create crash
        pg.display.init()
        pg.display.set_caption(self._title)
        # SCALED scales the small logical surface up to a window-sized one (and handles
        # fullscreen scaling); RESIZABLE lets the user drag-resize.
        mode_flags = pg.SCALED | (pg.FULLSCREEN if fullscreen else pg.RESIZABLE)
        self._screen_surface = pg.display.set_mode((width, height), mode_flags)
        if self._icon is not None:
            self._apply_icon(self._icon)
        if not fullscreen:
            # width follows the program's aspect, so the picture is scaled but never stretched
            self._resize_window(max(1, round(WINDOW_HEIGHT * width / height)), WINDOW_HEIGHT)

    def _os_window(self) -> Any:
        """the OS window behind the display surface (pygame.window.Window).

        `from_display_module` is pygame-ce's only handle to a window that `display.set_mode`
        created; it is deprecated in favour of building on `pygame.Window` from the start, which
        is a larger change than this module wants, so the deprecation warning is silenced here."""
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            return self._pygame.window.Window.from_display_module()

    def _resize_window(self, width: int, height: int) -> None:
        """Shrink the OS window without touching the logical surface - SCALED keeps doing the
        upscale, so the program still draws into its own width x height.

        Best effort: a video driver that refuses (SDL_VIDEODRIVER=dummy, some remote desktops)
        keeps pygame's default size rather than failing the run; the program's output does not
        depend on it."""
        try:
            self._os_window().size = (width, height)
        except self._pygame.error:
            pass

    def set_title(self, title: str) -> None:
        """set the caption now and remember it: `ensure_open` re-applies `self._title` whenever it
        (re)creates the surface, so the title survives a resize"""
        self._title = title
        if self._screen_surface is not None:
            self._pygame.display.set_caption(title)

    def set_icon(self, width: int, height: int, colours: Sequence[Tuple[int, int, int]]) -> None:
        """set the window icon from row-major RGB colours (opaque: the icon has no transparency)."""
        pg = self._pygame
        pg.display.init()
        rgb_bytes = bytes(channel for colour in colours for channel in colour)
        self._icon = pg.image.frombuffer(rgb_bytes, (width, height), 'RGB').copy()
        self._apply_icon(self._icon)

    def _apply_icon(self, surface: Any) -> None:
        pg = self._pygame
        # `pg.display.set_icon` only takes effect before `set_mode`, and the icon arrives over the
        # output stream after the window is open, so it goes through the live window
        try:
            self._os_window().set_icon(surface)
        except pg.error:
            pg.display.set_icon(surface)

    def ensure_open_for_input(self) -> None:
        """open a small window (if none is open yet) so SDL can deliver key events - for a
        window with no screen to open/size it (e.g. standalone keyboard tests)."""
        if not self.is_open:
            self.ensure_open(*INPUT_WINDOW_SIZE)

    def _byte_keycode(self, sdl_key: int) -> Optional[int]:
        return KEYCODES.get(sdl_key)

    def pump_events(self) -> None:
        """process pending window events: queue key transitions, F11 = fullscreen toggle,
        closing the window raises KeyboardInterrupt (a clean run termination)."""
        if self.closed or self._screen_surface is None:
            return
        pg = self._pygame
        for event in pg.event.get():
            if event.type == pg.QUIT:
                self.closed = True
                raise KeyboardInterrupt
            if event.type in (pg.KEYDOWN, pg.KEYUP):
                if event.key == pg.K_F11:
                    if event.type == pg.KEYDOWN:
                        try:
                            pg.display.toggle_fullscreen()
                        except pg.error:
                            pass  # some video drivers (e.g. the headless dummy) can't toggle
                    continue
                keycode = self._byte_keycode(event.key)
                if keycode is not None:
                    self.key_events.append((event.type == pg.KEYDOWN, keycode))

    def show_black(self) -> None:
        """fill the window black and show it (init_screen's black screen)."""
        assert self._screen_surface is not None
        self._screen_surface.fill((0, 0, 0))
        self._pygame.display.flip()

    def draw(self, width: int, height: int, rgb_pixels: List[Tuple[int, int, int]]) -> None:
        """blit a full frame given as row-major RGB tuples, and present it.

        Prefer `draw_indexed` when the caller already has palette indices: this signature forces
        the palette expansion to happen in python, one tuple per pixel."""
        if self._screen_surface is None:
            return
        pg = self._pygame
        rgb_bytes = bytes(channel for pixel in rgb_pixels for channel in pixel)
        self._blit(pg.image.frombuffer(rgb_bytes, (width, height), 'RGB'))

    def draw_indexed(self, width: int, height: int, indices: bytes, palette: List[Tuple[int, int, int]]) -> None:
        """blit a frame given as raw PALETTE INDICES, and present it.

        SDL expands the palette itself, so no per-pixel work is done in python (about 30x faster
        than `draw`, which builds an RGB list first)."""
        if self._screen_surface is None:
            return
        pg = self._pygame
        frame_surface = pg.image.frombuffer(indices, (width, height), 'P')
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
    """the InMemoryScreen command stream, presented into a real window.

    takes a PygameWindow to draw to (creating its own when not given a shared one); it does
    not deal with input - a live keyboard reads keys from the same window through its own
    WindowKeyEventSource, so the two devices share only the window, not each other."""

    def __init__(self, *, frames_dir: Optional[Path] = None, window: Optional[PygameWindow] = None):
        super().__init__(frames_dir=frames_dir)
        self.window = window if window is not None else PygameWindow()
        # time.perf_counter() of the previous present, for the max-fps cap; -inf: the first present never waits
        # (perf_counter's starting point is undefined, so 0.0 would hold the first frame on a clock that starts there)
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
        # wait first, so the frame is recorded (its frame_hashes time) when it is shown
        self._wait_for_frame_slot()
        super()._present()
        if self.bpp <= 8:
            # the INDEXED path: hand SDL the palette indices and let it expand them. `last_frame_rgb`
            # is lazy (see InMemoryScreen), so a windowed run never builds the per-pixel RGB list.
            self.window.draw_indexed(self.width, self.height, bytes(self.pixel_indices), self.palette)
        else:  # RGB, or more than 256 colours: SDL's 8-bit surfaces cannot hold the palette
            self.window.draw(self.width, self.height, self.last_frame_rgb)
        self.window.pump_events()

    def _wait_for_frame_slot(self) -> None:
        """the set_max_fps (0x03) cap: hold this present until 1/max_fps s have passed since the previous one.
        The wait sleeps in short slices and keeps pumping the window, so keys still queue and closing
        the window still ends the run while a fast frame waits."""
        if self.max_fps:
            due = self._last_flush + 1.0 / self.max_fps
            remaining = due - time.perf_counter()
            while remaining > 0:
                time.sleep(min(remaining, 0.005))
                self.window.pump_events()
                remaining = due - time.perf_counter()
        self._last_flush = time.perf_counter()


class PcIO(IODevice):
    """the complete 'pc' io-device: live keyboard input + the screen, together.

    a single device that owns both channels: read_bit comes from the keyboard, write_bit
    drives the screen. interactive() wires both onto the one real window it owns (the keys
    and the pixels). For a headless variant (e.g. scripted keys + PNG frames) build it from
    explicit components, via __init__ or headless()."""

    def __init__(self, screen: InMemoryScreen, keyboard: KeyboardIO):
        self._screen = screen
        self._keyboard = keyboard

    @property
    def outputs_live(self) -> bool:
        # output is presented live as screen frames (the window, or PNG frames when headless),
        # so the termination summary shouldn't re-dump the raw screen-command bytes.
        return True

    @classmethod
    def interactive(cls) -> 'PcIO':
        """a real window: live key presses in, a scaled screen out (one window).
        the screen opens+sizes the window on the program's init-screen command (so live keys
        are captured from then on - a pc program initializes its screen up front)."""
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
