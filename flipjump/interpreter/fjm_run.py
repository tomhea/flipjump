"""
the flipjump interpreter.
executes a compiled .fjm program one flipjump op at a time - managing the memory,
routing input/output through an IODevice, handling breakpoints, detecting termination,
and collecting run statistics (returned as TerminationStatistics).

three run-loops (engines) are implemented:
- the native engine (the default when built): the _fjcore C-extension - segment-aware paged
  memory and the fetch-flipjump loop in C, calling back into python only for IO. ~500x
  faster than the featured loop. build it with `pip install -e .`; disable it with the
  FLIPJUMP_NO_NATIVE=1 environment variable.
- the fast loop (the default otherwise): pure-python, the memory accesses and IO/termination
  checks are inlined, and the per-op statistics (flip/jump counters) are skipped. ~20x faster.
- the featured loop: supports breakpoints, tracing, and full statistics. selected when
  debugging features are requested, or explicitly with profile=True.
"""

from os import environ
from pathlib import Path
from typing import List, Optional, Deque

from flipjump.fjm import fjm_reader
from flipjump.fjm.fjm_reader import GarbageHandling

try:
    from flipjump.interpreter import _fjcore  # type: ignore[attr-defined]
except ImportError:  # the native engine is optional - fall back to the pure-python loops
    _fjcore = None
from flipjump.interpreter.debugging.breakpoints import BreakpointHandler, handle_breakpoint
from flipjump.utils.classes import TerminationCause, PrintTimer, RunStatistics
from flipjump.utils.exceptions import (
    FlipJumpRuntimeMemoryException,
    IOReadOnEOF,
    FlipJumpException,
    FlipJumpRuntimeException,
)
from flipjump.interpreter.io_devices.BrokenIO import BrokenIO
from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.interpreter.io_devices.device_memory import NativeDeviceMemory, ReaderDeviceMemory


class TerminationStatistics:
    """
    saves the run-statistics and data of the fj program-termination, to be presented nicely.
    also saves the program's output.
    """

    def __init__(
        self,
        run_statistics: RunStatistics,
        termination_cause: TerminationCause,
        *,
        memory_error_address: Optional[int] = None,
    ) -> None:
        self.run_time = run_statistics.get_run_time()

        self.op_counter = run_statistics.op_counter
        self.flip_counter = run_statistics.flip_counter
        self.jump_counter = run_statistics.jump_counter
        self.detailed_statistics = run_statistics.detailed_statistics
        self.storage_mode = run_statistics.storage_mode
        self.speculation_stats = run_statistics.speculation_stats
        self.last_ops_addresses: Optional[Deque[int]] = run_statistics.last_ops_addresses

        self.termination_cause = termination_cause
        self.memory_error_address = memory_error_address

    @staticmethod
    def beautify_address(address: int, breakpoint_handler: Optional[BreakpointHandler]) -> str:
        if not breakpoint_handler:
            return hex(address)

        return breakpoint_handler.get_address_str(address)

    def print(
        self, *, labels_handler: Optional[BreakpointHandler] = None, output_to_print: Optional[bytes] = None
    ) -> None:
        """
        Prints the termination cause, run times, ops-statistics.
        If ended not by looping - Then print the last-opcodes` addresses as well (and their label names if possible).
        @param labels_handler: Used to find the label name for each address (from the last-opcodes` addresses).
        @param output_to_print: if specified and terminated not by looping - print the given output.
        """

        flips_jumps_str = ''
        if self.detailed_statistics and self.op_counter:
            flips_percentage = self.flip_counter / self.op_counter * 100
            jumps_percentage = self.jump_counter / self.op_counter * 100
            flips_jumps_str = f'; {flips_percentage:.2f}% flips, {jumps_percentage:.2f}% jumps'

        storage_str = f'; {self.storage_mode} memory' if self.storage_mode else ''

        last_ops_str = ''
        output_str = ''
        if TerminationCause.Looping != self.termination_cause:
            if self.last_ops_addresses is not None:
                labels_handler_missing_string = (
                    ''
                    if labels_handler is not None and labels_handler.address_to_label
                    else '**** You may want to use debugging flags for more debugging info ****\n\n'
                )
                last_ops_str = (
                    f'\n\n{labels_handler_missing_string}'
                    f'Last {len(self.last_ops_addresses)} ops were at these addresses '
                    f'(The most-recent op, the one that failed, is first):\n  '
                    + '\n  '.join(
                        [self.beautify_address(address, labels_handler) for address in self.last_ops_addresses][::-1]
                    )
                )
            if output_to_print is not None:
                output_str = f"Program's output before it was terminated:  {output_to_print!r}\n\n"

        termination_cause_str = str(self.termination_cause)
        if self.memory_error_address is not None:
            termination_cause_str += f" (address {hex(self.memory_error_address)})"
        print(
            f'\n'
            f'{output_str}'
            f'Finished by {termination_cause_str} after {self.run_time:.3f}s '
            f'('
            f'{self.op_counter:,} ops executed'
            f'{flips_jumps_str}'
            f'{storage_str}'
            f').'
            f'{last_ops_str}'
        )


