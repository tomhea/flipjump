"""
unit-tests for the native engine's run(write_byte=...): the output bits gathered into whole bytes in C, so the
device is called once a byte instead of once a bit.

the property pinned here: a device sees EXACTLY the stream it sees bit by bit - the same bits, in the same order,
and the same bits already out at every input read - including output that is not byte-aligned at a read and at
the end of the run. the reference is the pure-python fast loop, which calls write_bit once a bit.
"""

from pathlib import Path
from typing import List, Tuple

import pytest

from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.interpreter.io_devices.IODevice import IODevice
from tests.unit.unit_utils import CAT_PROGRAM, assemble_to_path, native_engine_required

Event = Tuple[str, int]

# 3 bits out, a read (3 bits of a byte pending), 8 + 5 more (back on a byte boundary), a whole byte, an aligned
# read, then 3 bits left pending at the end of the run
UNALIGNED_PROGRAM = '''
    stl.startup
    stl.output_bit 1
    stl.output_bit 0
    stl.output_bit 1
    bit.input_bit b
    stl.output_char 0x41
    rep(5, i) stl.output_bit (i & 1)
    stl.output_char 0x42
    bit.input_bit b
    stl.output_bit 1
    stl.output_bit 1
    stl.output_bit 0
    stl.loop
b:
    bit.vec 1, 0
'''


class RecordingIO(FixedIO):
    """a FixedIO that logs every device call: ('bit', b), ('byte', B) and ('read', b). Its INPUT stays bit by bit
    (IODevice's read_byte: not an override), so the whole interleaving can be compared with the python loop's --
    the byte input is test_native_read_byte's."""

    read_byte = IODevice.read_byte

    def __init__(self, fixed_input: bytes, *, lsb_first: bool = True) -> None:
        super().__init__(fixed_input)
        self.events: List[Event] = []
        self.lsb_first = lsb_first

    def read_bit(self) -> bool:
        bit = super().read_bit()
        self.events.append(('read', int(bit)))
        return bit

    def write_bit(self, bit: bool) -> None:
        self.events.append(('bit', int(bit)))
        super().write_bit(bit)

    def write_byte(self, byte: int) -> None:
        self.events.append(('byte', byte))
        for i in range(8):
            super().write_bit(bool((byte >> (i if self.lsb_first else 7 - i)) & 1))


def as_bits(events: List[Event], *, lsb_first: bool = True) -> List[Event]:
    """the device's view as single bits: every byte call expanded to its 8 bits"""
    out: List[Event] = []
    for kind, value in events:
        if kind == 'byte':
            out += [('bit', (value >> (i if lsb_first else 7 - i)) & 1) for i in range(8)]
        else:
            out.append((kind, value))
    return out


def run_recorded(
    fjm_path: Path, fixed_input: bytes, *, native: bool, monkeypatch: pytest.MonkeyPatch, lsb_first: bool = True
) -> RecordingIO:
    io = RecordingIO(fixed_input, lsb_first=lsb_first)
    with monkeypatch.context() as m:
        if native:
            m.delenv('FLIPJUMP_NO_NATIVE', raising=False)
        else:
            m.setattr(fjm_run, '_fjcore', None)
        fjm_run.run(fjm_path, io_device=io, print_time=False)
    return io


@native_engine_required
@pytest.mark.parametrize('program_id,fixed_input', [('unaligned', b'\x01\x00'), ('cat', b'byte by byte!')])
def test_write_byte_delivers_the_bit_by_bit_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_id: str, fixed_input: bytes
) -> None:
    if program_id == 'unaligned':
        fjm_path = assemble_to_path(UNALIGNED_PROGRAM, tmp_path, use_stl=True)
    else:
        fjm_path = assemble_to_path(CAT_PROGRAM.read_text(), tmp_path, use_stl=True)

    reference = run_recorded(fjm_path, fixed_input, native=False, monkeypatch=monkeypatch)
    native = run_recorded(fjm_path, fixed_input, native=True, monkeypatch=monkeypatch)

    assert all(kind != 'byte' for kind, _ in reference.events)  # the python loop calls write_bit only
    assert as_bits(native.events) == reference.events
    assert native.get_output(allow_incomplete_output=True) == reference.get_output(allow_incomplete_output=True)
    assert any(kind == 'byte' for kind, _ in native.events)  # the gathered path ran


@native_engine_required
def test_unaligned_bits_go_out_one_by_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fjm_path = assemble_to_path(UNALIGNED_PROGRAM, tmp_path, use_stl=True)
    events = run_recorded(fjm_path, b'\x01\x00', native=True, monkeypatch=monkeypatch).events
    kinds = [kind for kind, _ in events]
    # 3 pending bits flushed before the first read; per-bit until the device is back on a byte boundary (5 more);
    # then whole bytes: 0x41's top 3 bits + the 5 single bits (0,1,0,1,0), and 0x42; the aligned read; and the 3
    # bits pending at the end of the run
    assert kinds == ['bit'] * 3 + ['read'] + ['bit'] * 5 + ['byte'] * 2 + ['read'] + ['bit'] * 3
    assert events[9:11] == [('byte', (0x41 >> 5) | (0b01010 << 3)), ('byte', 0x42)]


