"""
unit-tests for the stl's device macros: flipjump/stl/screen.fj (the screen commands) and hex.poll_key (the
keyboard poll), run through the real devices - InMemoryScreen and a scripted KeyboardIO.

The bytes are io_devices/README.md's: its own example stream, byte for byte, and every command against
tests/unit/screen_stream.py (written from the README alone). Each device has a negative control: a broken copy
of the macro, defined in the test program, must fail the same check.
"""

from pathlib import Path
from typing import List

import pytest

from flipjump import assemble, run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.interpreter.io_devices.KeyboardIO import KeyboardIO, KeyEvent, ScriptedKeyEventSource
from flipjump.interpreter.io_devices.ScreenIO import EGA16, InMemoryScreen
from flipjump.utils.exceptions import IODeviceException
from tests.unit import screen_stream as ss


def run_program(tmp_path: Path, text: str, device: IODevice) -> None:
    program = tmp_path / 'program.fj'
    program.write_text(text)
    fjm = tmp_path / 'program.fjm'
    assemble([program], fjm, memory_width=64, print_time=False)
    run(fjm, io_device=device, print_time=False, print_termination=False)


def output_bytes(tmp_path: Path, text: str) -> bytes:
    device = FixedIO(b'')
    run_program(tmp_path, text, device)
    return device.get_output()


# ---------------------------------------------------------------- the screen

# io_devices/README.md's example: a 4x3 screen, 8-bit colour, column 0 filled with colour 5, column 1 copied.
README_EXAMPLE = bytes.fromhex('01 0400 0300 08' '11' '30 05' '1F' '3D' 'FF' '1F')

# the README's example: the commands by the macros, the column records as the program's own output.
README_EXAMPLE_PROGRAM = '''
stl.startup
screen.init_screen 4, 3, 8
screen.draw_collines
stl.output_char 0x30
stl.output_char 0x05
stl.output_char 0x1F
stl.output_char 0x3D
screen.output_bits 4, 0xF
screen.end_frame 4, 4
stl.loop
'''


def test_the_readme_example_is_byte_exact(tmp_path: Path) -> None:
    assert output_bytes(tmp_path, README_EXAMPLE_PROGRAM) == README_EXAMPLE


def test_the_readme_example_draws_on_the_screen(tmp_path: Path) -> None:
    screen = InMemoryScreen()
    run_program(tmp_path, README_EXAMPLE_PROGRAM, screen)
    assert (screen.width, screen.height, screen.frame_count) == (4, 3, 1)
    assert [[screen.pixel_indices[4 * y + x] for x in range(4)] for y in range(3)] == [[5, 5, 0, 0]] * 3


def test_end_frame_negative_control(tmp_path: Path) -> None:
    """an end_frame that ends END_CMD a bit early: not the README's bytes, and the screen refuses the stream."""
    broken = README_EXAMPLE_PROGRAM.replace('screen.end_frame 4, 4', 'broken_end_frame') + '''
def broken_end_frame {
    screen.output_bits 3, 0x7
    stl.output_bit 0
    screen.present
}
'''
    assert output_bytes(tmp_path, broken) != README_EXAMPLE
    with pytest.raises(IODeviceException):
        run_program(tmp_path, broken, InMemoryScreen())


@pytest.mark.parametrize('screen_format', [3, 4, 5, 8])
def test_define_palette_packs_its_fields_at_the_format_width(tmp_path: Path, screen_format: int) -> None:
    colours = [(0x10, 0x20, 0x30), (0xA1, 0xB2, 0xC3), (0xFF, 0x00, 0x7F)]
    rgb = int.from_bytes(bytes(channel for colour in colours for channel in colour), 'little')
    program = f'''
stl.startup
screen.init_screen 2, 1, {screen_format}
screen.define_palette {screen_format}, 1, 2, 4, {hex(rgb)}
stl.loop
'''
    expected = ss.init_screen(2, 1, screen_format) + ss.define_palette(1, 2, colours, screen_format)
    assert output_bytes(tmp_path, program) == expected
    screen = InMemoryScreen()
    run_program(tmp_path, program, screen)
    assert screen.palettes[1][2:5] == colours


