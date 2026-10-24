"""
the .fjm file reader.
parses an .fjm binary's header and segments, decompresses the data when needed, and
exposes the program as a word-addressable memory dictionary for the interpreter.
"""

import dataclasses
import lzma
import os
import struct
from collections import defaultdict
from enum import IntEnum
from pathlib import Path
from struct import unpack
from time import sleep
from typing import BinaryIO, Callable, List, Tuple, Dict, Optional, Union

from flipjump.fjm.fjm_consts import (
    FJ_MAGIC,
    _reserved_dict_threshold,
    _header_base_format,
    _header_extension_format,
    _header_base_size,
    _header_extension_size,
    _segment_format,
    _segment_size,
    SUPPORTED_VERSIONS_NAMES,
    SUPPORTED_MEMORY_WIDTHS,
    _LZMA_FORMAT,
    _LZMA_DECOMPRESSION_FILTERS,
    _LZMA_MAX_DICT_DECOMPRESSION_FILTERS,
    _new_garbage_val,
    FJMVersion,
    bad_io_mode_message,
    bad_run_flags_message,
)
from flipjump.utils.exceptions import FlipJumpReadFjmException, FlipJumpRuntimeMemoryException


class GarbageHandling(IntEnum):
    """
    What to do when reading garbage memory (memory outside any segment).
    """

    Stop = 0  # Stop and finish
    SlowRead = 1  # Continue after a small waiting time, very slow and print a warning
    OnlyWarning = 2  # Continue and print a warning
    Continue = 3  # Continue normally


@dataclasses.dataclass
class MemorySegment:
    """
    Start and length are in word addresses, not bit addresses.
    """

    segment_start: int
    segment_length: int


