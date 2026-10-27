"""
unit-tests for the headless screen (ScreenIO.InMemoryScreen) against io_devices/README.md:
every opcode's happy path, the formats' default palettes and the built-ins, the bit packing,
the life cycle, the frame hash, PNGs, and the subclass contracts (`_present`, `_extension_command`).

The streams are written by tests/unit/screen_stream.py, from the README alone. The errors (every
row of the README's table) are in test_screen_errors.py; draw_collines in test_screen_collines.py.
"""

import hashlib
import struct
import zlib
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

import pytest

from flipjump.interpreter.io_devices.ScreenIO import (
    EGA16,
    XTERM256,
    InMemoryScreen,
    builtin_palette,
    default_palette,
    rgb_cube_split,
)
from flipjump.utils.exceptions import IODeviceException
from tests.unit import screen_stream as ss

BLACK, WHITE = (0, 0, 0), (255, 255, 255)


def screen_after(stream: bytes, frames_dir: Optional[Path] = None) -> InMemoryScreen:
    device = InMemoryScreen(frames_dir=frames_dir)
    ss.feed(device, stream)
    return device


# ---------------------------------------------------------------- init_screen and the formats


@pytest.mark.parametrize('screen_format', [*range(1, 17), 24])
def test_init_screen_accepts_every_format(screen_format: int) -> None:
    device = screen_after(ss.init_screen(5, 3, screen_format))
    assert (device.width, device.height, device.bpp) == (5, 3, screen_format)
    assert device.pixel_indices == [0] * 15
    assert device.selected_slot == 0
    assert device.frame_count == 0 and device.frame_hashes == [] and device.last_frame_rgb == []
    if screen_format == 24:
        assert device.palettes == []
    else:
        assert len(device.palettes) == 256
        assert all(len(slot) == 2**screen_format for slot in device.palettes)
        assert all(slot == device.palettes[0] for slot in device.palettes)  # every slot starts as the default


def test_default_palette_of_format_1_is_black_and_white() -> None:
    assert screen_after(ss.init_screen(1, 1, 1)).palettes[0] == [BLACK, WHITE]


def test_default_palette_of_format_2_is_four_greys() -> None:
    assert screen_after(ss.init_screen(1, 1, 2)).palettes[0] == [(0, 0, 0), (85, 85, 85), (170, 170, 170), WHITE]


def test_default_palette_of_format_4_is_ega16() -> None:
    palette = screen_after(ss.init_screen(1, 1, 4)).palettes[0]
    assert palette == list(EGA16)
    assert palette[1] == (0, 0, 170)  # blue
    assert palette[6] == (170, 85, 0)  # brown
    assert palette[8] == (85, 85, 85)  # dark grey
    assert palette[14] == (255, 255, 85)  # yellow
    assert palette[15] == WHITE


def test_default_palette_of_format_8_is_xterm256() -> None:
    palette = screen_after(ss.init_screen(1, 1, 8)).palettes[0]
    assert palette == list(XTERM256) and len(palette) == 256
    # the 16 colours
    assert palette[1] == (128, 0, 0) and palette[7] == (192, 192, 192) and palette[8] == (128, 128, 128)
    assert palette[9] == (255, 0, 0) and palette[15] == WHITE
    # the 6x6x6 cube: 16 + 36r + 6g + b over the levels 0, 95, 135, 175, 215, 255
    assert palette[16] == BLACK
    assert palette[21] == (0, 0, 255)
    assert palette[16 + 36 * 1 + 6 * 2 + 3] == (95, 135, 175)
    assert palette[196] == (255, 0, 0)
    assert palette[231] == WHITE
    # the 24 greys: 8 + 10k
    assert palette[232] == (8, 8, 8) and palette[243] == (118, 118, 118) and palette[255] == (238, 238, 238)


@pytest.mark.parametrize(
    'bits, split', [(3, (1, 1, 1)), (6, (2, 2, 2)), (12, (4, 4, 4)), (15, (5, 5, 5)), (16, (5, 6, 5))]
)
def test_the_rgb_cube_split_is_the_readmes(bits: int, split: Tuple[int, int, int]) -> None:
    assert rgb_cube_split(bits) == split


