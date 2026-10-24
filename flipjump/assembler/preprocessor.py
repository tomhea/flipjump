"""
the preprocessor (macro-resolution stage).
expands macro calls and rep (repetition) blocks recursively, starting from the main
macro, and produces the flat op-list that the assembler then resolves into addresses.
"""

from __future__ import annotations

import collections
import sys
from typing import Any, Dict, Tuple, Iterable, Union, Deque, Set, List, Optional, NoReturn, Callable, Sequence

from flipjump.interpreter.debugging.macro_usage_graph import show_macro_usage_pie_graph
from flipjump.utils.constants import (
    MACRO_SEPARATOR_STRING,
    STARTING_LABEL_IN_MACROS_STRING,
    DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    GAP_BETWEEN_PYTHONS_AND_PREPROCESSOR_MACRO_RECURSION_DEPTH,
)
from flipjump.utils.exceptions import FlipJumpPreprocessorException, FlipJumpExprException
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.ops import (
    FlipJump,
    WordFlip,
    Label,
    Segment,
    Reserve,
    MacroCall,
    RepCall,
    CodePosition,
    Macro,
    LastPhaseOp,
    MacroName,
    NewSegment,
    ReserveBits,
    Pad,
    Padding,
    Op,
    INITIAL_MACRO_NAME,
    INITIAL_ARGS,
    INITIAL_LABELS_PREFIX,
)
from flipjump.assembler.inner_classes.op_stream import OpStream
from flipjump.assembler.table_pool import (  # noqa: F401 - the pool is importable from here too
    BlockCensus,
    BlockPool,
    DECLINE_OTHER,
    DECLINE_STALE,
    DEFER_INLINE,
    DEFER_NEXT_ADDRESS,
    DEFER_OUT,
    DEFER_TOO_WIDE,
    DeferredTables,
    RAW_READERS,
    RESERVED_BELOW,
    TablePool,
    heat_key,
    merge_aliased_groups,
)

CurrTree = Deque[Union[MacroCall, RepCall]]
OpsQueue = OpStream  # the preprocessor's output
LabelsDict = Dict[str, int]

wflip_start_label = '_.wflip_area_start_'


def table_names_next_address(ops: Sequence[Op], pad_index: int, end_index: int) -> bool:
    """does an entry of the table after `ops[pad_index]` (through `ops[end_index]`) use `$`, the
    address of the op after it? Its value is computed while the macro expands, so such a table must
    have its address by then. (A jump to `$` already keeps relocatable_table_end from accepting it;
    this is about a FLIP of an address computed from `$`.)"""
    return any(
        isinstance(op, FlipJump) and '$' in op.flip.all_unknown_labels()
        for op in ops[pad_index + 1 : end_index + 1]  # noqa: E203
    )


def macro_resolve_error(
    curr_tree: CurrTree, msg: str = '', *, orig_exception: Optional[BaseException] = None
) -> NoReturn:
    """
    raise a descriptive error (with the macro-expansion trace).
    @param curr_tree: the ops in the macro-calling path to arrive in this macro
    @param msg: the message to show on error
    @param orig_exception: if not None, raise from this base error.
    """
    error_str = "Macro Resolve Error" + (f':\n  {msg}\n' if msg else '.\n')
    if curr_tree:
        error_str += 'Macro call trace:\n'
        for i, op in enumerate(curr_tree):
            error_str += f'  {i}) {op.trace_str()}\n'
    raise FlipJumpPreprocessorException(error_str) from orig_exception


_NOT_CACHED: Any = object()

# a macro-expansion's progress slice thinner than this is not tracked any deeper (see PreprocessorData)
_PROGRESS_MIN_SLICE = 1e-5
# ...and progress is reported only when it moved at least this much (one call per macro call is too many)
_PROGRESS_MIN_STEP = 1e-4


