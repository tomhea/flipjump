"""
shared helpers for the assembler/interpreter unit-tests.

these are thin wrappers over the public flipjump API for assembling small inline .fj
sources (no stl) to a .fjm path, getting a Reader for the result, or running it with a
FixedIO - so the individual test files stay short and focused.
"""

import importlib
import struct
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

import pytest

from flipjump import assemble
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.offset_guard import OffsetGuard
from flipjump.assembler.inner_classes.table_pool import (
    BlockPool,
    DeferredTables,
    KEPT_NO_JUMP_WORD,
    KEPT_RUNTIME_WORD,
    KEPT_TOO_WIDE,
    heat_key,
)
from flipjump.fjm.fjm_consts import FJMVersion, _header_base_size
from flipjump.fjm.fjm_reader import GarbageHandling, Reader
from flipjump.interpreter import fjm_run
from flipjump.interpreter.fjm_run import TerminationStatistics
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.utils.constants import DEFAULT_MAX_MACRO_RECURSION_DEPTH

try:
    _fjcore: Any = importlib.import_module('flipjump.interpreter._fjcore')  # the C extension: no type stubs
except ImportError:
    _fjcore = None

# skip marker for tests that need the compiled native engine
native_engine_required = pytest.mark.skipif(_fjcore is None, reason='the native engine (_fjcore) is not built')


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROGRAMS_DIR = REPO_ROOT / 'programs'
HELLO_NO_STL = PROGRAMS_DIR / 'print_tests' / 'hello_no-stl.fj'
CAT_PROGRAM = PROGRAMS_DIR / 'print_tests' / 'cat.fj'

HELLO_WORLD_OUTPUT: bytes = b'Hello, World!'

# a minimal no-stl preamble (taken from hello_no-stl.fj) that places the IO output-cell at
# address 2w and jumps over it to code_start. it's required before any real op in a no-stl
# program, and exposes the global label IO (for output via "IO + bit;").
MINIMAL_STARTUP = """
def startup @ code_start > IO {
    ;code_start
  IO:
    ;0
  code_start:
}
"""

# a no-stl program that loops forever (a clean Looping termination).
INFINITE_LOOP_PROGRAM = MINIMAL_STARTUP + '\nstartup\nloop:\n;loop\n'


def _sources_list(source: Union[str, List[str]]) -> List[str]:
    return [source] if isinstance(source, str) else source


def assemble_to_path(
    source: Union[str, List[str]],
    tmp_path: Path,
    *,
    memory_width: int = 64,
    use_stl: bool = False,
    fjm_version: FJMVersion = FJMVersion.NormalVersion,
    warning_as_errors: bool = True,
    max_recursion_depth: int = DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    with_debug: bool = False,
) -> Path:
    """
    write the inline .fj source(s) under tmp_path and assemble them into an out.fjm.
    @param source: a single .fj source string, or a list of them (assembled in order).
    @return: the path of the produced .fjm file.
    """
    fj_paths: List[Path] = []
    for index, single_source in enumerate(_sources_list(source)):
        fj_path = tmp_path / f'prog{index}.fj'
        fj_path.write_text(single_source)
        fj_paths.append(fj_path)

    fjm_path = tmp_path / 'out.fjm'
    debug_path = tmp_path / 'debug.fjd' if with_debug else None
    assemble(
        fj_paths,
        fjm_path,
        memory_width=memory_width,
        use_stl=use_stl,
        fjm_version=fjm_version,
        warning_as_errors=warning_as_errors,
        max_recursion_depth=max_recursion_depth,
        debugging_file_path=debug_path,
        show_progress=False,
    )
    return fjm_path


def compile_and_get_reader(
    source: Union[str, List[str]],
    tmp_path: Path,
    *,
    memory_width: int = 64,
    use_stl: bool = False,
    fjm_version: FJMVersion = FJMVersion.NormalVersion,
    garbage_handling: GarbageHandling = GarbageHandling.Stop,
) -> Reader:
    """assemble the inline source and return a Reader of the produced .fjm."""
    fjm_path = assemble_to_path(source, tmp_path, memory_width=memory_width, use_stl=use_stl, fjm_version=fjm_version)
    return Reader(fjm_path, garbage_handling=garbage_handling)


def run_source(
    source: Union[str, List[str]],
    tmp_path: Path,
    fixed_input: bytes = b'',
    *,
    memory_width: int = 64,
    use_stl: bool = False,
) -> Tuple[TerminationStatistics, FixedIO]:
    """assemble the inline source, run it with a FixedIO, and return (statistics, io_device)."""
    fjm_path = assemble_to_path(source, tmp_path, memory_width=memory_width, use_stl=use_stl)
    io_device = FixedIO(fixed_input)
    statistics = fjm_run.run(fjm_path, io_device=io_device, show_progress=False)
    return statistics, io_device


