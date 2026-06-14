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

pygame is an optional dependency: `pip install flipjump[io]`. the window must be
driven from the main thread (a macOS/SDL requirement) - the interpreter already runs
there. works on Windows, Linux and macOS; tests run headless with SDL's dummy driver.
"""

from collections import deque
from pathlib import Path
from typing import Any, Deque, List, Optional, Tuple

from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.interpreter.io_devices.KeyboardIO import KeyboardIO, KeyEventSource, ScriptedKeyEventSource
from flipjump.interpreter.io_devices.ScreenIO import InMemoryScreen
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
INPUT_WINDOW_SIZE = (320, 240)


def _import_pygame() -> Any:
    try:
        import pygame
    except ImportError as import_error:
        raise IODeviceException(
            'the interactive window needs the pygame package, which is not installed. '
            'install it with `pip install pygame`, or `pip install flipjump[io]` to pull it '
            'in as a flipjump extra.'
        ) from import_error
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
        """blit a full frame (row-major RGB tuples) and present it."""
        if self._screen_surface is None:
            return
        pg = self._pygame
        rgb_bytes = bytes(channel for pixel in rgb_pixels for channel in pixel)
        frame_surface = pg.image.frombuffer(rgb_bytes, (width, height), 'RGB')
        self._screen_surface.blit(frame_surface, (0, 0))
        pg.display.flip()

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

    def _init_screen(self, width: int, height: int, bpp: int, palette_size: int) -> None:
        super()._init_screen(width, height, bpp, palette_size)
        self.window.ensure_open(width, height)

    def _present(self) -> None:
        super()._present()
        self.window.draw(self.width, self.height, self.last_frame_rgb)
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
