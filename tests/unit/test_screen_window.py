"""
unit-tests for the interactive pygame window (pygame_window.py) - run headless with SDL's
dummy video driver, so they work on Windows/Linux/macOS and in CI.

pinned: the screen opens/draws the shared window; live key events (incl. the special
>=0x80 keycodes) flow from the window into a KeyboardIO that does NOT depend on the screen;
F11 toggles fullscreen and is never delivered; closing the window stops the run; and the CLI
wires the two interactive devices onto one shared window (a lone live keyboard opens its own).
"""

from pathlib import Path
from typing import Iterator

import pytest

pygame = pytest.importorskip('pygame')

from flipjump.interpreter import fjm_run  # noqa: E402
from flipjump.interpreter.io_devices.KeyboardIO import KeyboardIO  # noqa: E402
from flipjump.interpreter.io_devices.pygame_window import (  # noqa: E402
    KEYCODE_UP,
    InteractiveScreen,
    PcIO,
    WINDOW_HEIGHT,
    PygameWindow,
    WindowKeyEventSource,
)
from flipjump.utils.classes import TerminationCause  # noqa: E402
from tests.unit.test_devices_e2e import EXPECTED_FRAME_HASH, RAW_SCREEN_PROGRAM  # noqa: E402
from tests.unit.test_screen_io import write_byte, write_u16  # noqa: E402
from tests.unit.unit_utils import assemble_to_path  # noqa: E402


