"""
the flipjump interpreter.
executes a compiled .fjm program one flipjump op at a time - managing the memory,
routing input/output through an IODevice, handling breakpoints, detecting termination,
and collecting run statistics (returned as TerminationStatistics).

three run-loops (engines) are implemented:
- the native engine (the default when built): the _fjcore C-extension - segment-aware paged
  memory and the fetch-flipjump loop in C, calling back into python only for IO. ~500x
  faster than the featured loop. disable it with the FLIPJUMP_NO_NATIVE=1 environment variable.
- the fast loop (the default otherwise): pure-python, the memory accesses and IO/termination
  checks are inlined, and the per-op statistics (flip/jump counters) are skipped. ~20x faster.
- the featured loop: supports breakpoints, tracing, and full statistics. selected when
  debugging features are requested, or explicitly with profile=True.
"""

from os import environ
from pathlib import Path
from typing import Any, Optional, Deque

from flipjump.fjm import fjm_reader
from flipjump.fjm.fjm_reader import FjmHeader

try:
    from flipjump.interpreter import _fjcore  # type: ignore[attr-defined]
except ImportError:  # the native engine is optional - fall back to the pure-python loops
    _fjcore = None
from flipjump.interpreter.debugging.breakpoints import BreakpointHandler, handle_breakpoint
from flipjump.utils.classes import TerminationCause, RunStatistics
from flipjump.utils.constants import LAST_OPS_DEBUGGING_LIST_DEFAULT_LENGTH
from flipjump.utils.progress import ProgressBar
from flipjump.utils.exceptions import (
    FlipJumpFlatMemoryException,
    FlipJumpReadFjmException,
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
            else:
                # last-ops tracking is off by default (it slows the engine); on an abnormal
                # finish, point the user at how to get the last executed ops.
                last_ops_str = (
                    '\n\n**** Re-run with --debug-ops-list N (or the -d debug flags) '
                    'to see the last executed ops ****'
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


def _handle_output(flip_address: int, io_device: IODevice, w: int, statistics: RunStatistics) -> None:
    """
    if the ip is in the output-bit range, output the corresponding bit to the io_device.
    the write is timed as paused (like input), so it doesn't inflate the net fj-run-time.
    """
    out_addr = 2 * w
    if out_addr <= flip_address <= out_addr + 1:
        with statistics.pause_timer:
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


def _overrides(io_device: IODevice, method: str) -> bool:
    """does the device handle a whole byte itself in `method` ('write_byte' / 'read_byte'), not IODevice's per-bit
    default? a device that answers bit by bit (the keyboard's read_byte) does not, and the engine asks it bit by bit"""
    own = getattr(type(io_device), method, None)
    return own is not None and own is not getattr(IODevice, method)


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
    flat_required: bool = False,
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
    costs startup time + footprint (4 or 8 bytes a word, w<=32 / w=64), never per-op speed.
    @param flat_required: the flat window must be allocated: if it cannot be, raise
    FlipJumpFlatMemoryException instead of falling back to the slower paged memory (native engine only)
    @return: the run's termination-statistics
    """
    if io_device is None:
        io_device = BrokenIO()

    # Auto-enable when the featured loop runs (tracing / breakpoints / profiling)
    use_featured_loop = profile or show_trace or breakpoint_handler is not None
    if last_ops_debugging_list_length is None and use_featured_loop:
        last_ops_debugging_list_length = LAST_OPS_DEBUGGING_LIST_DEFAULT_LENGTH

    header = fjm_reader.read_fjm_header(fjm_path)
    header.assert_runnable()  # a program must hold its first op at address 0 (both engines)
    statistics = RunStatistics(header.memory_width, last_ops_debugging_list_length)
    # the native engine loads the .fjm's words straight from its (decompressed) bytes: no python object per word
    native = not use_featured_loop and is_native_engine_active()
    try:
        with ProgressBar('  loading memory:  ', print_time=print_time) as progress:
            if native:
                mem = _load_native(fjm_path, header, flat_max_words, flat_required, progress)
            else:
                mem = fjm_reader.Reader(fjm_path)
    except KeyboardInterrupt:
        return TerminationStatistics(statistics, TerminationCause.KeyboardInterrupt)

    try:
        if native:
            return _end_of_run(io_device, _run_native(mem, header.memory_width, io_device, statistics))
        if use_featured_loop:
            io_device.attach_memory(ReaderDeviceMemory(mem))
            return _end_of_run(io_device, _run_featured(mem, io_device, statistics, breakpoint_handler, show_trace))
        io_device.attach_memory(ReaderDeviceMemory(mem))
        return _end_of_run(io_device, _run_fast(mem, io_device, statistics))

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


# the terminations of a run that ended by itself: the device hears that its output is complete (IODevice.end_of_run)
_ENDED_BY_ITSELF = (TerminationCause.Looping, TerminationCause.NullIP, TerminationCause.EOF)


def _end_of_run(io_device: IODevice, termination: TerminationStatistics) -> TerminationStatistics:
    """tell the device the run is over (exactly once, on every engine), when the program ended by itself"""
    if termination.termination_cause in _ENDED_BY_ITSELF:
        io_device.end_of_run()
    return termination


def is_native_engine_active() -> bool:
    """
    is the native (C) engine present and enabled - i.e. will runs use it (for the programs it
    supports) rather than silently falling back to the ~200x-slower pure-python loop?

    a wheel can import cleanly yet have a broken/absent _fjcore (an ABI/libc/arch mismatch made
    the extension unimportable), in which case runs degrade to the python loop with no error at
    all. this predicate is the hard, cpu-independent signal for that: True iff _fjcore imported
    and FLIPJUMP_NO_NATIVE is not set. (whether a *specific* program also takes the native path
    additionally needs the default Stop garbage-handling - which every fjm_run.run uses.)
    """
    return _fjcore is not None and environ.get('FLIPJUMP_NO_NATIVE') != '1'


# the share of the loading bar that decompression takes; loading the words into the engine is the rest
# (decompressing takes about as long as loading the words).
_DECOMPRESS_SHARE = 0.5
# report the loading progress every this many segments
_LOAD_PROGRESS_SEGMENTS = 4096


def _load_native(
    fjm_path: Path, header: FjmHeader, flat_max_words: Optional[int], flat_required: bool, progress: ProgressBar
) -> Any:
    """
    load the .fjm straight into a native _fjcore.Memory: its segments, then its storage decided (so the
    words go directly into the flat image), then the words from the raw data bytes.
    @param header: the .fjm's header (read_fjm_header); the data must be read under the same header
    @return: the loaded core
    """
    assert _fjcore is not None
    fjm_reader.validate_segments(header.segments, data_words=1 << 64)  # the data pool is checked with the data
    core = _fjcore.Memory(
        header.memory_width, flat_max_words=flat_max_words if flat_max_words else 0, flat_required=flat_required
    )
    try:
        for segment_start, segment_length, _data_start, _data_length in header.segments:
            core.add_segment(segment_start, segment_length)
    except ValueError as bad_segment:
        raise FlipJumpReadFjmException(f"Bad .fjm file {fjm_path}: {bad_segment}") from bad_segment
    try:
        progress.clear()  # the engine may warn on stderr (a flat allocation that fell back to paged)
        core.decide_storage()
    except MemoryError as memory_error:
        window = min(header.span_words, flat_max_words) if flat_max_words else header.span_words
        raise FlipJumpFlatMemoryException(str(memory_error), window) from memory_error

    def on_decompress_progress(fraction: float) -> None:
        progress.update(fraction * _DECOMPRESS_SHARE)

    raw_header, data = fjm_reader.read_fjm_raw(fjm_path, on_decompress_progress if progress.active else None)
    if raw_header != header:
        raise FlipJumpReadFjmException(f"{fjm_path} changed while it was being loaded.")

    progress.update(_DECOMPRESS_SHARE)
    relative_jumps = header.relative_jumps
    segments_count = max(1, len(header.segments))
    for index, (segment_start, _segment_length, data_start, data_length) in enumerate(header.segments):
        if data_length:
            core.set_words_bytes(segment_start, data, data_start, data_length, relative_jumps)
        if progress.active and index % _LOAD_PROGRESS_SEGMENTS == 0:
            progress.update(_DECOMPRESS_SHARE + (1 - _DECOMPRESS_SHARE) * index / segments_count)
    progress.update(1.0)
    return core


def _run_native(
    core: Any,
    memory_width: int,
    io_device: IODevice,
    statistics: RunStatistics,
) -> TerminationStatistics:
    """
    run with the native (C) engine: execute the run-loop in C over a loaded _fjcore.Memory (see
    _load_native). behaves exactly like the python fast loop.
    """
    assert _fjcore is not None
    io_device.attach_memory(NativeDeviceMemory(core, memory_width))

    last_ops = statistics.last_ops_addresses
    statistics.detailed_statistics = False
    try:
        cause, op_count, error_bit_address, native_last_ops, paused_seconds = core.run(
            io_device.read_bit,
            io_device.write_bit,
            IOReadOnEOF,
            last_ops_length=last_ops.maxlen if last_ops is not None and last_ops.maxlen else 0,
            # only a device that handles whole bytes itself: the default write_byte is 8 write_bits anyway,
            # and gathering would only delay each bit (and a device error) to its byte's end
            write_byte=io_device.write_byte if _overrides(io_device, 'write_byte') else None,
            read_byte=io_device.read_byte if _overrides(io_device, 'read_byte') else None,
        )
    finally:
        # keep op_counter, the IO-paused time and the storage mode valid on the exception
        # paths too (Ctrl+C, IO-device errors)
        statistics.op_counter = core.last_run_op_count
        statistics.pause_timer.paused_time += core.last_run_paused_seconds
        statistics.storage_mode = core.storage_mode

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
        _handle_output(flip_address, io_device, w, statistics)
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
                    with pause_timer:
                        io_write_bit(out1 == flip_address)
            if in_addr >= ip > in_lo:
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
