"""
unit-tests for the .fjm file-format Writer and Reader.

covers round-trips across all versions and memory-widths, the relative-jump transparency
(v2/v3), unaligned/zeros-boundary reads, the garbage-handling modes, writer validation,
and reading corrupt files.
"""

import struct
from pathlib import Path
from typing import List

import pytest

from flipjump.fjm.fjm_consts import FJ_MAGIC, FJMVersion
from flipjump.fjm.fjm_reader import GarbageHandling, Reader
from flipjump.fjm.fjm_writer import Writer
from flipjump.utils.exceptions import (
    FlipJumpReadFjmException,
    FlipJumpRuntimeMemoryException,
    FlipJumpWriteFjmException,
)

ALL_VERSIONS = list(FJMVersion)
ALL_WIDTHS = [8, 16, 32, 64]


def _write(tmp_path: Path, memory_width: int, version: FJMVersion, segment_start: int, data: List[int]) -> Path:
    fjm_path = tmp_path / 'out.fjm'
    writer = Writer(fjm_path, memory_width, version)
    writer.add_simple_segment_with_data(segment_start, data)
    writer.write_to_file()
    return fjm_path


@pytest.mark.parametrize('version', ALL_VERSIONS)
@pytest.mark.parametrize('memory_width', ALL_WIDTHS)
def test_round_trip(tmp_path: Path, version: FJMVersion, memory_width: int) -> None:
    fjm_path = _write(tmp_path, memory_width, version, 0, [10, 20])
    reader = Reader(fjm_path)

    assert reader.memory_width == memory_width
    assert reader.version == version
    assert reader.segment_num == 1

    memory = reader.get_memory()
    assert memory[0] == 10
    assert memory[1] == 20


@pytest.mark.parametrize('version', ALL_VERSIONS)
def test_relative_jump_value_is_transparent(tmp_path: Path, version: FJMVersion) -> None:
    # the jump-word (odd index) of a non-zero-offset segment must read back as its absolute
    # value for every version - proving v2/v3's relative encoding is transparent.
    fjm_path = _write(tmp_path, 16, version, 2, [10, 20])
    memory = Reader(fjm_path).get_memory()
    assert memory[2] == 10
    assert memory[3] == 20


def test_read_write_bit_and_get_word(tmp_path: Path) -> None:
    fjm_path = _write(tmp_path, 8, FJMVersion.NormalVersion, 0, [0b10110010, 0])
    reader = Reader(fjm_path)

    assert reader.read_bit(1) is True
    assert reader.read_bit(0) is False
    assert reader.get_word(0) == 0b10110010

    reader.write_bit(0, True)
    assert reader.get_word(0) == 0b10110011

    reader.write_bit(1, False)  # the bit-clearing path
    assert reader.get_word(0) == 0b10110001


def test_get_word_past_last_bit_raises(tmp_path: Path) -> None:
    reader = Reader(_write(tmp_path, 8, FJMVersion.NormalVersion, 0, [0xAB, 0xCD]))
    last_word_address = (1 << 8) - 1
    unaligned_bit_address = (last_word_address << ((8).bit_length() - 1)) + 4
    with pytest.raises(FlipJumpRuntimeMemoryException):
        reader.get_word(unaligned_bit_address)


def test_get_word_unaligned(tmp_path: Path) -> None:
    data = [0xAB, 0xCD]
    reader = Reader(_write(tmp_path, 8, FJMVersion.NormalVersion, 0, data))
    expected = ((data[0] >> 4) | (data[1] << 4)) & 0xFF
    assert reader.get_word(4) == expected


def test_zeros_boundaries_lazy_init(tmp_path: Path) -> None:
    fjm_path = tmp_path / 'out.fjm'
    writer = Writer(fjm_path, 16, FJMVersion.NormalVersion)
    data_start = writer.add_data([0, 0])
    writer.add_segment(0, 2000, data_start, 2)  # padding (1998 words) exceeds the dict-threshold
    writer.write_to_file()

    reader = Reader(fjm_path)
    assert reader.zeros_boundaries
    assert reader.get_word(1500 * 16) == 0


def test_garbage_handling_stop_raises(tmp_path: Path) -> None:
    reader = Reader(_write(tmp_path, 16, FJMVersion.NormalVersion, 0, [0, 0]), garbage_handling=GarbageHandling.Stop)
    with pytest.raises(FlipJumpRuntimeMemoryException) as exc_info:
        reader.get_word(0x4000)
    assert exc_info.value.memory_address is not None