def test_the_rgb_cube_of_3_bits() -> None:
    palette = screen_after(ss.init_screen(1, 1, 3)).palettes[0]
    # red | green | blue from the high bit down
    assert palette == [BLACK, (0, 0, 255), (0, 255, 0), (0, 255, 255), (255, 0, 0), (255, 0, 255), (255, 255, 0), WHITE]


def test_the_rgb_cube_of_6_bits() -> None:
    palette = screen_after(ss.init_screen(1, 1, 6)).palettes[0]
    assert palette[0b110000] == (255, 0, 0)
    assert palette[0b001100] == (0, 255, 0)
    assert palette[0b000011] == (0, 0, 255)
    assert palette[0b010101] == (85, 85, 85)
    assert palette[0b101001] == (170, 170, 85)


def test_the_rgb_cube_of_15_bits() -> None:
    palette = screen_after(ss.init_screen(1, 1, 15)).palettes[0]
    assert len(palette) == 2**15
    assert palette[31 << 10] == (255, 0, 0)
    assert palette[31 << 5] == (0, 255, 0)
    assert palette[31] == (0, 0, 255)
    assert palette[1 << 5] == (0, 8, 0)  # 1/31 of 255 = 8.2
    assert palette[(16 << 10) | (16 << 5) | 16] == (132, 132, 132)  # 16/31 of 255 = 131.6


def test_the_rgb_cube_of_16_bits_is_rgb565() -> None:
    palette = screen_after(ss.init_screen(1, 1, 16)).palettes[0]
    assert len(palette) == 2**16
    assert palette[0xF800] == (255, 0, 0)
    assert palette[0x07E0] == (0, 255, 0)
    assert palette[0x001F] == (0, 0, 255)
    assert palette[0x0020] == (0, 4, 0)  # 1/63 of 255 = 4.05
    assert palette[0x8410] == (132, 130, 132)  # 16/31 = 131.6, 32/63 = 129.5 (halves up)
    assert palette[0xFFFF] == WHITE


@pytest.mark.parametrize('bits', [5, 7, 9, 10, 11, 13, 14])
def test_the_other_cubes_span_black_to_white(bits: int) -> None:
    palette = default_palette(bits)
    assert len(palette) == 2 ** bits and palette[0] == BLACK and palette[-1] == WHITE
    assert len(set(palette)) == 2**bits  # every value is its own colour


# ---------------------------------------------------------------- the built-ins


def test_builtin_palette_ids_in_format_8() -> None:
    stream = ss.init_screen(1, 1, 8) + b''.join(ss.builtin_palette(slot, slot) for slot in range(5))
    device = screen_after(stream)
    black_white, grey, ega, xterm, cube = device.palettes[:5]
    assert black_white[:2] == [BLACK, WHITE] and set(black_white[2:]) == {BLACK} and len(black_white) == 256
    assert grey == [(i, i, i) for i in range(256)]
    assert ega[:16] == list(EGA16) and set(ega[16:]) == {BLACK}
    assert xterm == list(XTERM256)
    assert cube[0b11100000] == (255, 0, 0) and cube[0b00011100] == (0, 255, 0) and cube[0b00000011] == (0, 0, 255)
    assert device.palettes[5] == list(XTERM256)  # the other slots keep the default


def test_builtin_palettes_fit_the_format() -> None:
    device = screen_after(ss.init_screen(1, 1, 2) + ss.builtin_palette(9, 2) + ss.builtin_palette(10, 1))
    assert device.palettes[9] == list(EGA16[:4])  # cut to 2^B entries
    assert device.palettes[10] == [(0, 0, 0), (85, 85, 85), (170, 170, 170), WHITE]


def test_builtin_palette_of_format_1() -> None:
    assert builtin_palette(4, 1) == (BLACK, (0, 255, 0))  # the 1-bit cube: green gets the bit
    assert builtin_palette(1, 1) == (BLACK, WHITE)