class Reader:
    """
    Used for reading a .fjm file from memory.
    """

    garbage_handling: GarbageHandling
    magic: int
    memory_width: int
    version: FJMVersion
    segment_num: int
    memory: Dict[int, int]
    zeros_boundaries: List[Tuple[int, int]]

    def __init__(self, input_file: Path, *, garbage_handling: GarbageHandling = GarbageHandling.Stop):
        """
        The .fjm-file reader
        @param input_file: the path to the .fjm file
        @param garbage_handling: how to handle access to memory not in any segment
        """
        self.garbage_handling = garbage_handling

        try:
            with open(input_file, 'rb') as fjm_file:
                self._init_header_fields(fjm_file)
                self._validate_header()
                segments = self._init_segments(fjm_file)
                data = self._read_decompressed_data(fjm_file)
                self._init_memory(segments, data)
        except struct.error as se:
            exception_message = f"Bad file {input_file}, can't unpack. Maybe it's not a .fjm file?"
            raise FlipJumpReadFjmException(exception_message) from se

    def _init_header_fields(self, fjm_file: BinaryIO) -> None:
        self.magic, self.memory_width, version, self.segment_num = unpack(
            _header_base_format, fjm_file.read(_header_base_size)
        )
        try:
            self.version = FJMVersion(version)
        except ValueError:
            raise FlipJumpReadFjmException(
                f'Error: unsupported version ({version}, this program supports {str(SUPPORTED_VERSIONS_NAMES)}).'
            ) from None
        if FJMVersion.BaseVersion == self.version:
            self.run_flags, self.io_mode, self.reserved = 0, 0, 0
        else:
            self.run_flags, self.io_mode, self.reserved = unpack(
                _header_extension_format, fjm_file.read(_header_extension_size)
            )

    @property
    def flags(self) -> int:
        """the header's run flags and io mode as the one u64 they replaced (run_flags low, io_mode high)"""
        return self.run_flags | (self.io_mode << 32)

    def _init_segments(self, fjm_file: BinaryIO) -> List[Tuple[int, int, int, int]]:
        # ONE read for the whole table: a read per 32-byte segment is ~2,000 small reads for a 555K-segment
        # program, which on a slow filesystem (WSL's /mnt/c: ~10 ms each) took 6-31 s, where this takes 0.1.
        # (at most the rest of the file is read, so a damaged segment_num can't ask for more memory than that)
        table_size = self.segment_num * _segment_size
        try:
            remaining = max(0, os.fstat(fjm_file.fileno()).st_size - fjm_file.tell())
        except (OSError, ValueError):  # not a regular file (a pipe, an in-memory buffer): read what is asked
            remaining = table_size
        table = fjm_file.read(min(table_size, remaining))
        if len(table) != table_size:
            raise struct.error(f'the segment table needs {table_size} bytes, the file has {len(table)} left')
        return list(struct.iter_unpack(_segment_format, table))

    def _validate_header(self) -> None:
        if self.magic != FJ_MAGIC:
            raise FlipJumpReadFjmException(f'Error: bad magic code ({hex(self.magic)}, should be {hex(FJ_MAGIC)}).')
        if self.version not in SUPPORTED_VERSIONS_NAMES:
            raise FlipJumpReadFjmException(
                f'Error: unsupported version ({self.version}, this program supports {str(SUPPORTED_VERSIONS_NAMES)}).'
            )
        if self.memory_width not in SUPPORTED_MEMORY_WIDTHS:
            raise FlipJumpReadFjmException(
                f'Error: unsupported memory width ({self.memory_width},'
                f' this program supports {sorted(SUPPORTED_MEMORY_WIDTHS)}).'
            )
        if self.reserved != 0:
            raise FlipJumpReadFjmException(f'Error: bad reserved value ({self.reserved}, should be 0).')
        for bad_flags in (bad_run_flags_message(self.run_flags), bad_io_mode_message(self.io_mode)):
            if bad_flags is not None:
                raise FlipJumpReadFjmException(f'Error: bad flags in the .fjm header - {bad_flags}.')

    @staticmethod
    def _decompress_data(compressed_data: bytes) -> bytes:
        try:
            return lzma.decompress(compressed_data, format=_LZMA_FORMAT, filters=_LZMA_DECOMPRESSION_FILTERS)
        except lzma.LZMAError:
            pass  # presets 7-9 may match farther back than the default dictionary reaches
        try:
            return lzma.decompress(compressed_data, format=_LZMA_FORMAT, filters=_LZMA_MAX_DICT_DECOMPRESSION_FILTERS)
        except lzma.LZMAError as e:
            raise FlipJumpReadFjmException('Error: The compressed data is damaged; Unable to decompress.') from e

    def _read_decompressed_data(self, fjm_file: BinaryIO) -> List[int]:
        """
        @param fjm_file: [in]: read from this file the data words.
        @return: list of the data words (decompressed if it was compressed).
        """
        read_tag = '<' + {8: 'B', 16: 'H', 32: 'L', 64: 'Q'}[self.memory_width]
        word_bytes_size = self.memory_width // 8

        file_data = fjm_file.read()
        if FJMVersion.CompressedVersion == self.version:
            file_data = self._decompress_data(file_data)

        data = [
            unpack(read_tag, file_data[i : i + word_bytes_size])[0]  # noqa: E203
            for i in range(0, len(file_data), word_bytes_size)
        ]
        return data

    def _init_memory(self, segments: List[Tuple[int, int, int, int]], data: List[int]) -> None:
        validate_segments(segments, len(data))
        self.memory = {}
        self.zeros_boundaries = []

        self.memory_segments: List[MemorySegment] = []
        for segment_start, segment_length, data_start, data_length in segments:
            # data is laid out as (flip-word, jump-word) op-pairs, so its length must be even
            #  (the relative-jump reconstruction below relies on this).
            self.memory_segments.append(MemorySegment(segment_start, segment_length))
            if self.version in (FJMVersion.RelativeJumpVersion, FJMVersion.CompressedVersion):
                word = (1 << self.memory_width) - 1
                for i in range(0, data_length, 2):
                    self.memory[segment_start + i] = data[data_start + i]
                    self.memory[segment_start + i + 1] = (
                        data[data_start + i + 1] + (segment_start + i + 1) * self.memory_width
                    ) & word
            else:
                for i in range(data_length):
                    self.memory[segment_start + i] = data[data_start + i]
            if segment_length > data_length:
                if segment_length - data_length < _reserved_dict_threshold:
                    for i in range(data_length, segment_length):
                        self.memory[segment_start + i] = 0
                else:
                    self.zeros_boundaries.append((segment_start + data_length, segment_start + segment_length))

    def _get_memory_word(self, word_address: int) -> int:
        word_address &= (1 << self.memory_width) - 1
        if word_address not in self.memory:
            for start, end in self.zeros_boundaries:
                if start <= word_address < end:
                    self.memory[word_address] = 0
                    return 0

            garbage_val = _new_garbage_val()
            memory_address = word_address << (self.memory_width.bit_length() - 1)
            garbage_message = f'Reading garbage word at mem[{hex(memory_address)[2:]}] = {hex(garbage_val)[2:]}'

            if GarbageHandling.Stop == self.garbage_handling:
                raise FlipJumpRuntimeMemoryException(garbage_message, memory_address)
            elif GarbageHandling.OnlyWarning == self.garbage_handling:
                print(f'\nWarning:  {garbage_message}')
            elif GarbageHandling.SlowRead == self.garbage_handling:
                print(f'\nWarning:  {garbage_message}')
                sleep(0.1)

            self.memory[word_address] = garbage_val

        return self.memory[word_address]

    def _set_memory_word(self, word_address: int, value: int) -> None:
        word_address &= (1 << self.memory_width) - 1
        value &= (1 << self.memory_width) - 1
        self.memory[word_address] = value

    def _bit_address_decompose(self, bit_address: int) -> Tuple[int, int]:
        """
        @param bit_address: the address
        @return: tuple of the word address and the bit offset
        """
        word_address = (bit_address >> (self.memory_width.bit_length() - 1)) & ((1 << self.memory_width) - 1)
        bit_offset = bit_address & (self.memory_width - 1)
        return word_address, bit_offset

    def read_bit(self, bit_address: int) -> bool:
        """
        read a bit from memory.
        @param bit_address: the address
        @return: True/False for 1/0
        """
        word_address, bit_offset = self._bit_address_decompose(bit_address)
        return (self._get_memory_word(word_address) >> bit_offset) & 1 == 1

    def write_bit(self, bit_address: int, bit_value: bool) -> None:
        """
        write a bit to memory.
        @param bit_address: the address
        @param bit_value: True/False for 1/0
        """
        word_address, bit_offset = self._bit_address_decompose(bit_address)
        word_value = self._get_memory_word(word_address)
        if bit_value:
            word_value |= 1 << bit_offset
        else:
            word_value &= (1 << self.memory_width) - 1 - (1 << bit_offset)
        self._set_memory_word(word_address, word_value)

    def get_word(self, bit_address: int) -> int:
        """
        read a word from memory (can be unaligned).
        @param bit_address: the address
        @return: the word value
        """
        word_address, bit_offset = self._bit_address_decompose(bit_address)
        if bit_offset == 0:
            return self._get_memory_word(word_address)
        if word_address == ((1 << self.memory_width) - 1):
            raise FlipJumpRuntimeMemoryException('Accessed outside of memory (beyond the last bit).', bit_address)

        lsw = self._get_memory_word(word_address)
        msw = self._get_memory_word(word_address + 1)
        return ((lsw >> bit_offset) | (msw << (self.memory_width - bit_offset))) & ((1 << self.memory_width) - 1)

    def get_memory(self) -> Dict[int, int]:
        """
        Return a dictionary from word_address to word_value (e.g. word address 3 is bit_address 3*w).
        Note that it ignores "garbage handling", and it's ment to be used to just read the memory.
        Uninitialized addresses will return zero.
        """
        return defaultdict(lambda: 0, self.memory)

    def assert_runnable(self) -> None:
        """
        A FlipJump program starts executing at address 0, so a segment must hold its first op
        (bits 0..2w-1, i.e. words 0 and 1). A loaded .fjm without it is not a runnable program.
        Raise FlipJumpReadFjmException if no segment covers the first op.
        """
        if not any(seg.segment_start == 0 and seg.segment_length >= 2 for seg in self.memory_segments):
            raise FlipJumpReadFjmException(
                "the program has no first op at address 0: no segment holds bits 0..2w-1 "
                "(words 0 and 1), but execution starts at address 0."
            )


