"""
the standard io-device.
reads the program's input from stdin and writes its output to stdout (output can be
silenced by turning off the output_verbose flag).
"""

from sys import stdin, stdout

from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.utils.exceptions import IOReadOnEOF, IncompleteOutput
from flipjump.utils.constants import IO_BYTES_ENCODING


class StandardIO(IODevice):
    """
    read from stdin, write to stdout
    """

    def __init__(self, output_verbose: bool):
        """
        @param output_verbose: if true print program's output
        """
        self.output_verbose = output_verbose
        self._output = b''

        self.current_input_byte = 0
        self.bits_to_read_in_input_byte = 0

        self.current_output_byte = 0
        self.bits_to_write_in_output_byte = 0

    @property
    def outputs_live(self) -> bool:
        # when verbose, write_bit prints each byte to stdout during the run
        return self.output_verbose

    def read_bit(self) -> bool:
        if 0 == self.bits_to_read_in_input_byte:
            read_bytes = stdin.read(1).encode(encoding=IO_BYTES_ENCODING)
            if 0 == len(read_bytes):
                raise IOReadOnEOF("Read an empty input on standard IO (EOF)")

            self.current_input_byte = read_bytes[0]
            self.bits_to_read_in_input_byte = 8

        bit = (self.current_input_byte & 1) == 1
        self.current_input_byte >>= 1
        self.bits_to_read_in_input_byte -= 1
        return bit

    def write_bit(self, bit: bool) -> None:
        self.current_output_byte |= bit << self.bits_to_write_in_output_byte
        self.bits_to_write_in_output_byte += 1

        if 8 == self.bits_to_write_in_output_byte:
            curr_output: bytes = self.current_output_byte.to_bytes(1, 'little')
            if self.output_verbose:
                stdout.write(curr_output.decode(encoding=IO_BYTES_ENCODING))
                stdout.flush()
            self._output += curr_output
            self.current_output_byte = 0
            self.bits_to_write_in_output_byte = 0

    def get_output(self, *, allow_incomplete_output: bool = False) -> bytes:
        if not allow_incomplete_output and 0 != self.bits_to_write_in_output_byte:
            raise IncompleteOutput(
                "tries to get output when an unaligned number of bits was outputted " "(doesn't divide 8)"
            )

        return self._output
