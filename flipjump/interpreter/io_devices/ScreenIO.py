"""
the InMemoryScreen output-device - headless-first.

The wire format is io_devices/README.md ("Screen"), and that file is the authority: this module
implements it.

Every error raises IODeviceException with a message of the form
    ScreenIO: <command> (0xNN) at output byte <offset>, frame <n>[, column <x>, cursor <c> | pixel (<x>, <y>)]: <reason>
The interpreter calls `end_of_run` when a run ends by itself; it raises if the output stopped inside a command.
The device's own internals are all named `_scr_...` (the subclass hooks `_init_screen`, `_set_window_title`,
`_set_window_icon`, `_present` and `_extension_command` aside), so a subclass's private names cannot collide with them.

The headless backend (this class) keeps, per `present`: the frame count, a (time ns, sha256) hash
(`frame_hashes`), the presented picture as RGB (`last_frame_rgb`, expanded lazily) and a PNG per
frame when `frames_dir` is given. It never waits (`max_fps` is recorded; only the interactive
window in pygame_window.py waits).

The frame hash:
- indexed formats: sha256(the picture's values, row-major, one byte each for formats 1-8 and two
  bytes little-endian each for formats 9-16  +  the RGB bytes of all 2^B entries of the palette
  selected at present);
- format 24: sha256(the picture's row-major R, G, B bytes).

Subclass hook (not part of the wire API): `_extension_command(opcode)` is consulted for an opcode
byte that is not an API opcode (and not the reserved 0x00). The base returns None, which makes it
the normal unknown-opcode error. A subclass may instead return
- True: a recognised extension command with no payload, already complete - parsing resumes at the
  very next byte;
- a callable: the device hands it every following output byte, one whole byte at a time, until it
  returns True, and resumes parsing commands at the next byte.
The byte offsets and the frame count keep counting through these bytes. A test harness uses this to
multiplex its own messages into the stream.
"""

import hashlib
import struct
import time
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Callable, Generator, List, Literal, Optional, Tuple, Union

from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.utils.exceptions import IODeviceException, IOReadOnEOF

RGB = Tuple[int, int, int]

CMD_RESERVED = 0x00
CMD_INIT_SCREEN = 0x01
CMD_DEFINE_PALETTE = 0x02
CMD_SET_MAX_FPS = 0x03
CMD_SET_WINDOW_TITLE = 0x04
CMD_SET_WINDOW_ICON = 0x05
CMD_SELECT_PALETTE = 0x06
CMD_BUILTIN_PALETTE = 0x07
CMD_DRAW_RAW = 0x10
CMD_DRAW_COLLINES = 0x11
CMD_PRESENT = 0x1F

COMMAND_NAMES = {
    CMD_INIT_SCREEN: 'init_screen',
    CMD_DEFINE_PALETTE: 'define_palette',
    CMD_SET_MAX_FPS: 'set_max_fps',
    CMD_SET_WINDOW_TITLE: 'set_window_title',
    CMD_SET_WINDOW_ICON: 'set_window_icon',
    CMD_SELECT_PALETTE: 'select_palette',
    CMD_BUILTIN_PALETTE: 'builtin_palette',
    CMD_DRAW_RAW: 'draw_raw',
    CMD_DRAW_COLLINES: 'draw_collines',
    CMD_PRESENT: 'present',
}
PALETTE_COMMANDS = (CMD_DEFINE_PALETTE, CMD_SELECT_PALETTE, CMD_BUILTIN_PALETTE)

FORMAT_RGB = 24
INDEXED_FORMATS = range(1, 17)
PALETTE_SLOTS = 256

MAX_WIDTH = 65_534  # C = 16 needs w < 2^16 - 1
MAX_HEIGHT = 65_531  # R = 16 needs h < 2^16 - 4
FIELD_WIDTHS = (4, 8, 12, 16)

