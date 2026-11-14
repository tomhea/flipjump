"""
the .fjm format constants.
the constants and definitions for the .fjm (FlipJump Memory) binary file format: the magic
number, the supported FJMVersion enum, the binary header/segment struct layouts, and the
LZMA compression settings used by the compressed versions.
"""

import lzma
import struct
from enum import Enum
from typing import Iterable, List, Dict, Optional, Tuple, Union

"""
struct {
    u16 fj_magic;   // 'F' + 'J'<<8  (0x4a46)
    u16 word_size;  // number of bits in the memory / a memory-word. also called "w". a power of two, 8 or more.
    u64 version;
    u64 segment_num;
    { // for versions > 0
        u32 run_flags;  // how to run it by default (RUN_FLAG_*); `fj --[no-]flat-memory` overrides
        u32 io_mode;    // the default io device (IO_MODE_*); `fj --io` overrides
        u32 reserved;   // 0
    }
    { // for version 4
        u64 segments_size;  // the byte length of the compressed segment table that follows
    }
    struct segment {    // each field is a u64, or w bits if w > 64 (segment_field_size)
        field segment_start;  // in memory words (w-bits)
        field segment_length; // in memory words (w-bits)
        field data_start;     // in the outer-struct.data words (w-bits)
        field data_length;    // in the outer-struct.data words (w-bits)
    } *segments;        // segments[segment_num] (lzma2-compressed in version 4)
    u8* data;       // the data: little-endian words of word_size/8 bytes (might be compressed in some versions)
} fjm_file;     // FlipJump Memory file
"""


FJ_MAGIC = ord('F') + (ord('J') << 8)


_reserved_dict_threshold = 1000

HEADER_BASE_FORMAT = '<HHQQ'
HEADER_BASE_SIZE = 2 + 2 + 8 + 8

HEADER_EXTENSION_FORMAT = '<LLL'
HEADER_EXTENSION_SIZE = 4 + 4 + 4

# the header's io_mode: the io device a program runs with, unless `fj --run` is given --io.
# Older files have a u64 'flags' here, always 0, so they read as io mode 0 (standard) with no run flags.
IO_MODE_STANDARD = 0
IO_MODE_PC = 1
IO_MODE_NAMES: Dict[int, str] = {IO_MODE_STANDARD: 'standard', IO_MODE_PC: 'pc'}
IO_MODE_NUMBERS: Dict[str, int] = {name: number for number, name in IO_MODE_NAMES.items()}

# the header's run_flags: 32 bits, all off by default.
# bit 0 - flat memory: run with the whole program in the native engine's flat array (`--flat-memory`).
RUN_FLAG_FLAT_MEMORY = 1 << 0
VALID_RUN_FLAGS = RUN_FLAG_FLAT_MEMORY


def bad_run_flags_message(run_flags: int) -> Optional[str]:
    """@return: why these run flags are invalid, or None when they are valid"""
    if not 0 <= run_flags < (1 << 32):
        return f'bad run flags {run_flags}: must be a 32-bit unsigned number'
    if run_flags & ~VALID_RUN_FLAGS:
        return (
            f'bad run flags {run_flags:#x}: only bit 0 (flat memory, {RUN_FLAG_FLAT_MEMORY}) is defined, '
            f'so the value must be 0 or {RUN_FLAG_FLAT_MEMORY}'
        )
    return None


def bad_io_mode_message(io_mode: Union[int, str]) -> Optional[str]:
    """@return: why this io mode (a name, or its number) is invalid, or None when it is valid"""
    if io_mode not in IO_MODE_NAMES and io_mode not in IO_MODE_NUMBERS:
        supported = ', '.join(f'{name} ({number})' for number, name in IO_MODE_NAMES.items())
        return f'bad io mode {io_mode!r}: supported io modes are {supported}'
    return None


SEGMENT_FIELDS = 4  # segment_start, segment_length, data_start, data_length
_U64_BYTES = 8
_U64_SEGMENT_FORMAT = '<QQQQ'

SEGMENTS_SIZE_FORMAT = '<Q'
SEGMENTS_SIZE_SIZE = 8


class FJMVersion(Enum):
    BaseVersion = 0  # initial version, minimal structure
    NormalVersion = 1  # added run flags, io mode and reserved
    RelativeJumpVersion = 2  # compress-friendly: jumps in data are saved relative to their address
    CompressedVersion = 3  # version 2 but data is lzma2-compressed
    CompressedSegmentsVersion = 4  # version 3 but the segment table is lzma2-compressed too