class PreprocessorData:
    """
    maintains the preprocessor "global" data structures, throughout its recursion.
     e.g. current address, resulting ops, labels' dictionary, macros' dictionary...
    also offer many functions to manipulate its data.
    @note should call finish before get_result...().
    """

    class _PrepareMacroCall:
        __slots__ = ('curr_tree', 'calling_op', 'macros', 'max_recursion_depth')

        def __init__(
            self,
            curr_tree: CurrTree,
            calling_op: Union[MacroCall, RepCall],
            macros: Dict[MacroName, Macro],
            max_recursion_depth: int,
        ):
            """
            Validates that the called macro exists, and that the macro depth is ok. Updates the curr_tree variable.
            @param curr_tree: the ops in the macro-calling path to arrive in this macro (not including the calling op)
            @param calling_op: the current macro call (either of type Macro or RepCall).
            @param macros: parser's result; the dictionary from the macro names to the macro declaration
            @param max_recursion_depth: The compiler supports macros that recursively uses other macros,
            up to the specified recursion depth.
            """
            self.curr_tree = curr_tree
            self.calling_op = calling_op
            self.macros = macros
            self.max_recursion_depth = max_recursion_depth

        def __enter__(self) -> None:
            macro_name = self.calling_op.macro_name
            if macro_name not in self.macros:
                macro_resolve_error(
                    self.curr_tree,
                    f"macro {macro_name} is used but isn't defined. " f"In {self.calling_op.code_position}.",
                )
            self.curr_tree.append(self.calling_op)
            if len(self.curr_tree) > self.max_recursion_depth:
                macro_resolve_error(
                    self.curr_tree,
                    "The maximal macro-expansion recursive depth was reached. "
                    "change the max_recursion_depth variable.",
                )

        def __exit__(self, exc_type, exc_val, exc_tb):  # type: ignore[no-untyped-def]
            self.curr_tree.pop()

    def __init__(
        self,
        memory_width: int,
        macros: Dict[MacroName, Macro],
        max_recursion_depth: int,
        *,
        save_debug_labels: bool = True,
        table_pool: Optional[TablePool] = None,
    ):
        """
        @param memory_width: the memory-width
        @param macros: parser's result; the dictionary from the macro names to the macro declaration
        @param max_recursion_depth: The compiler supports macros that recursively uses other macros,
        up to the specified recursion depth.
        @param save_debug_labels: whether to record the per-expansion `...---:start:` macro-start
        labels. See insert_macro_start_label for why they can be skipped.
        @param table_pool: if given, relocate the lookup tables it wants (see TablePool)
        """
        self.memory_width = memory_width
        self.macros = macros
        self.save_debug_labels = save_debug_labels
        self.consume_main = False  # see resolve_macros

        self.curr_address: int = 0

        self.macro_code_size: Dict[str, int] = collections.defaultdict(lambda: 0)

        self.curr_tree: CurrTree = collections.deque()

        self.curr_segment_index: int = 0
        # where each label is declared - read only to word a "declared twice" error. MEMORY: a list
        # aligned with the labels dict's insertion order (labels are never removed), 8 bytes a label,
        # not a second dict of millions of string keys; a one-pass deferred table's labels, which reach
        # the labels dict only when the expansion ends, wait in their own small dict until then.
        self._label_positions: List[Optional[CodePosition]] = []
        self._deferred_label_positions: Dict[str, CodePosition] = {}

        self.labels: Dict[str, int] = {}
        # the emitted ops, compactly (see OpStream). Every stream of this preprocessor shares one refs list.
        self._op_refs: List[Any] = []
        self.result_ops: OpStream = OpStream(self._op_refs, self.labels)
        self.addresses_with_labels: Set[int] = set()
        self.macro_start_labels: List[Tuple[int, str, CodePosition]] = []  # (address, label, code_position)

        first_segment: NewSegment = NewSegment(0)
        self.last_new_segment: NewSegment = first_segment
        self.result_ops.append(first_segment)

        # TABLE PLACEMENT (off unless a pool is given). Relocated tables are emitted into a
        # SEPARATE stream and flushed after the main one in finish(), so the main stream stays a
        # single ascending segment and the pool's runs become segments of their own.
        self.table_pool = table_pool
        self.pool_ops: OpStream = OpStream(self._op_refs, self.labels)
        self.emit_target: OpStream = self.result_ops
        self._reloc_stack: List[Tuple[int, OpStream]] = []
        self._pool_segment: Optional[NewSegment] = None

        # ONE-PASS BLOCKING (off unless the pool is a BlockPool(one_pass=True)): a table the pool
        # takes is emitted into the pool's stream behind a NewSegment with no address yet, and waits
        # in `_deferred` until finish() has the pool place them all
        self._one_pass: Optional[BlockPool] = (
            table_pool if isinstance(table_pool, BlockPool) and table_pool.one_pass else None
        )
        self._deferred: Optional[DeferredTables] = (
            None if self._one_pass is None else DeferredTables(self._one_pass.keeps_site_paths)
        )
        self._deferring = False  # emitting a deferred table's ops
        self._next_address_cache: Dict[Tuple[int, int], bool] = {}
        self._table_end_cache: Dict[Tuple[int, int], Optional[Tuple[int, int, Optional[Expr]]]] = {}

        # PROGRESS (off unless on_progress is given): each macro expansion owns a slice [lo, hi) of
        # the [0, 1) progress line, split evenly among its ops; a macro call / rep iteration takes its
        # op's sub-slice. Only CALLS look at it -- a plain op pays nothing -- and a slice thinner than
        # _PROGRESS_MIN_SLICE is not tracked (None) below it, so the bookkeeping stays tiny.
        self.on_progress: Optional[Callable[[float], None]] = None
        self.progress_slice: Optional[Tuple[float, float]] = None
        self._progress_reported = -1.0

        self.max_recursion_depth = max_recursion_depth
        # set python's recursion-limit so the preprocessor's own depth-check (at max_recursion_depth)
        #  triggers before python's RecursionError. Set it unconditionally - whether it's higher or lower than the
        #  current limit - so that raising max_recursion_depth actually allows deeper macro-recursion.
        sys.setrecursionlimit(max_recursion_depth + GAP_BETWEEN_PYTHONS_AND_PREPROCESSOR_MACRO_RECURSION_DEPTH)

    def enter_progress_slice(
        self, macro_slice: Tuple[float, float], op_index: int, ops_count: int, part: int, parts: int
    ) -> None:
        """a call at op `op_index` of a macro owning `macro_slice` (its `part`-th of `parts` rep
        iterations): report where it starts, and make its sub-slice the callee's"""
        lo, hi = macro_slice
        op_width = (hi - lo) / ops_count
        width = op_width / parts
        start = lo + op_width * op_index + width * part
        if start - self._progress_reported >= _PROGRESS_MIN_STEP:
            self._progress_reported = start
            assert self.on_progress is not None
            self.on_progress(start)
        self.progress_slice = (start, start + width) if width >= _PROGRESS_MIN_SLICE else None

    def patch_last_wflip_address(self) -> None:
        self.last_new_segment.wflip_start_address = self.curr_address

    def emit(self, op: LastPhaseOp) -> None:
        """append to whichever stream is being built -- the main one, or the table pool"""
        self.emit_target.append(op)

    def begin_relocation(
        self,
        macro_name: MacroName,
        ops_alignment: int,
        table_ops: int,
        labels_prefix: str = '',
        group: Optional[str] = None,
        group_expr: Optional[Expr] = None,
        table_source: Optional[Tuple[Macro, int, int]] = None,
    ) -> bool:
        """Move emission to a pool address instead of aligning in place.

        A table is reached only by jump, so relocating it changes nothing but its address -- and
        the address is what the arming wflip pays for. Returns False when there is no pool, the
        pool does not want this macro, or the pool is full; the caller then pads normally.

        `table_source` is (the macro, the pad's index, the table's last op index): a one-pass pool
        needs it to see whether an entry uses `$` (see _begin_deferral).
        """
        pool = self.table_pool
        if pool is None or not pool.wants(macro_name, labels_prefix):
            return False
        if group_expr is not None and self._is_runtime_word(group_expr):
            pool.reserved_words += 1
            return False
        if self._one_pass is not None:
            return self._begin_deferral(
                self._one_pass, ops_alignment, table_ops, labels_prefix, group, group_expr, table_source
            )
        reserved = pool.reserve(ops_alignment, table_ops, group, group_expr, labels_prefix)
        if reserved is None:
            return False
        address, starts_new_run, gap_ops = reserved
        self._reloc_stack.append((self.curr_address, self.emit_target))
        self.emit_target = self.pool_ops
        if starts_new_run:
            segment = NewSegment(address)
            self.pool_ops.append(segment)
            self._pool_segment = segment
        elif gap_ops:
            # alignment skipped inside this run -- the segment writes its words consecutively, so
            # the gap has to exist in the op stream too
            self.pool_ops.append(Padding(gap_ops))
        self.curr_address = address
        return True

    def _is_runtime_word(self, word: Expr) -> bool:
        """is `word` one of the runtime's (see RESERVED_BELOW)? Those are declared before any code
        dispatches through them, so they resolve here; a program variable, declared after the
        code, does not yet."""
        try:
            return word.exact_eval(self.labels) < RESERVED_BELOW
        except FlipJumpExprException:
            return False

    def table_end(self, macro: Macro, pad_index: int) -> Optional[Tuple[int, int, Optional[Expr]]]:
        """relocatable_table_end(macro.ops, pad_index), cached per macro and pad: it walks the table
        (building a label set per op), and a macro's ops never change while it expands. (The main
        macro is not cached: each of its pads is reached once, and consume_main drops its ops.)"""
        if macro is self.macros.get(INITIAL_MACRO_NAME):
            return relocatable_table_end(macro.ops, pad_index)
        key = (id(macro), pad_index)  # a macro lives as long as the expansion: its id is stable
        found = self._table_end_cache.get(key, _NOT_CACHED)
        if found is _NOT_CACHED:
            found = self._table_end_cache[key] = relocatable_table_end(macro.ops, pad_index)
        return found  # type: ignore[no-any-return]

    def _names_next_address(self, table_source: Optional[Tuple[Macro, int, int]]) -> bool:
        """does the table use `$` (table_names_next_address)? Cached per macro and pad. A caller that
        gives no source is assumed to (and its table stays inline)."""
        if table_source is None:
            return True
        macro, pad_index, end_index = table_source
        key = (id(macro), pad_index)  # a macro lives as long as the expansion: its id is stable
        names = self._next_address_cache.get(key)
        if names is None:
            names = table_names_next_address(macro.ops, pad_index, end_index)
            self._next_address_cache[key] = names
        return names

    def _begin_deferral(
        self,
        pool: BlockPool,
        ops_alignment: int,
        table_ops: int,
        labels_prefix: str,
        group: Optional[str],
        group_expr: Optional[Expr],
        table_source: Optional[Tuple[Macro, int, int]],
    ) -> bool:
        """one pass: take the table out of the op stream (see BlockPool's ONE PASS). Its ops go into
        the pool's stream behind a NewSegment whose address is set once it is placed, and its labels
        are recorded as offsets from its start, so curr_address counts from 0 inside it - nothing in
        the table can read that number, since an entry that uses `$` keeps its table inline. A table
        that stays inline (too wide for any slot, or using `$`) is still queued, so the pool declines
        it in its turn."""
        verdict = pool.defer(
            ops_alignment,
            table_ops,
            group,
            group_expr,
            labels_prefix,
            can_defer=not self._names_next_address(table_source),
        )
        if verdict == DEFER_INLINE:
            return False
        assert group is not None and self._deferred is not None  # defer() keeps an ungrouped table inline
        site = pool.kept_site(labels_prefix)
        if verdict != DEFER_OUT:
            self._deferred.add(group, group_expr, ops_alignment, table_ops, site, None, verdict)
            return False  # aligned and emitted in place, exactly as a placing pass declines it
        segment = NewSegment(0)  # its address is decided once the expansion is done
        self.pool_ops.append(segment)
        self._deferred.add(group, group_expr, ops_alignment, table_ops, site, segment)
        self._deferring = True
        self._reloc_stack.append((self.curr_address, self.emit_target))
        self.emit_target = self.pool_ops
        self.curr_address = 0
        return True

    def _place_deferred_tables(self, pool: BlockPool) -> None:
        """one pass, after the expansion: have the pool place every queued table, then give each
        that left the stream its labels and its segment's address (flush_table_pool sorts them)"""
        tables = self._deferred
        assert tables is not None
        op_bits = 2 * self.memory_width
        addresses = pool.place_deferred(tables, self.labels)
        for index, address in enumerate(addresses):
            if not address:
                continue  # it stayed inline: its labels are in place already
            for label, offset in tables.labels_of(index):
                self.labels[label] = address + offset
                self._label_positions.append(self._deferred_label_positions.get(label))
                if self.save_debug_labels:
                    self.addresses_with_labels.add(address + offset)
            segment = tables.segments[index]
            assert segment is not None
            segment.start_address = address
            segment.wflip_start_address = address + tables.sizes[index] * op_bits  # a table has no wflips
        self._deferred = DeferredTables(pool.keeps_site_paths)  # release them

    def end_relocation(self) -> None:
        """close the table and resume the interrupted stream where it left off.

        The run's segment ends wherever the last table in it ended, so its wflip_start_address is
        moved forward here rather than guessed: a relocated table contains no wflips, so that
        address is simply the segment's end.
        """
        if self._deferring:
            self._deferring = False
            assert self._deferred is not None
            promised = self._deferred.sizes[-1]
            if self.curr_address != promised * 2 * self.memory_width:
                raise FlipJumpPreprocessorException(
                    f'one-pass blocking: a deferred table holds {self.curr_address // (2 * self.memory_width)} '
                    f'ops where its pad promised {promised}'
                )
            self.curr_address, self.emit_target = self._reloc_stack.pop()
            return
        if self.table_pool is not None:
            self.table_pool.commit(self.curr_address)
        if self._pool_segment is not None:
            self._pool_segment.wflip_start_address = self.curr_address
        saved_address, saved_target = self._reloc_stack.pop()
        self.curr_address = saved_address
        self.emit_target = saved_target

    def flush_table_pool(self) -> None:
        """append the pool's segments after the main stream.

        Each run opened its own NewSegment, and the runs were allocated in ascending popcount --
        NOT ascending address -- so they are sorted here. The main stream's wflip area has already
        been patched, and the pool sits above it by construction (pool_base), so they cannot
        collide.
        """
        pool = self.pool_ops
        if not pool:
            return
        # a run is a NewSegment and the ops up to the next one (ops before the first are dropped);
        # the stream records where each OBJ op is, so the runs are cut without decoding it
        marks = pool.marks
        starts = [mark_index for mark_index, mark in enumerate(marks) if isinstance(mark[2], NewSegment)]
        runs: List[Tuple[NewSegment, int, int, int, int, int, int]] = []
        for i, mark_index in enumerate(starts):
            op_index, val_index, segment = marks[mark_index]
            if i + 1 < len(starts):
                mark_end = starts[i + 1]
                op_end, val_end = marks[mark_end][0], marks[mark_end][1]
            else:
                mark_end = len(marks)
                op_end, val_end = len(pool.kinds), len(pool.vals)
            runs.append((segment, op_index, op_end, val_index, val_end, mark_index, mark_end))
        runs.sort(key=lambda run: run[0].start_address)
        for segment, op_start, op_end, val_start, val_end, mark_start, mark_end in runs:
            self.last_new_segment = segment
            self.result_ops.extend_slice(pool, op_start, op_end, val_start, val_end, mark_start, mark_end)
        pool.clear()

    def finish(self, show_statistics: bool) -> None:
        self.patch_last_wflip_address()
        if self.table_pool is not None and self.curr_address > self.table_pool.pool_base:
            # The pool sits above the main stream by construction. If the program grew into it, a
            # relocated table is sitting on top of code and the program is silently wrong -- so
            # this is an error, not a warning.
            raise FlipJumpPreprocessorException(
                'table-placement: the program reached %s, which is inside the table pool based at '
                '%s. Raise pool_base, or relocate fewer tables.'
                % (hex(self.curr_address), hex(self.table_pool.pool_base))
            )
        if self._one_pass is not None:
            self._place_deferred_tables(self._one_pass)
        self.flush_table_pool()
        self.insert_macro_start_labels_if_their_address_not_used()
        if show_statistics:
            show_macro_usage_pie_graph(dict(self.macro_code_size), self.curr_address)

    def prepare_macro_call(self, calling_op: Union[MacroCall, RepCall]) -> PreprocessorData._PrepareMacroCall:
        return PreprocessorData._PrepareMacroCall(self.curr_tree, calling_op, self.macros, self.max_recursion_depth)

    def get_result_ops_and_labels(self) -> Tuple[OpsQueue, LabelsDict]:
        return self.result_ops, self.labels

    def insert_segment(self, next_segment_start: int) -> None:
        self.labels[f'{wflip_start_label}{self.curr_segment_index}'] = self.curr_address
        self._label_positions.append(None)
        self.curr_segment_index += 1

        self.patch_last_wflip_address()

        new_segment = NewSegment(next_segment_start)
        self.last_new_segment = new_segment
        self.result_ops.append(new_segment)

        self.curr_address = next_segment_start

    def insert_reserve(self, reserved_bits_size: int) -> None:
        self.curr_address += reserved_bits_size
        self.result_ops.append(ReserveBits(self.curr_address))

    def insert_label(self, label: str, code_position: CodePosition, *, address: Optional[int] = None) -> None:
        if address is None:
            address = self.curr_address

        # (one pass: a deferred table's label is in _deferred_label_positions before it is in labels)
        if label in self.labels or (self._one_pass is not None and label in self._deferred_label_positions):
            other_position = self.label_position(label)
            macro_resolve_error(
                self.curr_tree, f'label declared twice - "{label}" on ' f'{code_position} and {other_position}'
            )
        if self._deferring:
            # a deferred table's label: an offset now, an address once the table is placed
            assert self._deferred is not None
            self._deferred_label_positions[label] = code_position
            self._deferred.add_label(label, address)
            return
        self.labels[label] = address
        self._label_positions.append(code_position)
        if self.save_debug_labels:
            # only insert_macro_start_labels_if_their_address_not_used reads this set
            self.addresses_with_labels.add(address)

    def label_position(self, label: str) -> Optional[CodePosition]:
        """where `label` was declared (None if it was not declared by insert_label). Slow: it walks
        the labels in insertion order - only an error message asks."""
        if label in self._deferred_label_positions:
            return self._deferred_label_positions[label]
        for index, name in enumerate(self.labels):
            if name == label:
                return self._label_positions[index] if index < len(self._label_positions) else None
        return None

    def insert_macro_start_label(self, labels_prefix: str, code_position: CodePosition) -> None:
        """Record that a macro expansion starts here, so a `<path>---:start:` label can name it.
        The label is unreachable from fj source, and only the debugging file reads it.

        @note must be called at the start of the expansion.
        @note takes the PREFIX, not the finished label: when labels are off the string is never built.
        """
        if self.save_debug_labels:
            label = f'{labels_prefix}{MACRO_SEPARATOR_STRING}{STARTING_LABEL_IN_MACROS_STRING}'
            self.macro_start_labels.append((self.curr_address, label, code_position))

    def insert_macro_start_labels_if_their_address_not_used(self) -> None:
        for address, label, code_position in self.macro_start_labels[::-1]:
            if address not in self.addresses_with_labels:
                self.insert_label(label, code_position, address=address)

    def register_macro_code_size(self, macro_path: str, init_curr_address: int) -> None:
        if 1 <= len(self.curr_tree) <= 2:
            self.macro_code_size[macro_path] += self.curr_address - init_curr_address

    def align_current_address(self, ops_alignment: int) -> None:
        op_size = 2 * self.memory_width
        if self.curr_address % op_size != 0:
            macro_resolve_error(
                self.curr_tree,
                f"'pad' requires the current address to be op-aligned (a multiple of 2*w={op_size} bits), "
                f"but it's currently {self.curr_address} bits "
                f"(this usually happens after a 'reserve' or 'segment' that isn't 2*w-aligned).",
            )
        ops_to_pad = (-self.curr_address // op_size) % ops_alignment
        self.curr_address += ops_to_pad * op_size
        self.emit(Padding(ops_to_pad))

    def deferred_labels_note(self, op: Union[RepCall, Pad, Segment, Reserve]) -> str:
        """why an expression evaluated during the expansion may have failed: it names the label of a
        table a one-pass BlockPool places only after it ('' when it names none). Only the expression
        that is evaluated is searched - a rep's count, not its arguments."""
        if self._one_pass is None:
            return ''
        if isinstance(op, RepCall):
            expr = op.repeat_times
        elif isinstance(op, Pad):
            expr = op.ops_alignment
        elif isinstance(op, Segment):
            expr = op.start_address
        else:
            expr = op.reserved_bit_size
        # a deferred label is declared (it has a code position) but has no address yet
        named = sorted(
            label
            for label in expr.all_unknown_labels()
            if label in self._deferred_label_positions and label not in self.labels
        )
        if not named:
            return ''
        return (
            f' It names {", ".join(named)}, the label of a table that one-pass blocking places only after '
            f'the expansion: no address exists yet. Assemble this program with the two-pass BlockPool.'
        )


def get_rep_times(op: RepCall, preprocessor_data: PreprocessorData) -> int:
    try:
        return op.calculate_times(preprocessor_data.labels)
    except FlipJumpExprException as e:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f"Can't evaluate how many times to repeat in "
            f"'rep {op.macro_name}'. In {op.code_position}."
            f"{preprocessor_data.deferred_labels_note(op)}",
            orig_exception=e,
        )


