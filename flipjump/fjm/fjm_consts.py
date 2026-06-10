"""
the .fjm format constants.
the constants and definitions for the .fjm (FlipJump Memory) binary file format: the magic
number, the supported FJMVersion enum, the binary header/segment struct layouts, and the
LZMA compression settings used by the compressed version.
"""

import lzma
from enum import Enum
from typing import List, Dict

"""
struct {
    u16 fj_magic;   // 'F' + 'J'<<8  (0x4a46)
    u16 word_size;  // number of bits in the memory / a memory-word. also called "w".
    u64 version;
    u64 segment_num;
    { // for versions > 0
        u64 flags;
        u32 reserved;   // 0
    }
    struct segment {
        u64 segment_start;  // in memory words (w-bits)
        u64 segment_length; // in memory words (w-bits)
        u64 data_start;     // in the outer-struct.data words (w-bits)
        u64 data_length;    // in the outer-struct.data words (w-bits)
    } *segments;        // segments[segment_num]
    u8* data;       // the data (might be compressed in some versions)
} fjm_file;     // Flip-Jump Memory file
"""


FJ_MAGIC = ord('F') + (ord('J') << 8)


_reserved_dict_threshold = 1000

_header_base_format = '<HHQQ'
_header_base_size = 2 + 2 + 8 + 8

_header_extension_format = '<QL'
_header_extension_size = 8 + 4

_segment_format = '<QQQQ'
_segment_size = 8 + 8 + 8 + 8


class FJMVersion(Enum):
    BaseVersion = 0  # initial version, minimal structure
    NormalVersion = 1  # added flags and reserved
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


def _lzma_compression_filters(dw: int, preset: int) -> List[Dict[str, int]]:
    return [{"id": lzma.FILTER_LZMA2, "preset": preset, "nice_len": dw}]


def _new_garbage_val() -> int:
    return 0  # The value read when reading a word outside any segment.
