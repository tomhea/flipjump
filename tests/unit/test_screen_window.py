"""
unit-tests for the interactive pygame window (pygame_window.py) - run headless with SDL's
dummy video driver, so they work on Windows/Linux/macOS and in CI.

pinned: the screen opens/draws the shared window; live key events (io_devices/README.md's
keycodes) flow from the window into a KeyboardIO that does NOT depend on the screen;
F11 toggles fullscreen and is never delivered; closing the window stops the run; and the CLI
wires the two interactive devices onto one shared window (a lone live keyboard opens its own);
the title and the icon reach the window, and the set_max_fps cap holds a fast present until its slot.
"""

import time
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

pygame = pytest.importorskip('pygame')

from flipjump.interpreter import fjm_run  # noqa: E402
from flipjump.interpreter.io_devices.KeyboardIO import KeyboardIO  # noqa: E402
from flipjump.interpreter.io_devices.pygame_window import (  # noqa: E402
    KEYCODES,
    SDL_SCANCODE_KEY,
    InteractiveScreen,
    PcIO,
    WINDOW_HEIGHT,
    PygameWindow,
    WindowKeyEventSource,
)
from flipjump.interpreter.io_devices.ScreenIO import XTERM256  # noqa: E402
from flipjump.utils.classes import TerminationCause  # noqa: E402
from flipjump.utils.exceptions import IODeviceException  # noqa: E402
from tests.unit import screen_stream as ss  # noqa: E402
from tests.unit.test_devices_e2e import EXPECTED_FRAME_HASH, SCREEN_PROGRAM  # noqa: E402
from tests.unit.unit_utils import assemble_to_path  # noqa: E402