def get_pad_ops_alignment(op: Pad, preprocessor_data: PreprocessorData) -> int:
    try:
        ops_alignment = op.calculate_ops_alignment(preprocessor_data.labels)
    except FlipJumpExprException as e:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f"Can't evaluate how much to pad in "
            f"'pad {op.ops_alignment}'. In {op.code_position}."
            f"{preprocessor_data.deferred_labels_note(op)}",
            orig_exception=e,
        )
    if ops_alignment <= 0:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f"'pad' must get a positive ops-alignment, but got {ops_alignment}. In {op.code_position}.",
        )
    return ops_alignment


def get_next_segment_start(op: Segment, preprocessor_data: PreprocessorData) -> int:
    try:
        next_segment_start = op.calculate_address(preprocessor_data.labels)
        if next_segment_start % preprocessor_data.memory_width != 0:
            macro_resolve_error(
                preprocessor_data.curr_tree,
                f'segment ops must have a w-aligned '
                f'(memory-width-aligned) address: '
                f'{hex(next_segment_start)}. In {op.code_position}.',
            )
        return next_segment_start
    except FlipJumpExprException as e:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f'segment failed. In {op.code_position}.{preprocessor_data.deferred_labels_note(op)}',
            orig_exception=e,
        )


def get_reserved_bits_size(op: Reserve, preprocessor_data: PreprocessorData) -> int:
    try:
        reserved_bits_size = op.calculate_reserved_bit_size(preprocessor_data.labels)
        if reserved_bits_size % preprocessor_data.memory_width != 0:
            macro_resolve_error(
                preprocessor_data.curr_tree,
                f'reserve ops must have a w-aligned '
                f'(memory-width aligned) value: '
                f'{hex(reserved_bits_size)}. In {op.code_position}.',
            )
        return reserved_bits_size
    except FlipJumpExprException as e:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f'reserve failed. In {op.code_position}.{preprocessor_data.deferred_labels_note(op)}',
            orig_exception=e,
        )