def _handle_input(io_device: IODevice, ip: int, mem: fjm_reader.Reader, statistics: RunStatistics) -> None:
    """
    if the ip is in the input-bit range, read a bit from the io_device into the memory.
    """
    w = mem.memory_width
    in_addr = 3 * w + w.bit_length()  # 3w + #w

    if ip <= in_addr < ip + 2 * w:
        with statistics.pause_timer:
            input_bit = io_device.read_bit()
        mem.write_bit(in_addr, input_bit)


def _handle_output(flip_address: int, io_device: IODevice, w: int) -> None:
    """
    if the ip is in the output-bit range, output the corresponding bit to the io_device.
    """
    out_addr = 2 * w
    if out_addr <= flip_address <= out_addr + 1:
        io_device.write_bit(out_addr + 1 == flip_address)


def _trace_jump(jump_address: int, show_trace: bool) -> None:
    """
    if show_trace is enabled, print the current jump-address.
    """
    if show_trace:
        print(hex(jump_address)[2:])


def _trace_flip(ip: int, flip_address: int, show_trace: bool) -> None:
    """
    if show_trace is enabled, print the current ip-address and flip-address.
    """
    if show_trace:
        print(hex(ip)[2:].rjust(7), end=':   ')
        print(hex(flip_address)[2:], end='; ', flush=True)


def run(
    fjm_path: Path,
    *,
    breakpoint_handler: Optional[BreakpointHandler] = None,
    io_device: Optional[IODevice] = None,
    show_trace: bool = False,
    print_time: bool = False,
    last_ops_debugging_list_length: Optional[int] = None,
    profile: bool = False,
    flat_max_words: Optional[int] = None,
) -> TerminationStatistics:
    """
    run / debug a .fjm file (a FlipJump interpreter)
    @param fjm_path: the path to the .fjm file
    @param breakpoint_handler:[in]: the breakpoint handler (if not None - debug, and break on its breakpoints)
    @param io_device:[in,out]: the device handling input/output
    @param show_trace: if true print every opcode executed
    @param print_time: if true print running times
    @param last_ops_debugging_list_length: The length of the last-ops list
    @param profile: if true use the featured loop and collect the full per-op statistics
    (flip/jump counters). by default the fast loop is used (which skips them).
    @param flat_max_words: the native engine's flat-storage window, in words (default 2^23,
    also settable with the FLIPJUMP_FLAT_MAX_WORDS environment variable). memory below the window
    runs flat; segments reaching above it keep the paged path for that part (hybrid). raising it
    costs startup time + footprint (8 bytes x window), never per-op speed.
    @return: the run's termination-statistics
    """
    with PrintTimer('  loading memory:  ', print_time=print_time):
        mem = fjm_reader.Reader(fjm_path)
    mem.assert_runnable()  # a program must hold its first op at address 0 (both engines)

    if io_device is None:
        io_device = BrokenIO()

    statistics = RunStatistics(mem.memory_width, last_ops_debugging_list_length)

    try:
        if profile or show_trace or breakpoint_handler is not None:
            io_device.attach_memory(ReaderDeviceMemory(mem))
            return _run_featured(mem, io_device, statistics, breakpoint_handler, show_trace)
        if _is_native_engine_usable(mem):
            return _run_native(mem, io_device, statistics, flat_max_words)
        io_device.attach_memory(ReaderDeviceMemory(mem))
        return _run_fast(mem, io_device, statistics)

    except FlipJumpRuntimeMemoryException as mem_e:
        return TerminationStatistics(
            statistics, TerminationCause.RuntimeMemoryError, memory_error_address=mem_e.memory_address
        )
    except FlipJumpException as fj_exception:
        raise fj_exception
    except KeyboardInterrupt:
        return TerminationStatistics(statistics, TerminationCause.KeyboardInterrupt)
    except Exception as unknown_exception:
        raise FlipJumpRuntimeException(
            "Unknown exception during running an .fjm file, please report this bug"
        ) from unknown_exception


