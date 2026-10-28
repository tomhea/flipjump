"""
the abstract IO device interface.
defines the bit-level read/write contract every io-device implements, so the
interpreter can stay agnostic of where its input/output actually goes.
"""

from abc import ABC, abstractmethod

from flipjump.interpreter.io_devices.device_memory import DeviceMemory


class IODevice(ABC):
    """
    abstract IO device
    """

    @property
    def outputs_live(self) -> bool:
        """
        True if this device emits the program's output live during the run (e.g. straight to
        the terminal). when True, the termination summary skips re-dumping the buffered output -
        it's already been shown. defaults to False (buffer-only devices like FixedIO).
        """
        return False

    def attach_memory(self, device_memory: DeviceMemory) -> None:
        """
        called by the interpreter right before the run-loop starts, with the device<->memory
        hook - the device may keep it and read/write interpreter memory during the run.
        the default implementation ignores it.
        """

    def close(self) -> None:
        """release what the device holds (e.g. its window). the default holds nothing."""

    def end_of_run(self) -> None:
        """
        called by the interpreter once, when a run ends by itself (looping, a null ip, end of input).
        a device whose protocol can be left unfinished (e.g. the screen, inside a command) raises its error here.
        not called after Ctrl+C, a closed window or a memory error, or when the run raised. the default does nothing.
        """

    @abstractmethod
    def read_bit(self) -> bool:
        return False

    @abstractmethod
    def write_bit(self, bit: bool) -> None:
        pass

    def write_byte(self, byte: int) -> None:
        """
        write 8 output bits at once, lsb first: exactly write_bit of each. the native engine calls this once a
        byte (only from a byte boundary of the output stream), instead of write_bit once a bit; a device whose
        output is bytes overrides it to skip the 8 calls.
        """
        for i in range(8):
            self.write_bit(bool((byte >> i) & 1))

    @abstractmethod
    def get_output(self, *, allow_incomplete_output: bool = False) -> bytes:
        pass

    # Also, each class should implement a "__del__" to flush last changes before it gets deleted.