def get_params_dictionary(
    current_macro: Macro, args: Iterable[Expr], namespace: str, labels_prefix: str
) -> Dict[str, Expr]:
    """
    generates the dictionary between the labels (params and local-params) defined by the macro, and their Expr-values.
    @param current_macro: the current macro
    @param args: the macro's arguments (Expressions)
    @param namespace: the current namespace
    @param labels_prefix: the path to the currently-preprocessed macro
    @return: the parameters' dictionary
    """
    params_dict: Dict[str, Expr] = dict(zip(current_macro.params, args))

    for local_param in current_macro.local_params:
        params_dict[local_param] = Expr(f'{labels_prefix}{MACRO_SEPARATOR_STRING}{local_param}')

    # namespaced_params is empty for a macro with no namespace
    for namespaced_name, name in current_macro.namespaced_params:
        params_dict[namespaced_name] = params_dict[name]

    return params_dict


def relocatable_table_end(ops: List[Op], pad_index: int) -> Optional[Tuple[int, int, Optional[Expr]]]:
    """(index of the table's last op, number of ops in it, the source-word Expr) after
    `ops[pad_index]`, or None if the run must not be moved.

    THE TABLE is the maximal run of plain `a;b` ops after the `pad`, INCLUDING labels interleaved
    among them, but with TRAILING labels trimmed off. Both halves of that rule are load-bearing,
    and stl contains a counter-example to each:

      * `hex.exact_xor` ends `... d3;switch+7*dw` / `end:` / `wflip src+w, switch`. `end:` names
        the DISARM, which stays inline, so a trailing label must NOT be relocated -- otherwise
        every table entry jumps into empty pool space.

      * `hex.cmp` ends `__lt: ;lt` / `__eq: jumper+dbit ;eq` / `__gt: jumper+dbit+1 ;gt`. Those
        three are ONE table selected by flipping address bits of the jumper, so an interior label
        MUST be relocated with it -- splitting them apart breaks the compare.

    The run is refused when
      * an entry has an EMPTY jump target (`dst;`, continue to the next address): `bit.exact_xor`
        is that shape, and in the pool the next address is another table's slot;
      * the pad is reached by falling into it rather than by a jump (the op before it is a `dst;`
        or a `wflip` with no return address), since the pool is not the next address;
      * the run ends at a `wflip` that is not the DISARM of the run's head label. A dispatch table
        is armed and disarmed through one word (`wflip src+w, switch, src` ... `wflip src+w,
        switch`); a `wflip` that installs anything else is an ENTRY of a multi-op table
        (`hex.input_hex`'s `flip3: wflip stl.IO+w, flip3, end`, `hex.pointers.xor_hex_to_flip_ptr`'s
        `after_flip_bit2: wflip to_flip, ..., cleanup`), and moving the plain ops before it away
        from it breaks the table.
    """
    if not _reached_only_by_jump(ops, pad_index):
        return None
    head: Optional[str] = None
    last_flipjump = None
    count = 0
    source_word: Optional[Expr] = None
    # (indexed, not `enumerate(ops[pad_index + 1:])`: that copied the rest of the list - the whole
    # remaining top level, millions of ops, for every top-level pad)
    for offset in range(pad_index + 1, len(ops)):
        op = ops[offset]
        if isinstance(op, FlipJump):
            if _falls_through(op):
                return None
            last_flipjump = offset
            count += 1
        elif isinstance(op, Label):
            if count == 0 and head is None:
                head = op.name
        else:
            if isinstance(op, WordFlip):
                # the disarm names the head label; its word is the jump word the table is
                # dispatched through, and that word's EXPRESSION (the address is unknown this
                # early) groups the tables that share a word (see BlockPool)
                if head is None or not _same_local_label(op.flip_value, head):
                    return None
                source_word = op.word_address
            break
    if last_flipjump is None:
        return None
    return last_flipjump, count, source_word


