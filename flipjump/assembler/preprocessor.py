"""
the preprocessor (macro-resolution stage).
expands macro calls and rep (repetition) blocks recursively, starting from the main
macro, and produces the flat op-list that the assembler then resolves into addresses.
"""

from __future__ import annotations

import collections
import sys
from typing import Any, Dict, Tuple, Iterable, Union, Deque, Set, List, Optional, NoReturn, Callable

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
    get_used_labels,
)
from flipjump.assembler.inner_classes.op_stream import OpStream
from flipjump.assembler.table_pool import (
    BlockPool,
    DEFER_INLINE,
    DEFER_TOO_WIDE,
    DeferredTables,
    OffsetGuard,
    POINTER_RUNTIME_LABELS,
    RESERVED_BELOW,
    heat_key,
    reached_the_pool,
)

CurrTree = Deque[Union[MacroCall, RepCall]]
OpsQueue = OpStream  # the preprocessor's output
LabelsDict = Dict[str, int]

wflip_start_label = '_.wflip_area_start_'


# a macro whose innermost namespace is this declares a table a pool may move: see relocatable_call
RELOCATABLE_NAMESPACE = '__relocatable__'


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
        table_pool: Optional[BlockPool] = None,
    ):
        """
        @param memory_width: the memory-width
        @param macros: parser's result; the dictionary from the macro names to the macro declaration
        @param max_recursion_depth: The compiler supports macros that recursively uses other macros,
        up to the specified recursion depth.
        @param save_debug_labels: whether to record the per-expansion `...---:start:` macro-start
        labels. See insert_macro_start_label for why they can be skipped.
        @param table_pool: if given, relocate the declared tables (see relocatable_call, BlockPool)
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

        # TABLE PLACEMENT (off unless a pool is given): a table the pool takes is emitted into a SEPARATE stream,
        # behind a NewSegment whose address is set once the expansion is done, and waits in `_deferred` until finish()
        # has the pool place them all
        self.table_pool = table_pool
        if table_pool is not None:
            if table_pool.laid_out:
                raise FlipJumpPreprocessorException(
                    'table-placement: this pool already laid out an assembly; construct a fresh one for every '
                    'assemble()'
                )
            table_pool.laid_out = True
        self.pool_ops: OpStream = OpStream(self._op_refs, self.labels)
        # a pinned word's wflips tell an address from a number by its expression (BinaryData.pinned_value)
        self.result_ops.keep_label_values = self.pool_ops.keep_label_values = table_pool is not None
        self.relocatable_extents: Optional[List[Tuple[int, int]]] = None  # (a caller's) every inline table's extent
        # an operand that would point elsewhere once the tables moved is refused (see OffsetGuard)
        self.offset_guard: Optional[OffsetGuard] = None
        if table_pool is not None:
            self.offset_guard = table_pool.offset_guard = OffsetGuard(table_pool.pool_start(), self.labels)
            self.offset_guard.where = self._where
            self.offset_guard.emitting = self._emitting
        self.result_ops.guard = self.pool_ops.guard = self.offset_guard
        # {address: the bits a `pad` aligned it to, 0 where only a segment starts} (the OffsetGuard's, or a caller's)
        self.pads: Optional[Dict[int, int]] = None if self.offset_guard is None else self.offset_guard.pads
        self._dollar_ops: Dict[int, Dict[int, bool]] = {}  # id(macro) -> its ops that name `$` (see dollar_ops)
        self._pointer_runtime_users: Dict[int, bool] = {}  # id(macro) -> it names POINTER_RUNTIME_LABELS
        self.emit_target: OpStream = self.result_ops
        # (emitting a deferred table's ops) the inline address and the stream the expansion resumes at after it
        self._resume: Optional[Tuple[int, OpStream]] = None
        # (a table pool is on) how many ops the program without the pool is ahead of this one in this segment: the
        # moved tables' ops and pads, and the pads that padded differently since (see align_current_address)
        self._shift = 0
        self._code_end = 0  # (a table pool is on) where the code of the segments closed so far ends, at most

        self._deferred = DeferredTables()

        self.on_progress: Optional[Callable[[float], None]] = None  # told how far into the top level the expansion is

        self.max_recursion_depth = max_recursion_depth
        # set python's recursion-limit so the preprocessor's own depth-check (at max_recursion_depth)
        #  triggers before python's RecursionError. Set it unconditionally - whether it's higher or lower than the
        #  current limit - so that raising max_recursion_depth actually allows deeper macro-recursion.
        sys.setrecursionlimit(max_recursion_depth + GAP_BETWEEN_PYTHONS_AND_PREPROCESSOR_MACRO_RECURSION_DEPTH)

    def patch_last_wflip_address(self) -> None:
        self.last_new_segment.wflip_start_address = self.curr_address

    def emit(self, op: LastPhaseOp) -> None:
        """append to whichever stream is being built -- the main one, or the table pool"""
        self.emit_target.append(op)

    def begin_relocation(
        self,
        ops_alignment: int,
        table_ops: int,
        labels_prefix: str = '',
        group: Optional[str] = None,
        group_expr: Optional[Expr] = None,
        *,
        table: str = '',
    ) -> bool:
        """Take the declared table (`table`: its start label) out of the op stream instead of aligning it in place.

        A table is reached only by jump, so moving it changes nothing but its address. Its ops go into the pool's
        stream behind a NewSegment whose address is set once it is placed, and its labels are recorded as offsets from
        its start, so curr_address counts from 0 inside it (a declared table may not use `$`). Returns False when it
        stays inline: there is no pool, or moving it would change an operand, or the pool declines it (a table too
        wide for any slot is still queued, so the pool declines it in its turn).
        """
        pool = self.table_pool
        if pool is None:
            return False
        assert self.offset_guard is not None
        unsafe = self.offset_guard.may_move(self.curr_address)
        if unsafe is not None:
            pool.unsafe_declines[unsafe] = pool.unsafe_declines.get(unsafe, 0) + 1
            return False  # it stays inline: see OffsetGuard
        if group_expr is not None and self._is_runtime_word(group_expr):
            pool.reserved_words += 1
            return False
        verdict = pool.defer(ops_alignment, table_ops, group, group_expr, labels_prefix)
        if verdict == DEFER_INLINE:
            return False
        group = '' if group is None else group  # (a raw program's table may have none: BlockPool._place_cheapest)
        site = sys.intern(heat_key(labels_prefix)) if pool.heat else None
        if verdict == DEFER_TOO_WIDE:
            self._deferred.add(group, group_expr, ops_alignment, table_ops, site, None)
            return False  # aligned and emitted in place
        self._table_moved(table, ops_alignment, table_ops)
        segment = NewSegment(0)  # its address is decided once the expansion is done
        self.pool_ops.append(segment)
        self._deferred.add(group, group_expr, ops_alignment, table_ops, site, segment)
        self._resume = self.curr_address, self.emit_target
        self.emit_target = self.pool_ops
        self.curr_address = 0
        return True

    def _table_moved(self, table: str, ops_alignment: int, table_ops: int) -> None:
        """the table (its start label), behind a `pad ops_alignment`, leaves the inline stream here (see OffsetGuard)"""
        assert self.offset_guard is not None
        self.offset_guard.table_moved(self.curr_address, table)
        plain = self.curr_address // (2 * self.memory_width) + self._shift  # where the program without the pool is
        self._shift += (-plain) % ops_alignment + table_ops

    def _emitting(self) -> int:
        """the next inline address (OffsetGuard: a table moving out of it or above can still stay inline)"""
        return self._resume[0] if self._resume is not None else self.curr_address

    def dollar_ops(self, macro: Macro) -> Dict[int, bool]:
        """(a table pool is on) {op index: names `$` in a sum} of the macro's ops that name `$` (the next op's
        address) - not in a sum (False), it falls through: see dollar"""
        found = self._dollar_ops.get(id(macro))
        if found is None:
            found = self._dollar_ops[id(macro)] = {}
            for index, op in enumerate(macro.ops):
                if isinstance(op, FlipJump):
                    operands: Tuple[Expr, ...] = (op.flip, op.jump)
                elif isinstance(op, WordFlip):
                    operands = (op.word_address, op.flip_value, op.return_address)
                else:
                    continue
                in_sum = any(operand.value != '$' and '$' in operand.all_unknown_labels() for operand in operands)
                if in_sum or any(operand.value == '$' for operand in operands):
                    found[index] = in_sum
        return found

    def dollar(
        self, in_sum: bool, labels_prefix: str, op_index: int, code_position: CodePosition, params_dict: Dict[str, Expr]
    ) -> None:
        """(a table pool is on) the op being emitted (`op_index` of the macro at `labels_prefix`) names `$`: if in a
        sum, `$` becomes a label at the next op, which OffsetGuard checks as any inline label; else the op falls
        through to the next op"""
        assert self.offset_guard is not None
        if in_sum:
            name = f'{labels_prefix}{MACRO_SEPARATOR_STRING}$:{op_index}' if labels_prefix else f'$:{op_index}'
            self.insert_label(name, code_position)
            self.offset_guard.watched.add(name)
            params_dict['$'] = Expr(name)
        else:
            self.offset_guard.falls_to = self.curr_address

    def _where(self) -> str:
        """where the op being emitted is (an OffsetGuard refusal says)"""
        path = ' > '.join(call.trace_str() for call in self.curr_tree) or 'the top level'
        if self._resume is not None:
            return f' of an op of a moved table (in {path})'
        return f' of the op at {hex(self.curr_address - 2 * self.memory_width)} (in {path})'

    def _is_runtime_word(self, word: Expr) -> bool:
        """is `word` one of the runtime's (see RESERVED_BELOW)? Those are declared before any code
        dispatches through them, so they resolve here; a program variable, declared after the
        code, does not yet."""
        try:
            return word.exact_eval(self.labels) < RESERVED_BELOW
        except FlipJumpExprException:
            return False

    def _place_deferred_tables(self, pool: BlockPool) -> None:
        """after the expansion: have the pool place every queued table, then give each that left the stream its labels
        and its segment's address (flush_table_pool sorts them)"""
        tables = self._deferred
        op_bits = 2 * self.memory_width
        guard = self.offset_guard
        assert guard is not None
        # the program without the tables ends where its code does, or past every address an operand reaches
        addresses = pool.place_deferred(tables, self.labels, max(self._code_end, self.curr_address, guard.reach + 1))
        guard.inline_end = pool.pool_start()  # (for_flat: where the pool went)
        moved = 0  # the tables that left the stream, in the order they did (OffsetGuard.moved_tables)
        for index, address in enumerate(addresses):
            if not address:
                continue  # it stayed inline: its labels are in place already
            guard.table_placed(address, address + tables.sizes[index] * op_bits, guard.moved_tables[moved])
            moved += 1
            for label, offset in tables.labels_of(index):
                self.labels[label] = address + offset
                self._label_positions.append(self._deferred_label_positions.get(label))
                if self.save_debug_labels:
                    self.addresses_with_labels.add(address + offset)
            segment = tables.segments[index]
            assert segment is not None
            segment.start_address = address
            segment.wflip_start_address = address + tables.sizes[index] * op_bits  # a table has no wflips
        self._deferred = DeferredTables()  # release them

    def end_relocation(self) -> None:
        """close the deferred table, and resume the interrupted stream where it left off"""
        promised = self._deferred.sizes[-1]
        if self.curr_address != promised * 2 * self.memory_width:
            raise FlipJumpPreprocessorException(
                f'table-placement: a deferred table holds {self.curr_address // (2 * self.memory_width)} '
                f'ops where its pad promised {promised}'
            )
        assert self._resume is not None
        self.curr_address, self.emit_target = self._resume
        self._resume = None

    def flush_table_pool(self) -> None:
        """append the pool's segments after the main stream.

        Each table is a NewSegment and its ops up to the next one, placed in no particular address order, so they are
        sorted here (the stream records where each NewSegment is, so it is cut without decoding it). The main stream's
        wflip area has already been patched, and the pool sits above it by construction (pool_base).
        """
        pool = self.pool_ops
        marks = pool.marks
        ends = [(op_index, val_index) for op_index, val_index, _ in marks[1:]] + [(len(pool.kinds), len(pool.vals))]
        for index in sorted(range(len(marks)), key=lambda i: marks[i][2].start_address):
            op_start, val_start, segment = marks[index]
            self.last_new_segment = segment
            self.result_ops.extend_slice(pool, op_start, ends[index][0], val_start, ends[index][1], index, index + 1)
        pool.clear()

    def note_pointer_runtime(self, macro_name: MacroName, macro: Macro) -> None:
        """a macro that names the pointer runtime's words (POINTER_RUNTIME_LABELS) reads or writes cells through
        a pointer: tell the pool, which refuses to pin words such a program may reach (BlockPool)"""
        uses = self._pointer_runtime_users.get(id(macro))
        if uses is None:
            uses = self._pointer_runtime_users[id(macro)] = bool(get_used_labels(macro.ops) & POINTER_RUNTIME_LABELS)
        if uses and self.table_pool is not None:
            self.table_pool.reads_words_raw = True
            self.table_pool.raw_reader = str(macro_name)

    def _refuse_reaching_the_pool(self, address: int) -> None:
        """The pool sits above the main stream by construction. If a segment of the program reaches it, a
        relocated table is sitting on top of code and the program is silently wrong -- so this is an error,
        not a warning. (labels-resolve checks the wflip areas.)"""
        if self.table_pool is not None and address > self.table_pool.pool_start():
            raise reached_the_pool(address, self.table_pool.pool_start())

    def finish(self, show_statistics: bool) -> None:
        self.patch_last_wflip_address()
        self._refuse_reaching_the_pool(self.curr_address)
        if self.table_pool is not None:
            self._place_deferred_tables(self.table_pool)
        self.flush_table_pool()
        self.insert_macro_start_labels_if_their_address_not_used()
        if show_statistics:
            show_macro_usage_pie_graph(dict(self.macro_code_size), self.curr_address)

    def prepare_macro_call(self, calling_op: Union[MacroCall, RepCall]) -> PreprocessorData._PrepareMacroCall:
        return PreprocessorData._PrepareMacroCall(self.curr_tree, calling_op, self.macros, self.max_recursion_depth)

    def get_result_ops_and_labels(self) -> Tuple[OpsQueue, LabelsDict]:
        return self.result_ops, self.labels

    def insert_segment(self, next_segment_start: int) -> None:
        self._refuse_reaching_the_pool(self.curr_address)
        self._refuse_reaching_the_pool(next_segment_start + 1)
        self.labels[f'{wflip_start_label}{self.curr_segment_index}'] = self.curr_address
        self._label_positions.append(None)
        self.curr_segment_index += 1
        self._code_end = max(self._code_end, self.curr_address)
        self._shift = 0  # a segment starts where it does without the pool too
        if self.offset_guard is not None:
            self.offset_guard.segment_started(next_segment_start)
        self._aligned(next_segment_start, 0)

        self.patch_last_wflip_address()

        new_segment = NewSegment(next_segment_start)
        self.last_new_segment = new_segment
        self.result_ops.append(new_segment)

        self.curr_address = next_segment_start

    def insert_reserve(self, reserved_bits_size: int) -> None:
        self.curr_address += reserved_bits_size
        self._refuse_reaching_the_pool(self.curr_address)
        self.result_ops.append(ReserveBits(self.curr_address))

    def insert_label(self, label: str, code_position: CodePosition, *, address: Optional[int] = None) -> None:
        if address is None:
            address = self.curr_address

        # (a deferred table's label is in _deferred_label_positions before it is in labels)
        if label in self.labels or label in self._deferred_label_positions:
            other_position = self.label_position(label)
            macro_resolve_error(
                self.curr_tree, f'label declared twice - "{label}" on ' f'{code_position} and {other_position}'
            )
        if self._resume is not None:
            # a deferred table's label: an offset now, an address once the table is placed
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
        if self.save_debug_labels and self._resume is None:
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
        if self.offset_guard is not None:
            # without the pool the pad starts `_shift` ops further: if it pads differently, the distance across it
            # changes (OffsetGuard.pad_realigned)
            plain_pad = (-(self.curr_address // op_size + self._shift)) % ops_alignment
            if plain_pad != ops_to_pad:
                self.offset_guard.pad_realigned(self.curr_address, ops_alignment)
                self._shift += plain_pad - ops_to_pad
        self.curr_address += ops_to_pad * op_size
        self.emit(Padding(ops_to_pad))
        if ops_alignment > 1:
            bits = ops_alignment * op_size
            self._aligned(self.curr_address, (bits & -bits).bit_length() - 1)

    def check_evaluated(self, expr: Expr, value: int, is_address: bool = False) -> None:
        """(a table pool is on) an expression the expansion evaluates - a rep's count, a pad, a reserve's size, a
        segment's address - is checked as an operand is (OffsetGuard): a distance across a moved table's place would
        change it, and the program with it"""
        if self.offset_guard is not None and expr.all_unknown_labels():
            self.offset_guard.check_expr(expr, value, is_address)

    def _aligned(self, address: int, bits: int) -> None:
        """a `pad` aligned the inline `address` to `bits` bits, or (bits 0) a segment starts there (see `pads`)"""
        if self.pads is not None and address:
            self.pads[address] = max(self.pads.get(address, 0), bits)


def get_rep_times(op: RepCall, preprocessor_data: PreprocessorData) -> int:
    try:
        return op.calculate_times(preprocessor_data.labels)
    except FlipJumpExprException as e:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f"Can't evaluate how many times to repeat in " f"'rep {op.macro_name}'. In {op.code_position}.",
            orig_exception=e,
        )


def get_pad_ops_alignment(op: Pad, preprocessor_data: PreprocessorData) -> int:
    try:
        ops_alignment = op.calculate_ops_alignment(preprocessor_data.labels)
    except FlipJumpExprException as e:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f"Can't evaluate how much to pad in " f"'pad {op.ops_alignment}'. In {op.code_position}.",
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
        macro_resolve_error(preprocessor_data.curr_tree, f'segment failed. In {op.code_position}.', orig_exception=e)


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
        macro_resolve_error(preprocessor_data.curr_tree, f'reserve failed. In {op.code_position}.', orig_exception=e)


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


def is_relocatable(macro_name: MacroName) -> bool:
    """does the macro declare a relocatable table: is its innermost namespace RELOCATABLE_NAMESPACE?"""
    return macro_name.name.rpartition('.')[0].rpartition('.')[2] == RELOCATABLE_NAMESPACE


def relocatable_call(
    preprocessor_data: PreprocessorData, ops: List[Op], pad_index: int, params_dict: Dict[str, Expr]
) -> Optional[Tuple[int, int, Optional[str], Optional[Expr], str]]:
    """(the call's index, the table's ops, its group, the group's source-word Expr, its start label) if the op
    after the `pad` at `ops[pad_index]` (labels aside) calls a relocatable table, else None. A table pool is on.

    THE TABLE is one expansion of a macro whose innermost namespace is `__relocatable__`: its whole
    expansion, which the pool may place anywhere (the pad gives its alignment). Its first argument is
    the label of its start, which the arm and the dispatch use. A table breaking a rule the pool relies
    on is a compilation error: its start label must be declared before its first op, it may hold only
    labels and `a;b` ops, and nothing in it may use `$` (the next address - also a fall-through `dst;`),
    as the op after a moved table is not the next address.

    The group (see BlockPool) is the word the table is dispatched through: the word of the `wflip`
    right after the call, labels aside, that disarms the table's start label.
    """
    for index in range(pad_index + 1, len(ops)):
        op = ops[index]
        if isinstance(op, Label):
            continue
        if not isinstance(op, MacroCall) or not is_relocatable(op.macro_name):
            return None
        break
    else:
        return None
    call = op.eval_new(params_dict)
    macro = preprocessor_data.macros.get(call.macro_name)
    if macro is None:
        return None  # the expansion says so
    table_ops = _relocatable_table_ops(preprocessor_data, macro, call)
    group: Optional[str] = None
    group_expr: Optional[Expr] = None
    for after in range(index + 1, len(ops)):
        disarm = ops[after]
        if isinstance(disarm, Label):
            continue
        if isinstance(disarm, WordFlip) and disarm.flip_value.eval_new(params_dict).value == call.arguments[0].value:
            # bind the macro's parameters (`src + w` names the caller's variable); unknown labels
            # stay as names, so two sites on the same variable compare equal
            group_expr = disarm.word_address.eval_new(params_dict)
            group = str(group_expr)
        break
    return index, table_ops, group, group_expr, str(call.arguments[0].value)


def _relocatable_table_ops(preprocessor_data: PreprocessorData, macro: Macro, call: MacroCall) -> int:
    """the number of ops the relocatable `call` expands, or a compilation error (see relocatable_call)"""

    def refuse(message: str) -> NoReturn:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f'the relocatable table {call.macro_name} (called at {call.code_position}) {message}',
        )

    if not macro.params or not call.arguments or not isinstance(call.arguments[0].value, str):
        refuse("needs its start label as its first argument")
    start = macro.params[0]
    op_bits = 2 * preprocessor_data.memory_width
    offsets: Dict[str, int] = {}  # its labels (local and full names) -> their bit offsets in the table
    entries: List[FlipJump] = []
    for op in macro.ops:
        if isinstance(op, Label):
            if op.name.rpartition('.')[2] == start or offsets:
                offsets[op.name] = offsets[op.name.rpartition('.')[2]] = len(entries) * op_bits
        elif isinstance(op, FlipJump):
            if not offsets:
                refuse(f'must declare its start label ({start}, its first parameter) before its first op')
            if '$' in op.flip.all_unknown_labels() or '$' in op.jump.all_unknown_labels():
                refuse(f'uses `$` in {op} (a `dst;` falls through to `$`): a moved table has no next address')
            entries.append(op)
        else:
            refuse(f'may hold only labels and `a;b` ops, not {op}')
    if not entries:
        refuse('holds no op')
    size = len(entries) * op_bits
    if max(offsets.values()) >= size:
        refuse('declares a label after its last op: it names the op after the table, which a moved table does not have')
    for op in entries:
        for operand in (op.flip, op.jump):
            named = operand.all_unknown_labels()
            if not named & offsets.keys():
                continue
            if named - offsets.keys():
                refuse(f'mixes its own labels with others in {op}: whether it stays inside the table cannot be known')
            if not 0 <= operand.exact_eval(offsets) < size:
                refuse(f'leaves the table through {op}: an offset from its own label must land inside it')
    return len(entries)


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
    table_call = -1  # (a table pool is on) the relocatable call the last `pad` aligns
    relocated_call = -1  # ...if its table is being moved
    current_macro = preprocessor_data.macros[macro_name]
    if preprocessor_data.table_pool is not None and not preprocessor_data.table_pool.reads_words_raw:
        preprocessor_data.note_pointer_runtime(macro_name, current_macro)
    params_dict = get_params_dictionary(current_macro, args, current_macro.namespace, labels_prefix)

    preprocessor_data.insert_macro_start_label(labels_prefix, current_macro.code_position)
    # (a table pool is on) the ops that name `$`, the next op's address: OffsetGuard checks them
    dollar_ops = None if preprocessor_data.offset_guard is None else preprocessor_data.dollar_ops(current_macro)

    # when the caller gave the tree away (consume_main), the top level's ops are dropped as the expansion
    # passes them. What is kept: the last non-label op already expanded and everything after it, which
    # is all a `pad` looks forward to (relocatable_call).
    ops = current_macro.ops
    consume = preprocessor_data.consume_main and macro_name is INITIAL_MACRO_NAME
    released_up_to = 0  # ops[:released_up_to] are None
    last_non_label = 0
    on_progress = preprocessor_data.on_progress if macro_name is INITIAL_MACRO_NAME else None

    for op_index, op in enumerate(ops):
        if on_progress is not None:
            on_progress(op_index / len(ops))
        if consume:
            if last_non_label > released_up_to:
                for index in range(released_up_to, last_non_label):
                    ops[index] = None  # type: ignore[call-overload]
                released_up_to = last_non_label
            if not isinstance(op, Label):
                last_non_label = op_index
        if isinstance(op, Label):
            preprocessor_data.insert_label(op.eval_name(params_dict), op.code_position)

        elif isinstance(op, FlipJump) or isinstance(op, WordFlip):
            preprocessor_data.curr_address += 2 * preprocessor_data.memory_width
            params_dict['$'] = Expr(preprocessor_data.curr_address)
            if dollar_ops and op_index in dollar_ops:
                preprocessor_data.dollar(dollar_ops[op_index], labels_prefix, op_index, op.code_position, params_dict)
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
            preprocessor_data.check_evaluated(op.ops_alignment, ops_alignment)
            # a `pad` right before a relocatable call aligns a declared table: with a table pool it may be
            # emitted at a pool address instead, and the interrupted stream resumes after the call
            table = None
            if preprocessor_data.table_pool is not None:
                table = relocatable_call(preprocessor_data, ops, op_index, params_dict)
            if table is not None:
                table_call = table[0]
                if ops_alignment & (ops_alignment - 1):
                    macro_resolve_error(
                        preprocessor_data.curr_tree,
                        f'the relocatable table after `pad {ops_alignment}` (at {op.code_position}) needs a '
                        f'power-of-two pad: a pool places tables in power-of-two slots, so it could not place '
                        f'this one at its alignment',
                    )
            if table is not None and preprocessor_data.begin_relocation(
                ops_alignment, table[1], labels_prefix, table[2], table[3], table=table[4]
            ):
                relocated_call = table_call
            else:
                preprocessor_data.align_current_address(ops_alignment)

        elif isinstance(op, MacroCall):
            if preprocessor_data.table_pool is not None and op_index != table_call and is_relocatable(op.macro_name):
                macro_resolve_error(
                    preprocessor_data.curr_tree,
                    f'the relocatable table {op.macro_name} (called at {op.code_position}) needs a `pad` right '
                    f'before its call: the pool places it at that alignment',
                )
            op = op.eval_new(params_dict)
            next_macro_path = (
                f"{labels_prefix}{MACRO_SEPARATOR_STRING}" if labels_prefix else ""
            ) + f"{op.code_position.short_str()}:{op.macro_name}"
            extent_start = preprocessor_data.curr_address
            with preprocessor_data.prepare_macro_call(op):
                resolve_macro_aux(preprocessor_data, op.macro_name, op.arguments, next_macro_path)
            if op_index == relocated_call:
                preprocessor_data.end_relocation()
            elif preprocessor_data.relocatable_extents is not None and is_relocatable(op.macro_name):
                preprocessor_data.relocatable_extents.append((extent_start, preprocessor_data.curr_address))

        elif isinstance(op, RepCall):
            if preprocessor_data.table_pool is not None and is_relocatable(op.macro_name):
                macro_resolve_error(
                    preprocessor_data.curr_tree,
                    f'the relocatable table {op.macro_name} (called at {op.code_position}) is called by rep: '
                    f'each table needs its own `pad` right before its call',
                )
            # Make the rep iterator hygienic: rename it (and its references in the rep's arguments)
            # to a per-expansion-unique name BEFORE substituting params/@-labels, so a caller label
            # named like the iterator (e.g. `d`, `i`) can't be captured by it. The `:rep:` marker
            # keeps it distinct from any @-label global name (`labels_prefix---<name>`, no `:`).
            hygienic_iterator = (
                f"{labels_prefix}{MACRO_SEPARATOR_STRING}" if labels_prefix else ""
            ) + f"{op.code_position.short_str()}:rep:{op.iterator_name}"
            op = op.rename_iterator(hygienic_iterator)
            op = op.eval_new(params_dict)
            times = op.repeat_times
            rep_times = get_rep_times(op, preprocessor_data)
            preprocessor_data.check_evaluated(times, rep_times)
            if rep_times == 0:
                continue
            next_macro_path = (
                f"{labels_prefix}{MACRO_SEPARATOR_STRING}" if labels_prefix else ""
            ) + f"{op.code_position.short_str()}:rep{{}}:{op.macro_name}"
            with preprocessor_data.prepare_macro_call(op):
                for i in range(rep_times):
                    op.current_index = i
                    resolve_macro_aux(
                        preprocessor_data, op.macro_name, op.calculate_arguments(i), next_macro_path.format(i)
                    )

        elif isinstance(op, Segment):
            op = op.eval_new(params_dict)
            next_segment_start = get_next_segment_start(op, preprocessor_data)
            preprocessor_data.insert_segment(next_segment_start)
            preprocessor_data.check_evaluated(op.start_address, next_segment_start, True)

        elif isinstance(op, Reserve):
            op = op.eval_new(params_dict)
            reserved_bits_size = get_reserved_bits_size(op, preprocessor_data)
            preprocessor_data.check_evaluated(op.reserved_bit_size, reserved_bits_size)
            preprocessor_data.insert_reserve(reserved_bits_size)

        else:
            macro_resolve_error(preprocessor_data.curr_tree, f"Can't assemble this opcode - {str(op)}")

    preprocessor_data.register_macro_code_size(labels_prefix, init_curr_address)


