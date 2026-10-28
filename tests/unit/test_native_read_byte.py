"""
unit-tests for the native engine's run(read_byte=...): the input side of the byte path -- a device that hands over
whole bytes (it overrides read_byte: a byte stream such as stdin or a fixed buffer) is asked once a byte, and the
engine feeds the program the byte's bits; a device that answers bit by bit (it keeps IODevice's read_byte: the
keyboard) is asked bit by bit.

the properties pinned here, against the pure-python fast loop (which calls read_bit / write_bit once a bit):
- the device's OUTPUT bits are exactly the bit-by-bit run's, and the program's INPUT bits are its stream's first ones
  (a fetched byte's unread bits stay buffered in the engine; the device has the same input left either way);
- before every device input call the device has received exactly the output the program made before the first
  input bit that call serves (the pending output goes out first);
- bytes are fetched only from a byte boundary of the input stream (bits taken one by one first re-align it);
- EOF raised by read_byte ends the run at the same op as the bit-by-bit run;
- reset() drops the bits still buffered (a fresh stream), and so does a re-init (Memory.__init__);
- a bit-by-bit device sees exactly the bit-by-bit run's interleaving.
CONTROL: a device whose read_byte returns its byte msb first is caught.
"""

from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.utils.classes import TerminationCause
from tests.unit.unit_utils import CAT_PROGRAM, RecordingIO, assemble_to_path, native_engine_required, run_recorded

# a bit, output, a bit (both taken one by one through read_byte's buffer of one byte), output, then 3 single bits read
# across a byte boundary -- 1 + 1 + ... -- and a whole byte read, with output pending (3 bits) at an input read
MIXED_PROGRAM = '''
    stl.startup
    bit.input_bit b
    stl.output_bit 1
    stl.output_bit 0
    stl.output_bit 1
    bit.input_bit b
    stl.output_char 0x41
    rep(8, i) bit.input_bit b
    stl.output_bit 1
    bit.input ascii
    bit.print ascii
    stl.loop
b:
    bit.vec 1, 0
ascii:
    bit.vec 8, 0
'''

# reads one input bit, then loops
ONE_BIT_PROGRAM = 'stl.startup\nbit.input_bit b\nstl.loop\nb:\nbit.vec 1, 0\n'


class BitOnlyIO(RecordingIO):
    """the same device answering bit by bit in both directions (IODevice's defaults: not overrides)"""

    read_byte = IODevice.read_byte
    write_byte = IODevice.write_byte


class MsbFirstIO(RecordingIO):
    """hands its input bytes over msb first: a wrong device"""

    def read_byte(self) -> int:
        return int(f'{super().read_byte():08b}'[::-1], 2)


def streams(events: List[Tuple[Any, ...]]) -> Tuple[List[int], List[int]]:
    """(the input bits, the output bits) the device dealt in, every byte call expanded lsb first"""
    ins: List[int] = []
    outs: List[int] = []
    for e in events:
        if e[0] == 'read':
            ins.append(e[1])
        elif e[0] == 'rbyte':
            ins += [(e[1] >> i) & 1 for i in range(8)]
        elif e[0] == 'bit':
            outs.append(e[1])
        else:
            outs += [(e[1] >> i) & 1 for i in range(8)]
    return ins, outs


def input_calls(events: List[Tuple[Any, ...]]) -> List[Tuple[int, int]]:
    """per device input call: (the index of the first input bit it serves, the output bits received before it)"""
    out: List[Tuple[int, int]] = []
    served = 0
    for e in events:
        if e[0] in ('read', 'rbyte'):
            out.append((served, e[2]))
            served += 1 if e[0] == 'read' else 8
    return out


