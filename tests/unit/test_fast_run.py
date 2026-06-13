"""
unit-tests for the interpreter fast run-loops (flipjump/interpreter/fjm_run.py).

the interpreter has three engines: the featured loop (trace / breakpoints / full statistics,
selected with profile=True), the pure-python fast loop, and the native (C) engine. these
tests pin all engines to identical behavior - same output, same termination cause, same
op-counts - over programs covering every interpreter path: output, input+EOF, unaligned ops,
zeros-boundary segments, runtime memory errors, and each termination cause.
"""

from pathlib import Path
from typing import Optional, Tuple

import pytest

from flipjump.fjm.fjm_consts import FJMVersion
from flipjump.fjm.fjm_writer import Writer
from flipjump.interpreter import fjm_run
from flipjump.interpreter.fjm_run import TerminationStatistics
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.utils.classes import TerminationCause
from tests.unit.unit_utils import (
    CAT_PROGRAM,
    HELLO_NO_STL,
    HELLO_WORLD_OUTPUT,
    INFINITE_LOOP_PROGRAM,
    MINIMAL_STARTUP,
    assemble_to_path,
)

# a no-stl program (w=16) that flips a bit inside a big reserved (zeros-boundary) segment.
# the reserve is 1<<14 bits = 1024 words - above the dense-zeros threshold (1000 words),
# so reads of it go through the interpreter's zeros-boundaries path.
ZEROS_BOUNDARY_PROGRAM = MINIMAL_STARTUP + '''
startup
(1 << 14) + 7;
loop: ;loop

segment (1 << 14)
reserve (1 << 14)
'''


def build_unaligned_fjm(tmp_path: Path) -> Path:
    """
    hand-build a w=16 .fjm whose second op sits at the unaligned bit-address 72.
    op0 (at 0): flip bit 112, jump to 72. op1 (at 72): flip bit 112, jump to 72 (Looping).
    both the flip-word and jump-word of op1 are read with a non-zero bit offset.
    """
    fjm_path = tmp_path / 'unaligned.fjm'
    writer = Writer(fjm_path, 16, FJMVersion.NormalVersion)
    # w0..w7; op1's flip-word (=112) is encoded in bits 72..87 (w4-w5), its jump-word (=72)
    # in bits 88..103 (w5-w6). bit 112 (w7) is a scratch bit outside both ops.
    writer.add_simple_segment_with_data(0, [112, 72, 0, 0, 0x7000, 0x4800, 0, 0])
    writer.write_to_file()
    return fjm_path


def run_both_loops(
    fjm_path: Path, fixed_input: bytes
) -> Tuple[TerminationStatistics, TerminationStatistics, bytes, bytes]:
    """run the .fjm with the engine under test (the default loop) and the featured loop."""
    fast_io, featured_io = FixedIO(fixed_input), FixedIO(fixed_input)
    fast_stats = fjm_run.run(fjm_path, io_device=fast_io, print_time=False)
    featured_stats = fjm_run.run(fjm_path, io_device=featured_io, print_time=False, profile=True)
    return (
        fast_stats,
        featured_stats,
        fast_io.get_output(allow_incomplete_output=True),
        featured_io.get_output(allow_incomplete_output=True),
    )


@pytest.mark.parametrize(
    'program_id,fixed_input,expected_termination_cause,expected_output',
    [
        ('hello', b'', TerminationCause.Looping, HELLO_WORLD_OUTPUT),
        ('infinite_loop', b'', TerminationCause.Looping, b''),
        ('cat', b'fast-loop!', TerminationCause.EOF, b'fast-loop!'),
        ('null_ip', b'', TerminationCause.NullIP, b''),
        ('unaligned', b'', TerminationCause.Looping, b''),
        ('zeros_boundary', b'', TerminationCause.Looping, b''),
        ('memory_error', b'', TerminationCause.RuntimeMemoryError, b''),
    ],
)
def test_fast_and_featured_loops_are_equivalent(
    tmp_path: Path,
    engine: str,
    program_id: str,
    fixed_input: bytes,
    expected_termination_cause: TerminationCause,
    expected_output: bytes,
) -> None:
    fjm_path = _build_program(program_id, tmp_path)

    fast_stats, featured_stats, fast_output, featured_output = run_both_loops(fjm_path, fixed_input)

    assert fast_stats.termination_cause == expected_termination_cause
    assert featured_stats.termination_cause == expected_termination_cause
    assert fast_output == expected_output
    assert featured_output == expected_output
    assert fast_stats.op_counter == featured_stats.op_counter
    assert fast_stats.memory_error_address == featured_stats.memory_error_address


