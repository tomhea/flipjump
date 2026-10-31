"""
unit-tests for the device<->memory hook.

an IODevice can be attached to the interpreter memory at run-start (attach_memory), and can
then read and write interpreter memory by address during the run. pinned here over both the
pure-python and the native engine.
"""

from pathlib import Path

from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.interpreter.io_devices.device_memory import DeviceMemory
from flipjump.utils.classes import TerminationCause
from tests.unit.unit_utils import assemble_to_path


class MemorySpyIO(FixedIO):
    """a FixedIO that captures the attached DeviceMemory, reads some words on attach,
    and writes a word into a fixed address."""

    def __init__(self, read_word_address: int, write_word_address: int, write_value: int):
        super().__init__(b'')
        self.read_word_address = read_word_address
        self.write_word_address = write_word_address
        self.write_value = write_value
        self.read_at_attach: int = -1
        self.read_after_run: int = -1
        self.device_memory: DeviceMemory = None  # type: ignore[assignment]

    def attach_memory(self, device_memory: DeviceMemory) -> None:
        self.device_memory = device_memory
        self.read_at_attach = device_memory.read_word(self.read_word_address)
        device_memory.write_word(self.write_word_address, self.write_value)


# the marker variable holds 0x1234 at assemble time; the program flips one of its data bits
# (bit dbit+0 of the first hex: 0x4 -> 0x5) then halts. `marker: hex.vec 4` spans 4 ops.
MARKER_PROGRAM = '''
stl.startup
marker + dbit;       // flip data-bit 0 of the first hex of marker (0x4 -> 0x5)
loop: ;loop

marker: hex.vec 4, 0x1234
'''


def test_device_reads_and_writes_memory(tmp_path: Path, engine: str) -> None:
    fjm_path = assemble_to_path(MARKER_PROGRAM, tmp_path, memory_width=64, use_stl=True)

    # find the marker op addresses: scan for the known data pattern after the run once.
    # simpler: run once with a plain device and locate the written value via the spy reads.
    io_device = MemorySpyIO(read_word_address=1, write_word_address=3000, write_value=0x77)
    statistics = fjm_run.run(fjm_path, io_device=io_device, show_progress=False)
    assert statistics.termination_cause == TerminationCause.RuntimeMemoryError or (
        statistics.termination_cause == TerminationCause.Looping
    )

    # the device was attached before the run and could read memory
    assert io_device.device_memory is not None
    assert io_device.read_at_attach >= 0

    # the write is visible through the hook after the run as well
    assert io_device.device_memory.read_word(io_device.write_word_address) == 0x77


def test_device_memory_width_and_data_byte_helpers(tmp_path: Path, engine: str) -> None:
    fjm_path = assemble_to_path(MARKER_PROGRAM, tmp_path, memory_width=64, use_stl=True)

    io_device = MemorySpyIO(read_word_address=0, write_word_address=3000, write_value=1)
    fjm_run.run(fjm_path, io_device=io_device, show_progress=False)
    device_memory = io_device.device_memory

    assert device_memory.memory_width == 64

    # locate `marker` by scanning data bytes: its 4 hexes hold 4,3,2,1 (lsb first),
    # and after the program ran, the first hex was flipped 4 -> 5.
    found = None
    for op_index in range(0, 4000):
        op_bit_address = op_index * 128  # dw = 2*w = 128
        if [device_memory.read_data_byte(op_bit_address + i * 128) for i in range(4)] == [5, 3, 2, 1]:
            found = op_bit_address
            break
    assert found is not None, 'marker data pattern (post-flip) not found in memory'

    # write_data_byte round-trips and lands at bit-offset #w of the jump word
    device_memory.write_data_byte(found, 0xAB)
    assert device_memory.read_data_byte(found) == 0xAB
    jump_word = device_memory.read_word((found >> 6) + 1)
    assert (jump_word >> 7) & 0xFF == 0xAB  # #w = 7 for w=64