def is_native_engine_active() -> bool:
    """
    is the native (C) engine present and enabled - i.e. will runs use it (for the programs it
    supports) rather than silently falling back to the ~200x-slower pure-python loop?

    a wheel can import cleanly yet have a broken/absent _fjcore (an ABI/libc/arch mismatch made
    the extension unimportable), in which case runs degrade to the python loop with no error at
    all. this predicate is the hard, cpu-independent signal for that: True iff _fjcore imported
    and FLIPJUMP_NO_NATIVE is not set. (whether a *specific* program also takes the native path
    additionally needs Stop garbage-handling - see _is_native_engine_usable.)
    """
    return _fjcore is not None and environ.get('FLIPJUMP_NO_NATIVE') != '1'


def _is_native_engine_usable(mem: fjm_reader.Reader) -> bool:
    """
    is the native (C) engine available and able to run this program?
    (it implements the Stop garbage-handling only - the default of fjm_run.run.)
    """
    return is_native_engine_active() and mem.garbage_handling == GarbageHandling.Stop


def _run_native(
    mem: fjm_reader.Reader,
    io_device: IODevice,
    statistics: RunStatistics,
    flat_max_words: Optional[int] = None,
) -> TerminationStatistics:
    """
    run with the native (C) engine: load the parsed program into a _fjcore.Memory and
    execute the run-loop in C. behaves exactly like the python fast loop.
    """
    assert _fjcore is not None
    core = _fjcore.Memory(mem.memory_width, flat_max_words=flat_max_words if flat_max_words else 0)
    for memory_segment in mem.memory_segments:
        core.add_segment(memory_segment.segment_start, memory_segment.segment_length)

    # bulk-load the parsed memory words, grouped into contiguous runs
    run_start: Optional[int] = None
    next_address = 0
    run_values: List[int] = []
    for address in sorted(mem.memory):
        if run_start is None or address != next_address:
            if run_start is not None:
                core.set_words(run_start, run_values)
            run_start, run_values = address, []
        run_values.append(mem.memory[address])
        next_address = address + 1
    if run_start is not None:
        core.set_words(run_start, run_values)

    io_device.attach_memory(NativeDeviceMemory(core, mem.memory_width))

    last_ops = statistics.last_ops_addresses
    statistics.detailed_statistics = False
    try:
        cause, op_count, error_bit_address, native_last_ops, paused_seconds = core.run(
            io_device.read_bit,
            io_device.write_bit,
            IOReadOnEOF,
            last_ops_length=last_ops.maxlen if last_ops is not None and last_ops.maxlen else 0,
        )
    finally:
        # keep op_counter, the IO-paused time and the storage mode valid on the exception
        # paths too (Ctrl+C, IO-device errors)
        statistics.op_counter = core.last_run_op_count
        statistics.pause_timer.paused_time += core.last_run_paused_seconds
        statistics.storage_mode = core.storage_mode
        statistics.speculation_stats = core.speculation_stats

    statistics.op_counter = op_count
    if last_ops is not None:
        last_ops.extend(native_last_ops)

    if cause == _fjcore.TERM_LOOPING:
        return TerminationStatistics(statistics, TerminationCause.Looping)
    if cause == _fjcore.TERM_EOF:
        return TerminationStatistics(statistics, TerminationCause.EOF)
    if cause == _fjcore.TERM_NULL_IP:
        return TerminationStatistics(statistics, TerminationCause.NullIP)
    return TerminationStatistics(
        statistics, TerminationCause.RuntimeMemoryError, memory_error_address=error_bit_address
    )


