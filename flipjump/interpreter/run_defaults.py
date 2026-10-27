"""
how a .fjm runs when its caller doesn't say: the io device and the flat-memory window the program's header
asks for, shared by the `fj` command line and the python API (flipjump_quickstart.run / debug).

The header carries `io_mode` (fjm_consts.IO_MODE_*) and `run_flags` (fjm_consts.RUN_FLAG_*), compiled in with
`fj --asm --io-mode N --run-flags N` or `flipjump_quickstart.assemble(io_mode=, run_flags=)`. An explicit choice
always wins over them: an io device / `--io`, a window / `--flat-max-words`, `--flat-memory`, run flags given now.
"""

import os
import re
from typing import NamedTuple, Optional

from flipjump.fjm.fjm_consts import IO_MODE_NAMES, RUN_FLAG_FLAT_MEMORY, bad_run_flags_message
from flipjump.fjm.fjm_reader import FjmHeader
from flipjump.utils.exceptions import FlipJumpFlatMemoryException

# with no window option (and the flat-memory run flag off), a program spanning at most 2^24 words runs fully flat on
# its own: the array costs at most 64 MB (4-byte cells, w<=32) or 128 MB (w=64), small enough to allocate unasked;
# a larger span keeps the engine's default window
FLAT_MEMORY_AUTO_MAX_WORDS = 1 << 24


def io_mode_name(header: FjmHeader) -> str:
    """the name of the io device the program asks for (an `--io` mode: 'standard' unless it was compiled in)"""
    return IO_MODE_NAMES[header.io_mode]


class FlatWindow(NamedTuple):
    """the native engine's flat window for a run, and what chose it"""

    words: Optional[int]  # None: the engine's own default (FLIPJUMP_FLAT_MAX_WORDS, else 2^23)
    required: bool  # flat memory was asked for: failing to allocate it is an error, not a slow fallback
    source: str  # 'flat_max_words', 'flat_memory', 'env', 'run flag', 'auto', 'default'
    run_flag: str  # who turned the flat-memory run flag on: 'compiled', 'given', or '' (off)


def env_flat_max_words() -> int:
    """FLIPJUMP_FLAT_MAX_WORDS as far as the engine reads it (strtoull's leading number, any base prefix,
    an optional sign): nonzero exactly when the engine honours it; 0 when unset or not a number"""
    match = re.match(r'\s*[+-]?(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)', os.environ.get('FLIPJUMP_FLAT_MAX_WORDS', ''))
    if not match:
        return 0
    digits = match.group(1)
    base = 16 if digits[:2].lower() == '0x' else (8 if digits.startswith('0') else 10)
    return int(digits, base)


def flat_window(
    header: FjmHeader,
    *,
    flat_max_words: Optional[int] = None,
    flat_memory: bool = False,
    run_flags: Optional[int] = None,
) -> FlatWindow:
    """
    the native engine's flat window, in words. In this order of precedence:
      flat_max_words (--flat-max-words N)   - that window;
      flat_memory (--flat-memory)            - the program's span (its highest segment end), required;
      FLIPJUMP_FLAT_MAX_WORDS                - the engine reads it (a window set by the environment);
      the flat-memory run flag               - the span, required: `run_flags` if given (--run-flags), else the
                                               run flags compiled into the .fjm;
      otherwise                              - the span if it is at most FLAT_MEMORY_AUTO_MAX_WORDS, else the
                                               engine's default window.
    """
    if run_flags is not None:
        bad = bad_run_flags_message(run_flags)
        if bad is not None:
            raise ValueError(bad)  # the CLI and the writer reject these values too
    flags = header.run_flags if run_flags is None else run_flags
    run_flag = ''
    if flags & RUN_FLAG_FLAT_MEMORY:
        run_flag = 'compiled' if run_flags is None else 'given'

    if flat_max_words is not None:
        return FlatWindow(int(flat_max_words), False, 'flat_max_words', run_flag)
    span = max(1, header.span_words)
    if flat_memory:
        return FlatWindow(span, True, 'flat_memory', run_flag)
    if env_flat_max_words():
        return FlatWindow(None, False, 'env', run_flag)
    if run_flag:
        return FlatWindow(span, True, 'run flag', run_flag)
    if span <= FLAT_MEMORY_AUTO_MAX_WORDS:
        return FlatWindow(span, False, 'auto', run_flag)
    return FlatWindow(None, False, 'default', run_flag)


def flat_memory_failure_message(flat_error: FlipJumpFlatMemoryException, window: FlatWindow) -> str:
    """what to tell the user when the flat memory a run asked for could not be allocated"""
    causes = []
    if window.source == 'flat_memory':
        causes.append('--flat-memory was given')
    if window.run_flag == 'compiled':
        causes.append("this program's run flags (compiled into the .fjm) turn on flat memory")
    elif window.run_flag == 'given':
        causes.append('--run-flags turns on flat memory')
    fixes = []
    if window.source == 'flat_memory':
        fixes.append('without --flat-memory')
    if window.run_flag:
        fixes.append('with --run-flags 0')
    cause = ', and '.join(causes) or 'flat memory was asked for'
    fix = f"\nRunning it {' and '.join(fixes)} might fix that, at a cost of speed." if fixes else ''
    return (
        f"flipjump: {cause}: the whole program in one array of {flat_error.window_words:,} words, "
        f"and allocating it failed ({flat_error}).{fix}"
    )