def test_garbage_handling_continue_returns_zero(tmp_path: Path) -> None:
    reader = Reader(
        _write(tmp_path, 16, FJMVersion.NormalVersion, 0, [0, 0]), garbage_handling=GarbageHandling.Continue
    )
    # the expected value is whatever _new_garbage_val() returns (currently 0).
    assert reader.get_word(0x4000) == 0


# --- writer validation ---


def test_writer_bad_memory_width_raises(tmp_path: Path) -> None:
    with pytest.raises(FlipJumpWriteFjmException):
        Writer(tmp_path / 'x.fjm', 24, FJMVersion.NormalVersion)


def test_writer_base_version_with_flags_raises(tmp_path: Path) -> None:
    with pytest.raises(FlipJumpWriteFjmException):
        Writer(tmp_path / 'x.fjm', 16, FJMVersion.BaseVersion, flags=1)


def test_writer_compressed_bad_lzma_preset_raises(tmp_path: Path) -> None:
    with pytest.raises(FlipJumpWriteFjmException):
        Writer(tmp_path / 'x.fjm', 16, FJMVersion.CompressedVersion, lzma_preset=42)


def test_add_segment_odd_alignment_raises(tmp_path: Path) -> None:
    writer = Writer(tmp_path / 'x.fjm', 16, FJMVersion.NormalVersion)
    data_start = writer.add_data([0, 0])
    with pytest.raises(FlipJumpWriteFjmException):
        writer.add_segment(1, 2, data_start, 2)


def test_add_segment_length_less_than_data_raises(tmp_path: Path) -> None:
    writer = Writer(tmp_path / 'x.fjm', 16, FJMVersion.NormalVersion)
    data_start = writer.add_data([0, 0, 0, 0])
    with pytest.raises(FlipJumpWriteFjmException):
        writer.add_segment(0, 2, data_start, 4)


def test_add_segment_overlapping_raises(tmp_path: Path) -> None:
    writer = Writer(tmp_path / 'x.fjm', 16, FJMVersion.NormalVersion)
    writer.add_simple_segment_with_data(0, [0, 0, 0, 0])
    with pytest.raises(FlipJumpWriteFjmException):
        writer.add_simple_segment_with_data(2, [0, 0])


# --- corrupt reads ---


def test_read_bad_magic_raises(tmp_path: Path) -> None:
    bad = tmp_path / 'bad.fjm'
    bad.write_bytes(b'\x00' * 64)
    with pytest.raises(FlipJumpReadFjmException):
        Reader(bad)


def test_read_truncated_file_raises(tmp_path: Path) -> None:
    bad = tmp_path / 'bad.fjm'
    bad.write_bytes(b'\x00\x01')
    with pytest.raises(FlipJumpReadFjmException):
        Reader(bad)


def test_read_unsupported_version_raises(tmp_path: Path) -> None:
    good = _write(tmp_path, 16, FJMVersion.NormalVersion, 0, [0, 0])
    raw = bytearray(good.read_bytes())
    # the version is a u64 right after magic (u16) and word_size (u16).
    raw[4] = 99
    bad = tmp_path / 'bad.fjm'
    bad.write_bytes(bytes(raw))
    with pytest.raises(FlipJumpReadFjmException):
        Reader(bad)


def test_magic_constant_matches_fj() -> None:
    assert FJ_MAGIC == ord('F') + (ord('J') << 8)


# --- malformed-input robustness (security-audit findings) ---


def _v1_header(magic: int = FJ_MAGIC, word_size: int = 64, segment_num: int = 0) -> bytes:
    """Build a raw version-1 (NormalVersion) header with the given fields."""
    return struct.pack('<HHQQ', magic, word_size, 1, segment_num) + struct.pack('<QL', 0, 0)


@pytest.mark.parametrize('bad_width', [0, 3, 7, 12, 24, 33, 128])
def test_read_unsupported_memory_width_raises(tmp_path: Path, bad_width: int) -> None:
    bad = tmp_path / 'bad_width.fjm'
    bad.write_bytes(_v1_header(word_size=bad_width))
    with pytest.raises(FlipJumpReadFjmException):
        Reader(bad)


def test_read_segment_data_out_of_bounds_raises(tmp_path: Path) -> None:
    bad = tmp_path / 'bad_seg.fjm'
    # segment claims 4 words starting at data offset 0, but the data pool is empty
    segment = struct.pack('<QQQQ', 0, 4, 0, 4)
    bad.write_bytes(_v1_header(segment_num=1) + segment)
    with pytest.raises(FlipJumpReadFjmException):
        Reader(bad)
