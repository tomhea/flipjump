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

    @abstractmethod
    def read_bit(self) -> bool:
        return False

    @abstractmethod
    def write_bit(self, bit: bool) -> None:
        pass

    @abstractmethod
    def get_output(self, *, allow_incomplete_output: bool = False) -> bytes:
        pass

    # Also, each class should implement a "__del__" to flush last changes before it gets deleted.