@dataclasses.dataclass
class FjmHeader:
    """
    what the .fjm's header and segment table say - read without touching the data (see read_fjm_header).
    segments are (segment_start, segment_length, data_start, data_length), in words.
    """

    memory_width: int
    version: FJMVersion
    run_flags: int
    io_mode: int
    segments: List[Tuple[int, int, int, int]]

    @property
    def span_words(self) -> int:
        """the end of the highest segment, in words: the flat-storage window that holds every segment
        (what `fj --run --flat-memory` uses). 0 when there are no segments"""
        return max((start + length for start, length, _data_start, _data_length in self.segments), default=0)

    @property
    def relative_jumps(self) -> bool:
        """are the jump words stored relative to their address? (versions 2 and 3)"""
        return self.version in (FJMVersion.RelativeJumpVersion, FJMVersion.CompressedVersion)

    def assert_runnable(self) -> None:
        """execution starts at address 0, so a segment must hold the first op (words 0 and 1)"""
        if not any(start == 0 and length >= 2 for start, length, _, _ in self.segments):
            raise FlipJumpReadFjmException(
                "the program has no first op at address 0: no segment holds bits 0..2w-1 "
                "(words 0 and 1), but execution starts at address 0."
            )


def _read_header(fjm_file: BinaryIO, input_file: Path) -> FjmHeader:
    reader = Reader.__new__(Reader)
    try:
        reader._init_header_fields(fjm_file)
        reader._validate_header()
        segments = reader._init_segments(fjm_file)
    except struct.error as se:
        raise FlipJumpReadFjmException(f"Bad file {input_file}, can't unpack. Maybe it's not a .fjm file?") from se
    return FjmHeader(reader.memory_width, reader.version, reader.run_flags, reader.io_mode, segments)


