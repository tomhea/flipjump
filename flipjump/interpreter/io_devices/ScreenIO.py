"""
the InMemoryScreen output-device, headless-first: io_devices/README.md's "Screen" (the wire format, its errors and
the frame hash). Its internals are all `_scr_...` (the subclass hooks aside), so a subclass's names cannot collide.
"""

import hashlib
import struct
import time
import zlib
from functools import lru_cache
from itertools import chain
from pathlib import Path
from typing import Callable, Generator, List, Literal, Optional, Tuple, Union

from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.utils.exceptions import IODeviceException, IOReadOnEOF

RGB = Tuple[int, int, int]

# opcode -> command; each is parsed by the `_scr_cmd_<name>` method
COMMAND_NAMES = {
    0x01: 'init_screen',
    0x02: 'define_palette',
    0x03: 'set_max_fps',
    0x04: 'set_window_title',
    0x05: 'set_window_icon',
    0x06: 'select_palette',
    0x07: 'builtin_palette',
    0x10: 'draw_raw',
    0x11: 'draw_collines',
    0x1F: 'present',
}
PALETTE_COMMANDS = ('define_palette', 'select_palette', 'builtin_palette')

FORMAT_RGB = 24
PALETTE_SLOTS = 256
MAX_WIDTH = 65_534  # C = 16 needs w < 2^16 - 1
MAX_HEIGHT = 65_531  # R = 16 needs h < 2^16 - 4
FIELD_WIDTHS = (4, 8, 12, 16)


def row_field_width(height: int) -> int:
    """R: the smallest of 4, 8, 12, 16 with h < 2^R - 4."""
    return next(r for r in FIELD_WIDTHS if height < (1 << r) - 4)


def column_field_width(width: int) -> int:
    """C: the smallest of 4, 8, 12, 16 with w < 2^C - 1."""
    return next(c for c in FIELD_WIDTHS if width < (1 << c) - 1)


# ---------------------------------------------------------------- palettes


def scale_level(level: int, bits: int) -> int:
    """a `bits`-bit channel level as an 8-bit one: round(level * 255 / (2^bits - 1)), halves up; 0 bits give 0."""
    if bits == 0:
        return 0
    top = (1 << bits) - 1
    return (level * 510 + top) // (2 * top)


def _hex_colours(colours: str) -> Tuple[RGB, ...]:
    """'rrggbb rrggbb ...' as RGB tuples."""
    return tuple((int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)) for c in colours.split())


