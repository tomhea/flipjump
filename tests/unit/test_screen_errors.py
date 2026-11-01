"""
every row of io_devices/README.md's screen error table, with the EXACT message: where (the command,
its opcode, the output byte of the opcode, the frame, and inside a draw the column and cursor or the
pixel) and why (the field, its value, the values it allows).

Every test here is a negative control in its own right: a stream the device must reject.
"""

from typing import Optional

import pytest

from flipjump.interpreter.io_devices.ScreenIO import InMemoryScreen
from flipjump.utils.exceptions import IODeviceException
from tests.unit import screen_stream as ss

COMMANDS = 'commands: 0x01-0x07, 0x10, 0x11, 0x1F'


def rejected(stream: bytes, device: Optional[InMemoryScreen] = None) -> str:
    device = device if device is not None else InMemoryScreen()
    with pytest.raises(IODeviceException) as failure:
        ss.feed(device, stream)
    return str(failure.value)


# ---------------------------------------------------------------- opcodes


@pytest.mark.parametrize('opcode', [0x08, 0x0B, 0x0F, 0x12, 0x13, 0x14, 0x1E, 0x20, 0x23, 0x80, 0xFF])
def test_unknown_opcode(opcode: int) -> None:
    assert rejected(ss.init_screen(4, 3, 8) + bytes([opcode])) == (
        f'ScreenIO: unknown (0x{opcode:02X}) at output byte 6, frame 0: '
        f'opcode 0x{opcode:02X} is not a command ({COMMANDS})'
    )


def test_the_readmes_unknown_opcode_example() -> None:
    assert rejected(ss.init_screen(4, 3, 8) + ss.PRESENT + bytes([0x23])) == (
        f'ScreenIO: unknown (0x23) at output byte 7, frame 1: opcode 0x23 is not a command ({COMMANDS})'
    )


def test_reserved_opcode() -> None:
    assert rejected(ss.init_screen(4, 3, 8) + bytes([0x00])) == (
        'ScreenIO: reserved (0x00) at output byte 6, frame 0: opcode 0x00 is reserved (a stray zero byte?)'
    )


def test_reserved_opcode_as_the_first_byte() -> None:
    assert rejected(bytes([0x00])) == (
        'ScreenIO: reserved (0x00) at output byte 0, frame 0: opcode 0x00 is reserved (a stray zero byte?)'
    )


@pytest.mark.parametrize(
    'opcode, name',
    [
        (0x02, 'define_palette'),
        (0x03, 'set_max_fps'),
        (0x04, 'set_window_title'),
        (0x05, 'set_window_icon'),
        (0x06, 'select_palette'),
        (0x07, 'builtin_palette'),
        (0x10, 'draw_raw'),
        (0x11, 'draw_collines'),
        (0x1F, 'present'),
    ],
)
def test_a_command_before_init_screen(opcode: int, name: str) -> None:
    assert rejected(bytes([opcode])) == (
        f'ScreenIO: {name} (0x{opcode:02X}) at output byte 0, frame 0: '
        f'{name} (0x{opcode:02X}) before init_screen: the first command must be init_screen (0x01)'
    )


# ---------------------------------------------------------------- init_screen


@pytest.mark.parametrize('width', [0, 65_535])
def test_screen_width(width: int) -> None:
    assert rejected(ss.init_screen(width, 3, 8)) == (
        f'ScreenIO: init_screen (0x01) at output byte 0, frame 0: w = {width:,}: must be 1..65,534'
    )


@pytest.mark.parametrize('height', [0, 65_532, 65_534, 65_535])
def test_screen_height(height: int) -> None:
    assert rejected(ss.init_screen(4, height, 8)) == (
        f'ScreenIO: init_screen (0x01) at output byte 0, frame 0: h = {height:,}: must be 1..65,531'
    )


def test_the_largest_screens_are_accepted() -> None:
    """the vacuity control of the two above: the limits themselves pass"""
    for width, height in [(65_534, 1), (1, 65_531)]:
        device = InMemoryScreen()
        ss.feed(device, ss.init_screen(width, height, 1))
        assert (device.width, device.height) == (width, height)


@pytest.mark.parametrize('screen_format', [0, 17, 20, 23, 25, 32, 255])
def test_format(screen_format: int) -> None:
    assert rejected(ss.init_screen(4, 3, screen_format)) == (
        f'ScreenIO: init_screen (0x01) at output byte 0, frame 0: format = {screen_format}: must be 1-16 or 24'
    )


