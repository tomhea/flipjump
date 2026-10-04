"""
unit-tests for the 0x0B begin_frame_collines grammar of the InMemoryScreen device.

the grammar (see the ScreenIO module docstring): a record tag opens column x (or 0xFF ends the
frame); inside the column's list, [y2][colour] pairs fill rows [cursor, y2), 0xFF ends the column,
0xFE copies the whole column x-1 and ends it, and - on a screen of at most
COLLINES_TOKENS_MAX_HEIGHT rows - [0xFD][y] copies rows [cursor, y) from column x-1 (PARTIAL DITTO)
and [0xFC][y] leaves rows [cursor, y) as they are (KEEP), both moving the cursor to y and letting
the list continue.

every behaviour is a CHECK - a function of the decoder class - grouped as
  baseline  the grammar as it was before the two tokens
  partial   PARTIAL DITTO (0xFD)
  keep      KEEP (0xFC)
  compat    the tokens do not exist on a taller screen, and a legacy stream decodes as it did
each check runs against the real InMemoryScreen. the NEGATIVE CONTROLS at the bottom (R9) mutate
the real decoder's source and require the checks to reject the mutant: a suite that cannot fail
proves nothing.
"""

import hashlib
import inspect
import random
import textwrap
from typing import Callable, Dict, List, Sequence, Tuple, Type

import pytest

from flipjump.interpreter.io_devices import ScreenIO
from flipjump.interpreter.io_devices.ScreenIO import (
    COLLINES_DITTO,
    COLLINES_END,
    COLLINES_KEEP,
    COLLINES_PARTIAL_DITTO,
    COLLINES_TOKENS_MAX_HEIGHT,
    InMemoryScreen,
)
from flipjump.utils.exceptions import IODeviceException

ScreenClass = Type[InMemoryScreen]
Check = Callable[[ScreenClass], None]

BEGIN = 0x0B
END, DITTO, PARTIAL, KEEP = COLLINES_END, COLLINES_DITTO, COLLINES_PARTIAL_DITTO, COLLINES_KEEP
W, H = 4, 6  # the small screen most checks use


# ---------------------------------------------------------------- helpers


def feed(screen: InMemoryScreen, data: Sequence[int]) -> None:
    """the device takes BITS, lsb first - feed it exactly as the interpreter would."""
    for byte in data:
        for i in range(8):
            screen.write_bit((byte >> i) & 1 == 1)


def new_screen(cls: ScreenClass, width: int = W, height: int = H, bpp: int = 8) -> InMemoryScreen:
    """a headless screen after init_screen ([0x01][w:2][h:2][bpp][palette_size:2], palette 0)."""
    screen = cls()
    feed(screen, [0x01, width & 0xFF, width >> 8, height & 0xFF, height >> 8, bpp, 0, 0])
    return screen


def column(screen: InMemoryScreen, x: int) -> List[int]:
    return [screen.pixel_indices[row * screen.width + x] for row in range(screen.height)]


def base_colour(x: int, row: int) -> int:
    """the base picture: every pixel distinct, so a copy from the wrong place always shows."""
    return (16 * x + row) & 0xFF


def with_base_picture(cls: ScreenClass, width: int = W, height: int = H) -> InMemoryScreen:
    """a screen whose presented frame is the base picture, painted by one 0x0B frame of pairs."""
    screen = new_screen(cls, width, height)
    frame = [BEGIN]
    for x in range(width):
        frame.append(x)
        for row in range(height):
            frame += [row + 1, base_colour(x, row)]
        frame.append(END)
    frame.append(END)
    feed(screen, frame)
    assert screen.frame_count == 1
    return screen


def base_column(x: int, height: int = H) -> List[int]:
    return [base_colour(x, row) for row in range(height)]


CHECKS: Dict[str, List[Check]] = {'baseline': [], 'partial': [], 'keep': [], 'compat': []}


