"""
Post-build smoke + speed-normalization check for a native-engine wheel.

This runs against an *installed* wheel - cibuildwheel's `test-command` points here, so it
executes in the wheel's own target environment (every one of the shipped OS/arch wheels,
including the musllinux containers and the native-arm runners). It verifies three things
that ordinary tests miss:

  1. NATIVE ENGINE ACTIVE (the #1 silent failure) - a wheel can import cleanly yet have a
     broken/absent _fjcore (an ABI/libc/arch mismatch makes the extension unimportable),
     and runs then degrade to the ~200x-slower pure-python loop with NO error. `is_native_
     engine_active()` is the hard, cpu-independent guard for that.

  2. CORRECTNESS on this arch - assemble+run the flat path (loop) and the hybrid/paged path
     (sieve) at w=32 and w=64, and assert the exact, deterministic op-count for each. A
     per-arch codegen bug (the bit-63 magic sentinel, the flat/paged boundary, 32- vs 64-bit
     address arithmetic, endianness) would change an op-count or the termination cause - this
     catches it where it actually happens, on the target arch.

  3. SPEED, cpu-normalized - fj/s is NOT portable across the 8 targets (clock + IPC differ),
     so this does NOT gate on hitting any particular fj/s. It prints fj/s next to the C
     `cpu_calibrate()` yardstick (an L1-resident throughput micro-bench compiled by the SAME
     toolchain and sharing the engine's bottleneck), and fails only under a deliberately
     conservative ABSOLUTE floor and a cpu-normalized fj/C-ratio floor - both set to catch a
     hidden python-fallback or a catastrophic regression, never a merely-slow runner.

Usage:  python tests/wheel_smoke.py
Exit code is non-zero on any hard-check failure; the speed table is always printed.
"""

import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import flipjump
from flipjump import assemble, is_native_engine_active
from flipjump.interpreter import fjm_run
from flipjump.interpreter import _fjcore  # type: ignore[attr-defined]
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.utils.classes import TerminationCause

REPO_ROOT = Path(__file__).resolve().parent.parent

# (program source, run input) - loop is fixed-work (flat path); sieve takes n=5000 on stdin
# (hybrid/paged path). n=5000 keeps w=32 below the ~5792 mark-pointer wrap.
PROGRAMS = {
    'loop': (REPO_ROOT / 'tests' / 'benchmarks' / 'benchmark_loop.fj', b''),
    'sieve': (REPO_ROOT / 'programs' / 'prime_sieve.fj', b'5000\n'),
}

# exact, deterministic op-counts (verified against benchmark_results.md). a miscompile on any
# arch changes these - they are the cross-platform correctness anchor.
GOLDEN_OP_COUNTS = {
    ('loop', 32): 298_927_147,
    ('loop', 64): 351_500_749,
    ('sieve', 32): 16_580_560,
    ('sieve', 64): 33_304_073,
}

# the cpu_calibrate L1 micro-bench shares the engine's bottleneck, so its checksum is fixed for a
# fixed iteration count - same on every arch (pure uint64 arithmetic, endianness-independent). a
# mismatch means the calibration itself miscompiled; pin it.
CALIB_ITERATIONS = 200_000_000
GOLDEN_CALIB_CHECKSUM = 0xF1A951E3CDD861C3

# CONSERVATIVE catastrophe floors - NOT a perf target. the pure-python fallback tops out at
# ~3.9M fj/s, so 8M absolute already separates a hidden fallback from any real native run; and
# the fj/C ratio craters ~200x under a fallback (fj/s collapses while the C yardstick holds), so
# 0.01 catches it on any cpu with large margin while clearing the slowest real case by several x.
MIN_FJ_PER_SEC = 8_000_000
MIN_FJ_TO_C_RATIO = 0.01


def measure_cpu_ops_per_sec() -> float:
    """L1 throughput micro-bench (same toolchain + bottleneck as the engine); fixed iterations so
    the checksum is pinnable across arches, while the elapsed time encodes this cpu's speed."""
    result = _fjcore.cpu_calibrate(CALIB_ITERATIONS)
    assert (
        result['checksum'] == GOLDEN_CALIB_CHECKSUM
    ), f"cpu_calibrate checksum 0x{result['checksum']:016X} != golden (calibration miscompiled?)"
    return float(result['ops']) / float(result['seconds'])