@pytest.fixture(autouse=True)
def headless_sdl(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv('SDL_VIDEODRIVER', 'dummy')
    monkeypatch.setenv('SDL_AUDIODRIVER', 'dummy')
    yield
    pygame.quit()


def init_2x2_screen(device: InteractiveScreen) -> None:
    write_byte(device, 1)  # CMD init_screen
    write_u16(device, 2)
    write_u16(device, 2)
    write_byte(device, 8)  # bpp
    write_u16(device, 0)  # palette_size


def present_raw_2x2(device: InteractiveScreen) -> None:
    write_byte(device, 5)  # CMD update_screen_raw
    for pixel in (0, 0, 0, 0):
        write_byte(device, pixel)


def keyboard_on(window: PygameWindow) -> KeyboardIO:
    """a KeyboardIO reading live keys from the window - built without referencing any screen."""
    return KeyboardIO(WindowKeyEventSource(window))


def post_key(key: int, *, is_down: bool = True) -> None:
    event_type = pygame.KEYDOWN if is_down else pygame.KEYUP
    pygame.event.post(pygame.event.Event(event_type, key=key))


def read_status_hex(keyboard: KeyboardIO) -> int:
    return sum(int(keyboard.read_bit()) << i for i in range(4))


def read_byte(keyboard: KeyboardIO) -> int:
    return sum(int(keyboard.read_bit()) << i for i in range(8))


def test_init_screen_opens_the_window() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    surface = pygame.display.get_surface()
    assert surface is not None
    assert surface.get_size() == (2, 2)


def test_present_draws_the_frame_pixels() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    device.palette = [(9, 8, 7)]  # a palette entry, so the drawn pixel is not the default black
    present_raw_2x2(device)
    assert device.frame_count == 1
    surface = pygame.display.get_surface()
    assert surface.get_at((0, 0))[:3] == (9, 8, 7)


def test_live_key_events_reach_a_keyboard_device() -> None:
    window = PygameWindow()
    window.ensure_open_for_input()
    keyboard = keyboard_on(window)

    post_key(pygame.K_a)
    assert read_status_hex(keyboard) == 0x9  # key down
    assert read_byte(keyboard) == ord('a')

    post_key(pygame.K_a, is_down=False)
    assert read_status_hex(keyboard) == 0x8  # key up
    assert read_byte(keyboard) == ord('a')

    assert read_status_hex(keyboard) == 0x0  # idle


def test_special_keys_use_the_byte_keycodes() -> None:
    window = PygameWindow()
    window.ensure_open_for_input()
    keyboard = keyboard_on(window)
    post_key(pygame.K_UP)
    assert read_status_hex(keyboard) == 0x9
    assert read_byte(keyboard) == KEYCODE_UP


def test_f11_toggles_fullscreen_and_is_not_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    window = PygameWindow()
    window.ensure_open_for_input()
    keyboard = keyboard_on(window)
    toggle_calls = []
    monkeypatch.setattr(pygame.display, 'toggle_fullscreen', lambda: toggle_calls.append(True))
    post_key(pygame.K_F11)
    post_key(pygame.K_b)
    assert read_status_hex(keyboard) == 0x9
    assert read_byte(keyboard) == ord('b')  # 'b' arrives; F11 was consumed by the toggle
    assert toggle_calls == [True]


def test_unmapped_keys_are_ignored() -> None:
    window = PygameWindow()
    window.ensure_open_for_input()
    keyboard = keyboard_on(window)
    post_key(pygame.K_F5)  # no byte keycode - ignored
    assert read_status_hex(keyboard) == 0x0


def test_closing_the_window_raises_keyboard_interrupt() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    pygame.event.post(pygame.event.Event(pygame.QUIT))
    with pytest.raises(KeyboardInterrupt):
        device.window.pump_events()
    assert device.window.closed


def test_fj_program_presents_into_the_window(tmp_path: Path) -> None:
    # the raw-command E2E program, now presented into a (dummy-driver) window
    fjm_path = assemble_to_path(RAW_SCREEN_PROGRAM, tmp_path, use_stl=True)
    device = InteractiveScreen()
    statistics = fjm_run.run(fjm_path, io_device=device, print_time=False)
    assert statistics.termination_cause == TerminationCause.Looping
    assert device.frame_count == 1
    assert device.frame_hashes[0][1] == EXPECTED_FRAME_HASH
    surface = pygame.display.get_surface()
    assert surface.get_at((0, 0))[:3] == (10, 20, 30)  # palette entry 0
    assert surface.get_at((1, 0))[:3] == (200, 100, 0)  # palette entry 1


def test_pc_interactive_owns_one_window_for_keys_and_screen() -> None:
    # the complete `pc` device: keyboard + screen on ONE window it owns (no device sharing)
    pc = PcIO.interactive()
    screen, keyboard = pc._screen, pc._keyboard
    assert isinstance(screen, InteractiveScreen)
    assert isinstance(keyboard, KeyboardIO)
    assert keyboard.event_source._window is screen.window  # type: ignore[attr-defined]  # one shared window
    assert not screen.window.is_open  # opens on the program's init-screen command, sized to it


def test_pc_interactive_delegates_keys_in_and_pixels_out() -> None:
    pc = PcIO.interactive()

    # output: drive init + a raw 2x2 frame through PcIO.write_bit -> the screen presents
    write_byte(pc, 1)  # init_screen
    write_u16(pc, 2)
    write_u16(pc, 2)
    write_byte(pc, 8)  # bpp
    write_u16(pc, 0)  # palette_size
    write_byte(pc, 5)  # update_screen_raw
    for pixel in (0, 0, 0, 0):
        write_byte(pc, pixel)
    assert pc._screen.frame_count == 1

    # input: a key press read back through PcIO.read_bit
    post_key(pygame.K_a)
    assert sum(int(pc.read_bit()) << i for i in range(4)) == 0x9  # status: key-down
    assert sum(int(pc.read_bit()) << i for i in range(8)) == ord('a')


def test_pc_headless_uses_inmemory_screen_and_scripted_keys(tmp_path: Path) -> None:
    from flipjump.interpreter.io_devices.KeyboardIO import ScriptedKeyEventSource
    from flipjump.interpreter.io_devices.ScreenIO import InMemoryScreen

    events = tmp_path / 'events.txt'
    events.write_text('0, down, 72\n')
    pc = PcIO.headless(events, tmp_path / 'frames')
    assert isinstance(pc._screen, InMemoryScreen)
    assert not isinstance(pc._screen, InteractiveScreen)  # headless: a plain in-memory screen
    assert isinstance(pc._keyboard.event_source, ScriptedKeyEventSource)


def test_io_mode_pc_builds_an_interactive_pc() -> None:
    from flipjump.interpreter.io_devices.cli_devices import make_io_device

    pc = make_io_device('pc')
    assert isinstance(pc, PcIO)
    # interactive: the keyboard and the screen share the one window the device owns
    assert pc._keyboard.event_source._window is pc._screen.window  # type: ignore[attr-defined]


def test_closing_the_window_terminates_a_real_run(tmp_path: Path, engine: str) -> None:
    # QUIT is already pending when the program presents its first frame: the pump raises
    # KeyboardInterrupt inside the run, and the interpreter must turn it into a clean
    # keyboard-interrupt termination (on every engine).
    fjm_path = assemble_to_path(RAW_SCREEN_PROGRAM, tmp_path, use_stl=True)
    device = InteractiveScreen()
    device.window.ensure_open(4, 2)  # open early so the QUIT event can be posted
    pygame.event.post(pygame.event.Event(pygame.QUIT))

    statistics = fjm_run.run(fjm_path, io_device=device, print_time=False)
    assert statistics.termination_cause == TerminationCause.KeyboardInterrupt
    assert device.window.closed
    # storage_mode is set only by the native engine - prove the right engine actually ran
    assert (statistics.storage_mode is not None) == (engine == 'native')


# --- window controls (0x12 title, 0x13 icon) reach the window ---


def send_title(device: InteractiveScreen, title: bytes) -> None:
    write_byte(device, 0x12)
    for char in title + b'\x00':
        write_byte(device, char)


def send_icon_1x1(device: InteractiveScreen, index: int) -> None:
    write_byte(device, 0x13)
    for byte in (1, 1, index):
        write_byte(device, byte)


def test_window_title_reaches_the_caption() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    send_title(device, b'DOOM')
    assert device.window_title == 'DOOM'
    assert pygame.display.get_caption()[0] == 'DOOM'


def test_window_opens_at_the_program_aspect(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeOSWindow:
        size = (0, 0)

    fake = FakeOSWindow()
    monkeypatch.setattr(PygameWindow, '_os_window', lambda self: fake)
    device = InteractiveScreen()
    write_byte(device, 1)  # CMD init_screen, 160x100
    write_u16(device, 160)
    write_u16(device, 100)
    write_byte(device, 8)
    write_u16(device, 0)
    assert fake.size == (768, WINDOW_HEIGHT)


def test_window_icon_waits_for_the_palette(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.test_screen_io import PALETTE_ADDRESS, FakeDeviceMemory, store_palette, write_address

    device = InteractiveScreen()
    memory = FakeDeviceMemory()
    device.attach_memory(memory)
    calls = []
    monkeypatch.setattr(device.window, 'set_icon', lambda *args: calls.append(args))
    write_byte(device, 1)  # CMD init_screen 2x2 with a 2-entry palette
    write_u16(device, 2)
    write_u16(device, 2)
    write_byte(device, 8)
    write_u16(device, 2)
    send_icon_1x1(device, 1)
    assert device.window_icon == (1, 1, [1]) and calls == []  # no palette yet: nothing to draw
    store_palette(memory, [(10, 20, 30), (200, 100, 0)])
    write_byte(device, 2)  # CMD set_palette
    write_address(device, PALETTE_ADDRESS)
    assert calls == [(1, 1, [1], [(10, 20, 30), (200, 100, 0)], 0)]
    send_icon_1x1(device, 0)  # with a palette in place the icon is applied at once
    assert calls[-1] == (1, 1, [0], [(10, 20, 30), (200, 100, 0)], 0)