def check(group: str) -> Callable[[Check], Check]:
    def register(function: Check) -> Check:
        CHECKS[group].append(function)
        return function

    return register


# ---------------------------------------------------------------- baseline: the grammar before the tokens


@check('baseline')
def pairs_fill_from_the_cursor_and_the_tail_keeps_last_frame(cls: ScreenClass) -> None:
    screen = with_base_picture(cls)
    feed(screen, [BEGIN, 1, 2, 7, 5, 9, END, END])
    assert column(screen, 1) == [7, 7, 9, 9, 9] + base_column(1)[5:]
    for x in (0, 2, 3):  # columns the frame never mentions keep last frame's pixels
        assert column(screen, x) == base_column(x)
    assert screen.frame_count == 2


@check('baseline')
def full_ditto_copies_every_row_and_ends_the_column(cls: ScreenClass) -> None:
    screen = with_base_picture(cls)
    feed(screen, [BEGIN, 2, DITTO, 3, 3, 1, END, END])  # after the DITTO, `3` is the next record's tag
    assert column(screen, 2) == base_column(1)
    assert column(screen, 3) == [1, 1, 1] + base_column(3)[3:]


@check('baseline')
def full_ditto_after_pairs_still_copies_every_row(cls: ScreenClass) -> None:
    screen = with_base_picture(cls)
    feed(screen, [BEGIN, 2, 3, 9, DITTO, END])
    assert column(screen, 2) == base_column(1)


@check('baseline')
def end_at_tag_position_presents_and_leaves_the_mode(cls: ScreenClass) -> None:
    screen = new_screen(cls)
    feed(screen, [BEGIN, 0, H, 3, END])
    assert screen.frame_count == 0  # a column's end is not the frame's
    feed(screen, [END])
    assert screen.frame_count == 1
    assert len(screen.frame_hashes) == 1
    feed(screen, [0x05] + [1] * (W * H))  # the next byte is a COMMAND again (update_screen_raw)
    assert screen.frame_count == 2 and screen.pixel_indices == [1] * (W * H)


@check('baseline')
def pair_colour_is_masked_to_bpp(cls: ScreenClass) -> None:
    screen = new_screen(cls, bpp=4)
    feed(screen, [BEGIN, 0, H, 0xA7, END, END])
    assert column(screen, 0) == [0x7] * H


@check('baseline')
def baseline_errors(cls: ScreenClass) -> None:
    with pytest.raises(IODeviceException, match='behind the fill cursor'):
        feed(new_screen(cls), [BEGIN, 1, 4, 1, 3])
    with pytest.raises(IODeviceException, match='past the 6-row screen'):
        feed(new_screen(cls), [BEGIN, 1, H + 1])
    with pytest.raises(IODeviceException, match='DITTO for column 0'):
        feed(new_screen(cls), [BEGIN, 0, DITTO])
    with pytest.raises(IODeviceException, match='outside the 4-column screen'):
        feed(new_screen(cls), [BEGIN, W])
    with pytest.raises(IODeviceException, match='not initialized'):
        feed(cls(), [BEGIN])


# ---------------------------------------------------------------- PARTIAL DITTO (0xFD)


@check('partial')
def partial_ditto_top_rows_then_pairs_then_the_tail(cls: ScreenClass) -> None:
    screen = with_base_picture(cls)
    feed(screen, [BEGIN, 2, PARTIAL, 3, 5, 9, END, END])
    assert column(screen, 2) == base_column(1)[:3] + [9, 9] + base_column(2)[5:]
    assert column(screen, 1) == base_column(1)  # the source is only read


@check('partial')
def partial_ditto_mid_list_copies_only_cursor_to_y(cls: ScreenClass) -> None:
    screen = with_base_picture(cls)
    feed(screen, [BEGIN, 2, 2, 7, PARTIAL, 4, END, END])
    assert column(screen, 2) == [7, 7] + base_column(1)[2:4] + base_column(2)[4:]


