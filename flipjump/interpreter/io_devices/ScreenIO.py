"""
the InMemoryScreen output-device - headless-first.

the FJ program drives the screen with command bytes over the regular output stream; the
device reads the referenced buffers from the interpreter memory (the device<->memory hook),
so a blit costs the program just an enum + an address (~a few @).

command stream (all multi-byte integers little-endian; addresses are one fj-word wide,
i.e. memory_width/8 bytes):
  [0x01][width:2][height:2][bpp:1][palette_size:2]      init_screen
  [0x02][palette_fj_bit_address:w/8]                    set_palette
  [0x03][screen_fj_bit_address:w/8]                     update_screen (presents a frame)
  [0x04][x:2][y:2][rect_w:2][rect_h:2][screen_addr:w/8] update_rectangle (presents a frame)
  [0x05][width*height pixel bytes]                      update_screen_raw (presents a frame)
  [0x06][screen_fj_bit_address:w/8]                     update_screen_reg (presents a frame; fj 1.5.1)

the memory-hook commands (update_screen/update_rectangle/update_screen_reg) are the primary
path - reading the framebuffer straight out of program memory is DMA-like, ~free for the fj program.
update_screen_raw is the no-memory-hook alternative: the full frame's pixel bytes arrive
in-stream (row-major, one byte each, masked to bpp bits), so it works without
attach_memory - at the cost of the program outputting every pixel byte each frame.
it requires a prior init_screen (the pixel count defines the command length).

memory layout contracts (op-structured "packed bytes", one byte per fj-op, stride dw):
  framebuffer: pixel (px, py) is the packed byte at screen_address + (px + py*width)*dw,
    masked to bpp bits (bpp is 4 or 8 - the palette-index width).
  update_screen_reg (0x06, fj 1.5.1, bpp 8): the REGISTER-form framebuffer - each pixel is a
    hex.vec 2 (TWO ops, not one packed byte): the low nibble in op (2k) and the high nibble in
    op (2k+1), so pixel k is read from screen_address + 2k*dw (low) and +(2k+1)*dw (high). This
    is the format a `.lookup`/colormap writes DIRECTLY (its result-copy is register-form), so the
    renderer skips the pack/deposit step that the 0x03 packed framebuffer needs per pixel (DESIGN
    section 2.1: "two 4-bpp ops as one 8-bpp pixel - hex.vec 2 framebuffer device-direct").
  update_rectangle reads the same full-screen framebuffer (the address is the screen base,
    not the rectangle's): its source for row i is rect_width packed bytes starting at pixel
    (x + (y+i)*width) - so it never reads pixels outside the [x, x+rect_w) x [y, y+rect_h) box.
  palette: entry k is 3 packed bytes R,G,B at palette_address + 3k*dw.

the headless backend (default) keeps the expanded RGB frame (last_frame_rgb), writes one
PNG per present into frames_dir, timestamps presents, and keeps a per-frame hash log
(frame_hashes: sha256 over the raw pixel indices + palette bytes) for golden tests -
measured fps comes from this log, not hand-timing. the interactive (pygame) backend in
pygame_window.py presents the same command stream into a real window.
"""

import hashlib
import struct
import time
import zlib
from pathlib import Path
from typing import List, Optional, Tuple

from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.interpreter.io_devices.device_memory import DeviceMemory
from flipjump.utils.exceptions import IODeviceException, IOReadOnEOF

CMD_INIT_SCREEN = 0x01
CMD_SET_PALETTE = 0x02
CMD_UPDATE_SCREEN = 0x03
CMD_UPDATE_RECTANGLE = 0x04
CMD_UPDATE_SCREEN_RAW = 0x05
CMD_UPDATE_SCREEN_REG = 0x06  # fj 1.5.1: hex.vec-2 (register-form) framebuffer, two 4-bit ops/pixel (DESIGN section 2.1)

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


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


