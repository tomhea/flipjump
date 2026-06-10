"""
the .fjm file reader.
parses an .fjm binary's header and segments, decompresses the data when needed, and
exposes the program as a word-addressable memory dictionary for the interpreter.
"""

import dataclasses
import lzma
import struct
from collections import defaultdict
from enum import IntEnum
from pathlib import Path
from struct import unpack
from time import sleep
from typing import BinaryIO, List, Tuple, Dict

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
    _new_garbage_val,
    FJMVersion,
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
            self.flags, self.reserved = 0, 0
        else:
            self.flags, self.reserved = unpack(_header_extension_format, fjm_file.read(_header_extension_size))

    def _init_segments(self, fjm_file: BinaryIO) -> List[Tuple[int, int, int, int]]:
        return [unpack(_segment_format, fjm_file.read(_segment_size)) for _ in range(self.segment_num)]

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

    @staticmethod
    def _decompress_data(compressed_data: bytes) -> bytes:
        try:
            return lzma.decompress(compressed_data, format=_LZMA_FORMAT, filters=_LZMA_DECOMPRESSION_FILTERS)
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
        self.memory = {}
        self.zeros_boundaries = []

        self.memory_segments = []
        for segment_start, segment_length, data_start, data_length in segments:
            # data is laid out as (flip-word, jump-word) op-pairs, so its length must be even
            #  (the relative-jump reconstruction below relies on this).
            if data_length % 2 != 0:
                raise FlipJumpReadFjmException(
                    f"Bad .fjm file: segment data-length must be even (an integer number of ops), got {data_length}."
                )
            if data_start + data_length > len(data):
                raise FlipJumpReadFjmException(
                    f"Bad .fjm file: segment data range [{data_start}, {data_start + data_length})"
                    f" exceeds data pool length {len(data)}."
                )
            self.memory_segments.append(
                MemorySegment(
                    segment_start << (self.memory_width.bit_length() - 1),
                    segment_length << (self.memory_width.bit_length() - 1),
                )
            )
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
