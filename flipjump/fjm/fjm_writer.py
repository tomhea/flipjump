"""
the .fjm file writer.
writes a compiled flipjump program to an .fjm binary - emitting the header, the data
and segments, and the metadata (flags) - with support for the different fjm versions
and optional LZMA compression.
"""

import lzma
from pathlib import Path
from struct import pack
from typing import List, Tuple

from flipjump.fjm.fjm_consts import (
    FJ_MAGIC,
    _header_base_format,
    _header_extension_format,
    _segment_format,
    SUPPORTED_VERSIONS_NAMES,
    SUPPORTED_MEMORY_WIDTHS,
    _LZMA_FORMAT,
    _lzma_compression_filters,
    FJMVersion,
)
from flipjump.utils.exceptions import FlipJumpWriteFjmException


class Writer:
    """
    Used for creating a .fjm file in memory.
    The process is:
        1. add_data(...)
        2. add_segment(...)
        repeat steps 1-2 until you finished updating the fjm
        3. write_to_file()
    """

    def __init__(
        self,
        output_file: Path,
        memory_width: int,
        version: FJMVersion,
        *,
        flags: int = 0,
        lzma_preset: int = lzma.PRESET_DEFAULT,
    ):
        """
        the .fjm-file writer
        @param output_file: [in,out]: the path to the .fjm file
        @param memory_width: the memory-width
        @param version: the file's version
        @param flags: the file's flags
        @param lzma_preset: the preset to be used when compressing the .fjm data
        """
        if memory_width not in SUPPORTED_MEMORY_WIDTHS:
            raise FlipJumpWriteFjmException(f"Word size {memory_width} is not in {sorted(SUPPORTED_MEMORY_WIDTHS)}.")
        if version not in SUPPORTED_VERSIONS_NAMES:
            raise FlipJumpWriteFjmException(
                f'Error: unsupported version ({version}, this program supports {str(SUPPORTED_VERSIONS_NAMES)}).'
            )
        if flags < 0 or flags >= (1 << 64):
            raise FlipJumpWriteFjmException(f"flags must be a 64bit positive number, not {flags}")
        if FJMVersion.BaseVersion == version and flags != 0:
            raise FlipJumpWriteFjmException("version 0 does not support the flags option")
        if FJMVersion.CompressedVersion == version:
            if lzma_preset not in range(10):
                raise FlipJumpWriteFjmException("version 3 requires an LZMA preset (0-9, faster->smaller).")
            else:
                self.lzma_preset = lzma_preset

        self.output_file = output_file
        self.word_size = memory_width
        self.version = version
        self.flags = flags
        self.reserved: int = 0

        self.segments: List[Tuple[int, int, int, int]] = []
        self.data: List[int] = []  # words array

    def _compress_data(self, data: bytes) -> bytes:
        try:
            return lzma.compress(
                data, format=_LZMA_FORMAT, filters=_lzma_compression_filters(2 * self.word_size, self.lzma_preset)
            )
        except lzma.LZMAError as e:
            raise FlipJumpWriteFjmException('Error: Unable to compress the data.') from e

    def write_to_file(self) -> None:
        """
        writes the .fjm headers, segments and (might be compressed) data into the output_file.
        @note call this after finished adding data and segments and editing the Writer.
        """
        write_tag = '<' + {8: 'B', 16: 'H', 32: 'L', 64: 'Q'}[self.word_size]

        with open(self.output_file, 'wb') as f:
            f.write(pack(_header_base_format, FJ_MAGIC, self.word_size, self.version.value, len(self.segments)))
            if FJMVersion.BaseVersion != self.version:
                f.write(pack(_header_extension_format, self.flags, self.reserved))

            for segment in self.segments:
                f.write(pack(_segment_format, *segment))

            fjm_data = b''.join(pack(write_tag, word) for word in self.data)
            if FJMVersion.CompressedVersion == self.version:
                fjm_data = self._compress_data(fjm_data)

            f.write(fjm_data)

    def get_segment_addresses_repr(self, word_start_address: int, word_length: int) -> str:
        """
        @param word_start_address: the start address of the segment in memory (in words)
        @param word_length: the number of words the segment takes in memory
        @return: a nice looking segment-representation string by its addresses
        """
        return (
            f'[{hex(self.word_size * word_start_address)}, '
            f'{hex(self.word_size * (word_start_address + word_length))})'
        )

    @staticmethod
    def _is_collision(start1: int, end1: int, start2: int, end2: int) -> bool:
        if any(start2 <= address <= end2 for address in (start1, end1)):
            return True
        if any(start1 <= address <= end1 for address in (start2, end2)):
            return True
        return False

    def _validate_segment_addresses_not_overlapping(self, new_segment_start: int, new_segment_length: int) -> None:
        new_segment_end = new_segment_start + new_segment_length - 1
        for i, (segment_start, segment_length, _, _) in enumerate(self.segments):
            segment_end = segment_start + segment_length - 1

            if self._is_collision(segment_start, segment_end, new_segment_start, new_segment_end):
                raise FlipJumpWriteFjmException(
                    f"Overlapping segments addresses: "
                    f"seg[{i}]={self.get_segment_addresses_repr(segment_start, segment_length)}"
                    f" and "
                    f"seg[{len(self.segments)}]="
                    f"{self.get_segment_addresses_repr(new_segment_start, new_segment_length)}"
                )

    def _validate_segment_data_not_overlapping(self, new_data_start: int, new_data_length: int) -> None:
        if new_data_length == 0:
            return
        new_data_end = new_data_start + new_data_length - 1

        for i, (_, _, data_start, data_length) in enumerate(self.segments):
            if data_length == 0:
                continue
            data_end = data_start + data_length - 1

            if self._is_collision(data_start, data_end, new_data_start, new_data_end):
                raise FlipJumpWriteFjmException(
                    f"Overlapping segments data: "
                    f"seg[{i}]=data[{hex(data_start)}, {hex(data_end + 1)})"
                    f" and "
                    f"seg[{len(self.segments)}]=data[{hex(new_data_start)}, {hex(new_data_end + 1)})"
                )

    def _validate_segment_not_overlapping(
        self, segment_start: int, segment_length: int, data_start: int, data_length: int
    ) -> None:
        self._validate_segment_addresses_not_overlapping(segment_start, segment_length)

        if self.version in (FJMVersion.RelativeJumpVersion, FJMVersion.CompressedVersion):
            self._validate_segment_data_not_overlapping(data_start, data_length)

    def _update_to_relative_jumps(self, segment_start: int, data_start: int, data_length: int) -> None:
        word_mask = (1 << self.word_size) - 1
        for i in range(1, data_length, 2):
            self.data[data_start + i] = (self.data[data_start + i] - (segment_start + i) * self.word_size) & word_mask

    def add_segment(self, segment_start: int, segment_length: int, data_start: int, data_length: int) -> None:
        """
        inserts a new segment to the fjm file. checks that it doesn't overlap with any previously inserted segments.
        @param segment_start: the start address of the segment in memory (in words)
        @param segment_length: the number of words the segment takes in memory (if bigger that the data_length,
         the segment is padded with zeros after the end of the data).
        @param data_start: the index of the data's start in the inner data array
        @param data_length: the number of words in the segment's data
        """
        segment_addresses_str = f'seg[{self.segments}]={self.get_segment_addresses_repr(segment_start, segment_length)}'

        if segment_length <= 0:
            raise FlipJumpWriteFjmException(f"segment-length must be positive (in {segment_addresses_str}).")

        if segment_length < data_length:
            raise FlipJumpWriteFjmException(
                f"segment-length must be at-least data-length " f"(in {segment_addresses_str})."
            )

        if segment_start % 2 == 1 or segment_length % 2 == 1:
            raise FlipJumpWriteFjmException(
                f"segment-start and segment-length must be 2*w (2 * memory-width) aligned "
                f"(in {segment_addresses_str})."
            )

        self._validate_segment_not_overlapping(segment_start, segment_length, data_start, data_length)

        if self.version in (FJMVersion.RelativeJumpVersion, FJMVersion.CompressedVersion):
            self._update_to_relative_jumps(segment_start, data_start, data_length)

        self.segments.append((segment_start, segment_length, data_start, data_length))

    def add_data(self, data: List[int]) -> int:
        """
        append the data to the current data
        @param data: [in]: a list of words
        @return: the data start index
        """
        data_start = len(self.data)
        self.data += data
        return data_start

    def add_simple_segment_with_data(self, segment_start: int, data: List[int]) -> None:
        """
        adds the data and a segment that contains exactly the data, to the fjm
        @param segment_start: the start address of the segment in memory (in words)
        @param data: [in]: a list of words
        """
        data_start = self.add_data(data)
        self.add_segment(segment_start, len(data), data_start, len(data))
