"""
unit-tests for the .fjm file-format Writer and Reader.

covers round-trips across all versions and memory-widths, the relative-jump transparency
(v2-v4), unaligned/zeros-boundary reads, the garbage-handling modes, writer validation,
version 4's compressed segment table, and reading corrupt files.
"""

import lzma
import random
import struct
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flipjump.fjm import fjm_reader
from flipjump.fjm.fjm_consts import FJ_MAGIC, FJMVersion, _header_base_size, _header_extension_size
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
        Writer(tmp_path / 'x.fjm', 16, FJMVersion.BaseVersion, run_flags=1)


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


def test_assert_runnable_requires_first_op_at_address_zero(tmp_path: Path) -> None:
    # a .fjm whose only segment is not at address 0 is a valid FILE but not a runnable program
    not_runnable = Reader(_write(tmp_path, 16, FJMVersion.NormalVersion, 2, [10, 20]))
    with pytest.raises(FlipJumpReadFjmException):
        not_runnable.assert_runnable()
    # a normal program (a segment holding the first op at address 0) passes
    Reader(_write(tmp_path, 16, FJMVersion.NormalVersion, 0, [10, 20])).assert_runnable()


def test_add_segments_out_of_order_still_detects_overlap(tmp_path: Path) -> None:
    # the overlap check bisects a sorted view of the starts, so the neighbours of a new segment
    # are the only candidates - whatever order the segments were added in
    writer = Writer(tmp_path / 'x.fjm', 16, FJMVersion.NormalVersion)
    for start in (64, 0, 32, 96):
        writer.add_simple_segment_with_data(start, [0, 0, 0, 0])
    with pytest.raises(FlipJumpWriteFjmException):
        writer.add_simple_segment_with_data(34, [0, 0])  # inside the segment at 32
    with pytest.raises(FlipJumpWriteFjmException):
        writer.add_simple_segment_with_data(62, [0, 0, 0, 0])  # runs into the segment at 64
    writer.add_simple_segment_with_data(36, [0, 0, 0, 0])  # the gap between 32 and 64
    assert len(writer.segments) == 5


def test_add_segment_overlapping_data_raises(tmp_path: Path) -> None:
    # data overlap is only meaningful (and only checked) where the data is rewritten per segment
    writer = Writer(tmp_path / 'x.fjm', 16, FJMVersion.RelativeJumpVersion)
    data_start = writer.add_data([1, 2, 3, 4])
    writer.add_segment(0, 4, data_start, 4)
    with pytest.raises(FlipJumpWriteFjmException):
        writer.add_segment(64, 2, data_start + 2, 2)


def _far_repeat_words(memory_width: int) -> List[int]:
    """a random block, then the same block 9 MiB later (its jump words chosen so that their stored, relative
    values repeat too): an encoder whose dictionary is larger than 8 MiB matches it that far back"""
    far = (9 << 20) * 8 // memory_width
    block = [random.Random(1).getrandbits(memory_width) for _ in range(8192)]  # nosec B311 - test data
    words = [0] * (far + len(block))
    for index in range(1, len(words), 2):
        words[index] = index * memory_width  # a jump to itself is stored as 0
    for index, word in enumerate(block):
        for at in (index, far + index):
            words[at] = (word + at * memory_width) % (1 << memory_width) if at % 2 else word
    return words


def _write_preset(tmp_path: Path, words: List[int], preset: int) -> Path:
    fjm_path = tmp_path / f'preset{preset}.fjm'
    writer = Writer(fjm_path, 64, FJMVersion.CompressedVersion, lzma_preset=preset)
    writer.add_simple_segment_with_data(0, words)
    writer.write_to_file()
    return fjm_path


def test_the_lzma_presets_read_back(tmp_path: Path) -> None:
    # presets 7-9 encode with a 16-64 MiB dictionary, so the reader's decoder must be at least that large:
    # the smallest preset, the last one within liblzma's default 8 MiB, and the first and the largest past it
    words = _far_repeat_words(64)
    for preset in (0, 6, 7, 9):
        fjm_path = _write_preset(tmp_path, words, preset)
        memory = Reader(fjm_path).get_memory()
        assert [memory[index] for index in (0, 1, len(words) - 2, len(words) - 1)] == [
            words[0],
            words[1],
            words[-2],
            words[-1],
        ], preset
        _, data = fjm_reader.read_fjm_raw(fjm_path)  # the lean load decodes on its own (jump words stay relative)
        assert len(data) == 8 * len(words) and data[-16:-8] == words[-2].to_bytes(8, 'little'), preset