EGA16 = _hex_colours(
    '000000 0000aa 00aa00 00aaaa aa0000 aa00aa aa5500 aaaaaa 555555 5555ff 55ff55 55ffff ff5555 ff55ff ffff55 ffffff'
)
_XTERM16 = _hex_colours(
    '000000 800000 008000 808000 000080 800080 008080 c0c0c0 808080 ff0000 00ff00 ffff00 0000ff ff00ff 00ffff ffffff'
)
_XTERM_LEVELS = (0, 95, 135, 175, 215, 255)
XTERM256: Tuple[RGB, ...] = (
    _XTERM16
    + tuple((_XTERM_LEVELS[v // 36], _XTERM_LEVELS[v // 6 % 6], _XTERM_LEVELS[v % 6]) for v in range(216))
    + tuple((8 + 10 * k,) * 3 for k in range(24))
)


def rgb_cube_split(bits: int) -> Tuple[int, int, int]:
    """the RGB cube's (red, green, blue) bit counts: green ceil(b/3), red ceil((b - green)/2), blue the rest."""
    green = -(-bits // 3)
    red = -(-(bits - green) // 2)
    return red, green, bits - green - red


@lru_cache(maxsize=None)
def builtin_palette(builtin_id: int, bits: int) -> Tuple[RGB, ...]:
    """built-in `builtin_id` (0 black/white, 1 grey ramp, 2 EGA-16, 3 xterm-256, 4 the RGB cube) for a format of
    `bits` bits per colour: exactly 2^bits entries (a longer list cut, a shorter one padded with black)."""
    size = 1 << bits
    if builtin_id == 1:
        colours = tuple((scale_level(i, bits),) * 3 for i in range(size))
    elif builtin_id == 4:
        red_bits, green_bits, blue_bits = rgb_cube_split(bits)
        colours = tuple(
            (
                scale_level(value >> (green_bits + blue_bits), red_bits),
                scale_level((value >> blue_bits) & ((1 << green_bits) - 1), green_bits),
                scale_level(value & ((1 << blue_bits) - 1), blue_bits),
            )
            for value in range(size)
        )
    elif builtin_id in (0, 2, 3):
        colours = {0: ((0, 0, 0), (255, 255, 255)), 2: EGA16, 3: XTERM256}[builtin_id]
    else:
        raise ValueError(f'no built-in palette {builtin_id}')
    return colours[:size] + ((0, 0, 0),) * (size - len(colours))


def default_palette(screen_format: int) -> Tuple[RGB, ...]:
    """the palette every slot holds after init_screen: formats 1, 2, 4, 8 get built-ins 0-3, the others the cube."""
    return builtin_palette({1: 0, 2: 1, 4: 2, 8: 3}.get(screen_format, 4), screen_format)


def rgb_of(value: int) -> RGB:
    """a format-24 colour value (R in the low byte) as an RGB tuple."""
    return value & 0xFF, (value >> 8) & 0xFF, value >> 16


def to_rgb(values: Union[List[int], Tuple[int, ...]], palette: List[RGB]) -> List[RGB]:
    """colour values as RGB tuples: through `palette`, or as format-24 values when it is empty."""
    return [palette[value] for value in values] if palette else [rgb_of(value) for value in values]


def encode_png(width: int, height: int, rgb_rows: bytes) -> bytes:
    """a minimal 8-bit RGB PNG (rgb_rows: height * (1 + 3 * width) bytes of filter-byte-0-prefixed scanlines)."""

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + chunk_type + data + struct.pack('>I', zlib.crc32(chunk_type + data))

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)  # 8-bit, color-type 2 (RGB)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(rgb_rows)) + chunk(b'IEND', b'')


class _OutputEnded(Exception):
    """thrown into the parser when the program's output is declared over inside a command."""


Parser = Generator[None, int, None]


class InMemoryScreen(IODevice):
    """the screen: the command stream of io_devices/README.md, presented as frames in memory."""

    def __init__(self, *, frames_dir: Optional[Path] = None):
        self.frames_dir = frames_dir
        self.width = self.height = 0
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

        self._scr_row_bits = self._scr_column_bits = 0  # init_screen's field widths: R and C
        self._scr_default: List[RGB] = []  # init_screen's palette, the list every slot starts sharing
        self._scr_presented: Optional[Tuple[int, ...]] = None  # the presented values (expanded only when asked)
        self._scr_palette_bytes_cache: Optional[Tuple[List[RGB], bytes]] = None
        # the output stream: a partial byte, the bytes handed to the parser, and the parser's bit buffer
        self._scr_current_byte = self._scr_bits_count = self._scr_bytes_in = 0
        self._scr_acc = self._scr_nacc = 0
        self._scr_command: Optional[Tuple[str, int, int]] = None  # (name, opcode, its byte offset) inside a command
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
        self._scr_parser.send(byte)

    def read_bit(self) -> bool:
        raise IOReadOnEOF('the screen device has no input - use a device that also reads input (e.g. --io pc)')

    def end_of_run(self) -> None:
        """the output must not have ended inside a command (inside an _extension_command is the subclass's business)"""
        if self._scr_failure is not None:
            raise self._scr_failure
        if self._scr_in_extension:
            return
        if self._scr_command is not None:
            self._scr_parser.throw(_OutputEnded())
        elif self._scr_bits_count:
            raise IODeviceException(
                f'ScreenIO: an opcode at output byte {self._scr_bytes_in:,}, frame {self.frame_count:,}: '
                f"the program's output ended inside an opcode byte: {self._scr_bits_count} of its 8 bits read"
            )

    def get_output(self, *, allow_incomplete_output: bool = False) -> bytes:
        """the screen has no byte output. unless `allow_incomplete_output`, checks what end_of_run checks."""
        if not allow_incomplete_output:
            self.end_of_run()
        return b''

    # ---------------------------------------------------------------- command decoding

    def _extension_command(self, opcode: int) -> Optional[Union[Callable[[int], bool], Literal[True]]]:
        """
        a subclass hook (not the wire API) for an opcode that is not a command (nor 0x00). None: the unknown-opcode
        error. True: a command with no payload. A callable: handed every next output byte until it returns True;
        commands resume at the next byte. The byte offsets and the frame count count through these bytes.
        """
        return None

    def _scr_bits(self, count: int) -> Generator[None, int, int]:
        """read the next `count` bits of the stream as a number (lsb first)."""
        while self._scr_nacc < count:
            self._scr_acc |= (yield) << self._scr_nacc
            self._scr_nacc += 8
        value = self._scr_acc & ((1 << count) - 1)
        self._scr_acc >>= count
        self._scr_nacc -= count
        return value

    def _scr_error(self, reason: Optional[str] = None, at: str = '') -> IODeviceException:
        """the current command's error, `at` a place inside it; no reason: the output ended inside the command"""
        assert self._scr_command is not None
        name, opcode, offset = self._scr_command
        if reason is None:
            bits = (self._scr_bytes_in - offset) * 8 + self._scr_bits_count
            reason = f"the program's output ended inside {name} (0x{opcode:02X}): {bits:,} of its bits read"
        where = f'{name} (0x{opcode:02X}) at output byte {offset:,}, frame {self.frame_count:,}{at}'
        return IODeviceException(f'ScreenIO: {where}: {reason}')

    def _scr_parse(self) -> Parser:
        """the command parser: a generator sent one output byte at a time."""
        handlers = {opcode: getattr(self, f'_scr_cmd_{name}') for opcode, name in COMMAND_NAMES.items()}
        try:
            while True:
                assert self._scr_nacc == 0  # every command starts on a byte boundary
                opcode = yield from self._scr_bits(8)
                offset = self._scr_bytes_in - 1
                name = COMMAND_NAMES.get(opcode, 'unknown' if opcode else 'reserved')
                extension = self._extension_command(opcode) if name == 'unknown' else None
                if extension is not None:  # the subclass's command: its payload bytes are its own
                    self._scr_in_extension = True
                    while extension is not True and not extension((yield)):
                        pass
                    self._scr_in_extension = False
                    continue
                self._scr_command = (name, opcode, offset)
                if opcode not in COMMAND_NAMES:
                    raise self._scr_error(
                        f'opcode 0x{opcode:02X} is not a command (commands: 0x01-0x07, 0x10, 0x11, 0x1F)'
                        if opcode
                        else 'opcode 0x00 is reserved (a stray zero byte?)'
                    )
                if opcode != 0x01 and self.width == 0:
                    raise self._scr_error(
                        f'{name} (0x{opcode:02X}) before init_screen: the first command must be init_screen (0x01)'
                    )
                if name in PALETTE_COMMANDS and self.bpp == FORMAT_RGB:
                    raise self._scr_error(
                        f'{name} (0x{opcode:02X}) in format 24 (direct RGB): palettes exist only in formats 1-16'
                    )
                try:
                    yield from handlers[opcode]()
                except _OutputEnded:
                    raise self._scr_error() from None

                padding_bits, padding = self._scr_nacc, self._scr_acc
                self._scr_acc = self._scr_nacc = 0
                if padding:
                    raise self._scr_error(
                        f'the padding bits after the command are 0b{padding:0{padding_bits}b}, '
                        'not zero (the stream is probably misaligned)'
                    )
                self._scr_command = None
        except IODeviceException as failure:  # the parser is over: every later call raises it again
            self._scr_failure = failure
            raise

    # ---------------------------------------------------------------- screen functions

    def _scr_cmd_init_screen(self) -> Generator[None, int, None]:
        width = yield from self._scr_bits(16)
        height = yield from self._scr_bits(16)
        bpp = yield from self._scr_bits(8)
        if not 1 <= width <= MAX_WIDTH:
            raise self._scr_error(f'w = {width:,}: must be 1..{MAX_WIDTH:,}')
        if not 1 <= height <= MAX_HEIGHT:
            raise self._scr_error(f'h = {height:,}: must be 1..{MAX_HEIGHT:,}')
        if not 1 <= bpp <= 16 and bpp != FORMAT_RGB:
            raise self._scr_error(f'format = {bpp}: must be 1-16 or 24')
        self._init_screen(width, height, bpp)

    def _init_screen(self, width: int, height: int, bpp: int) -> None:
        """the life cycle's init_screen: everything but the title, the icon, max fps and the frame count resets."""
        self.width, self.height, self.bpp = width, height, bpp
        self._scr_row_bits, self._scr_column_bits = row_field_width(height), column_field_width(width)
        self.pixel_indices = [0] * (width * height)
        indexed = bpp != FORMAT_RGB
        self._scr_default = list(default_palette(bpp)) if indexed else []
        self.palettes = [self._scr_default] * PALETTE_SLOTS if indexed else []
        self.selected_slot = 0

    def _scr_cmd_define_palette(self) -> Generator[None, int, None]:
        slot = yield from self._scr_bits(8)
        first = yield from self._scr_bits(self.bpp)
        last = yield from self._scr_bits(self.bpp)
        if first > last:
            raise self._scr_error(f'first = {first:,} > last = {last:,}')
        entries = []
        for _ in range(last - first + 1):
            entries.append(rgb_of((yield from self._scr_bits(24))))  # R:8 G:8 B:8, R first: a format-24 value
        palette = self.palettes[slot]
        if palette is self._scr_default or palette is self.palette:  # shared, or frozen at a present: copy it first
            palette = self.palettes[slot] = list(palette)
        palette[first : last + 1] = entries  # noqa: E203

    def _scr_cmd_set_max_fps(self) -> Generator[None, int, None]:
        self.max_fps = yield from self._scr_bits(8)

    def _scr_cmd_set_window_title(self) -> Generator[None, int, None]:
        title = bytearray()
        while (byte := (yield from self._scr_bits(8))) != 0:
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
        if width == 0 or height == 0:
            raise self._scr_error(f'set_window_icon: {"w" if width == 0 else "h"} = 0: must be 1..255')
        values = [0] * (width * height)
        yield from self._scr_colours(values, width)
        palette = [] if self.bpp == FORMAT_RGB else self.palettes[self.selected_slot]
        self._set_window_icon(width, height, to_rgb(values, palette))

    def _set_window_icon(self, width: int, height: int, colours: List[RGB]) -> None:
        self.window_icon = (width, height, colours)

    def _scr_cmd_select_palette(self) -> Generator[None, int, None]:
        self.selected_slot = yield from self._scr_bits(8)

    def _scr_cmd_builtin_palette(self) -> Generator[None, int, None]:
        slot = yield from self._scr_bits(8)
        builtin_id = yield from self._scr_bits(8)
        if builtin_id > 4:
            raise self._scr_error(f'id = {builtin_id}: the built-ins are 0..4')
        self.palettes[slot] = list(builtin_palette(builtin_id, self.bpp))

    def _scr_cmd_draw_raw(self) -> Generator[None, int, None]:
        yield from self._scr_colours(self.pixel_indices, self.width)

    def _scr_colours(self, values: List[int], width: int) -> Generator[None, int, None]:
        """read colours of B bits into `values`, in order (rows of `width`), inline as draw_collines reads.
        The output ending inside them is an error at the pixel being read."""
        bits = self.bpp
        mask = (1 << bits) - 1
        acc, nacc = self._scr_acc, self._scr_nacc
        index = 0
        try:
            for index in range(len(values)):
                while nacc < bits:
                    acc |= (yield) << nacc
                    nacc += 8
                values[index], acc, nacc = acc & mask, acc >> bits, nacc - bits
        except _OutputEnded:
            raise self._scr_error(at=f', pixel ({index % width:,}, {index // width:,})') from None
        self._scr_acc, self._scr_nacc = acc, nacc

    def _scr_cmd_draw_collines(self) -> Generator[None, int, None]:
        width, height, picture = self.width, self.height, self.pixel_indices
        row_bits, column_bits, colour_bits = self._scr_row_bits, self._scr_column_bits, self.bpp
        row_mask, column_mask, colour_mask = (1 << row_bits) - 1, (1 << column_bits) - 1, (1 << colour_bits) - 1
        end_command, reserved_column = column_mask, column_mask - 1
        end, keep, copy, reserved_row = row_mask, row_mask - 1, row_mask - 2, row_mask - 3
        acc = nacc = 0  # the bit buffer, local and read inline (not a _scr_bits generator per field): the hot path
        x: Optional[int] = None
        cursor = 0

        def refuse(reason: Optional[str] = None) -> IODeviceException:  # at the column and its cursor, once x is read
            return self._scr_error(reason, '' if x is None else f', column {x:,}, cursor {cursor:,}')

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
                    raise refuse(
                        f'x = 0x{column:0{column_bits // 4}X} is the reserved column token (2^C - 2, C = {column_bits})'
                    )
                if column >= width:
                    raise refuse(f'x = {column:,}: the screen has columns 0..{width - 1:,} (w = {width:,})')
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
            raise refuse() from None

    def _scr_cmd_present(self) -> Generator[None, int, None]:
        self._present()
        yield from ()  # a generator, like every command

    def _present(self) -> None:
        """record the picture as a frame: the snapshot behind last_frame_rgb, the hash, the PNG."""
        values = tuple(self.pixel_indices)
        palette: List[RGB] = []
        if self.bpp == FORMAT_RGB:
            hashed = b''.join(value.to_bytes(3, 'little') for value in values)
        else:
            palette = self.palettes[self.selected_slot]  # frozen while it is self.palette (define_palette copies)
            cached = self._scr_palette_bytes_cache
            if cached is None or cached[0] is not palette:
                cached = self._scr_palette_bytes_cache = (palette, bytes(chain.from_iterable(palette)))
            value_bytes = bytes(values) if self.bpp <= 8 else struct.pack(f'<{len(values)}H', *values)
            hashed = value_bytes + cached[1]
        self.palette = palette
        self._scr_presented = values
        self.frame_count += 1
        self.frame_hashes.append((time.time_ns(), hashlib.sha256(hashed).hexdigest()))

        if self.frames_dir is not None:
            self.frames_dir.mkdir(parents=True, exist_ok=True)
            rgb, stride = bytes(chain.from_iterable(self.last_frame_rgb)), 3 * self.width
            rows = b''.join(b'\x00' + rgb[at : at + stride] for at in range(0, len(rgb), stride))  # noqa: E203
            png_path = self.frames_dir / f'frame_{self.frame_count - 1:06d}.png'
            png_path.write_bytes(encode_png(self.width, self.height, rows))

    @property
    def last_frame_rgb(self) -> List[RGB]:
        """the last presented frame as row-major RGB tuples ([] before the first present). LAZY (the window hands
        SDL the indices); its inputs are snapshotted at present time, so it is the frame that was presented."""
        return [] if self._scr_presented is None else to_rgb(self._scr_presented, self.palette)