def test_a_reinit_error_reports_its_frame() -> None:
    stream = ss.init_screen(4, 3, 8) + ss.PRESENT + ss.PRESENT + ss.init_screen(0, 3, 8)
    assert rejected(stream) == 'ScreenIO: init_screen (0x01) at output byte 8, frame 2: w = 0: must be 1..65,534'


# ---------------------------------------------------------------- palettes


@pytest.mark.parametrize(
    'opcode, name', [(0x02, 'define_palette'), (0x06, 'select_palette'), (0x07, 'builtin_palette')]
)
def test_a_palette_command_in_rgb(opcode: int, name: str) -> None:
    assert rejected(ss.init_screen(4, 3, 24) + bytes([opcode, 0, 0, 0])) == (
        f'ScreenIO: {name} (0x{opcode:02X}) at output byte 6, frame 0: '
        f'{name} (0x{opcode:02X}) in format 24 (direct RGB): palettes exist only in formats 1-16'
    )


def test_palette_range_first_after_last() -> None:
    stream = ss.init_screen(4, 3, 8) + ss.command(0x02, (1, 8), (12, 8), (3, 8))
    assert rejected(stream) == 'ScreenIO: define_palette (0x02) at output byte 6, frame 0: first = 12 > last = 3'


@pytest.mark.parametrize('builtin_id', [5, 7, 255])
def test_builtin_palette_id(builtin_id: int) -> None:
    assert rejected(ss.init_screen(4, 3, 8) + ss.builtin_palette(0, builtin_id)) == (
        f'ScreenIO: builtin_palette (0x07) at output byte 6, frame 0: id = {builtin_id}: the built-ins are 0..4'
    )


# ---------------------------------------------------------------- draw_collines


def test_column_past_the_screen() -> None:
    stream = ss.init_screen(160, 84, 8) + ss.command(0x11, (170, 8))
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0: x = 170: the screen has columns 0..159 (w = 160)'
    )


def test_column_w_is_past_the_screen() -> None:
    stream = ss.init_screen(13, 3, 8) + ss.command(0x11, (13, 4))  # C = 4: 13 is a column value, past w - 1
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0: x = 13: the screen has columns 0..12 (w = 13)'
    )


@pytest.mark.parametrize('width, column_bits', [(4, 4), (160, 8), (300, 12), (5000, 16)])
def test_reserved_column_token(width: int, column_bits: int) -> None:
    token = 2**column_bits - 2
    stream = ss.init_screen(width, 3, 8) + ss.command(0x11, (token, column_bits))
    assert rejected(stream) == (
        f'ScreenIO: draw_collines (0x11) at output byte 6, frame 0: '
        f'x = 0x{token:0{column_bits // 4}X} is the reserved column token (2^C - 2, C = {column_bits})'
    )


def test_the_readmes_example_error() -> None:
    """the README's example, to the byte: output byte 1,204, frame 37, column 17, cursor 40."""
    prefix = ss.init_screen(160, 84, 8) + ss.PRESENT * 37
    prefix += ss.set_window_title(b'a' * (1204 - len(prefix) - 2))
    assert len(prefix) == 1204
    stream = prefix + ss.command(0x11, (17, 8), (40, 8), (1, 8), (90, 8))
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 1,204, frame 37, column 17, cursor 40: '
        'y2 = 90 is past the screen (rows end at h = 84)'
    )


@pytest.mark.parametrize('token, name', [(0xFE, 'KEEP'), (0xFD, 'COPY')])
def test_keep_and_copy_past_the_screen(token: int, name: str) -> None:
    stream = ss.init_screen(160, 84, 8) + ss.command(0x11, (17, 8), (token, 8), (85, 8))
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0, column 17, cursor 0: '
        'y = 85 is past the screen (rows end at h = 84)'
    )


def test_a_token_after_keep_is_past_the_screen() -> None:
    stream = ss.init_screen(160, 84, 8) + ss.command(0x11, (17, 8), (0xFE, 8), (0xFF, 8))
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0, column 17, cursor 0: '
        'y = 255 is past the screen (rows end at h = 84)'
    )


def test_a_row_behind_the_cursor() -> None:
    stream = ss.init_screen(160, 84, 8) + ss.command(0x11, (17, 8), (40, 8), (1, 8), (30, 8))
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0, column 17, cursor 40: '
        'y2 = 30 is behind the cursor (need 40 <= y2 <= 84)'
    )