SETUP_TITLE = 'stl screen'
SETUP_PROGRAM = f'''
stl.startup
screen.init_screen 2, 2, 4
screen.set_window_title {hex(int.from_bytes(SETUP_TITLE.encode(), 'little'))}
screen.set_max_fps 30
screen.builtin_palette 3, 2
screen.define_palette 4, 1, 0, 0, 0x332211
screen.select_palette 1
screen.set_window_icon_header 1, 1
screen.output_bits 8, 0
screen.draw_collines
screen.output_bits 4, 0
screen.output_bits 4, 2
screen.output_bits 4, 0
screen.output_bits 4, 0xF
screen.end_frame 2, 0
stl.loop
'''


def test_the_setup_commands_are_byte_exact(tmp_path: Path) -> None:
    """the icon's one colour (B = 4) and the column record (x, y2, colour, END: R = C = 4) are the program's."""
    expected = (
        ss.init_screen(2, 2, 4)
        + ss.set_window_title(SETUP_TITLE.encode())
        + ss.set_max_fps(30)
        + ss.builtin_palette(3, 2)
        + ss.define_palette(1, 0, [(0x11, 0x22, 0x33)], 4)
        + ss.select_palette(1)
        + ss.set_window_icon(1, 1, [0], 4)
        + ss.command(0x11, (0, 4), (2, 4), (0, 4), (0xF, 4), (0xF, 4))
        + ss.PRESENT
    )
    assert output_bytes(tmp_path, SETUP_PROGRAM) == expected


def test_the_setup_commands_reach_the_screen(tmp_path: Path) -> None:
    screen = InMemoryScreen()
    run_program(tmp_path, SETUP_PROGRAM, screen)
    assert screen.window_title == SETUP_TITLE
    assert screen.max_fps == 30
    assert screen.palettes[3] == list(EGA16)
    assert screen.window_icon == (1, 1, [(0x11, 0x22, 0x33)])
    assert screen.frame_count == 1
    assert screen.palette[0] == (0x11, 0x22, 0x33)  # presented through slot 1


DRAW_RAW_PROGRAM = '''
stl.startup
screen.init_screen 3, 1, 4
screen.draw_raw
screen.output_bits 4, 0x5
screen.output_bits 4, 0xC
screen.output_bits 4, 0x7
screen.output_bits 4, 0
screen.present
stl.loop
'''


def test_draw_raw_is_byte_exact(tmp_path: Path) -> None:
    """the colours (B = 4, row-major) and the zero bits to the byte are the program's own output."""
    expected = ss.init_screen(3, 1, 4) + ss.draw_raw([0x5, 0xC, 0x7], 4) + ss.PRESENT
    assert output_bytes(tmp_path, DRAW_RAW_PROGRAM) == expected


def test_draw_raw_draws_on_the_screen(tmp_path: Path) -> None:
    screen = InMemoryScreen()
    run_program(tmp_path, DRAW_RAW_PROGRAM, screen)
    assert (screen.frame_count, screen.pixel_indices) == (1, [0x5, 0xC, 0x7])


@pytest.mark.parametrize('width', [1, 14, 15, 254, 255, 4094, 4095, 65534])
def test_end_frame_ends_with_the_column_fields_top_value(tmp_path: Path, width: int) -> None:
    column_bits = ss.field_widths(width, 1)[1]
    for used_bits in (0, 4):
        program = f'''
stl.startup
screen.output_bits {used_bits}, 0
screen.end_frame {width}, {used_bits}
stl.loop
'''
        expected = ss.pack([(0, used_bits), ((1 << column_bits) - 1, column_bits)]) + ss.PRESENT
        assert output_bytes(tmp_path, program) == expected


# ---------------------------------------------------------------- the keyboard

