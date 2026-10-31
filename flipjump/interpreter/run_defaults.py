"""
how a .fjm runs when its caller doesn't say: the io device and the flat-memory window the program's header
asks for, shared by the `fj` command line and the python API (flipjump_quickstart.run / debug).

The header carries `io_mode` (fjm_consts.IO_MODE_*) and whether to run in flat memory (fjm_consts.RUN_FLAG_FLAT_MEMORY
in its run flags), compiled in with `fj --asm --io-mode MODE --flat-memory` or
`flipjump_quickstart.assemble(io_mode=, flat_memory=)`. An explicit choice always wins over them: an io device /
`--io`, a window / `--flat-max-words`, `--flat-memory` / `--no-flat-memory` given at run time.
"""

import os
import re
from typing import NamedTuple, Optional

from flipjump.fjm.fjm_consts import RUN_FLAG_FLAT_MEMORY
from flipjump.fjm.fjm_reader import FjmHeader
from flipjump.utils.exceptions import FlipJumpFlatMemoryException

# with no window option (and no flat memory asked for), a program spanning at most 2^24 words runs fully flat on
# its own: the array costs at most 64 MB (4-byte cells, w<=32) or 128 MB (w=64), small enough to allocate unasked;
# a larger span keeps the engine's default window
FLAT_MEMORY_AUTO_MAX_WORDS = 1 << 24


class FlatWindow(NamedTuple):
    """the native engine's flat window for a run, and what chose it"""

    words: Optional[int]  # None: the engine's own default (FLIPJUMP_FLAT_MAX_WORDS, else 2^23)
    required: bool  # flat memory was asked for: failing to allocate it is an error, not a slow fallback
    source: str  # 'flat_max_words', 'flat_memory' (given at run time), 'env', 'fjm' (compiled in), 'auto', 'default'


def env_flat_max_words() -> int:
    """FLIPJUMP_FLAT_MAX_WORDS as far as the engine reads it (strtoull's leading number, any base prefix,
    an optional sign): nonzero exactly when the engine honours it; 0 when unset or not a number"""
    match = re.match(r'\s*[+-]?(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)', os.environ.get('FLIPJUMP_FLAT_MAX_WORDS', ''))
    digits = match.group(1) if match else '0'
    return int(digits, 16 if digits[:2].lower() == '0x' else 8 if digits[0] == '0' else 10)


def flat_window(
    header: FjmHeader,
    *,
    flat_memory: Optional[bool] = None,
    flat_max_words: Optional[int] = None,
) -> FlatWindow:
    """
    the native engine's flat window, in words. In this order of precedence:
      flat_max_words (--flat-max-words N)   - that window;
      flat_memory=True (--flat-memory)       - the program's span (its highest segment end), required;
      FLIPJUMP_FLAT_MAX_WORDS                - the engine reads it (a window set by the environment);
      the .fjm asks for flat memory          - the span, required; unless flat_memory=False (--no-flat-memory);
      otherwise                              - the span if it is at most FLAT_MEMORY_AUTO_MAX_WORDS, else the
                                               engine's default window.
    @param flat_memory: None - as the .fjm says; True / False - run in flat memory, or not, whatever it says
    @raises ValueError: both flat_memory=True and flat_max_words, two windows
    """
    if flat_max_words is not None:
        if flat_memory:
            raise ValueError('flat_memory=True and flat_max_words each choose the flat window: give one of them')
        return FlatWindow(int(flat_max_words), False, 'flat_max_words')
    span = max(1, header.span_words)
    if flat_memory:
        return FlatWindow(span, True, 'flat_memory')
    if env_flat_max_words():
        return FlatWindow(None, False, 'env')
    if flat_memory is None and header.run_flags & RUN_FLAG_FLAT_MEMORY:
        return FlatWindow(span, True, 'fjm')
    if span <= FLAT_MEMORY_AUTO_MAX_WORDS:
        return FlatWindow(span, False, 'auto')
    return FlatWindow(None, False, 'default')


def flat_memory_failure_message(flat_error: FlipJumpFlatMemoryException, window: FlatWindow) -> str:
    """what to tell the user when the flat memory a run asked for could not be allocated"""
    cause = {
        'flat_memory': '--flat-memory was given',
        'fjm': 'this program asks for flat memory (compiled into the .fjm with --flat-memory)',
    }.get(window.source, 'flat memory was asked for')
    fix = '\nRunning it with --no-flat-memory might fix that, at a cost of speed.' if window.required else ''
    return (
        f"flipjump: {cause}: the whole program in one array of {flat_error.window_words:,} words, "
        f"and allocating it failed ({flat_error}).{fix}"
    )
