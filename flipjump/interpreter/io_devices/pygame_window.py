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

keycodes delivered to the fj program (one byte): printable/control keys send their
ascii-like SDL keycode (k < 0x80, e.g. 'a'=97, esc=27, enter=13, space=32); the
arrows/shift/ctrl/alt send 0x80-0x86 (up,down,left,right,shift,ctrl,alt). other keys
are ignored. F11 is captured for the fullscreen toggle and is never delivered.

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
import warnings
from collections import deque
from pathlib import Path
from typing import Any, Deque, List, Optional, Sequence, Tuple

from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.interpreter.io_devices.KeyboardIO import KeyboardIO, KeyEventSource, ScriptedKeyEventSource
from flipjump.interpreter.io_devices.ScreenIO import ICON_TRANSPARENT_INDEX, InMemoryScreen
from flipjump.interpreter.io_devices.device_memory import DeviceMemory
from flipjump.utils.exceptions import IODeviceException

# the >=0x80 keycodes (the SDL keycodes of these keys don't fit a byte)
KEYCODE_UP = 0x80
KEYCODE_DOWN = 0x81
KEYCODE_LEFT = 0x82
KEYCODE_RIGHT = 0x83
KEYCODE_SHIFT = 0x84
KEYCODE_CTRL = 0x85
KEYCODE_ALT = 0x86

# the size of the standalone window opened for a live keyboard that has no interactive screen
# (`ensure_open_for_input`); a program that sends init_screen opens through `ensure_open` instead
INPUT_WINDOW_SIZE = (320, 240)