def test_builtin_palette_refuses_an_unknown_id() -> None:
    with pytest.raises(ValueError):
        builtin_palette(5, 8)


# ---------------------------------------------------------------- palettes


def test_define_palette_sets_a_range_of_one_slot() -> None:
    stream = ss.init_screen(1, 1, 4) + ss.define_palette(3, 2, [(1, 2, 3), (4, 5, 6)], bits=4)
    device = screen_after(stream)
    assert device.palettes[3][2:4] == [(1, 2, 3), (4, 5, 6)]
    assert device.palettes[3][:2] == list(EGA16[:2]) and device.palettes[3][4:] == list(EGA16[4:])
    assert device.palettes[0] == list(EGA16)  # only that slot


def test_a_large_palette_in_several_ranges() -> None:
    colours = [(i, 255 - i, i // 2) for i in range(256)]
    stream = (
        ss.init_screen(1, 1, 8)
        + ss.define_palette(1, 0, colours[:100], 8)
        + ss.define_palette(1, 100, colours[100:], 8)
    )
    assert screen_after(stream).palettes[1] == colours


def test_present_uses_the_palette_selected_when_it_runs() -> None:
    stream = (
        ss.init_screen(2, 1, 1)
        + ss.draw_raw([1, 0], 1)
        + ss.define_palette(7, 0, [(9, 9, 9), (200, 0, 0)], 1)
        + ss.select_palette(7)
        + ss.PRESENT
    )
    device = screen_after(stream)
    assert device.last_frame_rgb == [(200, 0, 0), (9, 9, 9)]
    assert device.palette == [(9, 9, 9), (200, 0, 0)]
    assert device.selected_slot == 7
    # switching the palette back is 2 bytes and re-presenting is 1: no redraw
    ss.feed(device, ss.select_palette(0) + ss.PRESENT)
    assert device.last_frame_rgb == [WHITE, BLACK]


def test_a_palette_change_after_present_does_not_change_the_presented_frame() -> None:
    device = screen_after(ss.init_screen(1, 1, 1) + ss.draw_raw([1], 1) + ss.PRESENT)
    ss.feed(device, ss.define_palette(0, 1, [(1, 2, 3)], 1))
    assert device.last_frame_rgb == [WHITE]
    assert device.palette == [BLACK, WHITE]
    assert device.palettes[0] == [BLACK, (1, 2, 3)]


def test_a_palette_command_needs_no_redraw_to_take_effect() -> None:
    device = screen_after(ss.init_screen(1, 1, 1) + ss.draw_raw([1], 1) + ss.PRESENT)
    ss.feed(device, ss.define_palette(0, 1, [(1, 2, 3)], 1) + ss.PRESENT)
    assert device.last_frame_rgb == [(1, 2, 3)]


# ---------------------------------------------------------------- the bit packing


def test_draw_raw_packs_1_bit_colours() -> None:
    stream = ss.init_screen(8, 1, 1) + bytes([0x10, 0b10001101])
    assert stream[-2:] == ss.draw_raw([1, 0, 1, 1, 0, 0, 0, 1], 1)
    assert screen_after(stream).pixel_indices == [1, 0, 1, 1, 0, 0, 0, 1]


def test_draw_raw_packs_3_bit_colours_8_pixels_in_3_bytes() -> None:
    # the README: "8 pixels of 3 bits take 3 bytes" - hand-packed, lsb first
    stream = ss.init_screen(8, 1, 3) + bytes([0x10, 0xD1, 0x58, 0x1F])
    assert stream[6:] == ss.draw_raw([1, 2, 3, 4, 5, 6, 7, 0], 3)
    assert screen_after(stream).pixel_indices == [1, 2, 3, 4, 5, 6, 7, 0]


def test_draw_raw_packs_4_bit_colours_low_nibble_first() -> None:
    stream = ss.init_screen(3, 1, 4) + bytes([0x10, 0x21, 0x0F])  # 1, 2, then 15 and 4 zero padding bits
    assert screen_after(stream).pixel_indices == [1, 2, 15]


def test_draw_raw_packs_12_bit_colours() -> None:
    stream = ss.init_screen(2, 1, 12) + bytes([0x10, 0xBC, 0x3A, 0x12])
    assert stream[6:] == ss.draw_raw([0xABC, 0x123], 12)
    assert screen_after(stream).pixel_indices == [0xABC, 0x123]


def test_draw_raw_in_format_24_is_r_g_b_bytes() -> None:
    stream = ss.init_screen(2, 1, 24) + bytes([0x10, 0x12, 0x34, 0x56, 0xFF, 0x00, 0x80]) + ss.PRESENT
    device = screen_after(stream)
    assert device.last_frame_rgb == [(0x12, 0x34, 0x56), (0xFF, 0x00, 0x80)]
    assert device.palette == []


def test_draw_raw_is_row_major() -> None:
    device = screen_after(ss.init_screen(3, 2, 8) + ss.draw_raw([1, 2, 3, 4, 5, 6], 8) + ss.PRESENT)
    assert device.pixel_indices == [1, 2, 3, 4, 5, 6]
    assert device.last_frame_rgb[3] == XTERM256[4]


@pytest.mark.parametrize('bits', [1, 3, 4, 12, 24])
def test_the_icon_packs_its_colours_like_draw_raw(bits: int) -> None:
    colours = [(i * 0x9E3779B1) % (1 << bits) for i in range(6)]
    device = screen_after(ss.init_screen(1, 1, bits) + ss.set_window_icon(3, 2, colours, bits))
    assert device.window_icon is not None
    width, height, rgb = device.window_icon
    assert (width, height) == (3, 2)
    if bits == 24:
        assert rgb == [(c & 0xFF, (c >> 8) & 0xFF, c >> 16) for c in colours]
    else:
        assert rgb == [default_palette(bits)[c] for c in colours]


def test_the_icon_resolves_through_the_palette_selected_when_it_runs() -> None:
    stream = (
        ss.init_screen(1, 1, 1)
        + ss.define_palette(5, 0, [(1, 1, 1), (2, 2, 2)], 1)
        + ss.select_palette(5)
        + ss.set_window_icon(2, 1, [1, 0], 1)
        + ss.select_palette(0)
    )
    assert screen_after(stream).window_icon == (2, 1, [(2, 2, 2), (1, 1, 1)])


def test_the_icon_hand_packed_3_bit() -> None:
    stream = ss.init_screen(1, 1, 3) + bytes([0x05, 8, 1, 0xD1, 0x58, 0x1F])
    assert stream[6:] == ss.set_window_icon(8, 1, [1, 2, 3, 4, 5, 6, 7, 0], 3)
    assert screen_after(stream).window_icon == (8, 1, [default_palette(3)[c] for c in [1, 2, 3, 4, 5, 6, 7, 0]])


# ---------------------------------------------------------------- the other setup commands


def test_set_window_title_is_utf8() -> None:
    device = screen_after(ss.init_screen(1, 1, 1) + ss.set_window_title('flipjump שלום'.encode()))
    assert device.window_title == 'flipjump שלום'


def test_an_empty_title() -> None:
    assert screen_after(ss.init_screen(1, 1, 1) + ss.set_window_title(b'')).window_title == ''


def test_set_max_fps_is_recorded_and_the_headless_screen_never_waits() -> None:
    device = screen_after(ss.init_screen(1, 1, 1) + ss.set_max_fps(35))
    assert device.max_fps == 35
    ss.feed(device, ss.set_max_fps(0))
    assert device.max_fps == 0


def test_select_palette_any_slot() -> None:
    assert screen_after(ss.init_screen(1, 1, 8) + ss.select_palette(255)).selected_slot == 255


# ---------------------------------------------------------------- the life cycle


def test_nothing_is_recorded_before_the_first_present(tmp_path: Path) -> None:
    device = screen_after(ss.init_screen(2, 1, 8) + ss.draw_raw([3, 4], 8), frames_dir=tmp_path)
    assert device.frame_count == 0 and device.frame_hashes == [] and device.last_frame_rgb == []
    assert device.palette == []
    assert not list(tmp_path.iterdir())


def test_present_counts_a_frame_and_writes_a_png(tmp_path: Path) -> None:
    device = screen_after(ss.init_screen(2, 1, 8) + ss.draw_raw([9, 12], 8) + ss.PRESENT, frames_dir=tmp_path)
    assert device.frame_count == 1 and len(device.frame_hashes) == 1
    assert device.last_frame_rgb == [XTERM256[9], XTERM256[12]]
    assert (tmp_path / 'frame_000000.png').read_bytes().startswith(b'\x89PNG')


def test_an_empty_present_repeats_the_picture_and_counts(tmp_path: Path) -> None:
    device = screen_after(
        ss.init_screen(2, 1, 8) + ss.draw_raw([9, 12], 8) + ss.PRESENT + ss.PRESENT, frames_dir=tmp_path
    )
    assert device.frame_count == 2
    assert device.frame_hashes[0][1] == device.frame_hashes[1][1]
    assert (tmp_path / 'frame_000001.png').read_bytes() == (tmp_path / 'frame_000000.png').read_bytes()


def test_another_init_screen_starts_over() -> None:
    device = screen_after(
        ss.init_screen(2, 1, 8)
        + ss.set_window_title(b'T')
        + ss.set_window_icon(1, 1, [5], 8)
        + ss.set_max_fps(30)
        + ss.define_palette(0, 0, [(1, 2, 3)], 8)
        + ss.select_palette(4)
        + ss.draw_raw([7, 7], 8)
        + ss.PRESENT
        + ss.init_screen(3, 2, 4)
    )
    assert (device.width, device.height, device.bpp) == (3, 2, 4)
    assert device.pixel_indices == [0] * 6
    assert device.selected_slot == 0
    assert all(slot == list(EGA16) for slot in device.palettes)
    # what stays: the title, the icon, max fps, the frame count (and the frame already presented)
    assert device.window_title == 'T' and device.window_icon == (1, 1, [XTERM256[5]]) and device.max_fps == 30
    assert device.frame_count == 1
    assert device.last_frame_rgb == [XTERM256[7]] * 2
    ss.feed(device, ss.PRESENT)
    assert device.frame_count == 2 and device.last_frame_rgb == [BLACK] * 6


def test_reinit_resets_the_field_widths() -> None:
    # 4x3 has 1-nibble fields; 20x3 has a byte-wide column field
    device = screen_after(ss.init_screen(4, 3, 8) + ss.init_screen(20, 3, 8))
    ss.feed(device, ss.draw_collines([(17, [('fill', 3, 6)])], 20, 3, 8) + ss.PRESENT)
    assert device.pixel_indices[17] == 6 and device.pixel_indices[2 * 20 + 17] == 6


# ---------------------------------------------------------------- the frame hash


def test_the_8_bit_frame_hash_is_pinned() -> None:
    """sha256(the values, one byte each + all 256 entries of the presented palette's RGB bytes), hand-pinned."""
    device = screen_after(
        ss.init_screen(2, 1, 8)
        + ss.define_palette(0, 0, [(10, 20, 30), (200, 100, 0)], 8)
        + ss.draw_raw([1, 0], 8)
        + ss.PRESENT
    )
    palette_bytes = bytes([10, 20, 30, 200, 100, 0]) + b''.join(bytes(rgb) for rgb in XTERM256[2:])
    assert device.frame_hashes[0][1] == hashlib.sha256(bytes([1, 0]) + palette_bytes).hexdigest()
    assert device.frame_hashes[0][1] == 'f8ba3b7f6301a1b61781fe278b3827cf018523c36c4b773430423405c1749f20'


def test_the_16_bit_frame_hash_uses_two_little_endian_bytes() -> None:
    device = screen_after(ss.init_screen(2, 1, 16) + ss.draw_raw([0x1234, 0xF800], 16) + ss.PRESENT)
    palette_bytes = b''.join(bytes(rgb) for rgb in default_palette(16))
    expected = hashlib.sha256(struct.pack('<2H', 0x1234, 0xF800) + palette_bytes).hexdigest()
    assert device.frame_hashes[0][1] == expected


def test_the_rgb_frame_hash_is_the_rgb_bytes() -> None:
    device = screen_after(ss.init_screen(2, 1, 24) + bytes([0x10, 1, 2, 3, 4, 5, 6]) + ss.PRESENT)
    assert device.frame_hashes[0][1] == hashlib.sha256(bytes([1, 2, 3, 4, 5, 6])).hexdigest()


def test_the_hash_sees_the_palette() -> None:
    device = screen_after(ss.init_screen(1, 1, 1) + ss.PRESENT + ss.define_palette(0, 1, [(1, 1, 1)], 1) + ss.PRESENT)
    assert device.frame_hashes[0][1] != device.frame_hashes[1][1]  # only an unused entry changed


# ---------------------------------------------------------------- the stream paths


def test_write_bit_and_write_byte_decode_the_same() -> None:
    stream = ss.init_screen(3, 2, 3) + ss.draw_raw([1, 2, 3, 4, 5, 6], 3) + ss.PRESENT
    by_bytes, by_bits = InMemoryScreen(), InMemoryScreen()
    ss.feed(by_bytes, stream)
    ss.feed_bits(by_bits, stream)
    assert by_bits.pixel_indices == by_bytes.pixel_indices == [1, 2, 3, 4, 5, 6]
    assert by_bits.frame_hashes[0][1] == by_bytes.frame_hashes[0][1]


def test_write_byte_after_a_partial_byte_decodes_the_same() -> None:
    stream = ss.init_screen(3, 2, 3) + ss.draw_raw([1, 2, 3, 4, 5, 6], 3) + ss.PRESENT
    bits = [(byte >> i) & 1 for byte in stream for i in range(8)]
    device = InMemoryScreen()
    for bit in bits[:3]:
        device.write_bit(bool(bit))  # off the byte boundary ...
    for k in range(3, len(bits) - 5, 8):  # ... so every write_byte goes bit by bit
        device.write_byte(sum(bits[k + i] << i for i in range(8)))
    for bit in bits[len(bits) - 5 :]:  # noqa: E203
        device.write_bit(bool(bit))
    assert device.pixel_indices == [1, 2, 3, 4, 5, 6] and device.frame_count == 1


def test_the_screen_never_reads_program_memory() -> None:
    device = InMemoryScreen()
    device.attach_memory(None)  # type: ignore[arg-type]  # the IODevice default: ignored
    ss.feed(device, ss.init_screen(1, 1, 1) + ss.draw_raw([1], 1) + ss.PRESENT)
    assert device.last_frame_rgb == [WHITE]


def test_control_a_misaligned_stream_is_refused() -> None:
    """the negative control of this file: drop the init's last byte, and draw_raw's opcode lands in the format field"""
    stream = ss.init_screen(8, 1, 3)[:-1] + bytes([0x10, 0xD1, 0x58, 0x1F])
    with pytest.raises(IODeviceException) as failure:
        ss.feed(InMemoryScreen(), stream)
    # the init's format field reads 0x10 (format 16, valid), and the stream is lost from there on
    assert str(failure.value) == (
        'ScreenIO: unknown (0xD1) at output byte 6, frame 0: '
        'opcode 0xD1 is not a command (commands: 0x01-0x07, 0x10, 0x11, 0x1F)'
    )


# ---------------------------------------------------------------- the subclass contracts


class PresentRecorder(InMemoryScreen):
    """overrides _present as downstream harnesses do: super() first, then read pixel_indices and palette."""

    def __init__(self) -> None:
        super().__init__()
        self.presents: List[Tuple[bytes, List[Tuple[int, int, int]]]] = []

    def _present(self) -> None:
        super()._present()
        self.presents.append((bytes(self.pixel_indices), list(self.palette)))


def test_present_runs_once_per_present_command_with_the_presented_picture_and_palette() -> None:
    device = PresentRecorder()
    ss.feed(device, ss.init_screen(2, 1, 1) + ss.set_max_fps(5) + ss.set_window_title(b'x') + ss.draw_raw([1, 0], 1))
    assert device.presents == []  # init_screen, setup and draws never present
    ss.feed(device, ss.PRESENT)
    ss.feed(device, ss.define_palette(3, 0, [(7, 7, 7), (8, 8, 8)], 1) + ss.select_palette(3) + ss.PRESENT)
    ss.feed(device, ss.init_screen(1, 1, 2))
    assert len(device.presents) == 2  # the re-init did not present
    ss.feed(device, ss.draw_raw([3], 2) + ss.PRESENT)
    assert device.presents == [
        (bytes([1, 0]), [BLACK, WHITE]),
        (bytes([1, 0]), [(7, 7, 7), (8, 8, 8)]),
        (bytes([3]), [(0, 0, 0), (85, 85, 85), (170, 170, 170), WHITE]),
    ]
    assert device.frame_count == 3


ExtensionResult = Optional[Union[Callable[[int], bool], bool]]


class HostChannelScreen(InMemoryScreen):
    """a test harness's multiplexed commands: 0x80 takes 2 payload bytes, 0x81 has none."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: List[Tuple[int, bytes]] = []

    def _extension_command(self, opcode: int) -> ExtensionResult:  # type: ignore[override]
        if opcode == 0x81:
            self.messages.append((0x81, b''))
            return True
        if opcode == 0x80:
            payload = bytearray()

            def take(byte: int) -> bool:
                payload.append(byte)
                if len(payload) == 2:
                    self.messages.append((0x80, bytes(payload)))
                    return True
                return False

            return take
        return None


def test_an_extension_command_takes_its_payload_bytes_and_parsing_resumes() -> None:
    device = HostChannelScreen()
    # the payload bytes look like commands (0x1F present, 0x00 reserved) and must not be parsed as such
    ss.feed(
        device, ss.init_screen(1, 1, 1) + bytes([0x80, 0x1F, 0x00]) + bytes([0x81]) + ss.draw_raw([1], 1) + ss.PRESENT
    )
    assert device.messages == [(0x80, bytes([0x1F, 0x00])), (0x81, b'')]
    assert device.frame_count == 1 and device.last_frame_rgb == [WHITE]


def test_an_extension_command_before_init_screen() -> None:
    device = HostChannelScreen()
    ss.feed(device, bytes([0x81, 0x80, 1, 2]) + ss.init_screen(1, 1, 1))
    assert device.messages == [(0x81, b''), (0x80, bytes([1, 2]))]


def test_the_byte_offsets_count_the_extension_bytes() -> None:
    device = HostChannelScreen()
    with pytest.raises(IODeviceException) as failure:
        ss.feed(device, ss.init_screen(1, 1, 1) + bytes([0x80, 5, 6, 0x81, 0x82]))
    assert str(failure.value) == (
        'ScreenIO: unknown (0x82) at output byte 10, frame 0: '
        'opcode 0x82 is not a command (commands: 0x01-0x07, 0x10, 0x11, 0x1F)'
    )


def test_control_without_the_hook_an_extension_opcode_is_unknown() -> None:
    with pytest.raises(IODeviceException) as failure:
        ss.feed(InMemoryScreen(), ss.init_screen(1, 1, 1) + bytes([0x80, 5, 6]))
    assert str(failure.value) == (
        'ScreenIO: unknown (0x80) at output byte 6, frame 0: '
        'opcode 0x80 is not a command (commands: 0x01-0x07, 0x10, 0x11, 0x1F)'
    )


def test_the_hook_is_not_asked_about_the_reserved_opcode() -> None:
    with pytest.raises(IODeviceException, match=r'opcode 0x00 is reserved'):
        ss.feed(HostChannelScreen(), bytes([0x00]))


# ---------------------------------------------------------------- last_frame_rgb


def test_last_frame_rgb_is_a_snapshot_not_a_live_view() -> None:
    device = screen_after(ss.init_screen(2, 1, 1) + ss.draw_raw([1, 1], 1) + ss.PRESENT)
    ss.feed(device, ss.draw_raw([0, 0], 1) + ss.define_palette(0, 1, [(5, 5, 5)], 1))  # the next frame arrives
    assert device.pixel_indices == [0, 0]
    assert device.last_frame_rgb == [WHITE, WHITE]


def test_png_pixels_decode_correctly(tmp_path: Path) -> None:
    device = screen_after(ss.init_screen(3, 2, 24) + bytes([0x10, *range(18)]) + ss.PRESENT, frames_dir=tmp_path)
    png = (tmp_path / 'frame_000000.png').read_bytes()
    width, height = struct.unpack('>II', png[16:24])
    assert (width, height) == (3, 2)
    start = png.index(b'IDAT')
    (length,) = struct.unpack('>I', png[start - 4 : start])  # noqa: E203
    rows = zlib.decompress(png[start + 4 : start + 4 + length])  # noqa: E203
    assert rows == bytes([0, *range(9), 0, *range(9, 18)])
    assert device.last_frame_rgb[4] == (12, 13, 14)


class CommonNamesScreen(InMemoryScreen):
    """a harness that uses common private names for its own state, before and after the device's __init__"""

    COMMON_NAMES = ('_bits', '_feed', '_parse', '_acc', '_byte', '_inp', '_error', '_command', '_parser', '_nacc')

    def __init__(self) -> None:
        for name in self.COMMON_NAMES:
            setattr(self, name, 0)
        super().__init__()
        for name in self.COMMON_NAMES:
            setattr(self, name, 7)


def test_a_subclass_using_common_private_names_still_decodes() -> None:
    device = CommonNamesScreen()
    ss.feed(device, ss.init_screen(2, 1, 8) + ss.draw_raw([3, 4], 8) + ss.PRESENT)
    assert device.frame_count == 1 and device.last_frame_rgb == [XTERM256[3], XTERM256[4]]
    assert all(getattr(device, name) == 7 for name in CommonNamesScreen.COMMON_NAMES)
    with pytest.raises(IODeviceException) as failure:  # and its errors are still the device's
        ss.feed(device, bytes([0x00]))
    assert str(failure.value) == (
        'ScreenIO: reserved (0x00) at output byte 10, frame 1: opcode 0x00 is reserved (a stray zero byte?)'
    )


def test_the_device_keeps_its_internals_under_one_prefix() -> None:
    """every private attribute and method of the device is _scr_..., except the documented hooks"""
    hooks = {'_present', '_extension_command', '_init_screen', '_set_window_title', '_set_window_icon', '_abc_impl'}
    device = InMemoryScreen()
    names = set(vars(device)) | {name for name in vars(InMemoryScreen)}
    private = {name for name in names if name.startswith('_') and not name.startswith('__')}
    assert private - hooks and all(name.startswith('_scr_') for name in private - hooks), private - hooks


def test_end_of_run_inside_a_command_raises_and_between_commands_does_not() -> None:
    complete = screen_after(ss.init_screen(2, 1, 8) + ss.PRESENT)
    complete.end_of_run()
    cut = screen_after(ss.init_screen(2, 1, 8) + bytes([0x10, 1]))
    with pytest.raises(IODeviceException) as failure:
        cut.end_of_run()
    assert str(failure.value) == (
        'ScreenIO: draw_raw (0x10) at output byte 6, frame 0, pixel (1, 0): '
        "the program's output ended inside draw_raw (0x10): 16 of its bits read"
    )


def test_an_unfinished_extension_command_is_silent_at_the_end() -> None:
    device = HostChannelScreen()
    ss.feed(device, ss.init_screen(1, 1, 1) + bytes([0x80, 5]))
    device.write_bit(True)  # even a partial byte inside its payload
    device.end_of_run()
    assert device.get_output() == b''
