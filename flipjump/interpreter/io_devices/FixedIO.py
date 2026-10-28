"""
a fixed-input io-device (mainly for tests).
reads input from a pre-supplied bytes buffer and collects the program's output in
memory (retrievable via get_output) instead of printing it.
"""

from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.utils.exceptions import IOReadOnEOF, IncompleteOutput


class FixedIO(IODevice):
    """
    read from fixed input, don't output (with get_output functionality)
    """

    def __init__(self, _input: bytes):
        self.remaining_input = _input
        self._output = b''

        self.current_input_byte = 0
        self.bits_to_read_in_input_byte = 0

        self.current_output_byte = 0
        self.bits_to_write_in_output_byte = 0

    def read_bit(self) -> bool:
        if 0 == self.bits_to_read_in_input_byte:
            if not self.remaining_input:
                raise IOReadOnEOF("Read an empty input on fixed IO (EOF)")

            self.current_input_byte = self.remaining_input[0]
            self.remaining_input = self.remaining_input[1:]
            self.bits_to_read_in_input_byte = 8

        bit = (self.current_input_byte & 1) == 1
        self.current_input_byte >>= 1
        self.bits_to_read_in_input_byte -= 1
        return bit

    def write_bit(self, bit: bool) -> None:
        self.current_output_byte |= bit << self.bits_to_write_in_output_byte
        self.bits_to_write_in_output_byte += 1

        if 8 == self.bits_to_write_in_output_byte:
            self._output += self.current_output_byte.to_bytes(1, 'little')
            self.current_output_byte = 0
            self.bits_to_write_in_output_byte = 0

    def get_output(self, *, allow_incomplete_output: bool = False) -> bytes:
        """
        @raise IncompleteOutput when the number of outputted bits can't be divided by 8
        @return: full output until now
        """
        if not allow_incomplete_output and 0 != self.bits_to_write_in_output_byte:
            raise IncompleteOutput(
                "tries to get output when an unaligned number of bits was outputted " "(doesn't divide 8)"
            )

        return self._output