def _same_local_label(value: Expr, label: str) -> bool:
    """does `value` name `label`? a macro body's labels are parsed namespaced (`hex.switch`) while
    a reference to one is the bare local name (`switch`), so the local part is what is compared"""
    return isinstance(value.value, str) and value.value.rpartition('.')[2] == label.rpartition('.')[2]


def _falls_through(op: Op) -> bool:
    """does control leave `op` by falling to the next address? (`dst;`, or a `wflip` with no
    return address)"""
    if isinstance(op, FlipJump):
        return '$' in op.jump.all_unknown_labels()
    if isinstance(op, WordFlip):
        return '$' in op.return_address.all_unknown_labels()
    return False


def _reached_only_by_jump(ops: List[Op], pad_index: int) -> bool:
    """is the pad at `ops[pad_index]` preceded by an op that jumps away? labels in between do not
    count, and a pad that opens the macro body is reached from whatever came before the call"""
    for index in range(pad_index - 1, -1, -1):  # (not `reversed(ops[:pad_index])`, which copied the prefix)
        op = ops[index]
        if isinstance(op, Label):
            continue
        return not _falls_through(op) and isinstance(op, (FlipJump, WordFlip))
    return False


def resolve_macro_aux(
    preprocessor_data: PreprocessorData,
    macro_name: MacroName,
    args: Iterable[Expr],
    labels_prefix: str,
) -> None:
    """
    recursively unwind the current macro into a serialized stream of ops and add them to the result_ops-queue.
    also add every label's value to the labels-dictionary. both saved in preprocessor_data.
    @param preprocessor_data: maintains the preprocessor "global" data structures
    @param macro_name: the name of the macro to unwind
    @param args: the arguments for the macro to unwind
    @param labels_prefix: The prefix for all labels defined in this macro
    """
    init_curr_address = preprocessor_data.curr_address
    relocated = False
    table_end = -1
    current_macro = preprocessor_data.macros[macro_name]
    if preprocessor_data.table_pool is not None and macro_name.name in RAW_READERS:
        preprocessor_data.table_pool.reads_words_raw = True
    params_dict = get_params_dictionary(current_macro, args, current_macro.namespace, labels_prefix)

    preprocessor_data.insert_macro_start_label(labels_prefix, current_macro.code_position)

    # when the caller gave the tree away (consume_main), the top level's ops are dropped as the expansion
    # passes them. What is kept: the last non-label op already expanded and everything after it, which
    # is all a `pad` looks back at (_reached_only_by_jump) or forward to (relocatable_table_end).
    ops = current_macro.ops
    consume = preprocessor_data.consume_main and macro_name is INITIAL_MACRO_NAME
    released_up_to = 0  # ops[:released_up_to] are None
    last_non_label = 0

    for op_index, op in enumerate(ops):
        if consume:
            if last_non_label > released_up_to:
                for index in range(released_up_to, last_non_label):
                    ops[index] = None  # type: ignore[call-overload]
                released_up_to = last_non_label
            if not isinstance(op, Label):
                last_non_label = op_index
        # a relocated table ends at its last `a;b` op (see relocatable_table_end); everything after
        # it - `exact_xor`'s `end:` label and its disarm wflip - belongs to the inline stream
        if relocated and op_index > table_end:
            preprocessor_data.end_relocation()
            relocated = False

        if isinstance(op, Label):
            preprocessor_data.insert_label(op.eval_name(params_dict), op.code_position)

        elif isinstance(op, FlipJump) or isinstance(op, WordFlip):
            preprocessor_data.curr_address += 2 * preprocessor_data.memory_width
            params_dict['$'] = Expr(preprocessor_data.curr_address)
            # straight into the compact stream - no FlipJump/WordFlip object is built for it
            if op.__class__ is FlipJump:
                preprocessor_data.emit_target.add_flip_jump_in(op.flip, op.jump, params_dict, op.code_position)
            elif op.__class__ is WordFlip:
                preprocessor_data.emit_target.add_word_flip_in(
                    op.word_address, op.flip_value, op.return_address, params_dict, op.code_position
                )
            else:
                preprocessor_data.emit(op.eval_new(params_dict))
            del params_dict['$']

        elif isinstance(op, Pad):
            op = op.eval_new(params_dict)
            ops_alignment = get_pad_ops_alignment(op, preprocessor_data)
            # a `pad` marks a lookup table: reached only by jump, so instead of aligning here it may
            # be emitted at a pool address, and the interrupted stream resumes after it (TablePool)
            found = preprocessor_data.table_end(current_macro, op_index)
            group = None
            group_expr: Optional[Expr] = None
            if found is not None and found[2] is not None:
                # bind the macro's parameters (`src + w` names the caller's variable); unknown
                # labels stay as names, so two sites on the same variable compare equal
                group_expr = found[2].eval_new(params_dict)
                group = str(group_expr)
            if (
                not relocated
                and found is not None
                and preprocessor_data.begin_relocation(
                    macro_name,
                    ops_alignment,
                    found[1],
                    labels_prefix,
                    group,
                    group_expr,
                    table_source=(current_macro, op_index, found[0]),
                )
            ):
                table_end = found[0]
                relocated = True
            else:
                preprocessor_data.align_current_address(ops_alignment)

        elif isinstance(op, MacroCall):
            op = op.eval_new(params_dict)
            next_macro_path = (
                f"{labels_prefix}{MACRO_SEPARATOR_STRING}" if labels_prefix else ""
            ) + f"{op.code_position.short_str()}:{op.macro_name}"
            progress_slice = preprocessor_data.progress_slice
            if progress_slice is not None:
                preprocessor_data.enter_progress_slice(progress_slice, op_index, len(current_macro.ops), 0, 1)
            with preprocessor_data.prepare_macro_call(op):
                resolve_macro_aux(preprocessor_data, op.macro_name, op.arguments, next_macro_path)
            if progress_slice is not None:
                preprocessor_data.progress_slice = progress_slice

        elif isinstance(op, RepCall):
            # Make the rep iterator hygienic: rename it (and its references in the rep's arguments)
            # to a per-expansion-unique name BEFORE substituting params/@-labels, so a caller label
            # named like the iterator (e.g. `d`, `i`) can't be captured by it. The `:rep:` marker
            # keeps it distinct from any @-label global name (`labels_prefix---<name>`, no `:`).
            hygienic_iterator = (
                f"{labels_prefix}{MACRO_SEPARATOR_STRING}" if labels_prefix else ""
            ) + f"{op.code_position.short_str()}:rep:{op.iterator_name}"
            op = op.rename_iterator(hygienic_iterator)
            op = op.eval_new(params_dict)
            rep_times = get_rep_times(op, preprocessor_data)
            if rep_times == 0:
                continue
            next_macro_path = (
                f"{labels_prefix}{MACRO_SEPARATOR_STRING}" if labels_prefix else ""
            ) + f"{op.code_position.short_str()}:rep{{}}:{op.macro_name}"
            progress_slice = preprocessor_data.progress_slice
            with preprocessor_data.prepare_macro_call(op):
                for i in range(rep_times):
                    op.current_index = i
                    if progress_slice is not None:
                        preprocessor_data.enter_progress_slice(
                            progress_slice, op_index, len(current_macro.ops), i, rep_times
                        )
                    resolve_macro_aux(
                        preprocessor_data, op.macro_name, op.calculate_arguments(i), next_macro_path.format(i)
                    )
            if progress_slice is not None:
                preprocessor_data.progress_slice = progress_slice

        elif isinstance(op, Segment):
            op = op.eval_new(params_dict)
            next_segment_start = get_next_segment_start(op, preprocessor_data)
            preprocessor_data.insert_segment(next_segment_start)

        elif isinstance(op, Reserve):
            op = op.eval_new(params_dict)
            reserved_bits_size = get_reserved_bits_size(op, preprocessor_data)
            preprocessor_data.insert_reserve(reserved_bits_size)

        else:
            macro_resolve_error(preprocessor_data.curr_tree, f"Can't assemble this opcode - {str(op)}")

    if relocated:
        preprocessor_data.end_relocation()

    preprocessor_data.register_macro_code_size(labels_prefix, init_curr_address)