SUPPORTED_VERSIONS_NAMES = {
    FJMVersion.BaseVersion: 'Base',
    FJMVersion.NormalVersion: 'Normal',
    FJMVersion.RelativeJumpVersion: 'RelativeJump',
    FJMVersion.CompressedVersion: 'Compressed',
    FJMVersion.CompressedSegmentsVersion: 'CompressedSegments',
}

# A memory width is a power of two (a bit address splits into a word address and a bit offset by a shift and a
# mask), at least a byte, and fits the header's u16 word_size.
MIN_MEMORY_WIDTH = 8
MAX_MEMORY_WIDTH = 1 << 15
SUPPORTED_MEMORY_WIDTHS_TEXT = f'a power of two from {MIN_MEMORY_WIDTH} to {MAX_MEMORY_WIDTH}'
DEFAULT_MEMORY_WIDTH = 64
# the widths whose words fit a typed array (and the native engine); wider words are python ints
ARRAY_TYPECODE_BY_WIDTH: Dict[int, str] = {8: 'B', 16: 'H', 32: 'I', 64: 'Q'}


SUPPORTED_MEMORY_WIDTHS: frozenset[int] = frozenset(
    1 << bits for bits in range(MIN_MEMORY_WIDTH.bit_length() - 1, MAX_MEMORY_WIDTH.bit_length())
)


def is_supported_memory_width(memory_width: int) -> bool:
    return memory_width in SUPPORTED_MEMORY_WIDTHS


def segment_field_size(memory_width: int) -> int:
    """Return the size, in bytes, of one field of the segment table: a u64, or a w-bit word if the memory is wider
    than 64 bits (so a segment can be anywhere in the memory)."""
    return max(_U64_BYTES, memory_width // 8)


def pack_ints(values: Iterable[int], size: int) -> bytes:
    """Return the values as little-endian unsigned integers of `size` bytes each."""
    return b''.join(value.to_bytes(size, 'little') for value in values)


def unpack_ints(data: bytes, size: int) -> List[int]:
    """Return the little-endian unsigned integers of `size` bytes each that `data` holds."""
    return [int.from_bytes(data[at : at + size], 'little') for at in range(0, len(data), size)]  # noqa: E203


def pack_segments(segments: Iterable[Tuple[int, int, int, int]], memory_width: int) -> bytes:
    """Return the bytes of the segment table: the four fields of each segment, little-endian."""
    field_size = segment_field_size(memory_width)
    if field_size == _U64_BYTES:
        return b''.join(struct.pack(_U64_SEGMENT_FORMAT, *segment) for segment in segments)
    return pack_ints((field for segment in segments for field in segment), field_size)


def unpack_segments(table: bytes, memory_width: int) -> List[Tuple[int, int, int, int]]:
    """Return the segments that the bytes of a segment table hold (see pack_segments)."""
    field_size = segment_field_size(memory_width)
    if len(table) % (SEGMENT_FIELDS * field_size) != 0:
        raise struct.error(f'the segment table is {len(table)} bytes, not a whole number of segments')
    if field_size == _U64_BYTES:
        return list(struct.iter_unpack(_U64_SEGMENT_FORMAT, table))
    fields = iter(unpack_ints(table, field_size))
    return list(zip(fields, fields, fields, fields))  # SEGMENT_FIELDS at a time


LZMA_FORMAT = lzma.FORMAT_RAW
LZMA_DECOMPRESSION_FILTERS: List[Dict[str, int]] = [{"id": lzma.FILTER_LZMA2}]
# preset 9's dictionary size, the largest any preset uses, so a decoder this large reads every preset. The reader
# uses it only when the default (8 MiB) decoder fails: presets 7-9 can match farther back than that.
_LZMA_MAX_DICT_SIZE = 1 << 26
_LZMA_MAX_DICT_DECOMPRESSION_FILTERS: List[Dict[str, int]] = [
    {"id": lzma.FILTER_LZMA2, "dict_size": _LZMA_MAX_DICT_SIZE}
]


_LZMA_MAX_NICE_LEN = 273  # the longest match length LZMA takes


def lzma_compression_filters(dw: int, preset: int) -> List[Dict[str, int]]:
    """The compression filters: `nice_len` is an op's length in bits (as far as LZMA allows), which packs ops well."""
    return [{"id": lzma.FILTER_LZMA2, "preset": preset, "nice_len": min(dw, _LZMA_MAX_NICE_LEN)}]


def _new_garbage_val() -> int:
    return 0  # The value read when reading a word outside any segment.