def resolve_macros(
    memory_width: int,
    macros: Dict[MacroName, Macro],
    *,
    show_statistics: bool = False,
    max_recursion_depth: int = DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    save_debug_labels: bool = True,
    table_pool: Optional[BlockPool] = None,
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
    @param table_pool: if given, relocate the declared tables (see relocatable_call) where they are cheap to arm
    (see BlockPool)
    @param save_debug_labels: record the per-expansion `...---:start:` macro-start labels. They are
    unreachable from fj source (see PreprocessorData.insert_macro_start_label), so a caller that is
    not writing a debugging file can pass False. It cannot change the emitted .fjm.
    @param on_progress: if given, called with the expansion's progress, a fraction in [0, 1) - by how
    far into the main macro's ops the expansion is. It cannot change the emitted .fjm.
    @param consume_main: the caller will not use `macros` again: the top level's ops are dropped (set to
    None in the main macro's op list) as the expansion passes them. It cannot change the emitted .fjm.
    @return: tuple of the queue of ops, and the labels' dictionary
    """
    preprocessor_data = PreprocessorData(
        memory_width,
        macros,
        max_recursion_depth,
        save_debug_labels=save_debug_labels,
        table_pool=table_pool,
    )
    preprocessor_data.consume_main = consume_main
    preprocessor_data.on_progress = on_progress
    resolve_macro_aux(preprocessor_data, INITIAL_MACRO_NAME, INITIAL_ARGS, INITIAL_LABELS_PREFIX)

    preprocessor_data.finish(show_statistics)
    return preprocessor_data.get_result_ops_and_labels()
