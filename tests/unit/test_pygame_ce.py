"""
unit-tests for the pygame-ce window backend (pygame_window.py), run headless with SDL's dummy video driver.

pinned: _import_pygame requires pygame-ce (both install as the `pygame` module, so a wrong install would otherwise
import and run silently), and draw_indexed - SDL gets the palette indices, not an expanded RGB list - paints exactly
what draw paints.
"""

from typing import List, Tuple

import pytest

pygame = pytest.importorskip('pygame')

from flipjump.interpreter.io_devices import pygame_window  # noqa: E402
from flipjump.utils.exceptions import IODeviceException  # noqa: E402

W, H = 8, 4
PALETTE = [(10, 20, 30), (200, 100, 0), (1, 2, 3), (255, 255, 255)]


@pytest.fixture(autouse=True)
def dummy_video(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SDL_VIDEODRIVER', 'dummy')


# --- the import guard ---


def test_the_installed_pygame_is_pygame_ce() -> None:
    # IS_CE is defined only by pygame-ce
    assert getattr(pygame, 'IS_CE', 0), (
        'upstream pygame is installed; this build requires pygame-ce '
        '(pip uninstall -y pygame && pip install pygame-ce)'
    )


def test_import_is_refused_when_upstream_pygame_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    # upstream pygame, simulated by removing IS_CE: a refusal that names the fix
    monkeypatch.delattr(pygame, 'IS_CE', raising=False)
    with pytest.raises(IODeviceException, match='requires pygame-ce.*pip install pygame-ce'):
        pygame_window._import_pygame()


def test_the_guard_passes_on_pygame_ce() -> None:
    # the control of the test above: a guard that refused everything would pass it
    assert pygame_window._import_pygame() is pygame


# --- the indexed blit ---


def _painted(window: pygame_window.PygameWindow) -> List[Tuple[int, int, int]]:
    """what landed on the window surface, as RGB tuples"""
    surface = window._screen_surface
    assert surface is not None
    return [tuple(surface.get_at((x, y))[:3]) for y in range(H) for x in range(W)]


def test_draw_indexed_paints_exactly_what_draw_paints() -> None:
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


def test_draw_indexed_on_a_closed_window_is_a_no_op() -> None:
    window = pygame_window.PygameWindow()
    window.draw_indexed(W, H, bytes(W * H), PALETTE)  # never opened: must not raise