BUILTIN_BLACK_WHITE = 0
BUILTIN_GREY_RAMP = 1
BUILTIN_EGA16 = 2
BUILTIN_XTERM256 = 3
BUILTIN_RGB_CUBE = 4
BUILTIN_IDS = range(5)

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def row_field_width(height: int) -> int:
    """R: the smallest of 4, 8, 12, 16 with h < 2^R - 4."""
    return next(r for r in FIELD_WIDTHS if height < (1 << r) - 4)


def column_field_width(width: int) -> int:
    """C: the smallest of 4, 8, 12, 16 with w < 2^C - 1."""
    return next(c for c in FIELD_WIDTHS if width < (1 << c) - 1)


# ---------------------------------------------------------------- palettes


def scale_level(level: int, bits: int) -> int:
    """a `bits`-bit channel level as an 8-bit one: round(level * 255 / (2^bits - 1)), halves up;
    a channel of 0 bits is always 0."""
    if bits == 0:
        return 0
    top = (1 << bits) - 1
    return (level * 510 + top) // (2 * top)


EGA16: Tuple[RGB, ...] = (
    (0, 0, 0),
    (0, 0, 170),
    (0, 170, 0),
    (0, 170, 170),
    (170, 0, 0),
    (170, 0, 170),
    (170, 85, 0),
    (170, 170, 170),
    (85, 85, 85),
    (85, 85, 255),
    (85, 255, 85),
    (85, 255, 255),
    (255, 85, 85),
    (255, 85, 255),
    (255, 255, 85),
    (255, 255, 255),
)

_XTERM16: Tuple[RGB, ...] = (
    (0, 0, 0),
    (128, 0, 0),
    (0, 128, 0),
    (128, 128, 0),
    (0, 0, 128),
    (128, 0, 128),
    (0, 128, 128),
    (192, 192, 192),
    (128, 128, 128),
    (255, 0, 0),
    (0, 255, 0),
    (255, 255, 0),
    (0, 0, 255),
    (255, 0, 255),
    (0, 255, 255),
    (255, 255, 255),
)
_XTERM_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
XTERM256: Tuple[RGB, ...] = (
    _XTERM16
    + tuple(
        (_XTERM_CUBE_LEVELS[r], _XTERM_CUBE_LEVELS[g], _XTERM_CUBE_LEVELS[b])
        for r in range(6)
        for g in range(6)
        for b in range(6)
    )
    + tuple((8 + 10 * k, 8 + 10 * k, 8 + 10 * k) for k in range(24))
)