def _run_featured(
    mem: fjm_reader.Reader,
    io_device: IODevice,
    statistics: RunStatistics,
    breakpoint_handler: Optional[BreakpointHandler],
    show_trace: bool,
) -> TerminationStatistics:
    """
    the featured run-loop: supports breakpoints and tracing, and collects the full
    per-op statistics (the flip/jump counters and the last-ops debugging list).
    """
    ip = 0
    w = mem.memory_width

    while True:
        statistics.register_op_address(ip)

        # handle breakpoints
        if breakpoint_handler and breakpoint_handler.should_break(ip, statistics.op_counter):
            breakpoint_handler = handle_breakpoint(breakpoint_handler, ip, mem, statistics)

        # read flip word
        flip_address = mem.get_word(ip)
        _trace_flip(ip, flip_address, show_trace)

        # handle IO
        _handle_output(flip_address, io_device, w)
        try:
            _handle_input(io_device, ip, mem, statistics)
        except IOReadOnEOF:
            return TerminationStatistics(statistics, TerminationCause.EOF)

        # FLIP!
        mem.write_bit(flip_address, not mem.read_bit(flip_address))

        # read jump word
        jump_address = mem.get_word(ip + w)
        _trace_jump(jump_address, show_trace)
        statistics.register_op(ip, flip_address, jump_address)

        # check finish?
        if jump_address == ip and not ip <= flip_address < ip + 2 * w:
            return TerminationStatistics(statistics, TerminationCause.Looping)
        if jump_address < 2 * w:
            return TerminationStatistics(statistics, TerminationCause.NullIP)

        # JUMP!
        ip = jump_address


def _run_fast(  # noqa: C901
    mem: fjm_reader.Reader, io_device: IODevice, statistics: RunStatistics
) -> TerminationStatistics:
    """
    the fast run-loop. behaves exactly like the featured loop, but the memory accesses and
    the IO/termination checks are inlined into the loop, and the per-op flip/jump counters
    are skipped (op_counter is still maintained). last-ops tracking, when requested, costs
    one predictable branch + one deque-append per op (~10%).
    """
    memory = mem.memory
    w = mem.memory_width
    ww = w.bit_length() - 1  # log2(w)
    dw = 2 * w
    bit_mask = w - 1
    in_addr = 3 * w + w.bit_length()  # 3w + #w
    in_lo = in_addr - dw  # the input bit is in the current op iff in_lo < ip <= in_addr
    out1 = dw + 1  # the output bits are dw, dw+1

    get_word = mem.get_word
    read_missing_word = mem._get_memory_word
    write_bit = mem.write_bit
    io_write_bit = io_device.write_bit
    io_read_bit = io_device.read_bit
    pause_timer = statistics.pause_timer

    last_ops = statistics.last_ops_addresses
    append_last_op = last_ops.append if last_ops is not None else None

    statistics.detailed_statistics = False
    ip = 0
    ops = 0
    try:
        while True:
            if append_last_op is not None:
                append_last_op(ip)

            # read flip word
            bit_offset = ip & bit_mask
            if bit_offset:
                flip_address = get_word(ip)
            else:
                word_address = ip >> ww
                try:
                    flip_address = memory[word_address]
                except KeyError:
                    flip_address = read_missing_word(word_address)

            # handle IO (both checks fail after a single comparison for almost all ops)
            if flip_address <= out1:
                if flip_address >= dw:
                    io_write_bit(out1 == flip_address)
            if ip <= in_addr and ip > in_lo:
                try:
                    with pause_timer:
                        input_bit = io_read_bit()
                except IOReadOnEOF:
                    statistics.op_counter = ops
                    return TerminationStatistics(statistics, TerminationCause.EOF)
                write_bit(in_addr, input_bit)

            # FLIP!
            flip_word_address = flip_address >> ww
            try:
                flip_word_value = memory[flip_word_address]
            except KeyError:
                flip_word_value = read_missing_word(flip_word_address)
            memory[flip_word_address] = flip_word_value ^ (1 << (flip_address & bit_mask))

            # read jump word (after the flip - the flip may modify it)
            if bit_offset:
                jump_address = get_word(ip + w)
            else:
                jump_word_address = (ip >> ww) + 1
                try:
                    jump_address = memory[jump_word_address]
                except KeyError:
                    jump_address = read_missing_word(jump_word_address)
            ops += 1

            # check finish?
            if jump_address == ip and not ip <= flip_address < ip + dw:
                statistics.op_counter = ops
                return TerminationStatistics(statistics, TerminationCause.Looping)
            if jump_address < dw:
                statistics.op_counter = ops
                return TerminationStatistics(statistics, TerminationCause.NullIP)

            # JUMP!
            ip = jump_address
    finally:
        # keep op_counter valid on the exception paths too (memory errors, Ctrl+C)
        statistics.op_counter = ops