def test_a_row_one_behind_the_cursor() -> None:
    stream = ss.init_screen(160, 84, 8) + ss.command(0x11, (17, 8), (40, 8), (1, 8), (39, 8))
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0, column 17, cursor 40: '
        'y2 = 39 is behind the cursor (need 40 <= y2 <= 84)'
    )


def test_control_a_row_at_the_cursor_is_a_zero_row_fill() -> None:
    """the vacuity control of the two above: y2 = the cursor is allowed (it paints nothing)"""
    device = InMemoryScreen()
    stream = ss.command(0x11, (17, 8), (40, 8), (1, 8), (40, 8), (2, 8), (0xFF, 8), (0xFF, 8))
    ss.feed(device, ss.init_screen(160, 84, 8) + stream + ss.PRESENT)
    assert device.pixel_indices[39 * 160 + 17] == 1 and device.pixel_indices[40 * 160 + 17] == 0


@pytest.mark.parametrize('token', [0xFE, 0xFD])
def test_keep_and_copy_behind_the_cursor(token: int) -> None:
    stream = ss.init_screen(160, 84, 8) + ss.command(0x11, (17, 8), (40, 8), (1, 8), (token, 8), (39, 8))
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0, column 17, cursor 40: '
        'y = 39 is behind the cursor (need 40 <= y <= 84)'
    )


def test_an_item_after_h_other_than_end_is_past_the_screen() -> None:
    stream = ss.init_screen(160, 84, 8) + ss.command(0x11, (17, 8), (84, 8), (1, 8), (85, 8))
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0, column 17, cursor 84: '
        'y2 = 85 is past the screen (rows end at h = 84)'
    )


@pytest.mark.parametrize('height, row_bits', [(3, 4), (84, 8), (300, 12), (5000, 16)])
def test_reserved_row_token(height: int, row_bits: int) -> None:
    token = 2**row_bits - 4
    stream = ss.init_screen(4, height, 8) + ss.command(0x11, (2, 4), (token, row_bits))
    assert rejected(stream) == (
        f'ScreenIO: draw_collines (0x11) at output byte 6, frame 0, column 2, cursor 0: '
        f'0x{token:0{row_bits // 4}X} is the reserved row token (2^R - 4, R = {row_bits})'
    )


def test_reserved_row_token_after_keep() -> None:
    stream = ss.init_screen(160, 84, 8) + ss.command(0x11, (17, 8), (0xFE, 8), (0xFC, 8))
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0, column 17, cursor 0: '
        '0xFC is the reserved row token (2^R - 4, R = 8)'
    )


def test_copy_in_column_0() -> None:
    stream = ss.init_screen(160, 84, 8) + ss.command(0x11, (0, 8), (10, 8), (1, 8), (0xFD, 8), (20, 8))
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0, column 0, cursor 10: '
        'COPY copies column x - 1, and column 0 has none'
    )


def test_keep_in_column_0_is_fine() -> None:
    """the vacuity control of the one above: KEEP needs no left neighbour"""
    device = InMemoryScreen()
    ss.feed(device, ss.init_screen(4, 3, 8) + ss.draw_collines([(0, [('keep', 3)])], 4, 3, 8))
    assert device.pixel_indices == [0] * 12


# ---------------------------------------------------------------- padding, the icon, the title


def test_non_zero_padding() -> None:
    # column 0 with nothing but its END, then END_CMD: 20 bits, and 0b0101 in the 4 padding bits
    stream = ss.init_screen(4, 3, 8) + bytes([0x11, 0xF0, 0x5F])
    assert rejected(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0: '
        'the padding bits after the command are 0b0101, not zero (the stream is probably misaligned)'
    )


def test_non_zero_padding_shows_every_padding_bit() -> None:
    # draw_raw of 1 pixel of 3 bits: 5 padding bits, the last one set
    stream = ss.init_screen(1, 1, 3) + bytes([0x10, 0b10000101])
    assert rejected(stream) == (
        'ScreenIO: draw_raw (0x10) at output byte 6, frame 0: '
        'the padding bits after the command are 0b10000, not zero (the stream is probably misaligned)'
    )


def test_padding_control_zero_padding_passes() -> None:
    device = InMemoryScreen()
    ss.feed(device, ss.init_screen(4, 3, 8) + bytes([0x11, 0x30, 0x05, 0xFF, 0x11, 0xF1, 0x0F]) + ss.PRESENT)
    assert device.pixel_indices[0::4] == [5, 5, 5]
    assert device.frame_count == 1 and device.get_output() == b''