@check('partial')
def partial_ditto_to_the_cursor_is_a_no_op(cls: ScreenClass) -> None:
    screen = with_base_picture(cls)
    feed(screen, [BEGIN, 2, 3, 7, PARTIAL, 3, END, END])
    assert column(screen, 2) == [7, 7, 7] + base_column(2)[3:]
    screen = with_base_picture(cls)
    feed(screen, [BEGIN, 2, PARTIAL, 0, END, END])  # at the top of the column, too
    assert column(screen, 2) == base_column(2)


@check('partial')
def partial_ditto_to_the_bottom_row_copies_the_whole_column(cls: ScreenClass) -> None:
    screen = with_base_picture(cls)
    feed(screen, [BEGIN, 3, PARTIAL, H, H, 5, END, END])  # the list continues: an empty pair is legal
    assert column(screen, 3) == base_column(2)


@check('partial')
def partial_ditto_copies_the_left_column_as_the_stream_left_it(cls: ScreenClass) -> None:
    screen = with_base_picture(cls)
    feed(screen, [BEGIN, 1, H, 3, END, 2, PARTIAL, 4, END, END])
    assert column(screen, 2) == [3, 3, 3, 3] + base_column(2)[4:]


@check('partial')
def partial_ditto_and_keep_chain_in_one_list(cls: ScreenClass) -> None:
    screen = with_base_picture(cls)
    feed(screen, [BEGIN, 2, PARTIAL, 2, KEEP, 4, PARTIAL, H, END, END])
    assert column(screen, 2) == base_column(1)[:2] + base_column(2)[2:4] + base_column(1)[4:]


@check('partial')
def partial_ditto_state_does_not_leak_into_the_next_column(cls: ScreenClass) -> None:
    screen = with_base_picture(cls)
    # the row byte after 0xFD is consumed as a row; the bytes after it are pairs / END as usual
    feed(screen, [BEGIN, 1, PARTIAL, 1, END, 2, 2, 8, END, END])
    assert column(screen, 1) == base_column(0)[:1] + base_column(1)[1:]
    assert column(screen, 2) == [8, 8] + base_column(2)[2:]


@check('partial')
def partial_ditto_errors(cls: ScreenClass) -> None:
    with pytest.raises(IODeviceException, match='PARTIAL DITTO for column 0'):
        feed(new_screen(cls), [BEGIN, 0, PARTIAL])
    with pytest.raises(IODeviceException, match='behind the fill cursor'):
        feed(new_screen(cls), [BEGIN, 1, 4, 1, PARTIAL, 3])
    with pytest.raises(IODeviceException, match='past the 6-row screen'):
        feed(new_screen(cls), [BEGIN, 1, PARTIAL, H + 1])
    with pytest.raises(IODeviceException, match='past the 6-row screen'):
        feed(new_screen(cls), [BEGIN, 1, PARTIAL, END])  # 0xFF is a row byte here, not the column's end


# ---------------------------------------------------------------- KEEP (0xFC)


@check('keep')
def keep_leaves_last_frames_rows_across_frames(cls: ScreenClass) -> None:
    screen = new_screen(cls)
    feed(screen, [BEGIN, 2, H, 5, END, END])  # frame 1: column 2 is all 5
    feed(screen, [BEGIN, 2, KEEP, 3, 4, 8, END, END])  # frame 2: keep [0, 3), paint row 3, tail kept
    assert column(screen, 2) == [5, 5, 5, 8, 5, 5]
    assert screen.frame_count == 2


@check('keep')
def keep_mid_list_and_at_column_0(cls: ScreenClass) -> None:
    screen = with_base_picture(cls)
    feed(screen, [BEGIN, 0, 1, 9, KEEP, 4, H, 6, END, END])  # column 0 has no left neighbour - KEEP needs none
    assert column(screen, 0) == [9] + base_column(0)[1:4] + [6, 6]


