"""
the device<->memory hook.
lets an IODevice read and write the interpreter memory by address during a run.

the interpreter attaches a DeviceMemory to the io-device (IODevice.attach_memory) right
before the run-loop starts - one adapter per engine (the python Reader / the native Memory).

addresses: read_word/write_word use word addresses (bit_address >> log2(w)). the
read_data_byte/write_data_byte helpers use the op-structured data convention instead: a
"packed byte" lives in the data bits (dbit..dbit+7, where dbit = w + #w) of one fj op -
bits #w..#w+7 of the op's jump word - so they take the op's (dw-aligned) bit-address
directly. (a hex.vec nibble is the low 4 of these bits; a packed byte is all 8.)
"""

from abc import ABC, abstractmethod

from flipjump.fjm.fjm_reader import Reader


class DeviceMemory(ABC):
    """read/write the interpreter memory by address during a run."""

    memory_width: int

    @abstractmethod
    def read_word(self, word_address: int) -> int:
        """read the w-bit word at the word-address (uninitialized words read 0)."""

    @abstractmethod
    def write_word(self, word_address: int, value: int) -> None:
        """write the w-bit word at the word-address (the value is masked to w bits)."""

    def read_data_byte(self, op_bit_address: int) -> int:
        """read the packed data-byte (bits dbit..dbit+7) of the op at the dw-aligned bit-address."""
        self._require_byte_capable_width()
        return (self.read_word(self._jump_word_address(op_bit_address)) >> self._data_bit_offset) & 0xFF

    def write_data_byte(self, op_bit_address: int, value: int) -> None:
        """write the packed data-byte (bits dbit..dbit+7) of the op at the dw-aligned bit-address."""
        self._require_byte_capable_width()
        jump_word_address = self._jump_word_address(op_bit_address)
        jump_word = self.read_word(jump_word_address)
        byte_mask = 0xFF << self._data_bit_offset
        self.write_word(jump_word_address, (jump_word & ~byte_mask) | ((value & 0xFF) << self._data_bit_offset))

    def _require_byte_capable_width(self) -> None:
        # a packed byte spans bits #w..#w+7 of the jump word - it only fits when w >= 16
        if self.memory_width < 16:
            raise ValueError(f'packed data-bytes require memory_width >= 16, got {self.memory_width}')

    @property
    def _data_bit_offset(self) -> int:
        # dbit = w + #w: the data bits sit at offset #w inside the op's jump word
        return self.memory_width.bit_length()

    def _jump_word_address(self, op_bit_address: int) -> int:
        """the word-address of the op's jump word (the op's second word, where the packed
        data-byte lives): bit_address / w gives the op's first (flip) word, + 1 the jump word."""
        return (op_bit_address >> (self.memory_width.bit_length() - 1)) + 1


class ReaderDeviceMemory(DeviceMemory):
    """the DeviceMemory adapter over the pure-python Reader memory (the python run-loops)."""

    def __init__(self, reader: Reader):
        self._reader = reader
        self.memory_width = reader.memory_width

    def read_word(self, word_address: int) -> int:
        # mask like the Reader's own accessors, so device addresses wrap consistently
        return self._reader.memory.get(word_address & ((1 << self.memory_width) - 1), 0)

    def write_word(self, word_address: int, value: int) -> None:
        word_mask = (1 << self.memory_width) - 1
        self._reader.memory[word_address & word_mask] = value & word_mask


class NativeDeviceMemory(DeviceMemory):
    """the DeviceMemory adapter over the native engine's memory (_fjcore.Memory)."""

    def __init__(self, core_memory, memory_width: int):  # type: ignore[no-untyped-def]
        self._core_memory = core_memory
        self.memory_width = memory_width

    def read_word(self, word_address: int) -> int:
        return int(self._core_memory.get_word(word_address))

    def write_word(self, word_address: int, value: int) -> None:
        self._core_memory.set_word(word_address, value & ((1 << self.memory_width) - 1))