@native_engine_required
@pytest.mark.parametrize('program_id,fixed_input', [('mixed', b'\x01\xa5\x3c'), ('cat', b'byte by byte!')])
@pytest.mark.parametrize('ring', [None, 16])
def test_read_byte_delivers_the_bit_by_bit_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_id: str, fixed_input: bytes, ring: Optional[int]
) -> None:
    src = MIXED_PROGRAM if program_id == 'mixed' else CAT_PROGRAM.read_text()
    fjm_path = assemble_to_path(src, tmp_path, use_stl=True)
    reference, native = RecordingIO(fixed_input), RecordingIO(fixed_input)
    ref_stats = run_recorded(fjm_path, reference, native=False, monkeypatch=monkeypatch)
    nat_stats = run_recorded(
        fjm_path, native, native=True, monkeypatch=monkeypatch, last_ops_debugging_list_length=ring
    )
    assert all(e[0] in ('read', 'bit') for e in reference.events)  # the python loop: bit by bit
    (nat_in, nat_out), (ref_in, ref_out) = streams(native.events), streams(reference.events)
    assert nat_out == ref_out
    # the program's input bits are the stream's first ones; a fetched byte's unread bits stay in the engine (fewer
    # than 8) -- the device itself took the same bytes either way (a bit-by-bit FixedIO pulls a whole byte at a byte's
    # first bit), so what it has left is the same
    assert nat_in[: len(ref_in)] == ref_in and len(nat_in) - len(ref_in) < 8
    assert native.remaining_input == reference.remaining_input
    assert any(e[0] == 'rbyte' for e in native.events)  # the byte path ran
    # every device input call had received the output made before the first bit it serves
    out_before = {k: outs for k, (k_, outs) in enumerate(input_calls(reference.events)) if k == k_}
    for first_bit, outs in input_calls(native.events):
        assert outs == out_before[first_bit], (first_bit, outs, out_before[first_bit])
    assert nat_stats.op_counter == ref_stats.op_counter
    assert nat_stats.termination_cause == ref_stats.termination_cause
    assert native.get_output(allow_incomplete_output=True) == reference.get_output(allow_incomplete_output=True)


@native_engine_required
def test_the_mixed_program_reads_bytes_only_on_a_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """the first bit fetches a byte (7 buffered); the program takes 1 + 8 + 8 bits: buffered bits first, then one more
    byte, its 8th bit on that byte's boundary... the device is asked once per 8 input bits"""
    fjm_path = assemble_to_path(MIXED_PROGRAM, tmp_path, use_stl=True)
    io = RecordingIO(b'\x01\xa5\x3c')
    run_recorded(fjm_path, io, native=True, monkeypatch=monkeypatch)
    calls = [e[0] for e in io.events if e[0] in ('read', 'rbyte')]
    assert calls == ['rbyte'] * 3  # 1 + 1 + 8 + 8 = 18 bits: 3 bytes


