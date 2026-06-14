"""
The interpreter speed benchmark (fj/s) - prime_sieve.fj at w=32 and w=64.

Used to track the interpreter speedup work:
every interpreter change is measured against the recorded baseline in
tests/benchmark_results.md.

Usage:
    python tests/benchmark_interpreter.py [n=10000] [--w 32 64] [--fast]

The compiled .fjm files are cached under tests/compiled/benchmark/ (keyed by width),
so only the first invocation pays the assemble time.
"""

import argparse
import sys
from pathlib import Path
from time import time

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from flipjump import assemble  # noqa: E402
from flipjump.interpreter import fjm_run  # noqa: E402
from flipjump.interpreter.io_devices.FixedIO import FixedIO  # noqa: E402
from flipjump.utils.classes import TerminationCause  # noqa: E402

PROGRAMS = {
    'sieve': REPO_ROOT / 'programs' / 'prime_sieve.fj',
    'loop': Path(__file__).parent / 'benchmark_loop.fj',
}
COMPILED_DIR = REPO_ROOT / 'tests' / 'compiled' / 'benchmark'


def get_benchmark_fjm(program: str, memory_width: int) -> Path:
    """assemble the benchmark program at the given width (cached on disk)."""
    fj_path = PROGRAMS[program]
    COMPILED_DIR.mkdir(parents=True, exist_ok=True)
    fjm_path = COMPILED_DIR / f'{program}_w{memory_width}.fjm'
    if not fjm_path.exists() or fjm_path.stat().st_mtime < fj_path.stat().st_mtime:
        print(f'assembling {fj_path.name} at w={memory_width}...')
        assemble([fj_path], fjm_path, memory_width=memory_width, print_time=False)
    return fjm_path


def benchmark(program: str, memory_width: int, n: int) -> None:
    fjm_path = get_benchmark_fjm(program, memory_width)
    io_device = FixedIO(f'{n}\n'.encode() if program == 'sieve' else b'')

    start_time = time()
    termination_statistics = fjm_run.run(fjm_path, io_device=io_device)
    wall_time = time() - start_time

    assert (
        termination_statistics.termination_cause == TerminationCause.Looping
    ), f'benchmark run failed: {termination_statistics.termination_cause}'

    ops = termination_statistics.op_counter
    run_time = termination_statistics.run_time
    print(f'w={memory_width}: {ops:,} ops in {run_time:.2f}s (wall {wall_time:.2f}s) ' f'=> {ops / run_time:,.0f} fj/s')


def main() -> None:
    # note: at w=32, prime_sieve's mark-pointer (PRIMES_MEMORY_START + p*p*dw) wraps the
    # 2^32-bit address space for n > ~5792, ending the run with a runtime-memory-error.
    # keep n <= 5000 when benchmarking w=32.
    parser = argparse.ArgumentParser(description='FlipJump interpreter speed benchmark')
    parser.add_argument('n', type=int, nargs='?', default=5000, help='sieve upper bound (default 5000)')
    parser.add_argument('--w', type=int, nargs='+', default=[32, 64], help='memory widths to benchmark')
    parser.add_argument(
        '--program',
        choices=sorted(PROGRAMS),
        default='sieve',
        help="the benchmark program: 'sieve' (sparse, half-address-space segment - the paged"
        " path) or 'loop' (compact memory - the flat-storage path)",
    )
    args = parser.parse_args()

    for memory_width in args.w:
        benchmark(args.program, memory_width, args.n)


if __name__ == '__main__':
    main()
