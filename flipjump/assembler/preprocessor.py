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
)
from flipjump.assembler.inner_classes.op_stream import OpStream
from flipjump.assembler.inner_classes.table_mover import TableMover
from flipjump.assembler.inner_classes.table_pool import BlockPool, heat_key  # noqa: F401 - importable from here too

CurrTree = Deque[Union[MacroCall, RepCall]]
OpsQueue = OpStream  # the preprocessor's output
LabelsDict = Dict[str, int]

wflip_start_label = '_.wflip_area_start_'


# a macro call is this many python calls deep: _expand_macro_call (or _expand_rep), and its resolve_macro_aux
PYTHON_CALLS_PER_MACRO_CALL = 2

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
        consume_main: bool = False,
        on_progress: Optional[Callable[[float], None]] = None,
    ):
        """
        @param memory_width: the memory-width
        @param macros: parser's result; the dictionary from the macro names to the macro declaration
        @param max_recursion_depth: The compiler supports macros that recursively uses other macros,
        up to the specified recursion depth.
        @param save_debug_labels: record the `...---:start:` macro-start labels (see insert_macro_start_label)
        @param table_pool: if given, move the declared tables (see relocatable_call, BlockPool)
        @param consume_main: the caller won't use `macros` again, so the top level's ops are dropped as they expand
        @param on_progress: if given, called with how far the top-level expansion is (a fraction in [0, 1))
        """
        self.memory_width = memory_width
        self.macros = macros
        self.save_debug_labels = save_debug_labels
        self.consume_main = consume_main
        self.on_progress = on_progress

        self.curr_address: int = 0
        self.curr_tree: CurrTree = collections.deque()
        self.curr_segment_index: int = 0
        self.macro_code_size: Dict[str, int] = collections.defaultdict(lambda: 0)

        self.labels: Dict[str, int] = {}
        # Where each label was declared, in the labels dict's insertion order (labels are never removed). Only a
        # "declared twice" error reads it (label_position).
        self._label_positions: List[Optional[CodePosition]] = []
        self.addresses_with_labels: Set[int] = set()
        self.macro_start_labels: List[Tuple[int, str, CodePosition]] = []  # (address, label, code_position)

        # what an op's slot cannot hold as a number (see OpStream). Every stream of this preprocessor shares it.
        self.op_refs: List[Any] = []
        # with a table pool: moves the declared tables to it (see relocatable_call, TableMover)
        self.mover: Optional[TableMover] = None if table_pool is None else TableMover(self, table_pool)
        # the emitted ops
        self.result_ops: OpStream = OpStream(
            self.op_refs, self.labels, None if self.mover is None else self.mover.guard
        )
        self.emit_target: OpStream = self.result_ops  # where the ops go now: result_ops, or a moved table's stream

        first_segment: NewSegment = NewSegment(0)
        self.last_new_segment: NewSegment = first_segment
        self.result_ops.append(first_segment)

        self.max_recursion_depth = max_recursion_depth
        # set python's recursion-limit so the preprocessor's own depth-check (at max_recursion_depth)
        #  triggers before python's RecursionError. Set it unconditionally - whether it's higher or lower than the
        #  current limit - so that raising max_recursion_depth actually allows deeper macro-recursion.
        sys.setrecursionlimit(
            PYTHON_CALLS_PER_MACRO_CALL * max_recursion_depth
            + GAP_BETWEEN_PYTHONS_AND_PREPROCESSOR_MACRO_RECURSION_DEPTH
        )

    def patch_last_wflip_address(self) -> None:
        self.last_new_segment.wflip_start_address = self.curr_address

    def emit(self, op: LastPhaseOp) -> None:
        self.emit_target.append(op)

    def finish(self, show_statistics: bool) -> None:
        self.patch_last_wflip_address()
        if self.mover is not None:
            self.mover.refuse_reaching_the_pool(self.curr_address)
            self.mover.place_tables()
        self.insert_macro_start_labels_if_their_address_not_used()
        if show_statistics:
            show_macro_usage_pie_graph(dict(self.macro_code_size), self.curr_address)

    def prepare_macro_call(self, calling_op: Union[MacroCall, RepCall]) -> PreprocessorData._PrepareMacroCall:
        return PreprocessorData._PrepareMacroCall(self.curr_tree, calling_op, self.macros, self.max_recursion_depth)

    def get_result_ops_and_labels(self) -> Tuple[OpsQueue, LabelsDict]:
        return self.result_ops, self.labels

    def insert_segment(self, next_segment_start: int) -> None:
        previous_end = self.curr_address
        self.labels[f'{wflip_start_label}{self.curr_segment_index}'] = previous_end
        self._label_positions.append(None)
        self.curr_segment_index += 1
        self.patch_last_wflip_address()

        new_segment = NewSegment(next_segment_start)
        self.last_new_segment = new_segment
        self.result_ops.append(new_segment)

        self.curr_address = next_segment_start
        if self.mover is not None:
            self.mover.segment_started(previous_end)
        self.aligned(next_segment_start, 0)

    def insert_reserve(self, reserved_bits_size: int) -> None:
        self.curr_address += reserved_bits_size
        if self.mover is not None:
            self.mover.refuse_reaching_the_pool(self.curr_address)
        self.result_ops.append(ReserveBits(self.curr_address))

    def insert_label(self, label: str, code_position: CodePosition, *, address: Optional[int] = None) -> None:
        if address is None:
            address = self.curr_address

        mover = self.mover
        # (a moved table's label waits in the mover until the table is placed)
        if label in self.labels or (mover is not None and label in mover.label_positions):
            other_position = self.label_position(label)
            macro_resolve_error(
                self.curr_tree, f'label declared twice - "{label}" on ' f'{code_position} and {other_position}'
            )
        if mover is not None and mover.resume is not None:
            mover.add_label(label, code_position, address)
            return
        self.declare_label(label, address, code_position)

    def declare_label(self, label: str, address: int, code_position: Optional[CodePosition]) -> None:
        """Add a label that is known to be new."""
        self.labels[label] = address
        self._label_positions.append(code_position)
        if self.save_debug_labels:
            # only insert_macro_start_labels_if_their_address_not_used reads this set
            self.addresses_with_labels.add(address)

    def label_position(self, label: str) -> Optional[CodePosition]:
        """Return where `label` was declared (None if insert_label didn't declare it). Slow: it walks every label, so
        only error messages use it."""
        if self.mover is not None and label in self.mover.label_positions:
            return self.mover.label_positions[label]
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
        if self.save_debug_labels and (self.mover is None or self.mover.resume is None):  # (not in a moved table)
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
        if self.mover is not None:
            self.mover.padding(ops_alignment, ops_to_pad)
        self.curr_address += ops_to_pad * op_size
        self.emit(Padding(ops_to_pad))
        if ops_alignment > 1:
            bits = ops_alignment * op_size
            self.aligned(self.curr_address, (bits & -bits).bit_length() - 1)

    def aligned(self, address: int, bits: int) -> None:
        """Note that `address` is aligned to 2^bits bits by a `pad` (bits is 0 where a segment just starts). The
        mover's guard keeps these (OffsetGuard.pads)."""
        if self.mover is not None and address:
            pads = self.mover.guard.pads
            pads[address] = max(pads.get(address, 0), bits)

    def check_evaluated(self, expr: Expr, value: int, what: str) -> None:
        """Check a number the expansion evaluated from labels: `what` says what it is and where. A table pool must not
        change it (OffsetGuard.check_number)."""
        if self.mover is not None and expr.all_unknown_labels():
            self.mover.check_number(expr, value, what)


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
    preprocessor_data: PreprocessorData, ops: List[Op], pad_index: int, params_dict: Dict[str, Expr], ops_alignment: int
) -> Optional[Tuple[int, int, Optional[Expr], str]]:
    """If the op after the `pad` at `ops[pad_index]` (skipping labels) calls a relocatable table, return (the call's
    index, the table's ops, its group's jump-word Expr, its start label). Else return None.

    The table is one expansion of a macro whose innermost namespace is `__relocatable__`; its first argument is its
    start label. It may hold only labels and `a;b` ops, and no `$` (see _relocatable_table_ops). Its jump word is
    the word of the `wflip` right after the call that disarms the start label.
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
    if ops_alignment & (ops_alignment - 1):
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f'the relocatable table after `pad {ops_alignment}` (at {ops[pad_index].code_position}) needs a '
            f'power-of-two pad: a pool places tables in power-of-two slots',
        )
    group_expr: Optional[Expr] = None
    for after in range(index + 1, len(ops)):
        disarm = ops[after]
        if isinstance(disarm, Label):
            continue
        if isinstance(disarm, WordFlip) and disarm.flip_value.eval_new(params_dict).value == call.arguments[0].value:
            # bind the macro's parameters (`src + w` names the caller's variable); unknown labels
            # stay as names, so two sites on the same variable compare equal
            group_expr = disarm.word_address.eval_new(params_dict)
        break
    return index, table_ops, group_expr, str(call.arguments[0].value)


def _relocatable_table_ops(preprocessor_data: PreprocessorData, macro: Macro, call: MacroCall) -> int:
    """Return how many ops the relocatable `call` expands to, or raise a compilation error (see
    relocatable_call)."""

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


class _TopLevelOps:
    """The top level's ops, as the expansion passes them: reports the progress, and drops the ops already expanded
    when the caller gave the macro tree away (consume_main). It keeps the last non-label op expanded and everything
    after it: that is all a `pad` looks ahead to (relocatable_call)."""

    def __init__(self, ops: List[Op], release: bool, on_progress: Optional[Callable[[float], None]]):
        self.ops = ops
        self.release = release
        self.on_progress = on_progress
        self.released_up_to = 0  # ops[:released_up_to] are None
        self.last_non_label = 0

    def passing(self, op_index: int, op: Op) -> None:
        """Note that the expansion is about to expand `op`, the op at `op_index`."""
        if self.on_progress is not None:
            self.on_progress(op_index / len(self.ops))
        if not self.release:
            return
        if self.last_non_label > self.released_up_to:
            for index in range(self.released_up_to, self.last_non_label):
                self.ops[index] = None  # type: ignore[call-overload]
            self.released_up_to = self.last_non_label
        if not isinstance(op, Label):
            self.last_non_label = op_index


def _sub_path(labels_prefix: str, part: str) -> str:
    """Return the macro-expansion path of `part` (a call, or a rep's name) inside the macro at `labels_prefix`."""
    return (f"{labels_prefix}{MACRO_SEPARATOR_STRING}" if labels_prefix else "") + part


def _refuse_relocatable(preprocessor_data: PreprocessorData, op: Union[MacroCall, RepCall], why: str) -> NoReturn:
    macro_resolve_error(
        preprocessor_data.curr_tree, f'the relocatable table {op.macro_name} (called at {op.code_position}) {why}'
    )


def _expand_pad(
    preprocessor_data: PreprocessorData, ops: List[Op], pad_index: int, params_dict: Dict[str, Expr], labels_prefix: str
) -> Tuple[int, int]:
    """Expand the `pad` at ops[pad_index].
    With a table pool, a `pad` right before a relocatable call aligns a declared table, and the table may go to the
    pool instead of being padded here. Return (the index of that call, the same index if its table is moving), with
    -1 for each that there is none."""
    pad = ops[pad_index]
    assert isinstance(pad, Pad)
    pad = pad.eval_new(params_dict)
    ops_alignment = get_pad_ops_alignment(pad, preprocessor_data)
    preprocessor_data.check_evaluated(
        pad.ops_alignment, ops_alignment, f'the alignment of the pad at {pad.code_position}'
    )
    mover = preprocessor_data.mover
    table = None
    if mover is not None:
        table = relocatable_call(preprocessor_data, ops, pad_index, params_dict, ops_alignment)
        if table is not None and mover.begin(ops_alignment, labels_prefix, *table[1:]):
            return table[0], table[0]
    preprocessor_data.align_current_address(ops_alignment)
    return (-1 if table is None else table[0]), -1


def _expand_macro_call(
    preprocessor_data: PreprocessorData, op: MacroCall, params_dict: Dict[str, Expr], labels_prefix: str, padded: bool
) -> None:
    """Expand a macro call. `padded` says a `pad` right before it found it to be a relocatable table's call."""
    declared = op.macro_name in preprocessor_data.macros  # (if not, prepare_macro_call says so)
    if preprocessor_data.mover is not None and declared and not padded and is_relocatable(op.macro_name):
        _refuse_relocatable(
            preprocessor_data, op, 'needs a `pad` right before its call: the pool places it at that alignment'
        )
    op = op.eval_new(params_dict)
    next_macro_path = _sub_path(labels_prefix, f"{op.code_position.short_str()}:{op.macro_name}")
    with preprocessor_data.prepare_macro_call(op):
        resolve_macro_aux(preprocessor_data, op.macro_name, op.arguments, next_macro_path)


def _expand_rep(
    preprocessor_data: PreprocessorData, op: RepCall, params_dict: Dict[str, Expr], labels_prefix: str
) -> None:
    """Expand a rep: its macro, once per iteration."""
    if preprocessor_data.mover is not None and is_relocatable(op.macro_name):
        _refuse_relocatable(
            preprocessor_data, op, 'is called by rep: each table needs its own `pad` right before its call'
        )
    # Make the rep iterator hygienic: rename it (and its references in the rep's arguments)
    # to a per-expansion-unique name BEFORE substituting params/@-labels, so a caller label
    # named like the iterator (e.g. `d`, `i`) can't be captured by it. The `:rep:` marker
    # keeps it distinct from any @-label global name (`labels_prefix---<name>`, no `:`).
    hygienic_iterator = _sub_path(labels_prefix, f"{op.code_position.short_str()}:rep:{op.iterator_name}")
    op = op.rename_iterator(hygienic_iterator)
    op = op.eval_new(params_dict)
    times = op.repeat_times  # (get_rep_times replaces it with its value)
    rep_times = get_rep_times(op, preprocessor_data)
    preprocessor_data.check_evaluated(times, rep_times, f'the count of the rep at {op.code_position}')
    if rep_times == 0:
        return
    next_macro_path = _sub_path(labels_prefix, f"{op.code_position.short_str()}:rep{{}}:{op.macro_name}")
    with preprocessor_data.prepare_macro_call(op):
        for i in range(rep_times):
            op.current_index = i
            resolve_macro_aux(preprocessor_data, op.macro_name, op.calculate_arguments(i), next_macro_path.format(i))


def _expand_segment(preprocessor_data: PreprocessorData, op: Segment) -> None:
    next_segment_start = get_next_segment_start(op, preprocessor_data)
    preprocessor_data.check_evaluated(
        op.start_address, next_segment_start, f'the address of the segment at {op.code_position}'
    )
    preprocessor_data.insert_segment(next_segment_start)


def _expand_reserve(preprocessor_data: PreprocessorData, op: Reserve) -> None:
    reserved_bits_size = get_reserved_bits_size(op, preprocessor_data)
    preprocessor_data.check_evaluated(
        op.reserved_bit_size, reserved_bits_size, f'the size of the reserve at {op.code_position}'
    )
    preprocessor_data.insert_reserve(reserved_bits_size)


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
    current_macro = preprocessor_data.macros[macro_name]
    mover = preprocessor_data.mover
    dollar_ops = None if mover is None else mover.entering(current_macro)
    params_dict = get_params_dictionary(current_macro, args, current_macro.namespace, labels_prefix)
    preprocessor_data.insert_macro_start_label(labels_prefix, current_macro.code_position)

    ops = current_macro.ops
    top_level = None
    if macro_name is INITIAL_MACRO_NAME:
        top_level = _TopLevelOps(ops, preprocessor_data.consume_main, preprocessor_data.on_progress)
    op_bits = 2 * preprocessor_data.memory_width
    # with a table pool: the relocatable call that the last `pad` aligns, and that call if its table is being moved
    table_call = relocated_call = -1

    for op_index, op in enumerate(ops):
        if top_level is not None:
            top_level.passing(op_index, op)

        if isinstance(op, Label):
            preprocessor_data.insert_label(op.eval_name(params_dict), op.code_position)

        elif isinstance(op, (FlipJump, WordFlip)):
            # Most ops are these, so they are expanded right here, straight into the compact stream: no function
            # call, and no FlipJump/WordFlip object.
            preprocessor_data.curr_address += op_bits
            params_dict['$'] = Expr(preprocessor_data.curr_address)
            if dollar_ops and op_index in dollar_ops:
                assert mover is not None
                mover.dollar(dollar_ops[op_index], labels_prefix, op_index, op.code_position, params_dict)
            if isinstance(op, FlipJump):
                preprocessor_data.emit_target.add_flip_jump(op.flip, op.jump, op.code_position, params_dict)
            else:
                preprocessor_data.emit_target.add_word_flip(
                    op.word_address, op.flip_value, op.return_address, op.code_position, params_dict
                )
            del params_dict['$']

        elif isinstance(op, Pad):
            table_call, relocated_call = _expand_pad(preprocessor_data, ops, op_index, params_dict, labels_prefix)

        elif isinstance(op, MacroCall):
            _expand_macro_call(preprocessor_data, op, params_dict, labels_prefix, op_index == table_call)
            if op_index == relocated_call:
                assert mover is not None
                mover.end()

        elif isinstance(op, RepCall):
            _expand_rep(preprocessor_data, op, params_dict, labels_prefix)

        elif isinstance(op, Segment):
            _expand_segment(preprocessor_data, op.eval_new(params_dict))

        elif isinstance(op, Reserve):
            _expand_reserve(preprocessor_data, op.eval_new(params_dict))

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
    @param table_pool: if given, move the declared tables (see relocatable_call, BlockPool)
    @param save_debug_labels: record the `...---:start:` macro-start labels (only the debugging file reads them)
    @param on_progress: if given, called with the expansion's progress (a fraction in [0, 1))
    @param consume_main: the caller won't use `macros` again, so the top level's ops are dropped as they expand
    @return: tuple of the queue of ops, and the labels' dictionary
    """
    preprocessor_data = PreprocessorData(
        memory_width,
        macros,
        max_recursion_depth,
        save_debug_labels=save_debug_labels,
        table_pool=table_pool,
        consume_main=consume_main,
        on_progress=on_progress,
    )
    resolve_macro_aux(preprocessor_data, INITIAL_MACRO_NAME, INITIAL_ARGS, INITIAL_LABELS_PREFIX)

    preprocessor_data.finish(show_statistics)
    return preprocessor_data.get_result_ops_and_labels()
