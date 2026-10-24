"""
the .fjm format constants.
the constants and definitions for the .fjm (FlipJump Memory) binary file format: the magic
number, the supported FJMVersion enum, the binary header/segment struct layouts, and the
LZMA compression settings used by the compressed version.
"""

import lzma
from enum import Enum
from typing import List, Dict, Optional

"""
struct {
    u16 fj_magic;   // 'F' + 'J'<<8  (0x4a46)
    u16 word_size;  // number of bits in the memory / a memory-word. also called "w".
    u64 version;
    u64 segment_num;
    { // for versions > 0
        u32 run_flags;  // how to run it by default (RUN_FLAG_*); `fj --run-flags` overrides
        u32 io_mode;    // the default io device (IO_MODE_*); `fj --io` overrides
        u32 reserved;   // 0
    }
    struct segment {
        u64 segment_start;  // in memory words (w-bits)
        u64 segment_length; // in memory words (w-bits)
        u64 data_start;     // in the outer-struct.data words (w-bits)
        u64 data_length;    // in the outer-struct.data words (w-bits)
    } *segments;        // segments[segment_num]
    u8* data;       // the data (might be compressed in some versions)
} fjm_file;     // FlipJump Memory file
"""


FJ_MAGIC = ord('F') + (ord('J') << 8)


_reserved_dict_threshold = 1000

_header_base_format = '<HHQQ'
_header_base_size = 2 + 2 + 8 + 8

_header_extension_format = '<LLL'
_header_extension_size = 4 + 4 + 4

# the header's io_mode: the io device a program runs with when `fj --run` is not given --io.
# (these were once the low/high halves of a u64 `flags` that nothing read and older fj wrote as 0 by
# default, so old files read unchanged: the standard io and no run flags. nonzero old flags now read
# as bad flags.)
IO_MODE_STANDARD = 0
IO_MODE_PC = 1
IO_MODE_NAMES: Dict[int, str] = {IO_MODE_STANDARD: 'standard', IO_MODE_PC: 'pc'}

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


def bad_io_mode_message(io_mode: int) -> Optional[str]:
    """@return: why this io mode is invalid, or None when it is valid"""
    if io_mode not in IO_MODE_NAMES:
        supported = ', '.join(f'{number} ({name})' for number, name in IO_MODE_NAMES.items())
        return f'bad io mode {io_mode}: supported io modes are {supported}'
    return None


_segment_format = '<QQQQ'
_segment_size = 8 + 8 + 8 + 8


class FJMVersion(Enum):
    BaseVersion = 0  # initial version, minimal structure
    NormalVersion = 1  # added run flags, io mode and reserved
    RelativeJumpVersion = 2  # compress-friendly: jumps in data are saved relative to their address
    CompressedVersion = 3  # version 2 but data is lzma2-compressed


SUPPORTED_VERSIONS_NAMES = {
    FJMVersion.BaseVersion: 'Base',
    FJMVersion.NormalVersion: 'Normal',
    FJMVersion.RelativeJumpVersion: 'RelativeJump',
    FJMVersion.CompressedVersion: 'Compressed',
}

SUPPORTED_MEMORY_WIDTHS: frozenset[int] = frozenset({8, 16, 32, 64})


_LZMA_FORMAT = lzma.FORMAT_RAW
_LZMA_DECOMPRESSION_FILTERS: List[Dict[str, int]] = [{"id": lzma.FILTER_LZMA2}]
# preset 9's dictionary, the largest any preset writes with: a decoder this large reads every preset. The reader
# allocates it only for a stream that the default (8 MiB) decoder cannot read: presets 7-9 can match farther back.
_LZMA_MAX_DICT_SIZE = 1 << 26
_LZMA_MAX_DICT_DECOMPRESSION_FILTERS: List[Dict[str, int]] = [
    {"id": lzma.FILTER_LZMA2, "dict_size": _LZMA_MAX_DICT_SIZE}
]


def _lzma_compression_filters(dw: int, preset: int) -> List[Dict[str, int]]:
    return [{"id": lzma.FILTER_LZMA2, "preset": preset, "nice_len": dw}]


def _new_garbage_val() -> int:
    return 0  # The value read when reading a word outside any segment.