@pytest.mark.parametrize('width, height, field', [(0, 3, 'w'), (3, 0, 'h'), (0, 0, 'w')])
def test_icon_size(width: int, height: int, field: str) -> None:
    stream = ss.init_screen(4, 3, 8) + ss.command(0x05, (width, 8), (height, 8))
    assert rejected(stream) == (
        f'ScreenIO: set_window_icon (0x05) at output byte 6, frame 0: set_window_icon: {field} = 0: must be 1..255'
    )


def test_title_not_utf8() -> None:
    stream = ss.init_screen(4, 3, 8) + ss.set_window_title(b'Hello\xc3(')
    assert rejected(stream) == (
        'ScreenIO: set_window_title (0x04) at output byte 6, frame 0: '
        'set_window_title: title byte 5 (0xC3) is not valid UTF-8'
    )


def test_title_with_a_bare_continuation_byte() -> None:
    stream = ss.init_screen(4, 3, 8) + ss.set_window_title(b'\x80')
    assert rejected(stream) == (
        'ScreenIO: set_window_title (0x04) at output byte 6, frame 0: '
        'set_window_title: title byte 0 (0x80) is not valid UTF-8'
    )


# ---------------------------------------------------------------- the output ends inside a command


def ended(stream: bytes, extra_bits: int = 0) -> str:
    device = InMemoryScreen()
    ss.feed(device, stream)
    for _ in range(extra_bits):
        device.write_bit(False)
    assert device.get_output(allow_incomplete_output=True) == b''  # allowed: no error
    with pytest.raises(IODeviceException) as failure:
        device.get_output()
    return str(failure.value)


def test_output_ends_inside_draw_collines() -> None:
    # the README example up to column 1's x, then 5 more bits: 37 of the command's bits
    stream = ss.init_screen(4, 3, 8) + bytes([0x11, 0x30, 0x05, 0x1F])
    assert ended(stream, extra_bits=5) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0, column 1, cursor 0: '
        "the program's output ended inside draw_collines (0x11): 37 of its bits read"
    )


def test_output_ends_inside_draw_collines_between_columns() -> None:
    stream = ss.init_screen(4, 3, 8) + bytes([0x11])
    assert ended(stream) == (
        'ScreenIO: draw_collines (0x11) at output byte 6, frame 0: '
        "the program's output ended inside draw_collines (0x11): 8 of its bits read"
    )


def test_output_ends_inside_draw_raw() -> None:
    stream = ss.init_screen(3, 2, 8) + ss.PRESENT + bytes([0x10, 1, 2, 3, 4])
    assert ended(stream) == (
        'ScreenIO: draw_raw (0x10) at output byte 7, frame 1, pixel (1, 1): '
        "the program's output ended inside draw_raw (0x10): 40 of its bits read"
    )


def test_output_ends_inside_init_screen() -> None:
    assert ended(ss.init_screen(4, 3, 8)[:3]) == (
        'ScreenIO: init_screen (0x01) at output byte 0, frame 0: '
        "the program's output ended inside init_screen (0x01): 24 of its bits read"
    )


def test_output_ends_inside_the_icon() -> None:
    assert ended(ss.init_screen(4, 3, 8) + bytes([0x05, 2, 2, 7])) == (
        'ScreenIO: set_window_icon (0x05) at output byte 6, frame 0, pixel (1, 0): '
        "the program's output ended inside set_window_icon (0x05): 32 of its bits read"
    )


def test_output_ends_inside_an_opcode_byte() -> None:
    assert ended(ss.init_screen(4, 3, 8), extra_bits=3) == (
        'ScreenIO: an opcode at output byte 6, frame 0: '
        "the program's output ended inside an opcode byte: 3 of its 8 bits read"
    )


def test_output_ending_between_commands_is_complete() -> None:
    """the vacuity control of the ones above"""
    device = InMemoryScreen()
    ss.feed(device, ss.init_screen(4, 3, 8) + ss.PRESENT)
    assert device.get_output() == b''


# ---------------------------------------------------------------- after an error


def test_the_device_stays_failed() -> None:
    device = InMemoryScreen()
    message = rejected(bytes([0x00]), device)
    with pytest.raises(IODeviceException) as again:
        ss.feed(device, ss.init_screen(4, 3, 8))
    assert str(again.value) == message
    with pytest.raises(IODeviceException) as at_end:
        device.end_of_run()
    assert str(at_end.value) == message