POLLS = 6
EVENTS = [KeyEvent(0, True, 0x61), KeyEvent(2, False, 0x61), KeyEvent(3, True, 0xDA), KeyEvent(3, False, 0x20)]


def poll_program(poll_macro: str) -> str:
    return f'''
stl.startup
rep({POLLS}, i) poll_and_print
stl.loop
hex.init

// prints "<status> <keycode>" and a new line.
def poll_and_print @ status, keycode, end {{
    {poll_macro} status, keycode
    hex.print_as_digit status, 0
    stl.output ' '
    hex.print_as_digit keycode+dw, 0
    hex.print_as_digit keycode, 0
    stl.output_char 10
    ;end
  status: hex.hex
  keycode: hex.vec 2
  end:
}}
'''


def expected_polls(events: List[KeyEvent]) -> bytes:
    """one status per poll; an event's keycode follows it; an idle poll leaves the keycode as it was (here: 0,
    as every poll_and_print has its own keycode)."""
    source = ScriptedKeyEventSource(events)
    lines = []
    for tic in range(POLLS):
        event = source.next_due_event(tic)
        status = 0 if event is None else (0x9 if event[0] else 0x8)
        keycode = 0 if event is None else event[1]
        lines.append(f'{status:x} {keycode:02x}\n')
    return ''.join(lines).encode()


def test_poll_key_reads_the_status_and_the_keycode(tmp_path: Path) -> None:
    keyboard = KeyboardIO(ScriptedKeyEventSource(EVENTS))
    run_program(tmp_path, poll_program('hex.poll_key'), keyboard)
    assert keyboard.get_output() == expected_polls(EVENTS) == b'9 61\n0 00\n8 61\n9 da\n8 20\n0 00\n'


def test_poll_key_negative_control(tmp_path: Path) -> None:
    """a poll that reads only half of the keycode byte reads every later status out of phase."""
    broken = poll_program('broken_poll_key') + '''
def broken_poll_key status, keycode @ end {
    hex.input_hex status
    hex.if0 status, end
    hex.input_hex keycode
  end:
}
'''
    keyboard = KeyboardIO(ScriptedKeyEventSource(EVENTS))
    run_program(tmp_path, broken, keyboard)
    assert keyboard.get_output() != expected_polls(EVENTS)


SHARED_KEYCODE_PROGRAM = '''
stl.startup
rep(3, i) poll_and_print
stl.loop
hex.init

status: hex.hex
keycode: hex.vec 2, 0x5a

// polls into the one shared keycode (it starts as 0x5a), and prints "<status> <keycode>" and a new line.
def poll_and_print < status, keycode {
    POLL status, keycode
    hex.print_as_digit status, 0
    stl.output ' '
    hex.print_as_digit keycode+dw, 0
    hex.print_as_digit keycode, 0
    stl.output_char 10
}
'''


def test_an_idle_poll_key_leaves_the_keycode_alone(tmp_path: Path) -> None:
    keyboard = KeyboardIO(ScriptedKeyEventSource([KeyEvent(1, True, 0x61)]))
    run_program(tmp_path, SHARED_KEYCODE_PROGRAM.replace('POLL', 'hex.poll_key'), keyboard)
    assert keyboard.get_output() == b'0 5a\n9 61\n0 61\n'


def test_an_idle_poll_key_negative_control(tmp_path: Path) -> None:
    """a poll that zeroes the keycode when no key event arrived does not leave it alone."""
    broken = SHARED_KEYCODE_PROGRAM.replace('POLL', 'zeroing_poll_key') + '''
def zeroing_poll_key status, keycode {
    hex.zero 2, keycode
    hex.poll_key status, keycode
}
'''
    keyboard = KeyboardIO(ScriptedKeyEventSource([KeyEvent(1, True, 0x61)]))
    run_program(tmp_path, broken, keyboard)
    assert keyboard.get_output() != b'0 5a\n9 61\n0 61\n'