@native_engine_required
def test_control_a_wrong_write_byte_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """the comparison above can fail: a device that unpacks the byte msb first sees another stream"""
    fjm_path = assemble_to_path(CAT_PROGRAM.read_text(), tmp_path, use_stl=True)
    reference = run_recorded(fjm_path, b'ab', native=False, monkeypatch=monkeypatch)
    wrong = run_recorded(fjm_path, b'ab', native=True, monkeypatch=monkeypatch, lsb_first=False)
    assert wrong.get_output(allow_incomplete_output=True) != reference.get_output(allow_incomplete_output=True)


# 11 bits out: one whole byte and 3 bits left pending at the end of the run
ELEVEN_BITS_PROGRAM = '''
    stl.startup
    stl.output_char 0x41
    stl.output_bit 1
    stl.output_bit 0
    stl.output_bit 1
    stl.loop
'''


@native_engine_required
def test_the_paged_loop_delivers_the_bit_by_bit_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """a last-ops ring routes the native run through the generic (paged) loop, which gathers the same way"""
    fjm_path = assemble_to_path(UNALIGNED_PROGRAM, tmp_path, use_stl=True)
    reference = run_recorded(fjm_path, b'\x01\x00', native=False, monkeypatch=monkeypatch)
    io = RecordingIO(b'\x01\x00')
    monkeypatch.delenv('FLIPJUMP_NO_NATIVE', raising=False)
    fjm_run.run(fjm_path, io_device=io, print_time=False, last_ops_debugging_list_length=16)
    assert as_bits(io.events) == reference.events
    assert any(kind == 'byte' for kind, _ in io.events)


class RaisingIO(RecordingIO):
    """fails as its second output byte completes -- in write_byte, or in the write_bit that ends it"""

    def write_bit(self, bit: bool) -> None:
        super().write_bit(bit)
        if sum(kind == 'bit' for kind, _ in self.events) == 16:
            raise ValueError('device failure')

    def write_byte(self, byte: int) -> None:
        super().write_byte(byte)
        if sum(kind == 'byte' for kind, _ in self.events) == 2:
            raise ValueError('device failure')


@native_engine_required
def test_a_device_error_mid_run_stops_the_run_at_the_same_bit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fjm_path = assemble_to_path(CAT_PROGRAM.read_text(), tmp_path, use_stl=True)
    streams = []
    for native in (False, True):
        io = RaisingIO(b'abcd')
        with monkeypatch.context() as m:
            if native:
                m.delenv('FLIPJUMP_NO_NATIVE', raising=False)
            else:
                m.setattr(fjm_run, '_fjcore', None)
            # the engines report the device's error differently (the native run wraps it), but both stop on it
            with pytest.raises(Exception) as failure:
                fjm_run.run(fjm_path, io_device=io, print_time=False)
            assert 'device failure' in repr(failure.value) + repr(failure.value.__cause__)
        streams.append(as_bits(io.events))
    assert streams[0] == streams[1]


def test_only_a_device_with_its_own_write_byte_gets_bytes() -> None:
    """IODevice's default write_byte is 8 write_bits: gathering for it would only delay each bit to its byte's end"""
    from flipjump.interpreter.io_devices.ScreenIO import InMemoryScreen

    class BitIO(IODevice):
        def read_bit(self) -> bool:
            return False

        def write_bit(self, bit: bool) -> None:
            pass

        def get_output(self, *, allow_incomplete_output: bool = False) -> bytes:
            return b''

    assert not fjm_run._overrides(BitIO(), 'write_byte')
    assert fjm_run._overrides(FixedIO(b''), 'write_byte')
    assert fjm_run._overrides(RecordingIO(b''), 'write_byte')
    assert fjm_run._overrides(InMemoryScreen(), 'write_byte')


@native_engine_required
def test_reset_starts_the_device_stream_on_a_byte_boundary(tmp_path: Path) -> None:
    """a run that ends mid-byte leaves the device's phase at 3; reset() (a fresh stream: the per-frame walker's
    fresh device) must gather the next run's first byte again"""
    from flipjump.fjm import fjm_reader
    from flipjump.utils.exceptions import IOReadOnEOF
    from flipjump.utils.progress import ProgressBar

    fjm_path = assemble_to_path(ELEVEN_BITS_PROGRAM, tmp_path, use_stl=True)
    header = fjm_reader.read_fjm_header(fjm_path)
    with ProgressBar('', print_time=False, total=1.0) as progress:
        core = fjm_run._load_native(fjm_path, header, None, False, progress)
    core.freeze()
    kinds = []
    for _ in range(2):
        core.reset()
        io = RecordingIO(b'')
        core.run(io.read_bit, io.write_bit, IOReadOnEOF, write_byte=io.write_byte)
        kinds.append([kind for kind, _ in io.events])
    assert kinds[0] == ['byte'] + ['bit'] * 3
    assert kinds[1] == kinds[0]