def run_program(name: str, width: int, tmp_dir: Path) -> Tuple[int, float]:
    """assemble+run one program at one width; return (op_count, fj_per_sec). raises on any
    correctness failure (wrong termination cause or wrong op-count)."""
    fj_path, run_input = PROGRAMS[name]
    fjm_path = tmp_dir / f'{name}_w{width}.fjm'
    assemble([fj_path], fjm_path, memory_width=width, print_time=False)

    stats = fjm_run.run(fjm_path, io_device=FixedIO(run_input), print_time=False, last_ops_debugging_list_length=None)

    if stats.termination_cause != TerminationCause.Looping:
        raise AssertionError(f'{name} w={width}: terminated as {stats.termination_cause.name}, expected Looping')
    expected_ops = GOLDEN_OP_COUNTS[(name, width)]
    if stats.op_counter != expected_ops:
        raise AssertionError(
            f'{name} w={width}: op-count {stats.op_counter:,} != expected {expected_ops:,} (miscompile?)'
        )

    return stats.op_counter, stats.op_counter / max(stats.run_time, 1e-9)


def main() -> int:
    print(f'flipjump loaded from: {flipjump.__file__}')
    print(f'python: {sys.version.split()[0]}  platform: {sys.platform}')

    # --- hard check 1: native engine must be active --------------------------------------
    if not is_native_engine_active():
        print(
            'FAIL: native engine is NOT active - this wheel would silently fall back to the '
            'pure-python loop (~200x slower). _fjcore failed to import or is disabled.'
        )
        return 1
    print('OK: native engine active')

    cpu_ops_per_sec = measure_cpu_ops_per_sec()
    print(f'C cpu yardstick (L1 micro-bench): {cpu_ops_per_sec:,.0f} ops/sec\n')

    # --- hard checks 2 & 3: correctness + cpu-normalized speed, per program/width ---------
    failures: List[str] = []
    rows: List[Tuple[str, int, Optional[int], Optional[float], Optional[float], str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for name in PROGRAMS:
            for width in (32, 64):
                try:
                    op_count, fj_per_sec = run_program(name, width, tmp_dir)
                except AssertionError as correctness_error:
                    failures.append(str(correctness_error))
                    rows.append((name, width, None, None, None, 'CORRECTNESS FAIL'))
                    continue

                ratio = fj_per_sec / cpu_ops_per_sec
                status = 'ok'
                if fj_per_sec < MIN_FJ_PER_SEC:
                    failures.append(
                        f'{name} w={width}: {fj_per_sec:,.0f} fj/s < {MIN_FJ_PER_SEC:,} floor '
                        '(hidden python fallback or catastrophic regression?)'
                    )
                    status = 'SPEED FAIL (floor)'
                elif ratio < MIN_FJ_TO_C_RATIO:
                    failures.append(
                        f'{name} w={width}: fj/C ratio {ratio:.4f} < {MIN_FJ_TO_C_RATIO} floor '
                        '(native far slower than the cpu warrants - fallback/regression?)'
                    )
                    status = 'SPEED FAIL (ratio)'
                rows.append((name, width, op_count, fj_per_sec, ratio, status))

    # --- report (always printed) ----------------------------------------------------------
    print(f'{"program":<8} {"w":>3} {"ops":>14} {"fj/s":>16} {"fj/C ratio":>11}  status')
    print('-' * 70)
    for row_name, row_width, row_ops, row_fjs, row_ratio, row_status in rows:
        ops_s = f'{row_ops:,}' if row_ops is not None else '-'
        fjs_s = f'{row_fjs:,.0f}' if row_fjs is not None else '-'
        ratio_s = f'{row_ratio:.4f}' if row_ratio is not None else '-'
        print(f'{row_name:<8} {row_width:>3} {ops_s:>14} {fjs_s:>16} {ratio_s:>11}  {row_status}')
    print()

    if failures:
        print(f'SMOKE FAILED ({len(failures)} hard-check failure(s)):')
        for failure in failures:
            print(f'  - {failure}')
        return 1

    print('SMOKE PASSED: native active, all op-counts exact, speed above the cpu-normalized floors.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