def test_an_8mib_decoder_cannot_read_preset_9(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the control: the far repeat above is matched only by a dictionary larger than liblzma's default 8 MiB
    fjm_path = _write_preset(tmp_path, _far_repeat_words(64), 9)
    monkeypatch.setattr(fjm_reader, '_LZMA_MAX_DICT_DECOMPRESSION_FILTERS', [{"id": lzma.FILTER_LZMA2}])
    with pytest.raises(FlipJumpReadFjmException):
        Reader(fjm_path)
    with pytest.raises(FlipJumpReadFjmException):
        fjm_reader.read_fjm_raw(fjm_path)


# --- version 4: the segment table is compressed too ---

V4 = FJMVersion.CompressedSegmentsVersion
SEGMENT_COUNTS = [0, 1, 50]


def _write_segments(path: Path, memory_width: int, version: FJMVersion, segment_count: int) -> Dict[int, int]:
    """`segment_count` one-op segments, 4 words apart; @return: the memory they hold, {word address: word}"""
    writer = Writer(path, memory_width, version)
    memory = {}
    for index in range(segment_count):
        words = [(0x9E3779B97F4A7C15 * (2 * index + k + 1)) % (1 << memory_width) for k in range(2)]  # all different
        memory.update({4 * index: words[0], 4 * index + 1: words[1]})
        writer.add_simple_segment_with_data(4 * index, words)
    writer.write_to_file()
    return memory


def _v4_table_end(path: Path) -> int:
    """the file offset where version 4's compressed segment table ends (and the data stream starts)"""
    stored_size: int = struct.unpack_from('<Q', path.read_bytes(), _header_base_size + _header_extension_size)[0]
    return _header_base_size + _header_extension_size + 8 + stored_size


@pytest.mark.parametrize('segment_count', SEGMENT_COUNTS)
@pytest.mark.parametrize('memory_width', ALL_WIDTHS)
def test_v4_round_trip(tmp_path: Path, memory_width: int, segment_count: int) -> None:
    memory = _write_segments(tmp_path / 'v4.fjm', memory_width, V4, segment_count)
    v3_memory = _write_segments(tmp_path / 'v3.fjm', memory_width, FJMVersion.CompressedVersion, segment_count)
    reader, v3_reader = Reader(tmp_path / 'v4.fjm'), Reader(tmp_path / 'v3.fjm')
    assert (reader.version, reader.memory_width, reader.segment_num) == (V4, memory_width, segment_count)
    assert reader.memory == memory == v3_reader.memory == v3_memory
    header, v3_header = fjm_reader.read_fjm_header(tmp_path / 'v4.fjm'), fjm_reader.read_fjm_header(tmp_path / 'v3.fjm')
    assert header.segments == v3_header.segments == [(4 * i, 2, 2 * i, 2) for i in range(segment_count)]
    assert header.relative_jumps
    (_, data), (_, v3_data) = fjm_reader.read_fjm_raw(tmp_path / 'v4.fjm'), fjm_reader.read_fjm_raw(tmp_path / 'v3.fjm')
    assert data == v3_data and len(data) == 2 * segment_count * memory_width // 8
    if segment_count > 1:  # the point of version 4: the table compresses
        assert (tmp_path / 'v4.fjm').stat().st_size < (tmp_path / 'v3.fjm').stat().st_size


def test_v4_header_read_decompresses_only_the_segment_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'v4.fjm'
    _write_segments(path, 64, V4, 50)
    table_end = _v4_table_end(path)
    raw = path.read_bytes()
    path.write_bytes(raw[:table_end] + b'\xff' * (len(raw) - table_end))  # the data stream, damaged
    decompressed = []
    real = fjm_reader._decompress_in_chunks

    def spy(file_data: bytes, *args: Any) -> bytearray:
        decompressed.append(len(file_data))
        return real(file_data, *args)

    monkeypatch.setattr(fjm_reader, '_decompress_in_chunks', spy)
    header = fjm_reader.read_fjm_header(path)
    assert header.segments == [(4 * i, 2, 2 * i, 2) for i in range(50)]
    assert decompressed == [table_end - _header_base_size - _header_extension_size - 8]  # the table's stream only
    with pytest.raises(
        FlipJumpReadFjmException, match='^Error: The compressed data is damaged; Unable to decompress.$'
    ):
        Reader(path)  # the full read decompresses the data, and finds it damaged


def test_v3_keeps_its_raw_segment_table(tmp_path: Path) -> None:
    path = tmp_path / 'v3.fjm'
    _write_segments(path, 32, FJMVersion.CompressedVersion, 2)
    table = path.read_bytes()[_header_base_size + _header_extension_size :][: 2 * 32]  # noqa: E203
    assert list(struct.iter_unpack('<QQQQ', table)) == [(0, 2, 0, 2), (4, 2, 2, 2)]
    assert Reader(path).memory == _write_segments(tmp_path / 'again.fjm', 32, FJMVersion.CompressedVersion, 2)


def _v4_damaged(tmp_path: Path, damage: str) -> Path:
    path = tmp_path / 'v4.fjm'
    _write_segments(path, 32, V4, 50)
    raw = bytearray(path.read_bytes())
    stream_start = _header_base_size + _header_extension_size + 8
    table_end = _v4_table_end(path)
    if damage == 'truncated':
        raw = raw[: (stream_start + table_end) // 2]
    elif damage == 'size past the end':
        struct.pack_into('<Q', raw, stream_start - 8, len(raw))
    elif damage == 'garbage stream':
        raw[stream_start:table_end] = b'\xff' * (table_end - stream_start)
    elif damage == 'one segment fewer':
        struct.pack_into('<Q', raw, 12, 49)  # segment_num: a u64 after magic, width and version
    path.write_bytes(bytes(raw))
    return path


@pytest.mark.parametrize(
    'damage, message, cause',
    [
        ('truncated', "can't unpack", 'the segment table needs '),
        ('size past the end', "can't unpack", 'the segment table needs '),
        ('garbage stream', '^Error: The compressed segment table is damaged; Unable to decompress.$', None),
        ('one segment fewer', '^Error: the segment table is 1600 bytes; 49 segments take 1568.$', None),
    ],
)
def test_a_damaged_v4_segment_table_is_rejected(tmp_path: Path, damage: str, message: str, cause: str) -> None:
    path = _v4_damaged(tmp_path, damage)
    for read in (Reader, fjm_reader.read_fjm_header, fjm_reader.read_fjm_raw):
        with pytest.raises(FlipJumpReadFjmException, match=message) as error:
            read(path)
        if cause is not None:
            assert str(error.value.__cause__).startswith(cause)
