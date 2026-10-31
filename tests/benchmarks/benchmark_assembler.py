"""
The assembler speed benchmark.

Times the assemble pipeline (parse / macro resolve / labels resolve / create binary) on
three workload shapes:
- hello_world.fj: the fixed cost (stl parsing dominates)
- prime_sieve.fj: macro-heavy (deep stl macro expansion)
- lut64k.fj (generated): data-heavy - a 64K-entry byte-LUT program (the mega-data-table
  workload shape)

Usage:
    python tests/benchmark_assembler.py [--profile [PHASE_SUBSTRING]] [--runs N]

--profile runs cProfile over the whole assemble of each workload and prints the top
functions (cumulative), for finding the next optimization target.
"""

import argparse
import cProfile
import pstats
import sys
import tempfile
from pathlib import Path
from time import time

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from flipjump import assemble  # noqa: E402

PROGRAMS_DIR = REPO_ROOT / 'programs'


def generate_lut64k_fj(target_path: Path) -> None:
    """a data-heavy program: a 64K-entry packed-byte data table."""
    lines = [
        'stl.startup',
        "stl.output 'k'",
        'stl.loop',
        '',
        'byte_table:',
    ]
    lines.extend(f';{(k * 0x9E37) & 0xFF} * dw' for k in range(1 << 16))
    newline = chr(10)
    target_path.write_text(newline.join(lines) + newline)


def benchmark(name: str, fj_path: Path, runs: int, profile: bool) -> None:
    durations = []
    for _ in range(runs):
        with tempfile.TemporaryDirectory() as temp_dir:
            fjm_path = Path(temp_dir) / 'out.fjm'
            start_time = time()
            if profile:
                profiler = cProfile.Profile()
                profiler.enable()
            assemble([fj_path], fjm_path, memory_width=64, show_progress=runs == 1)
            if profile:
                profiler.disable()
            durations.append(time() - start_time)
    print(f'{name:16}: {min(durations):7.3f}s  (best of {runs})')
    if profile:
        stats = pstats.Stats(profiler)
        stats.sort_stats('cumulative')
        stats.print_stats(25)


def main() -> None:
    parser = argparse.ArgumentParser(description='FlipJump assembler speed benchmark')
    parser.add_argument('--profile', action='store_true', help='cProfile each workload and print the top functions')
    parser.add_argument('--runs', type=int, default=3, help='runs per workload (the minimum is reported)')
    parser.add_argument(
        '--programs',
        nargs='+',
        default=['hello_world', 'prime_sieve', 'lut64k'],
        choices=['hello_world', 'prime_sieve', 'lut64k'],
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as generated_dir:
        workloads = {
            'hello_world': PROGRAMS_DIR / 'print_tests' / 'hello_world.fj',
            'prime_sieve': PROGRAMS_DIR / 'prime_sieve.fj',
        }
        if 'lut64k' in args.programs:
            lut_path = Path(generated_dir) / 'lut64k.fj'
            generate_lut64k_fj(lut_path)
            workloads['lut64k'] = lut_path

        for name in args.programs:
            benchmark(name, workloads[name], args.runs, args.profile)


if __name__ == '__main__':
    main()