def resolve_macros(
    memory_width: int,
    macros: Dict[MacroName, Macro],
    *,
    show_statistics: bool = False,
    max_recursion_depth: int = DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    save_debug_labels: bool = True,
    table_pool: Optional[TablePool] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    consume_main: bool = False,
) -> Tuple[OpsQueue, LabelsDict]:
    """
    unwind the macro tree to a serialized-queue of ops,
    and creates a dictionary from label's name to its address.
    @param memory_width: the memory-width
    @param macros: parser's result; the dictionary from the macro names to the macro declaration
    @param show_statistics: if True then prints the macro-usage statistics
    @param max_recursion_depth: The compiler supports macros that recursively uses other macros,
    up to the specified recursion depth.
    @param table_pool: if given, relocate the lookup tables it wants to low-popcount addresses.
    See TablePool -- a table is reached only by jump, so its address is free to choose, and that
    address is what the arming wflip pays for on every call.
    @param save_debug_labels: record the per-expansion `...---:start:` macro-start labels. They are
    unreachable from fj source (see PreprocessorData.insert_macro_start_label), so a caller that is
    not writing a debugging file can pass False. It cannot change the emitted .fjm.
    @param on_progress: if given, called with the expansion's progress, a fraction in [0, 1) - by how
    far into the main macro (and the calls in it) the expansion is. It cannot change the emitted .fjm.
    @param consume_main: the caller will not use `macros` again: the top level's ops are dropped (set to
    None in the main macro's op list) as the expansion passes them. Off by default - the two-pass
    BlockPool expands one tree twice. It cannot change the emitted .fjm.
    @return: tuple of the queue of ops, and the labels' dictionary
    """
    preprocessor_data = PreprocessorData(
        memory_width, macros, max_recursion_depth, save_debug_labels=save_debug_labels, table_pool=table_pool
    )
    preprocessor_data.consume_main = consume_main
    if on_progress is not None:
        preprocessor_data.on_progress = on_progress
        preprocessor_data.progress_slice = (0.0, 1.0)
    resolve_macro_aux(preprocessor_data, INITIAL_MACRO_NAME, INITIAL_ARGS, INITIAL_LABELS_PREFIX)

    preprocessor_data.finish(show_statistics)
    return preprocessor_data.get_result_ops_and_labels()