@pytest.fixture(autouse=True)
def headless_sdl(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv('SDL_VIDEODRIVER', 'dummy')
    monkeypatch.setenv('SDL_AUDIODRIVER', 'dummy')
    yield
    pygame.quit()


def init_2x2_screen(device: InteractiveScreen) -> None:
    ss.feed(device, ss.init_screen(2, 2, 8))


def present_raw_2x2(device: InteractiveScreen) -> None:
    ss.feed(device, ss.draw_raw([0, 0, 0, 0], 8) + ss.PRESENT)


def painted() -> List[Tuple[int, int, int]]:
    surface = pygame.display.get_surface()
    width, height = surface.get_size()
    return [tuple(surface.get_at((x, y))[:3]) for y in range(height) for x in range(width)]


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


# ---------------------------------------------------------------- the life cycle in the window


def test_init_screen_opens_the_window() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    surface = pygame.display.get_surface()
    assert surface is not None
    assert surface.get_size() == (2, 2)
    assert painted() == [(0, 0, 0)] * 4
    assert device.frame_count == 0


def test_present_draws_the_frame_pixels() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    ss.feed(device, ss.define_palette(3, 0, [(9, 8, 7), (1, 2, 3)], 8) + ss.draw_raw([0, 1, 1, 0], 8))
    ss.feed(device, ss.PRESENT)
    assert painted() == [XTERM256[0], XTERM256[1], XTERM256[1], XTERM256[0]]  # slot 0 is still selected
    ss.feed(device, ss.select_palette(3) + ss.PRESENT)  # no redraw: the new palette, the same picture
    assert painted() == [(9, 8, 7), (1, 2, 3), (1, 2, 3), (9, 8, 7)]
    assert device.frame_count == 2


def test_a_reinit_shows_black_at_once_even_at_the_same_size() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    ss.feed(device, ss.draw_raw([15, 15, 15, 15], 8) + ss.PRESENT)
    assert painted() == [(255, 255, 255)] * 4
    init_2x2_screen(device)
    assert painted() == [(0, 0, 0)] * 4


def test_a_reinit_resizes_the_window() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    ss.feed(device, ss.init_screen(3, 1, 1) + ss.draw_raw([1, 0, 1], 1) + ss.PRESENT)
    assert pygame.display.get_surface().get_size() == (3, 1)
    assert painted() == [(255, 255, 255), (0, 0, 0), (255, 255, 255)]


def test_a_second_window_closes_the_first_display_before_opening(monkeypatch: pytest.MonkeyPatch) -> None:
    PygameWindow().ensure_open(2, 2)
    quits = []
    real_quit = pygame.display.quit

    def recording_quit() -> None:
        quits.append(True)
        real_quit()

    monkeypatch.setattr(pygame.display, 'quit', recording_quit)
    PygameWindow().ensure_open(3, 1)
    assert quits == [True] and pygame.display.get_surface().get_size() == (3, 1)


def test_rgb_frames_are_drawn() -> None:
    device = InteractiveScreen()
    ss.feed(device, ss.init_screen(2, 1, 24) + bytes([0x10, 1, 2, 3, 250, 128, 0]) + ss.PRESENT)
    assert painted() == [(1, 2, 3), (250, 128, 0)]


def test_frames_of_more_than_256_colours_are_drawn() -> None:
    device = InteractiveScreen()
    ss.feed(device, ss.init_screen(2, 1, 16) + ss.draw_raw([0xF800, 0x001F], 16) + ss.PRESENT)
    assert painted() == [(255, 0, 0), (0, 0, 255)]


def test_the_window_shows_what_the_headless_screen_records() -> None:
    device = InteractiveScreen()
    ss.feed(device, ss.init_screen(3, 2, 4) + ss.draw_raw([1, 2, 3, 4, 5, 6], 4) + ss.PRESENT)
    assert painted() == device.last_frame_rgb


def test_window_opens_at_the_program_aspect(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeOSWindow:
        size = (0, 0)

    fake = FakeOSWindow()
    monkeypatch.setattr(PygameWindow, '_os_window', lambda self: fake)
    device = InteractiveScreen()
    ss.feed(device, ss.init_screen(160, 100, 8))
    assert fake.size == (768, WINDOW_HEIGHT)


# ---------------------------------------------------------------- the title and the icon


def test_window_title_reaches_the_caption() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    ss.feed(device, ss.set_window_title(b'flipjump'))
    assert device.window_title == 'flipjump'
    assert pygame.display.get_caption()[0] == 'flipjump'


def test_the_title_survives_a_reinit() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    ss.feed(device, ss.set_window_title(b'flipjump') + ss.init_screen(4, 4, 1))
    assert pygame.display.get_caption()[0] == 'flipjump'


def test_the_icon_reaches_the_window_resolved_through_the_palette(monkeypatch: pytest.MonkeyPatch) -> None:
    device = InteractiveScreen()
    calls = []
    real_set_icon = device.window.set_icon

    def recording_set_icon(*args):  # type: ignore[no-untyped-def]
        calls.append(args)
        real_set_icon(*args)

    monkeypatch.setattr(device.window, 'set_icon', recording_set_icon)
    ss.feed(
        device,
        ss.init_screen(2, 2, 1)
        + ss.define_palette(2, 0, [(10, 20, 30), (40, 50, 60)], 1)
        + ss.select_palette(2)
        + ss.set_window_icon(2, 1, [1, 0], 1),
    )
    assert calls == [(2, 1, [(40, 50, 60), (10, 20, 30)])]


def test_an_rgb_icon_is_opaque() -> None:
    """the icon has no transparency: even black pixels are drawn"""
    window = PygameWindow()
    window.ensure_open(2, 2)
    window.set_icon(2, 1, [(0, 0, 0), (255, 0, 0)])  # through the dummy driver: must not raise
    assert window._icon.get_colorkey() is None and window._icon.get_at((0, 0))[:3] == (0, 0, 0)


def test_the_icon_survives_a_resize(monkeypatch: pytest.MonkeyPatch) -> None:
    device = InteractiveScreen()
    ss.feed(device, ss.init_screen(2, 2, 1) + ss.set_window_icon(1, 1, [1], 1))
    applied: List[object] = []
    monkeypatch.setattr(device.window, '_apply_icon', applied.append)
    ss.feed(device, ss.init_screen(3, 1, 1))
    assert applied == [device.window._icon]


def test_icon_through_the_whole_window_path() -> None:
    device = InteractiveScreen()
    ss.feed(device, ss.init_screen(4, 4, 24) + ss.set_window_icon(2, 2, [0xFF0000, 0x00FF00, 0x0000FF, 0], 24))
    assert device.window_icon == (2, 2, [(0, 0, 255), (0, 255, 0), (255, 0, 0), (0, 0, 0)])


def test_control_a_bad_title_stops_the_window_device() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    with pytest.raises(IODeviceException) as failure:
        ss.feed(device, ss.set_window_title(b'\xff'))
    assert str(failure.value) == (
        'ScreenIO: set_window_title (0x04) at output byte 6, frame 0: '
        'set_window_title: title byte 0 (0xFF) is not valid UTF-8'
    )
    assert pygame.display.get_caption()[0] == 'FlipJump'


# ---------------------------------------------------------------- the keys


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


# the README's keycode table, row by row, as pygame keys
README_KEYS = [
    (pygame.K_BACKSPACE, 0x08),
    (pygame.K_TAB, 0x09),
    (pygame.K_RETURN, 0x0D),
    (pygame.K_ESCAPE, 0x1B),
    (pygame.K_SPACE, 0x20),
    (pygame.K_0, ord('0')),
    (pygame.K_9, ord('9')),
    (pygame.K_a, ord('a')),
    (pygame.K_z, ord('z')),
    (pygame.K_MINUS, ord('-')),
    (pygame.K_EQUALS, ord('=')),
    (pygame.K_LEFTBRACKET, ord('[')),
    (pygame.K_BACKSLASH, ord('\\')),
    (pygame.K_SEMICOLON, ord(';')),
    (pygame.K_QUOTE, ord("'")),
    (pygame.K_BACKQUOTE, ord('`')),
    (pygame.K_COMMA, ord(',')),
    (pygame.K_PERIOD, ord('.')),
    (pygame.K_SLASH, ord('/')),
    (pygame.K_LCTRL, 0x80),
    (pygame.K_LSHIFT, 0x81),
    (pygame.K_LALT, 0x82),
    (pygame.K_LGUI, 0x83),
    (pygame.K_RCTRL, 0x84),
    (pygame.K_RSHIFT, 0x85),
    (pygame.K_RALT, 0x86),
    (pygame.K_RGUI, 0x87),
    (pygame.K_CAPSLOCK, 0xC1),
    (pygame.K_F1, 0xC2),
    (pygame.K_F10, 0xCB),
    (pygame.K_F12, 0xCD),
    (pygame.K_PRINTSCREEN, 0xCE),
    (pygame.K_SCROLLLOCK, 0xCF),
    (pygame.K_PAUSE, 0xD0),
    (pygame.K_INSERT, 0xD1),
    (pygame.K_HOME, 0xD2),
    (pygame.K_PAGEUP, 0xD3),
    (pygame.K_DELETE, 0xD4),
    (pygame.K_END, 0xD5),
    (pygame.K_PAGEDOWN, 0xD6),
    (pygame.K_RIGHT, 0xD7),
    (pygame.K_LEFT, 0xD8),
    (pygame.K_DOWN, 0xD9),
    (pygame.K_UP, 0xDA),
    (pygame.K_NUMLOCK, 0xDB),
    (pygame.K_KP_DIVIDE, 0xDC),
    (pygame.K_KP_MULTIPLY, 0xDD),
    (pygame.K_KP_MINUS, 0xDE),
    (pygame.K_KP_PLUS, 0xDF),
    (pygame.K_KP_ENTER, 0xE0),
    (pygame.K_KP1, 0xE1),
    (pygame.K_KP9, 0xE9),
    (pygame.K_KP0, 0xEA),
    (pygame.K_KP_PERIOD, 0xEB),
    (SDL_SCANCODE_KEY | 0x65, 0xED),  # SDLK_APPLICATION, the "Menu" key (pygame has no constant for it)
    (pygame.K_F13, 0xF0),
    (pygame.K_F15, 0xF2),
    (SDL_SCANCODE_KEY | 0x73, 0xFB),  # SDLK_F24
]


@pytest.mark.parametrize('key, code', README_KEYS)
def test_the_key_table_is_the_readmes(key: int, code: int) -> None:
    assert KEYCODES[key] == code


@pytest.mark.parametrize('key, code', [(pygame.K_UP, 0xDA), (pygame.K_RCTRL, 0x84), (pygame.K_DELETE, 0xD4)])
def test_special_keys_use_the_byte_keycodes(key: int, code: int) -> None:
    window = PygameWindow()
    window.ensure_open_for_input()
    keyboard = keyboard_on(window)
    post_key(key)
    assert read_status_hex(keyboard) == 0x9
    assert read_byte(keyboard) == code


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


@pytest.mark.parametrize('key', [pygame.K_KP_EQUALS, pygame.K_MENU, pygame.K_SYSREQ, 0xE9, ord('A')])
def test_unmapped_keys_are_ignored(key: int) -> None:
    window = PygameWindow()
    window.ensure_open_for_input()
    keyboard = keyboard_on(window)
    post_key(key)
    assert read_status_hex(keyboard) == 0x0


def test_closing_the_window_raises_keyboard_interrupt() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    pygame.event.post(pygame.event.Event(pygame.QUIT))
    with pytest.raises(KeyboardInterrupt):
        device.window.pump_events()
    assert device.window.closed


# ---------------------------------------------------------------- whole runs


def test_fj_program_presents_into_the_window(tmp_path: Path) -> None:
    fjm_path = assemble_to_path(SCREEN_PROGRAM, tmp_path, use_stl=True)
    device = InteractiveScreen()
    statistics = fjm_run.run(fjm_path, io_device=device, print_time=False)
    assert statistics.termination_cause == TerminationCause.Looping
    assert device.frame_count == 1
    assert device.frame_hashes[0][1] == EXPECTED_FRAME_HASH
    surface = pygame.display.get_surface()
    assert surface.get_at((0, 0))[:3] == (10, 20, 30)  # palette entry 0
    assert surface.get_at((1, 0))[:3] == (200, 100, 0)  # palette entry 1


def test_pc_interactive_owns_one_window_for_keys_and_screen() -> None:
    pc = PcIO.interactive()
    screen, keyboard = pc._screen, pc._keyboard
    assert isinstance(screen, InteractiveScreen)
    assert isinstance(keyboard, KeyboardIO)
    assert keyboard.event_source._window is screen.window  # type: ignore[attr-defined]  # one shared window
    assert not screen.window.is_open  # opens on the program's init-screen command, sized to it


def test_pc_interactive_delegates_keys_in_and_pixels_out() -> None:
    pc = PcIO.interactive()
    ss.feed(pc, ss.init_screen(2, 2, 8) + ss.draw_raw([0, 0, 0, 0], 8) + ss.PRESENT)
    assert pc._screen.frame_count == 1

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
    assert pc._keyboard.event_source._window is pc._screen.window  # type: ignore[attr-defined]


def test_closing_the_window_terminates_a_real_run(tmp_path: Path, engine: str) -> None:
    # QUIT is already pending when the program presents its first frame: the pump raises
    # KeyboardInterrupt inside the run, and the interpreter must turn it into a clean
    # keyboard-interrupt termination (on every engine).
    fjm_path = assemble_to_path(SCREEN_PROGRAM, tmp_path, use_stl=True)
    device = InteractiveScreen()
    device.window.ensure_open(4, 2)  # open early so the QUIT event can be posted
    pygame.event.post(pygame.event.Event(pygame.QUIT))

    statistics = fjm_run.run(fjm_path, io_device=device, print_time=False)
    assert statistics.termination_cause == TerminationCause.KeyboardInterrupt
    assert device.window.closed
    # storage_mode is set only by the native engine - prove the right engine actually ran
    assert (statistics.storage_mode is not None) == (engine == 'native')


# ---------------------------------------------------------------- set_max_fps: only the window waits


class FakeClock:
    """time.perf_counter / time.sleep: sleeping advances the clock. It starts at 0.0 -- perf_counter's starting
    point is undefined, and the first present must not wait whatever it is."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: List[float] = []
        self.on_sleep: List[object] = []  # callables run on each sleep (a test's hook into the wait)

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        for hook in self.on_sleep:
            hook()  # type: ignore[operator]


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(time, 'perf_counter', clock.perf_counter)
    monkeypatch.setattr(time, 'sleep', clock.sleep)
    return clock


def send_max_fps(device: InteractiveScreen, max_fps: int) -> None:
    ss.feed(device, ss.set_max_fps(max_fps))


@pytest.mark.parametrize('max_fps', [20, 25])
def test_max_fps_holds_a_fast_present_until_its_slot(fake_clock: FakeClock, max_fps: int) -> None:
    clock = fake_clock
    device = InteractiveScreen()
    init_2x2_screen(device)
    send_max_fps(device, max_fps)
    present_raw_2x2(device)  # the first present has no predecessor to wait for
    assert clock.slept == []
    first = clock.now
    clock.now += 0.001  # the next frame was cheap: 1 ms of work
    present_raw_2x2(device)
    assert clock.now - first == pytest.approx(1.0 / max_fps)  # it waited out the rest of its slot
    assert max(clock.slept) <= 0.005  # in short slices, pumping the window between them


def test_max_fps_applies_to_an_empty_present(fake_clock: FakeClock) -> None:
    """a present with nothing drawn since the last one still counts as a frame, and waits"""
    clock = fake_clock
    device = InteractiveScreen()
    init_2x2_screen(device)
    send_max_fps(device, 10)
    ss.feed(device, ss.PRESENT)
    first = clock.now
    ss.feed(device, ss.PRESENT)
    assert clock.now - first == pytest.approx(0.1)
    assert device.frame_count == 2


def test_max_fps_does_not_delay_a_slow_frame(fake_clock: FakeClock) -> None:
    clock = fake_clock
    device = InteractiveScreen()
    init_2x2_screen(device)
    send_max_fps(device, 20)
    present_raw_2x2(device)
    clock.now += 0.2  # a heavy frame: slower than the cap already
    present_raw_2x2(device)
    assert clock.slept == []


def test_max_fps_zero_is_no_cap(fake_clock: FakeClock) -> None:
    clock = fake_clock
    device = InteractiveScreen()
    init_2x2_screen(device)
    send_max_fps(device, 20)
    send_max_fps(device, 0)  # the program removed the cap
    for _ in range(3):
        present_raw_2x2(device)
    assert clock.slept == []


def test_max_fps_survives_a_reinit(fake_clock: FakeClock) -> None:
    clock = fake_clock
    device = InteractiveScreen()
    init_2x2_screen(device)
    send_max_fps(device, 20)
    present_raw_2x2(device)
    init_2x2_screen(device)
    present_raw_2x2(device)
    assert sum(clock.slept) == pytest.approx(0.05)


def test_max_fps_draws_only_after_the_wait(fake_clock: FakeClock) -> None:
    clock = fake_clock
    device = InteractiveScreen()
    init_2x2_screen(device)
    drawn_at: List[float] = []
    real_draw = device.window.draw_indexed

    def draw_and_stamp(*args, **kwargs):  # type: ignore[no-untyped-def]
        drawn_at.append(clock.now)
        return real_draw(*args, **kwargs)

    device.window.draw_indexed = draw_and_stamp  # type: ignore[method-assign]
    send_max_fps(device, 20)
    present_raw_2x2(device)
    clock.now += 0.001
    present_raw_2x2(device)
    assert drawn_at[1] - drawn_at[0] == pytest.approx(1.0 / 20)


def test_max_fps_keeps_pumping_the_window_while_it_waits(fake_clock: FakeClock) -> None:
    clock = fake_clock
    device = InteractiveScreen()
    init_2x2_screen(device)
    send_max_fps(device, 1)
    present_raw_2x2(device)
    draws: List[int] = []
    real_draw = device.window.draw_indexed

    def counting_draw(*args, **kwargs):  # type: ignore[no-untyped-def]
        draws.append(1)
        return real_draw(*args, **kwargs)

    device.window.draw_indexed = counting_draw  # type: ignore[method-assign]
    clock.on_sleep.append(lambda: pygame.event.post(pygame.event.Event(pygame.QUIT)))
    with pytest.raises(KeyboardInterrupt):
        present_raw_2x2(device)
    assert clock.now < 0.1  # well inside the 1 s slot: the first slice's pump saw the close
    assert draws == []


# ---------------------------------------------------------------- re-creating the window at a new size


def test_a_close_pending_at_a_resize_still_ends_the_run() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    pygame.event.post(pygame.event.Event(pygame.QUIT))
    with pytest.raises(KeyboardInterrupt):
        ss.feed(device, ss.init_screen(3, 1, 8))
    assert device.window.closed


def test_a_key_pending_at_a_resize_is_still_delivered() -> None:
    window = PygameWindow()
    device = InteractiveScreen(window=window)
    keyboard = keyboard_on(window)
    init_2x2_screen(device)
    post_key(pygame.K_q)
    ss.feed(device, ss.init_screen(3, 1, 8))
    assert pygame.display.get_surface().get_size() == (3, 1)
    assert read_status_hex(keyboard) == 0x9
    assert read_byte(keyboard) == ord('q')


def test_control_a_key_pending_at_the_same_size_is_delivered_too() -> None:
    window = PygameWindow()
    device = InteractiveScreen(window=window)
    keyboard = keyboard_on(window)
    init_2x2_screen(device)
    post_key(pygame.K_q)
    init_2x2_screen(device)
    assert (read_status_hex(keyboard), read_byte(keyboard)) == (0x9, ord('q'))


def test_fullscreen_carries_over_a_resize(monkeypatch: pytest.MonkeyPatch) -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    assert not pygame.display.get_surface().get_flags() & pygame.FULLSCREEN
    monkeypatch.setattr(pygame.display, 'is_fullscreen', lambda: True)  # the dummy driver cannot toggle it
    ss.feed(device, ss.init_screen(3, 1, 8))
    assert pygame.display.get_surface().get_flags() & pygame.FULLSCREEN


def test_a_windowed_resize_stays_windowed() -> None:
    device = InteractiveScreen()
    init_2x2_screen(device)
    ss.feed(device, ss.init_screen(3, 1, 8))
    assert not pygame.display.get_surface().get_flags() & pygame.FULLSCREEN


def test_the_frame_is_recorded_when_it_is_shown(fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    """the frame_hashes time of a held frame is after the max-fps wait, not before it"""
    clock = fake_clock
    monkeypatch.setattr(time, 'time_ns', lambda: round(clock.now * 1e9))
    device = InteractiveScreen()
    init_2x2_screen(device)
    send_max_fps(device, 10)
    present_raw_2x2(device)
    clock.now += 0.01
    present_raw_2x2(device)
    (first, _), (second, _) = device.frame_hashes
    assert second - first == pytest.approx(0.1e9)