# how tall a program's window OPENS, in screen pixels; the width follows the program's aspect
# ratio. `pg.SCALED` alone picks the largest integer scale that fits the desktop, which for a
# small logical surface is a near-fullscreen window. The upscale is nearest-neighbour, so a
# whole-number factor is crispest (400 or 500 for a 100-row surface); the user can still
# drag-resize or F11 - SCALED | RESIZABLE stay on.
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
    # upstream pygame installs under the SAME module name, so a wrong install is otherwise
    # silent. IS_CE is defined only by pygame-ce.
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
        self.key_events: Deque[Tuple[bool, int]] = deque()  # (is_down, keycode)
        self.closed = False

        pg = self._pygame
        self._special_keycodes = {
            pg.K_UP: KEYCODE_UP,
            pg.K_DOWN: KEYCODE_DOWN,
            pg.K_LEFT: KEYCODE_LEFT,
            pg.K_RIGHT: KEYCODE_RIGHT,
            pg.K_LSHIFT: KEYCODE_SHIFT,
            pg.K_RSHIFT: KEYCODE_SHIFT,
            pg.K_LCTRL: KEYCODE_CTRL,
            pg.K_RCTRL: KEYCODE_CTRL,
            pg.K_LALT: KEYCODE_ALT,
            pg.K_RALT: KEYCODE_ALT,
        }

    @property
    def is_open(self) -> bool:
        return self._screen_surface is not None

    def ensure_open(self, width: int, height: int) -> None:
        """open (or resize) the window for a width x height logical surface."""
        if self._screen_surface is not None and self._screen_surface.get_size() == (width, height):
            return  # already open at this size - recreating the window would flicker
        pg = self._pygame
        pg.display.init()
        pg.display.set_caption(self._title)
        # SCALED scales the small logical surface up to a window-sized one (and handles
        # fullscreen scaling); RESIZABLE lets the user drag-resize.
        self._screen_surface = pg.display.set_mode((width, height), pg.SCALED | pg.RESIZABLE)
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

    def set_icon(
        self,
        width: int,
        height: int,
        indices: Sequence[int],
        palette: Sequence[Tuple[int, int, int]],
        transparent_index: Optional[int] = None,
    ) -> None:
        """set the window icon from palette indices, the form the program sends them in.
        `transparent_index` becomes a colorkey rather than an alpha channel."""
        pg = self._pygame
        pg.display.init()
        surface = pg.Surface((width, height))
        for y in range(height):
            row = y * width
            for x in range(width):
                idx = indices[row + x]
                surface.set_at((x, y), palette[idx] if idx < len(palette) else (0, 0, 0))
        if transparent_index is not None and transparent_index < len(palette):
            surface.set_colorkey(palette[transparent_index])
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
        if 0 < sdl_key < 0x80:
            return sdl_key
        return self._special_keycodes.get(sdl_key)

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

        SDL expands the palette itself, so the per-pixel work leaves python entirely. MEASURED on
        a 160x100 frame: 0.04 ms against `draw`'s 1.36 ms for the same picture -- 34x, and the
        caller no longer needs to build a per-pixel RGB list at all (another 0.47 ms). That is the
        whole per-frame cost of presenting, so it goes from ~1.83 ms to ~0.04 ms."""
        if self._screen_surface is None:
            return
        pg = self._pygame
        frame_surface = pg.image.frombuffer(indices, (width, height), 'P')
        frame_surface.set_palette(palette)
        self._blit(frame_surface)

    def _blit(self, frame_surface) -> None:
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
        # an icon that arrived before the palette; applied once _set_palette runs
        self._pending_icon: Optional[Tuple[int, int, List[int]]] = None
        self._palette_set = False

    def _init_screen(self, width: int, height: int, bpp: int, palette_size: int) -> None:
        super()._init_screen(width, height, bpp, palette_size)
        self.window.ensure_open(width, height)

    def _set_window_title(self, text: str) -> None:
        super()._set_window_title(text)
        self.window.set_title(text)

    def _set_window_icon(self, width: int, height: int, indices: List[int]) -> None:
        super()._set_window_icon(width, height, indices)
        # if the program set the icon before its palette, `self.palette` is still the init_screen
        # zero-fill and the icon would be black; keep it and apply it once a palette arrives
        self._pending_icon = (width, height, list(indices))
        if self._palette_set:
            self.window.set_icon(width, height, indices, self.palette, ICON_TRANSPARENT_INDEX)

    def _set_palette(self, palette_bit_address: int) -> None:
        super()._set_palette(palette_bit_address)
        self._palette_set = True
        if self._pending_icon is not None:
            width, height, indices = self._pending_icon
            self.window.set_icon(width, height, indices, self.palette, ICON_TRANSPARENT_INDEX)

    def _present(self) -> None:
        super()._present()
        # the INDEXED path: hand SDL the palette indices and let it expand them. `last_frame_rgb`
        # is lazy (see InMemoryScreen), so a windowed run never builds the per-pixel RGB list.
        self.window.draw_indexed(self.width, self.height, bytes(self.pixel_indices), list(self.palette))
        self.window.pump_events()


class PcIO(IODevice):
    """the complete 'pc' io-device: live keyboard input + a 256-color screen, together.

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
        """a real window: live key presses in, a scaled 256-color screen out (one window).
        the screen opens+sizes the window on the program's init-screen command (so live keys
        are captured from then on - a pc program initializes its screen up front)."""
        window = PygameWindow()
        return cls(InteractiveScreen(window=window), KeyboardIO(WindowKeyEventSource(window)))

    @classmethod
    def headless(cls, events_file: Path, frames_dir: Path) -> 'PcIO':
        """no window: a scripted keyboard in, PNG frames out (deterministic replays / CI)."""
        return cls(InMemoryScreen(frames_dir=frames_dir), KeyboardIO(ScriptedKeyEventSource.from_file(events_file)))

    def attach_memory(self, device_memory: DeviceMemory) -> None:
        self._screen.attach_memory(device_memory)  # the screen reads the framebuffer; the keyboard needs no memory

    def read_bit(self) -> bool:
        return self._keyboard.read_bit()

    def write_bit(self, bit: bool) -> None:
        self._screen.write_bit(bit)

    def get_output(self, *, allow_incomplete_output: bool = False) -> bytes:
        return self._screen.get_output(allow_incomplete_output=allow_incomplete_output)