class InMemoryScreen(IODevice):
    """see the module docstring for the command stream and the memory layout contracts."""

    def __init__(self, *, frames_dir: Optional[Path] = None):
        self.frames_dir = frames_dir
        self.device_memory: Optional[DeviceMemory] = None

        self.width = 0
        self.height = 0
        self.bpp = 8
        self.palette_size = 0
        self.palette: List[Tuple[int, int, int]] = []
        self.pixel_indices: List[int] = []

        self.frame_count = 0
        self.last_frame_rgb: List[Tuple[int, int, int]] = []
        self.frame_hashes: List[Tuple[int, str]] = []  # (present-time ns, sha256 hexdigest)

        self._current_byte = 0
        self._bits_count = 0
        self._command_buffer: List[int] = []

    def attach_memory(self, device_memory: DeviceMemory) -> None:
        self.device_memory = device_memory

    # ---------------------------------------------------------------- output stream

    def write_bit(self, bit: bool) -> None:
        self._current_byte |= bit << self._bits_count
        self._bits_count += 1
        if self._bits_count == 8:
            byte, self._current_byte, self._bits_count = self._current_byte, 0, 0
            self._handle_byte(byte)

    def read_bit(self) -> bool:
        raise IOReadOnEOF('the screen device has no input - use a device that also reads input (e.g. --io pc)')

    def get_output(self, *, allow_incomplete_output: bool = False) -> bytes:
        return b''

    # ---------------------------------------------------------------- command decoding

    def _address_bytes(self) -> int:
        if self.device_memory is None:
            raise IODeviceException('the screen device is not attached to the interpreter memory')
        return self.device_memory.memory_width // 8

    def _command_length(self, command: int) -> int:
        if command == CMD_INIT_SCREEN:
            return 1 + 2 + 2 + 1 + 2
        if command in (CMD_SET_PALETTE, CMD_UPDATE_SCREEN, CMD_UPDATE_SCREEN_REG):
            return 1 + self._address_bytes()
        if command == CMD_UPDATE_RECTANGLE:
            return 1 + 2 + 2 + 2 + 2 + self._address_bytes()
        if command == CMD_UPDATE_SCREEN_RAW:
            self._require_initialized_screen()  # the pixel count defines the command length
            return 1 + self.width * self.height
        raise IODeviceException(f'unknown screen-device command: {command:#x}')

    def _handle_byte(self, byte: int) -> None:
        self._command_buffer.append(byte)
        if len(self._command_buffer) >= self._command_length(self._command_buffer[0]):
            command, *payload = self._command_buffer
            self._command_buffer = []
            self._execute_command(command, payload)

    @staticmethod
    def _u16(payload: List[int], offset: int) -> int:
        return payload[offset] | (payload[offset + 1] << 8)

    def _read_address(self, payload: List[int], offset: int) -> int:
        value = 0
        for i in range(self._address_bytes()):
            value |= payload[offset + i] << (8 * i)
        return value

    def _execute_command(self, command: int, payload: List[int]) -> None:
        if command == CMD_INIT_SCREEN:
            self._init_screen(self._u16(payload, 0), self._u16(payload, 2), payload[4], self._u16(payload, 5))
        elif command == CMD_SET_PALETTE:
            self._set_palette(self._read_address(payload, 0))
        elif command == CMD_UPDATE_SCREEN:
            self._update_screen(self._read_address(payload, 0))
        elif command == CMD_UPDATE_SCREEN_REG:
            self._update_screen_reg(self._read_address(payload, 0))
        elif command == CMD_UPDATE_RECTANGLE:
            self._update_rectangle(
                self._u16(payload, 0),
                self._u16(payload, 2),
                self._u16(payload, 4),
                self._u16(payload, 6),
                self._read_address(payload, 8),
            )
        elif command == CMD_UPDATE_SCREEN_RAW:
            self._update_screen_raw(payload)

    # ---------------------------------------------------------------- screen functions

    def _init_screen(self, width: int, height: int, bpp: int, palette_size: int) -> None:
        if bpp not in (4, 8):
            raise IODeviceException(f'screen bpp must be 4 or 8, got {bpp}')
        if width == 0 or height == 0:
            raise IODeviceException(f'screen size must be nonzero, got {width}x{height}')
        self.width, self.height, self.bpp, self.palette_size = width, height, bpp, palette_size
        self.palette = [(0, 0, 0)] * palette_size
        self.pixel_indices = [0] * (width * height)

    def _read_packed_bytes(self, first_op_bit_address: int, count: int) -> List[int]:
        if self.device_memory is None:
            raise IODeviceException('the screen device is not attached to the interpreter memory')
        dw = 2 * self.device_memory.memory_width
        return [self.device_memory.read_data_byte(first_op_bit_address + k * dw) for k in range(count)]

    def _set_palette(self, palette_bit_address: int) -> None:
        rgb_bytes = self._read_packed_bytes(palette_bit_address, 3 * self.palette_size)
        self.palette = [
            (rgb_bytes[3 * k], rgb_bytes[3 * k + 1], rgb_bytes[3 * k + 2]) for k in range(self.palette_size)
        ]

    def _require_initialized_screen(self) -> None:
        if self.width == 0 or self.height == 0:
            raise IODeviceException('the screen was not initialized (send the init_screen command first)')

    def _update_screen(self, screen_bit_address: int) -> None:
        self._require_initialized_screen()
        pixel_mask = (1 << self.bpp) - 1
        raw = self._read_packed_bytes(screen_bit_address, self.width * self.height)
        self.pixel_indices = [pixel & pixel_mask for pixel in raw]
        self._present()

    def _read_reg_bytes(self, first_op_bit_address: int, count: int) -> List[int]:
        """read `count` REGISTER-form palette bytes (fj 1.5.1): each pixel is a hex.vec 2 (two ops) -
        the low nibble in op (2k), the high nibble in op (2k+1) - combined into one 8-bit value. this
        is the layout a `.lookup`/colormap result-copy writes directly (no pack/deposit step)."""
        if self.device_memory is None:
            raise IODeviceException('the screen device is not attached to the interpreter memory')
        dw = 2 * self.device_memory.memory_width
        out = []
        for k in range(count):
            lo = self.device_memory.read_data_byte(first_op_bit_address + (2 * k) * dw) & 0xF
            hi = self.device_memory.read_data_byte(first_op_bit_address + (2 * k + 1) * dw) & 0xF
            out.append(lo | (hi << 4))
        return out

    def _update_screen_reg(self, screen_bit_address: int) -> None:
        """present a hex.vec-2 (register-form) framebuffer: two 4-bit ops per pixel (low, high). bpp
        must be 8 (the register byte is two nibbles). DESIGN section 2.1 device extension."""
        self._require_initialized_screen()
        if self.bpp != 8:
            raise IODeviceException(f'update_screen_reg (0x06) requires bpp 8, got {self.bpp}')
        raw = self._read_reg_bytes(screen_bit_address, self.width * self.height)
        self.pixel_indices = [pixel & 0xFF for pixel in raw]
        self._present()

    def _update_screen_raw(self, pixels: List[int]) -> None:
        """a full frame whose pixel bytes arrived in-stream - no memory-hook read."""
        self._require_initialized_screen()
        pixel_mask = (1 << self.bpp) - 1
        self.pixel_indices = [pixel & pixel_mask for pixel in pixels]
        self._present()

    def _update_rectangle(self, x: int, y: int, rect_width: int, rect_height: int, screen_bit_address: int) -> None:
        # the address is the full-screen framebuffer base (NOT the rectangle's): the source of
        # the [x, x+rect_width) x [y, y+rect_height) sub-rectangle is read with the screen's
        # stride, so row i is rect_width bytes starting at pixel (x + (y+i)*screen_width).
        self._require_initialized_screen()
        if self.device_memory is None:
            raise IODeviceException('the screen device is not attached to the interpreter memory')
        if x + rect_width > self.width or y + rect_height > self.height:
            raise IODeviceException(
                f'update_rectangle [{x},{y}] {rect_width}x{rect_height} exceeds the {self.width}x{self.height} screen'
            )
        pixel_mask = (1 << self.bpp) - 1
        dw = 2 * self.device_memory.memory_width
        for row in range(rect_height):
            line_first_pixel = (y + row) * self.width + x
            line = self._read_packed_bytes(screen_bit_address + line_first_pixel * dw, rect_width)
            for col in range(rect_width):
                self.pixel_indices[(y + row) * self.width + (x + col)] = line[col] & pixel_mask
        self._present()

    def _present(self) -> None:
        black = (0, 0, 0)
        self.last_frame_rgb = [
            self.palette[index] if index < len(self.palette) else black for index in self.pixel_indices
        ]
        self.frame_count += 1

        palette_bytes = b''.join(bytes(color) for color in self.palette)
        frame_hash = hashlib.sha256(bytes(self.pixel_indices) + palette_bytes).hexdigest()
        self.frame_hashes.append((time.time_ns(), frame_hash))

        if self.frames_dir is not None:
            self.frames_dir.mkdir(parents=True, exist_ok=True)
            row_slices = [
                self.last_frame_rgb[row * self.width : (row + 1) * self.width]  # noqa: E203
                for row in range(self.height)
            ]
            rows = b''.join(b'\x00' + b''.join(bytes(rgb) for rgb in row_slice) for row_slice in row_slices)
            png_path = self.frames_dir / f'frame_{self.frame_count - 1:06d}.png'
            png_path.write_bytes(encode_png(self.width, self.height, rows))
