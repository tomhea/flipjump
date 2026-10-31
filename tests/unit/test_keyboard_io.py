"""
unit-tests for the keyboard input-device.

the keyboard is non-blocking and runs on virtual time: the FJ program polls one status HEX
per tic - 0x0 = no event, 0x8 = key released, 0x9 = key pressed. on an event the keycode
byte follows through the input stream. the event source is pluggable: a scripted event
file makes replays deterministic; idle polling never EOFs. the keycodes are io_devices/README.md's.
"""

import re
from pathlib import Path
from typing import List, Set, Tuple

from flipjump.interpreter.io_devices.KeyboardIO import (
    KeyEvent,
    KeyboardIO,
    ScriptedKeyEventSource,
)
from flipjump.interpreter.io_devices.pygame_window import (
    HID_KEYCODE_BASE,
    KEYCODE_DOWN,
    KEYCODE_LEFT,
    KEYCODE_LEFT_ALT,
    KEYCODE_LEFT_CTRL,
    KEYCODE_LEFT_GUI,
    KEYCODE_LEFT_SHIFT,
    KEYCODE_RIGHT,
    KEYCODE_RIGHT_ALT,
    KEYCODE_RIGHT_CTRL,
    KEYCODE_RIGHT_GUI,
    KEYCODE_RIGHT_SHIFT,
    KEYCODE_UP,
    KEYCODES,
)


def read_byte(device: KeyboardIO) -> int:
    value = 0
    for i in range(8):
        value |= int(device.read_bit()) << i
    return value


def read_status_hex(device: KeyboardIO) -> int:
    """read the 4-bit (lsb-first) status hex - what hex.input_hex sees."""
    value = 0
    for i in range(4):
        value |= int(device.read_bit()) << i
    return value


class TestScriptedKeyEventSource:
    def test_parses_event_file_lines(self) -> None:
        source = ScriptedKeyEventSource.from_text('0, down, 72\n2, up, 72\n# comment\n\n5, down, 0x20\n')
        assert source.events == [KeyEvent(0, True, 72), KeyEvent(2, False, 72), KeyEvent(5, True, 0x20)]

    def test_accepts_numeric_down_up(self) -> None:
        source = ScriptedKeyEventSource.from_text('3, 1, 10\n4, 0, 11\n')
        assert source.events == [KeyEvent(3, True, 10), KeyEvent(4, False, 11)]


class TestKeyboard:
    def test_replay_is_deterministic_and_never_eofs(self) -> None:
        source = ScriptedKeyEventSource.from_text('0, down, 72\n2, up, 72\n')
        device = KeyboardIO(source)

        # tic 0: key-down event -> status 0x9, then the keycode byte
        assert read_status_hex(device) == 0x9
        assert read_byte(device) == 72
        # tic 1: nothing -> status 0x0
        assert read_status_hex(device) == 0x0
        # tic 2: the key-up event -> status 0x8
        assert read_status_hex(device) == 0x8
        assert read_byte(device) == 72
        # idle forever - one 0x0 status hex per tic, never raises IOReadOnEOF
        for _ in range(50):
            assert read_status_hex(device) == 0x0

    def test_late_events_are_delivered_in_order(self) -> None:
        # two events on the same tic: the second is delivered on the next poll
        source = ScriptedKeyEventSource.from_text('0, down, 1\n0, down, 2\n')
        device = KeyboardIO(source)
        assert (read_status_hex(device), read_byte(device)) == (0x9, 1)
        assert (read_status_hex(device), read_byte(device)) == (0x9, 2)
        assert read_status_hex(device) == 0x0


# ---------------------------------------------------------------- the keycodes (io_devices/README.md)

README = Path(__file__).resolve().parents[2] / 'flipjump' / 'interpreter' / 'io_devices' / 'README.md'


