"""
shared helper classes.
the TerminationCause enum (why a run ended), the PrintTimer context-manager for timing and
printing code-stage durations, and RunStatistics for collecting execution metrics during a
run.
"""

from __future__ import annotations

from collections import deque
from enum import IntEnum
from time import time
from typing import Optional, Deque


class TerminationCause(IntEnum):
    # Finished by jumping to the last op, without flipping it (the "regular" finish/exit)
    Looping = 0
    # Finished by reading input when there is no more input
    EOF = 1
    # Finished by jumping back to the initial op 0 (bad finish)
    NullIP = 2
    # FOR FUTURE SUPPORT - tried to access an unaligned word (bad finish)
    UnalignedWord = 3
    # FOR FUTURE SUPPORT - tried to access a dword-unaligned op (bad finish)
    UnalignedOp = 4
    # Finished by trying to read/write something out of the defined memory (probably a bug in the fj-program)
    RuntimeMemoryError = 5
    # Finished by keyboard interrupt from the user
    KeyboardInterrupt = 6

    def __str__(self) -> str:
        return [
            'looping',
            'EOF',
            'ip<2w',
            'unaligned-word',
            'unaligned-op',
            'runtime-memory-error',
            "keyboard-interrupt",
        ][self.value]


class PrintTimer:
    """
    prints the time a code segment took.
    usage:
    with PrintTimer('long_function time: '):
        long_function()
    """

    def __init__(self, init_message: str, *, print_time: bool = True):
        self.init_message = init_message
        self.print_time = print_time

    def __enter__(self) -> None:
        if self.print_time:
            self.start_time = time()
            print(self.init_message, end='', flush=True)

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        if self.print_time:
            print(f'{time() - self.start_time:.3f}s')


class RunStatistics:
    """
    maintains times and counters of the current run.
    """

    class PauseTimer:
        def __init__(self) -> None:
            self.paused_time: float = 0.0

        def __enter__(self) -> None:
            self.pause_start_time = time()

        def __exit__(self, exc_type, exc_val, exc_tb):  # type: ignore[no-untyped-def]
            self.paused_time += time() - self.pause_start_time

    def __init__(self, memory_width: int, last_ops_debugging_list_length: Optional[int]):
        """
        Saves statistics about the current run (and a queue of the last executed ops).
        @param memory_width: the memory bit-length
        @param last_ops_debugging_list_length: The length of the last-ops list
        """
        self._op_size = 2 * memory_width
        self._after_null_flip = 2 * memory_width

        self.op_counter = 0
        self.flip_counter = 0
        self.jump_counter = 0
        # False when the run-loop skips the flip/jump counters (the fast loop).
        self.detailed_statistics = True
        # 'flat'/'hybrid'/'paged' when the native engine ran (which memory storage it chose); None otherwise.
        self.storage_mode: Optional[str] = None

        self.last_ops_addresses: Optional[Deque[int]] = None
        if last_ops_debugging_list_length is not None:
            self.last_ops_addresses = deque(maxlen=last_ops_debugging_list_length)

        self._start_time = time()
        self.pause_timer = self.PauseTimer()

    def get_run_time(self) -> float:
        return time() - self._start_time - float(self.pause_timer.paused_time)

    def register_op_address(self, ip: int) -> None:
        if self.last_ops_addresses is not None:
            self.last_ops_addresses.append(ip)

    def register_op(self, ip: int, flip_address: int, jump_address: int) -> None:
        self.op_counter += 1
        if flip_address >= self._after_null_flip:
            self.flip_counter += 1
        if jump_address != ip + self._op_size:
            self.jump_counter += 1
