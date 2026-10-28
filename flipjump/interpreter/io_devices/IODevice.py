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
        called once when a run ends by itself (looping, a null ip, end of input) - e.g. the screen raises here
        if the output stopped inside a command. the default does nothing.
        """

    @abstractmethod
    def read_bit(self) -> bool:
        return False

    @abstractmethod
    def write_bit(self, bit: bool) -> None:
        pass

    def read_byte(self) -> int:
        """
        read 8 input bits, lsb first. a device that overrides it is called once a byte by the native engine, so it
        overrides it only when its next byte does not depend on what the program outputs meanwhile (a byte stream).
        """
        return sum(int(self.read_bit()) << i for i in range(8))

    def write_byte(self, byte: int) -> None:
        """
        write 8 output bits, lsb first. a device that overrides it is called once a byte by the native engine.
        """
        for i in range(8):
            self.write_bit(bool((byte >> i) & 1))

    @abstractmethod
    def get_output(self, *, allow_incomplete_output: bool = False) -> bytes:
        pass

    # Also, each class should implement a "__del__" to flush last changes before it gets deleted.