def readme_keycode_rows() -> List[Tuple[Set[int], str]]:
    """the README's keycode table: (the codes of a row, its key names). `0xA`-`0xB` is a range."""
    text = README.read_text(encoding='utf-8')
    table = text[text.index('| Code | Key |') : text.index('A program that wants')]  # noqa: E203
    rows = []
    for line in table.splitlines()[2:]:
        if not line.startswith('|'):
            continue
        codes_cell, names = line.strip('|').split('|')
        codes: Set[int] = set()
        for match in re.finditer(r'`(0x[0-9A-F]{2})`(?:\u2013`(0x[0-9A-F]{2})`)?', codes_cell):
            first = int(match.group(1), 16)
            last = int(match.group(2), 16) if match.group(2) else first
            codes |= set(range(first, last + 1))
        rows.append((codes, names.strip()))
    return rows


def test_the_readme_table_parses() -> None:
    rows = readme_keycode_rows()
    assert len(rows) == 15
    assert rows[0] == ({0x08, 0x09, 0x0D, 0x1B}, 'Backspace, Tab, Enter, Esc')
    assert rows[1][0] == set(range(0x20, 0x7F))
    assert rows[9] == ({0xD7, 0xD8, 0xD9, 0xDA}, '**Right, Left, Down, Up**')
    assert rows[-1] == (set(range(0xF0, 0xFC)), 'F13–F24')


def test_the_window_key_table_delivers_exactly_the_readmes_codes() -> None:
    """KEYCODES (pygame_window, the one table) against the README: every code the README lists is
    delivered by some key, nothing else is - except 'A'-'Z', which no key sends (letters are lowercase)."""
    readme_codes = set().union(*(codes for codes, _ in readme_keycode_rows()))
    assert set(KEYCODES.values()) == readme_codes - set(range(ord('A'), ord('Z') + 1))
    assert len(set(KEYCODES.values())) == len(KEYCODES)  # one key per code


def test_control_a_moved_code_breaks_the_table_check() -> None:
    """the negative control: the check above must reject a table with one key moved (Up at 0xDB)"""
    moved = {key: (0xDB if code == 0xDA else code) for key, code in KEYCODES.items()}
    readme_codes = set().union(*(codes for codes, _ in readme_keycode_rows()))
    assert set(moved.values()) != readme_codes - set(range(ord('A'), ord('Z') + 1))


def test_the_named_keycodes_are_the_readmes() -> None:
    modifiers = [KEYCODE_LEFT_CTRL, KEYCODE_LEFT_SHIFT, KEYCODE_LEFT_ALT, KEYCODE_LEFT_GUI]
    modifiers += [KEYCODE_RIGHT_CTRL, KEYCODE_RIGHT_SHIFT, KEYCODE_RIGHT_ALT, KEYCODE_RIGHT_GUI]
    assert modifiers == list(range(0x80, 0x88))
    assert (KEYCODE_RIGHT, KEYCODE_LEFT, KEYCODE_DOWN, KEYCODE_UP) == (0xD7, 0xD8, 0xD9, 0xDA)
    assert KEYCODE_UP == HID_KEYCODE_BASE + 0x52  # HID usage 0x52 is Up Arrow


# the key table as 1.6.0 spelled it out, entry by entry: the computed KEYCODES must be exactly it
_SDL = 1 << 30
_SPELLED_OUT_KEYCODES = {
    0x08: 0x08,
    0x09: 0x09,
    0x0D: 0x0D,
    0x1B: 0x1B,
    **{code: code for code in range(0x20, 0x7F) if not ord('A') <= code <= ord('Z')},
    0x7F: 0x88 + 0x4C,
    _SDL | 0xE0: 0x80,
    _SDL | 0xE1: 0x81,
    _SDL | 0xE2: 0x82,
    _SDL | 0xE3: 0x83,
    _SDL | 0xE4: 0x84,
    _SDL | 0xE5: 0x85,
    _SDL | 0xE6: 0x86,
    _SDL | 0xE7: 0x87,
    **{_SDL | usage: 0x88 + usage for usage in range(0x39, 0x4C)},
    **{_SDL | usage: 0x88 + usage for usage in range(0x4D, 0x64)},
    _SDL | 0x65: 0x88 + 0x65,
    **{_SDL | usage: 0x88 + usage for usage in range(0x68, 0x74)},
}


def test_the_key_table_is_the_spelled_out_one() -> None:
    assert KEYCODES == _SPELLED_OUT_KEYCODES and len(KEYCODES) == 137