@check('keep')
def keep_moves_the_cursor(cls: ScreenClass) -> None:
    with pytest.raises(IODeviceException, match='behind the fill cursor'):
        feed(new_screen(cls), [BEGIN, 1, KEEP, 4, 3])  # a pair ending above the KEEP's row


@check('keep')
def keep_errors(cls: ScreenClass) -> None:
    with pytest.raises(IODeviceException, match='behind the fill cursor'):
        feed(new_screen(cls), [BEGIN, 1, 4, 1, KEEP, 3])
    with pytest.raises(IODeviceException, match='past the 6-row screen'):
        feed(new_screen(cls), [BEGIN, 1, KEEP, H + 1])


# ---------------------------------------------------------------- compatibility


TALL = 252  # the first height on which 0xFC is a legal y2 (y2 <= H)


@check('compat')
def tall_screen_reads_0xfc_as_a_plain_y2(cls: ScreenClass) -> None:
    screen = new_screen(cls, width=3, height=TALL)
    feed(screen, [BEGIN, 1, KEEP, 7, END, END])
    assert column(screen, 1) == [7] * TALL


@check('compat')
def tall_screen_reads_0xfd_as_a_y2_past_the_screen(cls: ScreenClass) -> None:
    with pytest.raises(IODeviceException, match='past the 252-row screen'):
        feed(new_screen(cls, width=3, height=TALL), [BEGIN, 1, PARTIAL])


@check('compat')
def the_bound_screen_has_both_tokens(cls: ScreenClass) -> None:
    height = COLLINES_TOKENS_MAX_HEIGHT
    screen = new_screen(cls, width=3, height=height)
    feed(screen, [BEGIN, 0, height, 4, END, 1, PARTIAL, 2, KEEP, height, END, END])
    assert column(screen, 1) == [4, 4] + [0] * (height - 2)


@check('compat')
def the_token_values_and_their_bound(cls: ScreenClass) -> None:
    # a build checks for these names to refuse emitting the tokens against an older device
    assert (COLLINES_PARTIAL_DITTO, COLLINES_KEEP, COLLINES_TOKENS_MAX_HEIGHT) == (0xFD, 0xFC, 0xFB)
    # on a recognising screen, no token byte can be a legal y2 (y2 <= H <= the bound)
    assert COLLINES_TOKENS_MAX_HEIGHT < min(COLLINES_KEEP, COLLINES_PARTIAL_DITTO, COLLINES_DITTO, COLLINES_END)


def legacy_stream(seed: int = 2026_10_04, frames: int = 3, width: int = 160, height: int = 100) -> List[int]:
    """a deterministic stream in the PRE-TOKEN grammar, shaped like the shipped renderer's frames
    (doom's 160x100 screen, columns left to right, ~35% dittos, some columns cut short or skipped)."""
    # a fixed test vector, not a security use of randomness
    rng = random.Random(seed)  # nosec B311
    out = [0x01, width & 0xFF, width >> 8, height & 0xFF, height >> 8, 8, 0, 0]
    for _ in range(frames):
        out.append(BEGIN)
        for x in range(width):
            roll = rng.random()
            if roll < 0.08:
                continue
            out.append(x)
            if x > 0 and roll < 0.43:
                out.append(DITTO)
                continue
            cursor = 0
            while cursor < height and rng.random() < 0.93:
                cursor = rng.randint(cursor, min(height, cursor + 24))
                out += [cursor, rng.randrange(256)]
            out.append(END)
        out.append(END)
    return out


# sha256 over the presented frames' hashes, frozen with the decoder BEFORE the two tokens existed
# (flipjump 1.5.1 bc8ee63): the change must leave every legacy stream's pixels where they were.
LEGACY_STREAM_DIGEST = '6f6bfb9d3a557da0127d98268e3ed29ebd20e1ac85fc3783565b64ea77b60890'