def read_fjm_header(input_file: Path) -> FjmHeader:
    """
    read the .fjm's header and segment table only - the memory itself is not read or decompressed,
    so this is cheap even for a huge program.
    @param input_file: the path to the .fjm file
    """
    with open(input_file, 'rb') as fjm_file:
        return _read_header(fjm_file, input_file)


# the decompression buffer starts at this many times the compressed size (doom-flipjump's 51M-word
# program expands ~9.4x, so it is allocated once); it grows by doubling, up to the size the segments imply
_INITIAL_EXPANSION = 16
# the decompression is fed in chunks of this many compressed bytes, so its progress can be shown
_DECOMPRESS_CHUNK = 1 << 20


def read_fjm_raw(
    input_file: Path, on_progress: Optional[Callable[[float], None]] = None
) -> Tuple[FjmHeader, Union[bytes, bytearray]]:
    """
    read the .fjm's header, segments and its raw data: the (decompressed) data pool as bytes of
    little-endian w-bit words, exactly as the segments index it (jump words still relative in
    versions 2-3, see FjmHeader.relative_jumps). This is the lean load: no python object per word.
    @param input_file: the path to the .fjm file
    @param on_progress: if given, called with the decompression's progress, a fraction in [0, 1]
    @return: the header, and the data (a bytearray when it was decompressed)
    """
    with open(input_file, 'rb') as fjm_file:
        header = _read_header(fjm_file, input_file)
        stored_data = fjm_file.read()
    file_data: Union[bytes, bytearray] = stored_data
    if FJMVersion.CompressedVersion == header.version:
        data_words = max((start + length for _, _, start, length in header.segments), default=0)
        file_data = _decompress_in_chunks(stored_data, on_progress, data_words * header.memory_width // 8)
        del stored_data
    word_bytes = header.memory_width // 8
    if len(file_data) % word_bytes != 0:
        raise FlipJumpReadFjmException(
            f"Bad file {input_file}, can't unpack: its data is {len(file_data)} bytes, "
            f"not a whole number of {header.memory_width}-bit words."
        )
    validate_segments(header.segments, len(file_data) // word_bytes)
    return header, file_data


def _decompress_in_chunks(
    file_data: bytes, on_progress: Optional[Callable[[float], None]], expected_size: int = 0
) -> bytearray:
    """
    exactly lzma.decompress(file_data, format=_LZMA_FORMAT, filters=...), fed in chunks so its progress
    can be reported: the data must end with a complete stream; further streams after it are decoded
    too, and junk after the last stream is ignored - as lzma.decompress does.
    @param expected_size: the decompressed size the segments imply. The buffer is allocated at that size,
    capped at _INITIAL_EXPANSION x the compressed size, and filled in place; beyond it, it grows with the
    decoded data. A legitimate program is allocated once; a damaged claim costs about 2x the real data.
    """
    damaged = 'Error: The compressed data is damaged; Unable to decompress.'
    # the size comes from the (not yet validated) segment table, so it is trusted only as far as the
    # compressed size makes plausible; past that the buffer grows as decoded bytes arrive - a damaged table
    # costs about 2x the real data (or 16x the compressed size), never its own claim
    expected_size = max(0, expected_size)
    output = bytearray(min(expected_size, _INITIAL_EXPANSION * len(file_data)))
    size = 0

    def put(piece: bytes) -> None:
        nonlocal size
        end = size + len(piece)
        if end > len(output):
            del output[size:]
            output.extend(piece)  # bytearray's own amortized growth, no zero-filled temporary
        else:
            with memoryview(output) as view:
                view[size:end] = piece  # (a slice assignment to the bytearray would copy the piece first)
        size = end

    remaining = file_data
    first_stream = True
    filters = _LZMA_DECOMPRESSION_FILTERS
    while True:
        decompressor = lzma.LZMADecompressor(format=_LZMA_FORMAT, filters=filters)
        position = 0
        stream_start = size
        try:
            while position < len(remaining) and not decompressor.eof:
                chunk = remaining[position : position + _DECOMPRESS_CHUNK]  # noqa: E203
                position += len(chunk)
                put(decompressor.decompress(chunk))
                if on_progress is not None:
                    on_progress(min(1.0, (len(file_data) - len(remaining) + position) / len(file_data)))
        except lzma.LZMAError as e:
            size = stream_start
            if filters is _LZMA_DECOMPRESSION_FILTERS:
                filters = _LZMA_MAX_DICT_DECOMPRESSION_FILTERS  # presets 7-9 may match farther back: decode again
                continue
            if first_stream:
                raise FlipJumpReadFjmException(damaged) from e
            break  # junk after the last stream: ignored, as lzma.decompress does
        if not decompressor.eof:
            raise FlipJumpReadFjmException(damaged)
        remaining = decompressor.unused_data + remaining[position:]
        if not remaining:
            break
        first_stream = False
        filters = _LZMA_DECOMPRESSION_FILTERS
    del output[size:]
    return output


def validate_segments(segments: List[Tuple[int, int, int, int]], data_words: int) -> None:
    """
    the checks every segment of a .fjm must pass before it is loaded: an even data length within the data
    pool, data that fits its segment, and segments that don't overlap (the writer guarantees all of these;
    a file that breaks them is damaged).
    """
    for segment_start, segment_length, data_start, data_length in segments:
        if data_length % 2 != 0:
            raise FlipJumpReadFjmException(
                f"Bad .fjm file: segment data-length must be even (an integer number of ops), got {data_length}."
            )
        if data_start + data_length > data_words:
            raise FlipJumpReadFjmException(
                f"Bad .fjm file: segment data range [{data_start}, {data_start + data_length})"
                f" exceeds data pool length {data_words}."
            )
        if data_length > segment_length:
            raise FlipJumpReadFjmException(
                f"Bad .fjm file: segment data-length {data_length} exceeds its segment-length {segment_length}."
            )
    ordered = sorted((start, start + length) for start, length, _, _ in segments)
    for (start, end), (next_start, _next_end) in zip(ordered, ordered[1:]):
        if next_start < end:
            raise FlipJumpReadFjmException(
                f"Bad .fjm file: segments [{hex(start)}, {hex(end)}) and [{hex(next_start)}, ...) overlap (in words)."
            )
