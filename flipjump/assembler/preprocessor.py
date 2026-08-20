"""
the preprocessor (macro-resolution stage).
expands macro calls and rep (repetition) blocks recursively, starting from the main
macro, and produces the flat op-list that the assembler then resolves into addresses.
"""

from __future__ import annotations

import collections
import sys
from typing import Dict, Tuple, Iterable, Union, Deque, Set, List, Optional, NoReturn

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
    INITIAL_MACRO_NAME,
    INITIAL_ARGS,
    INITIAL_LABELS_PREFIX,
)

CurrTree = Deque[Union[MacroCall, RepCall]]
OpsQueue = Deque[LastPhaseOp]
LabelsDict = Dict[str, int]

wflip_start_label = '_.wflip_area_start_'


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
        # PERF (doom-flipjump, 2026-08-20): __slots__, derived from this class's own __init__.
        # One of these is allocated per MACRO EXPANSION -- several million on the doom-flipjump
        # program -- purely to hold four references across a `with` block.
        # (A further step is possible and deliberately NOT taken here: the object could be dropped
        # altogether by inlining enter/exit as a try/finally at the two call sites, since __exit__
        # only pops curr_tree. That trades this class's encapsulation for the allocation, and is
        # worth measuring separately rather than folding into a __slots__ change.)
        __slots__ = ('curr_tree', 'calling_op', 'macros', 'max_recursion_depth',)

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
    ):
        """
        @param memory_width: the memory-width
        @param macros: parser's result; the dictionary from the macro names to the macro declaration
        @param max_recursion_depth: The compiler supports macros that recursively uses other macros,
        up to the specified recursion depth.
        @param save_debug_labels: whether to record the per-expansion `...---:start:` macro-start
        labels. See insert_macro_start_label for why they can be skipped.
        """
        self.memory_width = memory_width
        self.macros = macros
        self.save_debug_labels = save_debug_labels

        self.curr_address: int = 0

        self.macro_code_size: Dict[str, int] = collections.defaultdict(lambda: 0)

        self.curr_tree: CurrTree = collections.deque()

        self.curr_segment_index: int = 0
        self.labels_code_positions: Dict[str, CodePosition] = {}

        self.result_ops: Deque[LastPhaseOp] = collections.deque()
        self.labels: Dict[str, int] = {}
        self.addresses_with_labels: Set[int] = set()
        self.macro_start_labels: List[Tuple[int, str, CodePosition]] = []  # (address, label, code_position)

        first_segment: NewSegment = NewSegment(0)
        self.last_new_segment: NewSegment = first_segment
        self.result_ops.append(first_segment)

        self.max_recursion_depth = max_recursion_depth
        # set python's recursion-limit so the preprocessor's own depth-check (at max_recursion_depth)
        #  triggers before python's RecursionError. Set it unconditionally - whether it's higher or lower than the
        #  current limit - so that raising max_recursion_depth actually allows deeper macro-recursion.
        sys.setrecursionlimit(max_recursion_depth + GAP_BETWEEN_PYTHONS_AND_PREPROCESSOR_MACRO_RECURSION_DEPTH)

    def patch_last_wflip_address(self) -> None:
        self.last_new_segment.wflip_start_address = self.curr_address

    def finish(self, show_statistics: bool) -> None:
        self.patch_last_wflip_address()
        self.insert_macro_start_labels_if_their_address_not_used()
        if show_statistics:
            show_macro_usage_pie_graph(dict(self.macro_code_size), self.curr_address)

    def prepare_macro_call(self, calling_op: Union[MacroCall, RepCall]) -> PreprocessorData._PrepareMacroCall:
        return PreprocessorData._PrepareMacroCall(self.curr_tree, calling_op, self.macros, self.max_recursion_depth)

    def get_result_ops_and_labels(self) -> Tuple[OpsQueue, LabelsDict]:
        return self.result_ops, self.labels

    def insert_segment(self, next_segment_start: int) -> None:
        self.labels[f'{wflip_start_label}{self.curr_segment_index}'] = self.curr_address
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

        if label in self.labels:
            other_position = self.labels_code_positions[label]
            macro_resolve_error(
                self.curr_tree, f'label declared twice - "{label}" on ' f'{code_position} and {other_position}'
            )
        self.labels_code_positions[label] = code_position
        self.labels[label] = address
        if self.save_debug_labels:
            # only insert_macro_start_labels_if_their_address_not_used reads this set
            self.addresses_with_labels.add(address)

    def insert_macro_start_label(self, labels_prefix: str, code_position: CodePosition) -> None:
        """Record that a macro expansion starts here, so a `<path>---:start:` label can name it.

        ⚠ THESE LABELS ARE UNREACHABLE FROM FJ SOURCE, exactly like `:wflips:N`:
        STARTING_LABEL_IN_MACROS_STRING is ':start:' and the lexer's identifier rule is
        `[a-zA-Z_][a-zA-Z_0-9]*`, so no Expr can ever name one. They exist for the debugging file
        and for macro-trace readability. MEASURED (doom-flipjump, 2026-08-20): that program does
        1.69M macro expansions, so this is 1.69M long f-strings and 1.69M list entries, plus a
        set of every labelled address, built for a debugging file that is never requested.

        @note must be called at the start of the expansion.
        @note takes the PREFIX, not the finished label: when labels are off the string is never
        built at all, which is where most of the saving is.
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
        self.result_ops.append(Padding(ops_to_pad))


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

    # PERF: the `namespace.name` keys are a property of the macro DECLARATION, so they are built
    # once in Macro.__post_init__ rather than re-formatted on every expansion. `namespaced_params`
    # is empty for a macro with no namespace, which is why the `if namespace` test is gone.
    for namespaced_name, name in current_macro.namespaced_params:
        params_dict[namespaced_name] = params_dict[name]

    return params_dict


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
    params_dict = get_params_dictionary(current_macro, args, current_macro.namespace, labels_prefix)

    preprocessor_data.insert_macro_start_label(labels_prefix, current_macro.code_position)

    for op in current_macro.ops:
        if isinstance(op, Label):
            preprocessor_data.insert_label(op.eval_name(params_dict), op.code_position)

        elif isinstance(op, FlipJump) or isinstance(op, WordFlip):
            preprocessor_data.curr_address += 2 * preprocessor_data.memory_width
            params_dict['$'] = Expr(preprocessor_data.curr_address)
            preprocessor_data.result_ops.append(op.eval_new(params_dict))
            del params_dict['$']

        elif isinstance(op, Pad):
            op = op.eval_new(params_dict)
            ops_alignment = get_pad_ops_alignment(op, preprocessor_data)
            preprocessor_data.align_current_address(ops_alignment)

        elif isinstance(op, MacroCall):
            op = op.eval_new(params_dict)
            next_macro_path = (
                f"{labels_prefix}{MACRO_SEPARATOR_STRING}" if labels_prefix else ""
            ) + f"{op.code_position.short_str()}:{op.macro_name}"
            with preprocessor_data.prepare_macro_call(op):
                resolve_macro_aux(preprocessor_data, op.macro_name, op.arguments, next_macro_path)

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

        elif isinstance(op, Reserve):
            op = op.eval_new(params_dict)
            reserved_bits_size = get_reserved_bits_size(op, preprocessor_data)
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
) -> Tuple[OpsQueue, LabelsDict]:
    """
    unwind the macro tree to a serialized-queue of ops,
    and creates a dictionary from label's name to its address.
    @param memory_width: the memory-width
    @param macros: parser's result; the dictionary from the macro names to the macro declaration
    @param show_statistics: if True then prints the macro-usage statistics
    @param max_recursion_depth: The compiler supports macros that recursively uses other macros,
    up to the specified recursion depth.
    @param save_debug_labels: record the per-expansion `...---:start:` macro-start labels. They are
    unreachable from fj source (see PreprocessorData.insert_macro_start_label), so a caller that is
    not writing a debugging file can pass False. It cannot change the emitted .fjm.
    @return: tuple of the queue of ops, and the labels' dictionary
    """
    preprocessor_data = PreprocessorData(
        memory_width, macros, max_recursion_depth, save_debug_labels=save_debug_labels
    )
    resolve_macro_aux(preprocessor_data, INITIAL_MACRO_NAME, INITIAL_ARGS, INITIAL_LABELS_PREFIX)

    preprocessor_data.finish(show_statistics)
    return preprocessor_data.get_result_ops_and_labels()