def legacy_digest(cls: ScreenClass) -> str:
    screen = cls()
    feed(screen, legacy_stream())
    assert screen.frame_count == 3
    return hashlib.sha256(''.join(frame_hash for _, frame_hash in screen.frame_hashes).encode()).hexdigest()


@check('compat')
def legacy_stream_decodes_to_the_frozen_pixels(cls: ScreenClass) -> None:
    assert legacy_digest(cls) == LEGACY_STREAM_DIGEST


# ---------------------------------------------------------------- the suite against the real decoder


ALL_CHECKS: List[Tuple[str, Check]] = [(group, fn) for group, fns in CHECKS.items() for fn in fns]


@pytest.mark.parametrize('group, check_fn', ALL_CHECKS, ids=[f'{g}-{fn.__name__}' for g, fn in ALL_CHECKS])
def test_collines(group: str, check_fn: Check) -> None:
    check_fn(InMemoryScreen)


# ---------------------------------------------------------------- negative controls (R9)


def mutant(old: str, new: str) -> ScreenClass:
    """InMemoryScreen with ONE textual edit to the real `_handle_collines_byte`. The edit must match
    exactly once, so a control whose target drifted away fails loudly instead of testing nothing."""
    source = textwrap.dedent(inspect.getsource(InMemoryScreen._handle_collines_byte))
    assert source.count(old) == 1, f'the mutation target {old!r} is not in the decoder exactly once'
    namespace: Dict[str, object] = {}
    # exec of the decoder's OWN source with one asserted edit - the R9 mutation, not untrusted input
    exec(compile(source.replace(old, new), '<mutant>', 'exec'), dict(vars(ScreenIO)), namespace)  # nosec B102
    return type('MutantScreen', (InMemoryScreen,), {'_handle_collines_byte': namespace['_handle_collines_byte']})


def failing_checks(cls: ScreenClass, group: str) -> List[str]:
    failed = []
    for check_fn in CHECKS[group]:
        try:
            check_fn(cls)
        # an assertion, a pytest.raises that did not raise (pytest's Failed is not an Exception),
        # or an unexpected exception such as a wrong-row IndexError: all of them reject the mutant
        except (Exception, pytest.fail.Exception):
            failed.append(check_fn.__name__)
    return failed


def test_negative_control_partial_ditto_one_row_too_many() -> None:
    """a decoder whose PARTIAL DITTO copies [cursor, y] instead of [cursor, y)."""
    cls = mutant('range(self._collines_row, byte)', 'range(self._collines_row, byte + 1)')
    assert failing_checks(cls, 'baseline') == []  # the mutation is narrow: the old grammar is untouched
    failed = failing_checks(cls, 'partial')
    assert 'partial_ditto_to_the_cursor_is_a_no_op' in failed, failed
    assert 'partial_ditto_mid_list_copies_only_cursor_to_y' in failed, failed


def test_negative_control_tokens_on_a_too_tall_screen() -> None:
    """a decoder that recognises 0xFD / 0xFC whatever the screen height."""
    cls = mutant('self.height <= COLLINES_TOKENS_MAX_HEIGHT', 'True')
    assert failing_checks(cls, 'baseline') == []
    assert failing_checks(cls, 'partial') == [] and failing_checks(cls, 'keep') == []
    failed = failing_checks(cls, 'compat')
    assert 'tall_screen_reads_0xfc_as_a_plain_y2' in failed, failed
    assert 'tall_screen_reads_0xfd_as_a_y2_past_the_screen' in failed, failed


def test_negative_control_legacy_digest_sees_a_one_pixel_change() -> None:
    """the frozen legacy digest is not vacuous: a DITTO that skips the bottom row changes it."""
    cls = mutant('for row in range(self.height):', 'for row in range(self.height - 1):')
    assert legacy_digest(cls) != LEGACY_STREAM_DIGEST