@native_engine_required
def test_eof_from_read_byte_ends_the_run_where_the_bits_would(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fjm_path = assemble_to_path(CAT_PROGRAM.read_text(), tmp_path, use_stl=True)
    ref = run_recorded(fjm_path, RecordingIO(b'ab'), native=False, monkeypatch=monkeypatch)
    nat = run_recorded(fjm_path, RecordingIO(b'ab'), native=True, monkeypatch=monkeypatch)
    assert nat.termination_cause == ref.termination_cause == TerminationCause.EOF
    assert nat.op_counter == ref.op_counter


@native_engine_required
def test_a_bit_by_bit_device_sees_the_bit_by_bit_interleaving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fjm_path = assemble_to_path(MIXED_PROGRAM, tmp_path, use_stl=True)
    reference, native = BitOnlyIO(b'\x01\xa5\x3c'), BitOnlyIO(b'\x01\xa5\x3c')
    run_recorded(fjm_path, reference, native=False, monkeypatch=monkeypatch)
    run_recorded(fjm_path, native, native=True, monkeypatch=monkeypatch)
    assert native.events == reference.events


@native_engine_required
def test_control_a_wrong_read_byte_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fjm_path = assemble_to_path(CAT_PROGRAM.read_text(), tmp_path, use_stl=True)
    reference = RecordingIO(b'ab')
    run_recorded(fjm_path, reference, native=False, monkeypatch=monkeypatch)
    wrong = MsbFirstIO(b'ab')
    run_recorded(fjm_path, wrong, native=True, monkeypatch=monkeypatch)
    assert wrong.get_output(allow_incomplete_output=True) != reference.get_output(allow_incomplete_output=True)


def test_only_a_device_with_its_own_read_byte_is_read_by_byte() -> None:
    """the keyboard (PcIO) answers bit by bit -- it queues 12-bit events, so a byte fetched at once would poll it for
    the next event early; the byte streams hand over bytes"""
    from flipjump.interpreter.io_devices.StandardIO import StandardIO
    from flipjump.interpreter.io_devices.pygame_window import PcIO
    from flipjump.interpreter.io_devices.ScreenIO import InMemoryScreen
    from flipjump.interpreter.io_devices.KeyboardIO import KeyboardIO, ScriptedKeyEventSource

    assert fjm_run._overrides(FixedIO(b''), 'read_byte')
    assert fjm_run._overrides(StandardIO(False), 'read_byte')
    pc = PcIO(InMemoryScreen(), KeyboardIO(ScriptedKeyEventSource([])))
    assert not fjm_run._overrides(pc, 'read_byte')
    assert fjm_run._overrides(pc, 'write_byte')
    assert not fjm_run._overrides(BitOnlyIO(b''), 'read_byte')


@native_engine_required
def test_reset_drops_the_buffered_input_bits(tmp_path: Path) -> None:
    """a run that takes 1 bit of a fetched byte leaves 7 buffered; after reset() (a fresh stream) the next run fetches
    a byte again; without reset the next run takes its bit from the buffer"""
    from flipjump.fjm import fjm_reader
    from flipjump.utils.exceptions import IOReadOnEOF
    from flipjump.utils.progress import ProgressBar

    fjm_path = assemble_to_path(ONE_BIT_PROGRAM, tmp_path, use_stl=True)
    header = fjm_reader.read_fjm_header(fjm_path)
    with ProgressBar('', print_time=False) as progress:
        core = fjm_run._load_native(fjm_path, header, None, False, progress)
    core.freeze()
    calls = []
    for do_reset in (True, False, True):
        if do_reset:
            core.reset()
        io = RecordingIO(b'\x05\x05')
        core.run(io.read_bit, io.write_bit, IOReadOnEOF, write_byte=io.write_byte, read_byte=io.read_byte)
        calls.append([e[0] for e in io.events if e[0] in ('read', 'rbyte')])
    assert calls == [['rbyte'], [], ['rbyte']]


@native_engine_required
def test_reinit_drops_the_buffered_input_bits(tmp_path: Path) -> None:
    """a run that takes 1 bit of a fetched byte leaves 7 buffered; Memory.__init__ on the live object (then the same
    program loaded again) starts a fresh stream, so the next run fetches a byte again"""
    from flipjump.fjm import fjm_reader
    from flipjump.utils.exceptions import IOReadOnEOF
    from flipjump.utils.progress import ProgressBar

    fjm_path = assemble_to_path(ONE_BIT_PROGRAM, tmp_path, use_stl=True)
    header = fjm_reader.read_fjm_header(fjm_path)
    with ProgressBar('', print_time=False) as progress:
        core = fjm_run._load_native(fjm_path, header, None, False, progress)
    calls = []
    for do_reinit in (False, True):
        if do_reinit:
            core.__init__(header.memory_width)
            for start, length, _data_start, _data_length in header.segments:
                core.add_segment(start, length)
            _raw_header, data = fjm_reader.read_fjm_raw(fjm_path)
            for start, _length, data_start, data_length in header.segments:
                core.set_words_bytes(start, data, data_start, data_length, header.relative_jumps)
        io = RecordingIO(b'\x05\x05')
        core.run(io.read_bit, io.write_bit, IOReadOnEOF, write_byte=io.write_byte, read_byte=io.read_byte)
        calls.append([e[0] for e in io.events if e[0] in ('read', 'rbyte')])
    assert calls == [['rbyte'], ['rbyte']]