def _build_program(program_id: str, tmp_path: Path) -> Path:
    if program_id == 'hello':
        return assemble_to_path(HELLO_NO_STL.read_text(), tmp_path)
    if program_id == 'infinite_loop':
        return assemble_to_path(INFINITE_LOOP_PROGRAM, tmp_path)
    if program_id == 'cat':
        return assemble_to_path(CAT_PROGRAM.read_text(), tmp_path, use_stl=True)
    if program_id == 'null_ip':
        return assemble_to_path(MINIMAL_STARTUP + '\nstartup\n;0\n', tmp_path)
    if program_id == 'unaligned':
        return build_unaligned_fjm(tmp_path)
    if program_id == 'zeros_boundary':
        return assemble_to_path(ZEROS_BOUNDARY_PROGRAM, tmp_path, memory_width=16)
    if program_id == 'memory_error':
        return assemble_to_path(MINIMAL_STARTUP + '\nstartup\n;0x4000\n', tmp_path, memory_width=16)
    raise ValueError(program_id)


def test_unaligned_op_runs_correctly(tmp_path: Path, engine: str) -> None:
    fjm_path = build_unaligned_fjm(tmp_path)
    statistics = fjm_run.run(fjm_path, io_device=FixedIO(b''), print_time=False)
    assert statistics.termination_cause == TerminationCause.Looping
    assert statistics.op_counter == 2


def test_fast_loop_is_the_default_and_skips_detailed_counters(tmp_path: Path) -> None:
    fjm_path = assemble_to_path(HELLO_NO_STL.read_text(), tmp_path)

    fast_stats = fjm_run.run(fjm_path, io_device=FixedIO(b''), print_time=False)
    assert fast_stats.op_counter > 0
    assert fast_stats.flip_counter == 0
    assert fast_stats.jump_counter == 0

    featured_stats = fjm_run.run(fjm_path, io_device=FixedIO(b''), print_time=False, profile=True)
    assert featured_stats.flip_counter > 0
    assert featured_stats.jump_counter > 0


def test_fast_loop_tracks_last_ops_when_requested(tmp_path: Path, engine: str) -> None:
    fjm_path = assemble_to_path(INFINITE_LOOP_PROGRAM, tmp_path)

    statistics = fjm_run.run(fjm_path, io_device=FixedIO(b''), print_time=False, last_ops_debugging_list_length=3)
    assert statistics.last_ops_addresses is not None
    assert 0 < len(statistics.last_ops_addresses) <= 3

    statistics_none = fjm_run.run(fjm_path, io_device=FixedIO(b''), print_time=False)
    assert statistics_none.last_ops_addresses is None


def test_is_native_engine_active_tracks_engine_state(engine: str) -> None:
    # the public predicate is the wheel-smoke guard against a silent python fallback. the
    # `engine` fixture sets _fjcore=None for the python loops and clears FLIPJUMP_NO_NATIVE for
    # native, so the predicate must report active iff the native engine would actually be used.
    assert fjm_run.is_native_engine_active() == (engine == 'native')


def test_no_native_env_var_disables_native_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    if fjm_run._fjcore is None:  # type: ignore[attr-defined]
        pytest.skip('the native engine (_fjcore) is not built')
    monkeypatch.setenv('FLIPJUMP_NO_NATIVE', '1')
    assert fjm_run.is_native_engine_active() is False
    monkeypatch.delenv('FLIPJUMP_NO_NATIVE', raising=False)
    assert fjm_run.is_native_engine_active() is True


@pytest.mark.parametrize('last_ops_length', [None, 10])
def test_eof_op_counting_matches_featured_loop(tmp_path: Path, engine: str, last_ops_length: Optional[int]) -> None:
    # the op that reads past the input-end is not counted - in either loop.
    fjm_path = assemble_to_path(CAT_PROGRAM.read_text(), tmp_path, use_stl=True)

    fast_stats = fjm_run.run(
        fjm_path, io_device=FixedIO(b'A'), print_time=False, last_ops_debugging_list_length=last_ops_length
    )
    featured_stats = fjm_run.run(
        fjm_path,
        io_device=FixedIO(b'A'),
        print_time=False,
        profile=True,
        last_ops_debugging_list_length=last_ops_length,
    )

    assert fast_stats.termination_cause == TerminationCause.EOF
    assert featured_stats.termination_cause == TerminationCause.EOF
    assert fast_stats.op_counter == featured_stats.op_counter
