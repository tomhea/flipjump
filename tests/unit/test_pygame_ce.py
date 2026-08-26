"""pygame-ce: the import guard, the indexed blit, and the lazy frame expansion.

Three behaviours introduced when the interactive window moved from upstream pygame to pygame-ce:

  * `_import_pygame` REQUIRES pygame-ce. The two install under the same module name, so a wrong
    install is otherwise completely silent -- everything imports and runs.
  * `PygameWindow.draw_indexed` hands SDL the palette indices instead of a python-expanded RGB
    list. It must paint EXACTLY what `draw` paints; the point is that it is faster, not different.
  * `InMemoryScreen.last_frame_rgb` is now a lazy property, so a windowed run never builds it. It
    must still be a SNAPSHOT of the presented frame, not a live view of the pixel buffer.
"""
import os

import pytest

pygame = pytest.importorskip('pygame')

from flipjump.interpreter.io_devices.ScreenIO import InMemoryScreen        # noqa: E402
from flipjump.interpreter.io_devices import pygame_window                  # noqa: E402
from flipjump.utils.exceptions import IODeviceException                    # noqa: E402

W, H = 8, 4
PALETTE = [(10, 20, 30), (200, 100, 0), (1, 2, 3), (255, 255, 255)]


@pytest.fixture(autouse=True)
def dummy_video():
    old = os.environ.get('SDL_VIDEODRIVER')
    os.environ['SDL_VIDEODRIVER'] = 'dummy'
    yield
    if old is None:
        os.environ.pop('SDL_VIDEODRIVER', None)
    else:
        os.environ['SDL_VIDEODRIVER'] = old


# -- the guard ----------------------------------------------------------------------------------

def test_the_installed_pygame_is_pygame_ce():
    """The whole point of the move. IS_CE is defined only by pygame-ce."""
    assert getattr(pygame, 'IS_CE', 0), (
        'upstream pygame is installed; this build requires pygame-ce '
        '(pip uninstall -y pygame && pip install pygame-ce)')


def test_import_is_refused_when_upstream_pygame_is_installed(monkeypatch):
    """R9 -- the control. Simulate upstream by removing IS_CE and require a LOUD refusal, because
    the failure this guards against is otherwise invisible: both packages import as `pygame`."""
    monkeypatch.delattr(pygame, 'IS_CE', raising=False)
    with pytest.raises(IODeviceException, match='requires pygame-ce'):
        pygame_window._import_pygame()


def test_the_refusal_names_the_fix(monkeypatch):
    monkeypatch.delattr(pygame, 'IS_CE', raising=False)
    with pytest.raises(IODeviceException, match='pip install pygame-ce'):
        pygame_window._import_pygame()


def test_the_guard_passes_on_pygame_ce():
    """Vacuity control for the two above: with IS_CE present the import must SUCCEED, or they
    would pass against a guard that refuses everything."""
    assert pygame_window._import_pygame() is pygame


# -- the indexed blit ---------------------------------------------------------------------------

def _painted(window):
    """what actually landed on the window surface, as RGB tuples."""
    surface = window._screen_surface
    return [surface.get_at((x, y))[:3] for y in range(H) for x in range(W)]


def test_draw_indexed_paints_exactly_what_draw_paints():
    """THE test. The indexed path is an optimisation, so it has to be pixel-identical."""
    indices = bytes([(x * 3 + y) % len(PALETTE) for y in range(H) for x in range(W)])
    rgb = [PALETTE[i] for i in indices]

    a = pygame_window.PygameWindow()
    a.ensure_open(W, H)
    a.draw(W, H, rgb)
    expanded = _painted(a)
    a.close()

    b = pygame_window.PygameWindow()
    b.ensure_open(W, H)
    b.draw_indexed(W, H, indices, PALETTE)
    indexed = _painted(b)
    b.close()

    assert indexed == expanded
    assert len(set(indexed)) > 1, 'a blank frame would make this vacuous'


def test_draw_indexed_on_a_closed_window_is_a_no_op():
    window = pygame_window.PygameWindow()
    window.draw_indexed(W, H, bytes(W * H), PALETTE)      # never opened -- must not raise


# -- the lazy frame -----------------------------------------------------------------------------

def _present_one(device, indices):
    device.width, device.height, device.bpp = W, H, 8
    device.palette = list(PALETTE)
    device.pixel_indices = list(indices)
    device._present()


def test_last_frame_rgb_expands_the_presented_frame():
    device = InMemoryScreen()
    _present_one(device, [1] * (W * H))
    assert device.last_frame_rgb == [PALETTE[1]] * (W * H)


def test_last_frame_rgb_is_a_snapshot_not_a_live_view():
    """It used to be built eagerly at present time; making it lazy must not turn it into a window
    onto whatever the device has been filling in SINCE. 0x0B frames mutate pixel_indices between
    presents, so this is the difference that would matter."""
    device = InMemoryScreen()
    _present_one(device, [1] * (W * H))
    before = device.last_frame_rgb
    device.pixel_indices = [2] * (W * H)                  # the NEXT frame starts arriving
    assert device.last_frame_rgb == before
    assert device.last_frame_rgb == [PALETTE[1]] * (W * H)


def test_the_palette_is_snapshotted_too():
    device = InMemoryScreen()
    _present_one(device, [0] * (W * H))
    device.palette = [(9, 9, 9)] * len(PALETTE)
    assert device.last_frame_rgb == [PALETTE[0]] * (W * H)


def test_before_any_present_it_is_empty():
    assert InMemoryScreen().last_frame_rgb == []


def test_an_index_past_the_palette_is_black():
    device = InMemoryScreen()
    _present_one(device, [200] * (W * H))
    assert device.last_frame_rgb == [(0, 0, 0)] * (W * H)
