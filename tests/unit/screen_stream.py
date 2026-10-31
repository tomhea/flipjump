"""
a tiny writer of the screen's command stream (io_devices/README.md), for the screen tests.

Written from the README alone, independently of ScreenIO.py: fields are packed back to back at
their exact width, least-significant bit first, and every command is padded with zero bits to the
next byte boundary.
"""

from typing import Any, Iterable, List, Sequence, Tuple

from flipjump.interpreter.io_devices.IODevice import IODevice

Field = Tuple[int, int]  # (value, width in bits)


def pack(fields: Iterable[Field], padding: int = 0) -> bytes:
    """pack (value, width) fields lsb-first, then pad to a byte boundary with `padding`'s bits (lsb first)."""
    bits: List[int] = []
    for value, width in fields:
        assert 0 <= value < (1 << width), (value, width)
        bits.extend((value >> i) & 1 for i in range(width))
    pad_count = -len(bits) % 8
    assert padding < (1 << pad_count) or padding == 0, 'no room for that padding'
    bits.extend((padding >> i) & 1 for i in range(pad_count))
    return bytes(sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits), 8))


def command(opcode: int, *fields: Field, padding: int = 0) -> bytes:
    return pack([(opcode, 8), *fields], padding)


def init_screen(width: int, height: int, screen_format: int) -> bytes:
    return command(0x01, (width, 16), (height, 16), (screen_format, 8))


def define_palette(slot: int, first: int, colours: Sequence[Tuple[int, int, int]], bits: int) -> bytes:
    fields = [(slot, 8), (first, bits), (first + len(colours) - 1, bits)]
    for colour in colours:
        fields += [(channel, 8) for channel in colour]
    return command(0x02, *fields)


def set_max_fps(fps: int) -> bytes:
    return command(0x03, (fps, 8))


def set_window_title(title: bytes) -> bytes:
    return bytes([0x04]) + title + b'\x00'


def set_window_icon(width: int, height: int, colours: Sequence[int], bits: int) -> bytes:
    return command(0x05, (width, 8), (height, 8), *[(colour, bits) for colour in colours])


def select_palette(slot: int) -> bytes:
    return command(0x06, (slot, 8))


def builtin_palette(slot: int, builtin_id: int) -> bytes:
    return command(0x07, (slot, 8), (builtin_id, 8))


def draw_raw(colours: Sequence[int], bits: int) -> bytes:
    return command(0x10, *[(colour, bits) for colour in colours])


PRESENT = bytes([0x1F])


def field_widths(width: int, height: int) -> Tuple[int, int]:
    """(R, C) as the README defines them."""
    row_bits = min(r for r in (4, 8, 12, 16) if height < 2**r - 4)
    column_bits = min(c for c in (4, 8, 12, 16) if width < 2**c - 1)
    return row_bits, column_bits


def feed(device: IODevice, stream: bytes) -> None:
    for byte in stream:
        device.write_byte(byte)


def feed_bits(device: IODevice, stream: bytes) -> None:
    """the same stream, one write_bit at a time (the python engines' path)."""
    for byte in stream:
        for i in range(8):
            device.write_bit(bool((byte >> i) & 1))


def collines_fields(
    columns: Sequence[Tuple[int, Sequence[Tuple[Any, ...]]]], row_bits: int, column_bits: int, bits: int
) -> List[Field]:
    """draw_collines's fields (after the opcode). `columns` is [(x, items)], an item one of
    ('fill', y2, colour), ('keep', y), ('copy', y), ('end',) - each written as the README's tokens. A column
    whose items do not end with ('end',) gets one: END closes every column."""
    end, keep, copy = 2**row_bits - 1, 2**row_bits - 2, 2**row_bits - 3
    fields: List[Field] = []
    for x, items in columns:
        fields.append((x, column_bits))
        for item in items:
            if item[0] == 'fill':
                fields += [(item[1], row_bits), (item[2], bits)]
            elif item[0] == 'keep':
                fields += [(keep, row_bits), (item[1], row_bits)]
            elif item[0] == 'copy':
                fields += [(copy, row_bits), (item[1], row_bits)]
            else:
                assert item[0] == 'end'
                fields.append((end, row_bits))
        if not items or items[-1][0] != 'end':
            fields.append((end, row_bits))
    fields.append((2**column_bits - 1, column_bits))  # END_CMD
    return fields


def draw_collines(
    columns: Sequence[Tuple[int, Sequence[Tuple[Any, ...]]]], width: int, height: int, bits: int, padding: int = 0
) -> bytes:
    row_bits, column_bits = field_widths(width, height)
    return command(0x11, *collines_fields(columns, row_bits, column_bits, bits), padding=padding)