class RecordingIO(FixedIO):
    """
    a FixedIO that logs every device call, with the count of output bits it had received at each input call:
    ('bit', b), ('byte', B), ('read', b, outs), ('rbyte', B, outs).
    """

    def __init__(self, fixed_input: bytes) -> None:
        super().__init__(fixed_input)
        self.events: List[Tuple[Any, ...]] = []
        self.outs = 0

    def read_bit(self) -> bool:
        bit = super().read_bit()
        self.events.append(('read', int(bit), self.outs))
        return bit

    def read_byte(self) -> int:
        value = super().read_byte()
        self.events.append(('rbyte', value, self.outs))
        return value

    def write_bit(self, bit: bool) -> None:
        self.events.append(('bit', int(bit)))
        self.outs += 1
        super().write_bit(bit)

    def write_byte(self, byte: int) -> None:
        self.events.append(('byte', byte))
        self.outs += 8
        super().write_byte(byte)


def overrides(io_device: IODevice, method: str) -> bool:
    """does the device's class override IODevice's `method` - what makes fjm_run hand the engine its byte call"""
    return getattr(type(io_device), method) is not getattr(IODevice, method)


def run_recorded(
    fjm_path: Path, io_device: IODevice, *, native: bool, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> TerminationStatistics:
    """run on the native engine, or on the pure-python fast loop (which calls the device once a bit)."""
    with monkeypatch.context() as patch:
        if native:
            patch.delenv('FLIPJUMP_NO_NATIVE', raising=False)
        else:
            patch.setattr(fjm_run, '_fjcore', None)
        return fjm_run.run(fjm_path, io_device=io_device, show_progress=False, **kwargs)


def relocatable(name: str, params: str, start: str, entries: str) -> str:
    """the declaration of the relocatable table `name.__relocatable__.table params`: `start:` then the entries"""
    return (
        f'ns {name} {{\n    ns __relocatable__ {{\n        def table {params} {{\n          {start}:\n'
        + entries
        + '        }\n    }\n}\n'
    )


# the table pool's controls switch its safety rules off through these public seams only, so the pool's insides can
# change freely: every operand refusal goes through OffsetGuard.refuse, every keep-inline decision through
# OffsetGuard.may_move, every dropped pin through pins.unpin


def no_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    """the control: the table pool's OffsetGuard refuses no operand (its checks run, and let every one pass)"""
    monkeypatch.setattr(OffsetGuard, 'refuse', lambda self, operand, why, where=None: None)


def every_table_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    """the control: the table pool's OffsetGuard keeps no table inline"""
    monkeypatch.setattr(OffsetGuard, 'may_move', lambda self, anchor: None)


def merged_census(pool: BlockPool) -> Dict[str, Dict[int, int]]:
    """the pool's census, {group: {table width: how many}}, with the groups that share a block counted as one"""
    merged: Dict[str, Dict[int, int]] = {}
    for group, census in pool.census.items():
        widths = merged.setdefault(pool.alias.get(group, group), {})
        for width, count in census.widths.items():
            widths[width] = widths.get(width, 0) + count
    return merged


def kept_by_the_guard(pool: BlockPool) -> Dict[str, int]:
    """the tables a pool kept inline because moving them would change an operand, counted by why"""
    other = (KEPT_NO_JUMP_WORD, KEPT_RUNTIME_WORD, KEPT_TOO_WIDE)
    return {why: count for why, count in pool.kept_inline.items() if why not in other}


def pool_stats(pool: BlockPool) -> Dict[str, Any]:
    """what a pool did, counted from what it keeps: its table_sites, blocks, census and kept_inline"""
    raw = pool.reads_words_raw and pool.pin_all_but is None  # its tables went to cheap addresses, in no block
    placed = [site for site in pool.table_sites if site.address]
    in_blocks = len(placed) if raw else sum(sum(block.taken.values()) for block in pool.blocks.values())
    blocked = {group for group, block in pool.blocks.items() if block.base is not None}
    hot = {group: block.hot for group, block in pool.blocks.items() if block.hot is not None}
    matched = width_changed = 0
    for site in placed:
        listed = hot[site.group].ranks.get((site.site, site.occurrence)) if site.group in hot else None
        if listed is not None:
            matched += listed[0] == site.slot_ops
            width_changed += listed[0] != site.slot_ops
    group_keys = {heat_key(group) for group in merged_census(pool)}
    return {
        'allocated': in_blocks,  # tables placed in a block
        'deferred': len(placed),  # tables moved
        'overflowed': len(placed) - in_blocks,  # tables placed in the overflow region
        'ungrouped': pool.kept_inline.get(KEPT_NO_JUMP_WORD, 0),
        'declined': sum(1 for site in pool.table_sites if not site.address and site.group in blocked),
        'groups': len(blocked),
        'broken_groups': sum(block.broken for block in pool.blocks.values()),
        'heat': {
            'hot_groups': len(hot),
            'hot_groups_missing': sum(key not in group_keys for key in pool.heat_unmatched),
            'hot_groups_ambiguous': sum(key in group_keys for key in pool.heat_unmatched),
            'hot_sites_listed': sum(len(group.ranks) for group in hot.values()),
            'hot_sites_matched': matched,
            'hot_sites_width_changed': width_changed,
        },
    }


def labels_off_their_ops(
    monkeypatch: pytest.MonkeyPatch, delta_ops: int, which: Callable[[BlockPool, DeferredTables, Sequence[int]], int]
) -> None:
    """the control: a pool whose ops and labels disagree - the labels of one moved table (`which` picks its index from
    the pool's placement: BlockPool.place_deferred) lie `delta_ops` ops off its ops (DeferredTables.labels_of)"""
    place, labels_of = vars(BlockPool)['place_deferred'], vars(DeferredTables)['labels_of']  # the originals

    def placing(self: BlockPool, tables: DeferredTables, labels: Any, end: int) -> Sequence[int]:
        addresses: Sequence[int] = place(self, tables, labels, end)
        tables.shifted = which(self, tables, addresses)  # type: ignore[attr-defined]
        return addresses

    def shifted(self: DeferredTables, index: int) -> Iterator[Tuple[str, int]]:
        for label, offset in labels_of(self, index):
            yield label, offset + (delta_ops if index == getattr(self, 'shifted', -1) else 0)

    monkeypatch.setattr(BlockPool, 'place_deferred', placing)
    monkeypatch.setattr(DeferredTables, 'labels_of', shifted)


def build_and_run(
    source: str,
    tmp_path: Path,
    pool: Optional[BlockPool],
    name: str = 'p',
    fixed_input: bytes = b'',
    memory_width: int = 32,
) -> Tuple[bytes, int, Path]:
    """assemble `source` with the stl (and the pool, if given), run it on `fixed_input`, and
    return (output, op count, fjm path)."""
    fj_path = tmp_path / f'{name}.fj'
    fj_path.write_text(source)
    fjm_path = tmp_path / f'{name}.fjm'
    assemble([fj_path], fjm_path, memory_width=memory_width, show_progress=False, table_pool=pool)
    io_device = FixedIO(fixed_input)
    statistics = fjm_run.run(fjm_path, io_device=io_device, show_progress=False)
    return io_device.get_output(allow_incomplete_output=True), statistics.op_counter, fjm_path


def sum_expr(*parts: Any) -> Expr:
    """a left-nested `+` tree of label names / ints (as the parser builds `a + b + c`)"""
    expr = Expr(parts[0])
    for part in parts[1:]:
        expr = Expr(('+', (expr, Expr(part))))
    return expr


def high_segment_program(top: int) -> str:
    """a no-stl looping program whose small reserved segment ends 4 words past `top` (its span, at w=32)."""
    return MINIMAL_STARTUP + f'\nstartup\nloop: ;loop\n\nsegment {top} * w\nreserve 4 * w\n'


def patch_run_header(fjm_path: Path, *, run_flags: int = 0, io_mode: int = 0) -> None:
    """rewrite the run flags and the io mode in the .fjm's header."""
    raw = bytearray(fjm_path.read_bytes())
    struct.pack_into('<LL', raw, _header_base_size, run_flags, io_mode)
    fjm_path.write_bytes(bytes(raw))


def spy_engine_runs(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[Optional[int], bool, Optional[str]]]:
    """
    record every fjm_run.run call from now on.
    @return: the list it appends (flat_max_words passed, flat_required passed, the storage mode it ran in) to.
    """
    seen: List[Tuple[Optional[int], bool, Optional[str]]] = []
    real_run = fjm_run.run

    def spy(*args: Any, **kwargs: Any) -> TerminationStatistics:
        statistics = real_run(*args, **kwargs)
        seen.append((kwargs.get('flat_max_words'), kwargs.get('flat_required', False), statistics.storage_mode))
        return statistics

    monkeypatch.setattr(fjm_run, 'run', spy)
    return seen