def rgb_cube_split(bits: int) -> Tuple[int, int, int]:
    """the RGB cube's (red, green, blue) bit counts: green ceil(b/3), red ceil((b - green)/2), blue the rest."""
    green = -(-bits // 3)
    red = -(-(bits - green) // 2)
    return red, green, bits - green - red


def _fit(colours: Tuple[RGB, ...], bits: int) -> Tuple[RGB, ...]:
    """exactly 2^bits entries: cut a longer list, pad a shorter one with black."""
    size = 1 << bits
    return colours[:size] + ((0, 0, 0),) * (size - len(colours))


@lru_cache(maxsize=None)
def builtin_palette(builtin_id: int, bits: int) -> Tuple[RGB, ...]:
    """the built-in palette `builtin_id` for a format of `bits` bits per colour: exactly 2^bits entries."""
    size = 1 << bits
    if builtin_id == BUILTIN_BLACK_WHITE:
        return _fit(((0, 0, 0), (255, 255, 255)), bits)
    if builtin_id == BUILTIN_GREY_RAMP:
        return tuple((level, level, level) for level in (scale_level(i, bits) for i in range(size)))
    if builtin_id == BUILTIN_EGA16:
        return _fit(EGA16, bits)
    if builtin_id == BUILTIN_XTERM256:
        return _fit(XTERM256, bits)
    if builtin_id == BUILTIN_RGB_CUBE:
        red_bits, green_bits, blue_bits = rgb_cube_split(bits)
        return tuple(
            (
                scale_level(value >> (green_bits + blue_bits), red_bits),
                scale_level((value >> blue_bits) & ((1 << green_bits) - 1), green_bits),
                scale_level(value & ((1 << blue_bits) - 1), blue_bits),
            )
            for value in range(size)
        )
    raise ValueError(f'no built-in palette {builtin_id}')


def default_palette(screen_format: int) -> Tuple[RGB, ...]:
    """the palette every slot holds after init_screen."""
    builtin_id = {1: BUILTIN_BLACK_WHITE, 2: BUILTIN_GREY_RAMP, 4: BUILTIN_EGA16, 8: BUILTIN_XTERM256}.get(
        screen_format, BUILTIN_RGB_CUBE
    )
    return builtin_palette(builtin_id, screen_format)


def rgb_of(value: int) -> RGB:
    """a format-24 colour value (R in the low byte) as an RGB tuple."""
    return value & 0xFF, (value >> 8) & 0xFF, value >> 16


# ---------------------------------------------------------------- png


def encode_png(width: int, height: int, rgb_rows: bytes) -> bytes:
    """encode a minimal 8-bit RGB PNG (no filtering; rgb_rows is height*(1+3*width) bytes
    of filter-byte-0-prefixed scanlines)."""

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            struct.pack('>I', len(data))
            + chunk_type
            + data
            + struct.pack('>I', zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)  # 8-bit, color-type 2 (RGB)
    return PNG_SIGNATURE + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(rgb_rows)) + chunk(b'IEND', b'')


class _OutputEnded(Exception):
    """thrown into the parser when the program's output is declared over inside a command."""


Parser = Generator[None, int, None]


class InMemoryScreen(IODevice):
    """see the module docstring for the command stream."""

    def __init__(self, *, frames_dir: Optional[Path] = None):
        self.frames_dir = frames_dir

        self.width = 0
        self.height = 0
        self.bpp = 8
        self.pixel_indices: List[int] = []
        # slots never written may share one list object: read-only; the device copies a slot before writing it
        self.palettes: List[List[RGB]] = []
        self.selected_slot = 0
        self.palette: List[RGB] = []  # the slot selected at the last present

        self.frame_count = 0
        self.frame_hashes: List[Tuple[int, str]] = []  # (present-time ns, sha256 hexdigest)

        self.window_title: Optional[str] = None
        self.window_icon: Optional[Tuple[int, int, List[RGB]]] = None
        self.max_fps = 0

        # the field widths init_screen fixes: R bits per row field, C per column field
        self._scr_row_bits = 0
        self._scr_column_bits = 0
        # which slots own their list (a slot that does not may share it, and is copied before a write)
        self._scr_owned_slots: List[bool] = []
        # (format, the presented values, the presented palette) - expanded only when asked
        self._scr_presented: Optional[Tuple[int, Tuple[int, ...], List[RGB]]] = None
        self._scr_palette_bytes_cache: Optional[Tuple[List[RGB], bytes]] = None

        # the output stream: a partial byte, the bytes handed to the parser, and the parser's bit buffer
        self._scr_current_byte = 0
        self._scr_bits_count = 0
        self._scr_bytes_in = 0
        self._scr_acc = 0
        self._scr_nacc = 0
        # the command being parsed: (name, opcode, offset of its opcode byte), None between commands
        self._scr_command: Optional[Tuple[str, int, int]] = None
        self._scr_failure: Optional[IODeviceException] = None
        self._scr_in_extension = False  # inside an _extension_command's payload
        self._scr_parser: Parser = self._scr_parse()
        next(self._scr_parser)

    # ---------------------------------------------------------------- output stream

    def write_bit(self, bit: bool) -> None:
        self._scr_current_byte |= bit << self._scr_bits_count
        self._scr_bits_count += 1
        if self._scr_bits_count == 8:
            byte, self._scr_current_byte, self._scr_bits_count = self._scr_current_byte, 0, 0
            self.write_byte(byte)

    def write_byte(self, byte: int) -> None:
        if self._scr_bits_count:  # never from the engine (it gathers bytes from a byte boundary only)
            super().write_byte(byte)
            return
        if self._scr_failure is not None:
            raise self._scr_failure
        self._scr_bytes_in += 1
        try:
            self._scr_parser.send(byte)
        except IODeviceException as failure:
            self._scr_failure = failure
            raise

    def read_bit(self) -> bool:
        raise IOReadOnEOF('the screen device has no input - use a device that also reads input (e.g. --io pc)')

    def end_of_run(self) -> None:
        """the run is over: the program's output must not have ended inside a command (the README's "output ends
        inside a command" error). An unfinished _extension_command payload is the subclass's business: silent."""
        self._scr_check_output_complete()

    def get_output(self, *, allow_incomplete_output: bool = False) -> bytes:
        """the screen has no byte output. Unless `allow_incomplete_output`, the program's output must not
        have ended inside a command (as end_of_run checks)."""
        if not allow_incomplete_output:
            self._scr_check_output_complete()
        return b''

    # ---------------------------------------------------------------- command decoding

    def _extension_command(self, opcode: int) -> Optional[Union[Callable[[int], bool], Literal[True]]]:
        """a subclass hook, not part of the wire API: see the module docstring. None = an unknown opcode."""
        return None

    def _scr_check_output_complete(self) -> None:
        if self._scr_failure is not None:
            raise self._scr_failure
        if self._scr_in_extension:
            return
        if self._scr_command is not None:
            try:
                self._scr_parser.throw(_OutputEnded())
            except IODeviceException as failure:
                self._scr_failure = failure
                raise
        elif self._scr_bits_count:
            raise IODeviceException(
                f'ScreenIO: an opcode at output byte {self._scr_bytes_in:,}, frame {self.frame_count:,}: '
                f"the program's output ended inside an opcode byte: {self._scr_bits_count} of its 8 bits read"
            )

    def _scr_bits(self, count: int) -> Generator[None, int, int]:
        """read the next `count` bits of the stream as a number (lsb first)."""
        while self._scr_nacc < count:
            self._scr_acc |= (yield) << self._scr_nacc
            self._scr_nacc += 8
        value = self._scr_acc & ((1 << count) - 1)
        self._scr_acc >>= count
        self._scr_nacc -= count
        return value

    def _scr_error(
        self,
        reason: str,
        *,
        column: Optional[int] = None,
        cursor: Optional[int] = None,
        pixel: Optional[Tuple[int, int]] = None,
    ) -> IODeviceException:
        assert self._scr_command is not None
        name, opcode, offset = self._scr_command
        where = f'ScreenIO: {name} (0x{opcode:02X}) at output byte {offset:,}, frame {self.frame_count:,}'
        if column is not None:
            where += f', column {column:,}, cursor {cursor:,}'
        if pixel is not None:
            where += f', pixel ({pixel[0]:,}, {pixel[1]:,})'
        return IODeviceException(f'{where}: {reason}')

    def _scr_ended_reason(self) -> str:
        assert self._scr_command is not None
        name, opcode, offset = self._scr_command
        bits = (self._scr_bytes_in - offset) * 8 + self._scr_bits_count
        return f"the program's output ended inside {name} (0x{opcode:02X}): {bits:,} of its bits read"

    def _scr_parse(self) -> Parser:
        """the command parser: a generator sent one output byte at a time."""
        handlers = {opcode: getattr(self, f'_scr_cmd_{name}') for opcode, name in COMMAND_NAMES.items()}
        while True:
            assert self._scr_nacc == 0  # every command starts on a byte boundary
            opcode = yield from self._scr_bits(8)
            offset = self._scr_bytes_in - 1
            name = COMMAND_NAMES.get(opcode)
            if name is None:
                if opcode == CMD_RESERVED:
                    self._scr_command = ('reserved', opcode, offset)
                    raise self._scr_error('opcode 0x00 is reserved (a stray zero byte?)')
                extension = self._extension_command(opcode)
                if extension is None:
                    self._scr_command = ('unknown', opcode, offset)
                    raise self._scr_error(
                        f'opcode 0x{opcode:02X} is not a command (commands: 0x01-0x07, 0x10, 0x11, 0x1F)'
                    )
                if extension is not True:
                    self._scr_in_extension = True
                    while not extension((yield)):
                        pass
                    self._scr_in_extension = False
                continue

            self._scr_command = (name, opcode, offset)
            if opcode != CMD_INIT_SCREEN and self.width == 0:
                raise self._scr_error(
                    f'{name} (0x{opcode:02X}) before init_screen: the first command must be init_screen (0x01)'
                )
            if opcode in PALETTE_COMMANDS and self.bpp == FORMAT_RGB:
                raise self._scr_error(
                    f'{name} (0x{opcode:02X}) in format 24 (direct RGB): palettes exist only in formats 1-16'
                )
            try:
                yield from handlers[opcode]()
            except _OutputEnded:
                raise self._scr_error(self._scr_ended_reason()) from None

            padding_bits, padding = self._scr_nacc, self._scr_acc
            self._scr_acc = self._scr_nacc = 0
            if padding:
                raise self._scr_error(
                    f'the padding bits after the command are 0b{padding:0{padding_bits}b}, '
                    'not zero (the stream is probably misaligned)'
                )
            self._scr_command = None

    # ---------------------------------------------------------------- screen functions

    def _scr_cmd_init_screen(self) -> Generator[None, int, None]:
        width = yield from self._scr_bits(16)
        height = yield from self._scr_bits(16)
        bpp = yield from self._scr_bits(8)
        if not 1 <= width <= MAX_WIDTH:
            raise self._scr_error(f'w = {width:,}: must be 1..{MAX_WIDTH:,}')
        if not 1 <= height <= MAX_HEIGHT:
            raise self._scr_error(f'h = {height:,}: must be 1..{MAX_HEIGHT:,}')
        if bpp not in INDEXED_FORMATS and bpp != FORMAT_RGB:
            raise self._scr_error(f'format = {bpp}: must be 1-16 or 24')
        self._init_screen(width, height, bpp)

    def _init_screen(self, width: int, height: int, bpp: int) -> None:
        """the life cycle's init_screen: everything but the title, the icon, max fps and the frame count resets."""
        self.width, self.height, self.bpp = width, height, bpp
        self._scr_row_bits = row_field_width(height)
        self._scr_column_bits = column_field_width(width)
        self.pixel_indices = [0] * (width * height)
        if bpp == FORMAT_RGB:
            self.palettes = []
            self._scr_owned_slots = []
        else:
            shared_default = list(default_palette(bpp))
            self.palettes = [shared_default] * PALETTE_SLOTS
            self._scr_owned_slots = [False] * PALETTE_SLOTS
        self.selected_slot = 0

    def _scr_writable_slot(self, slot: int) -> List[RGB]:
        if not self._scr_owned_slots[slot]:
            self.palettes[slot] = list(self.palettes[slot])
            self._scr_owned_slots[slot] = True
        return self.palettes[slot]

    def _scr_cmd_define_palette(self) -> Generator[None, int, None]:
        slot = yield from self._scr_bits(8)
        first = yield from self._scr_bits(self.bpp)
        last = yield from self._scr_bits(self.bpp)
        if first > last:
            raise self._scr_error(f'first = {first:,} > last = {last:,}')
        entries = []
        for _ in range(last - first + 1):
            entries.append(
                ((yield from self._scr_bits(8)), (yield from self._scr_bits(8)), (yield from self._scr_bits(8)))
            )
        self._scr_writable_slot(slot)[first : last + 1] = entries  # noqa: E203

    def _scr_cmd_set_max_fps(self) -> Generator[None, int, None]:
        self.max_fps = yield from self._scr_bits(8)

    def _scr_cmd_set_window_title(self) -> Generator[None, int, None]:
        title = bytearray()
        while True:
            byte = yield from self._scr_bits(8)
            if byte == 0:
                break
            title.append(byte)
        try:
            text = title.decode('utf-8')
        except UnicodeDecodeError as bad:
            raise self._scr_error(
                f'set_window_title: title byte {bad.start:,} (0x{title[bad.start]:02X}) is not valid UTF-8'
            ) from None
        self._set_window_title(text)

    def _set_window_title(self, title: str) -> None:
        self.window_title = title

    def _scr_cmd_set_window_icon(self) -> Generator[None, int, None]:
        width = yield from self._scr_bits(8)
        height = yield from self._scr_bits(8)
        if width == 0:
            raise self._scr_error('set_window_icon: w = 0: must be 1..255')
        if height == 0:
            raise self._scr_error('set_window_icon: h = 0: must be 1..255')
        bits = self.bpp
        values = []
        x = y = 0
        try:
            for y in range(height):
                for x in range(width):
                    values.append((yield from self._scr_bits(bits)))
        except _OutputEnded:
            raise self._scr_error(self._scr_ended_reason(), pixel=(x, y)) from None
        if self.bpp == FORMAT_RGB:
            colours = [rgb_of(value) for value in values]
        else:
            palette = self.palettes[self.selected_slot]
            colours = [palette[value] for value in values]
        self._set_window_icon(width, height, colours)

    def _set_window_icon(self, width: int, height: int, colours: List[RGB]) -> None:
        self.window_icon = (width, height, colours)

    def _scr_cmd_select_palette(self) -> Generator[None, int, None]:
        self.selected_slot = yield from self._scr_bits(8)

    def _scr_cmd_builtin_palette(self) -> Generator[None, int, None]:
        slot = yield from self._scr_bits(8)
        builtin_id = yield from self._scr_bits(8)
        if builtin_id not in BUILTIN_IDS:
            raise self._scr_error(f'id = {builtin_id}: the built-ins are 0..4')
        self.palettes[slot] = list(builtin_palette(builtin_id, self.bpp))
        self._scr_owned_slots[slot] = True

    def _scr_cmd_draw_raw(self) -> Generator[None, int, None]:
        bits, width, picture = self.bpp, self.width, self.pixel_indices
        x = y = 0
        try:
            for y in range(self.height):
                row = y * width
                for x in range(width):
                    picture[row + x] = yield from self._scr_bits(bits)
        except _OutputEnded:
            raise self._scr_error(self._scr_ended_reason(), pixel=(x, y)) from None

    def _scr_cmd_draw_collines(self) -> Generator[None, int, None]:
        width, height, picture = self.width, self.height, self.pixel_indices
        row_bits, column_bits, colour_bits = self._scr_row_bits, self._scr_column_bits, self.bpp
        row_mask, column_mask, colour_mask = (1 << row_bits) - 1, (1 << column_bits) - 1, (1 << colour_bits) - 1
        end_command, reserved_column = column_mask, column_mask - 1
        end, keep, copy, reserved_row = row_mask, row_mask - 1, row_mask - 2, row_mask - 3
        acc = nacc = 0  # the bit buffer, local and read inline (not a _scr_bits generator per field): the hot path
        x: Optional[int] = None
        cursor = 0

        def refuse(reason: str) -> IODeviceException:
            return self._scr_error(reason, column=x, cursor=cursor)

        try:
            while True:
                x = None
                while nacc < column_bits:
                    acc |= (yield) << nacc
                    nacc += 8
                column, acc, nacc = acc & column_mask, acc >> column_bits, nacc - column_bits
                if column == end_command:
                    self._scr_acc, self._scr_nacc = acc, nacc
                    return
                if column == reserved_column:
                    raise self._scr_error(
                        f'x = 0x{column:0{column_bits // 4}X} is the reserved column token '
                        f'(2^C - 2, C = {column_bits})'
                    )
                if column >= width:
                    raise self._scr_error(f'x = {column:,}: the screen has columns 0..{width - 1:,} (w = {width:,})')
                x, cursor = column, 0
                while True:  # only END closes a column, also once the cursor reached h
                    while nacc < row_bits:
                        acc |= (yield) << nacc
                        nacc += 8
                    token, acc, nacc = acc & row_mask, acc >> row_bits, nacc - row_bits
                    if token == end:
                        break
                    field = 'y2'
                    if token == keep or token == copy:
                        if token == copy and x == 0:
                            raise refuse('COPY copies column x - 1, and column 0 has none')
                        field = 'y'
                        while nacc < row_bits:
                            acc |= (yield) << nacc
                            nacc += 8
                        row, acc, nacc = acc & row_mask, acc >> row_bits, nacc - row_bits
                    else:
                        row = token
                    if row == reserved_row:
                        raise refuse(f'0x{row:0{row_bits // 4}X} is the reserved row token (2^R - 4, R = {row_bits})')
                    if row > height:
                        raise refuse(f'{field} = {row:,} is past the screen (rows end at h = {height:,})')
                    if row < cursor:  # row == cursor: an item of zero rows, which paints nothing
                        raise refuse(
                            f'{field} = {row:,} is behind the cursor (need {cursor:,} <= {field} <= {height:,})'
                        )
                    start, stop = cursor * width + x, row * width + x
                    if token == copy:
                        picture[start:stop:width] = picture[start - 1 : stop - 1 : width]  # noqa: E203
                    elif token != keep:
                        while nacc < colour_bits:
                            acc |= (yield) << nacc
                            nacc += 8
                        colour, acc, nacc = acc & colour_mask, acc >> colour_bits, nacc - colour_bits
                        picture[start:stop:width] = [colour] * (row - cursor)
                    cursor = row
        except _OutputEnded:
            if x is None:
                raise self._scr_error(self._scr_ended_reason()) from None
            raise refuse(self._scr_ended_reason()) from None

    def _scr_cmd_present(self) -> Generator[None, int, None]:
        self._present()
        return
        yield  # a generator, like every command

    def _scr_palette_bytes(self, palette: List[RGB]) -> bytes:
        cached = self._scr_palette_bytes_cache
        if cached is not None and cached[0] is palette:
            return cached[1]
        palette_bytes = bytes(channel for colour in palette for channel in colour)
        self._scr_palette_bytes_cache = (palette, palette_bytes)
        return palette_bytes

    def _present(self) -> None:
        """record the picture as a frame: the snapshot behind last_frame_rgb, the hash, the PNG."""
        values = tuple(self.pixel_indices)
        if self.bpp == FORMAT_RGB:
            palette: List[RGB] = []
            hashed = b''.join(value.to_bytes(3, 'little') for value in values)
        else:
            palette = self.palettes[self.selected_slot]
            # the presented list is frozen from here on: a later define_palette copies the slot first
            self._scr_owned_slots[self.selected_slot] = False
            if self.bpp <= 8:
                value_bytes = bytes(values)
            else:
                value_bytes = struct.pack(f'<{len(values)}H', *values)
            hashed = value_bytes + self._scr_palette_bytes(palette)
        self.palette = palette
        self._scr_presented = (self.bpp, values, palette)
        self.frame_count += 1
        self.frame_hashes.append((time.time_ns(), hashlib.sha256(hashed).hexdigest()))

        if self.frames_dir is not None:
            self.frames_dir.mkdir(parents=True, exist_ok=True)
            frame_rgb = self.last_frame_rgb  # bound once: the property expands the whole frame on every read
            row_slices = [
                frame_rgb[row * self.width : (row + 1) * self.width] for row in range(self.height)  # noqa: E203
            ]
            rows = b''.join(b'\x00' + b''.join(bytes(rgb) for rgb in row_slice) for row_slice in row_slices)
            png_path = self.frames_dir / f'frame_{self.frame_count - 1:06d}.png'
            png_path.write_bytes(encode_png(self.width, self.height, rows))

    @property
    def last_frame_rgb(self) -> List[RGB]:
        """the last presented frame, expanded to row-major RGB tuples ([] before the first present).

        LAZY: the interactive window does not need it (it hands SDL the indices). The INPUTS are
        snapshotted at present time, so this is the frame that was presented, not whatever the device
        has been drawing since."""
        if self._scr_presented is None:
            return []
        screen_format, values, palette = self._scr_presented
        if screen_format == FORMAT_RGB:
            return [rgb_of(value) for value in values]
        return [palette[value] for value in values]
