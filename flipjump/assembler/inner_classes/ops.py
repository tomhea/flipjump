"""
the op (operation) classes.
the operations produced by the parser and consumed by the assembler - FlipJump, WordFlip,
Label, Macro, MacroCall, rep, segment/reserve directives, and the final-phase ops -
together with their flipjump source-code positions.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from typing import Union, Dict, Set, List, Tuple, Any

from flipjump.utils.exceptions import FlipJumpExprException
from flipjump.assembler.inner_classes.expr import Expr


@dataclass(slots=True)
class CodePosition:
    """
    A position in the .fj files.
    """

    file: str
    file_short_name: str  # shortened file name. usually s1,s2,... for stl, and f1,f2,... for the rest.
    line: int

    def __str__(self) -> str:
        return f"file {self.file} (line {self.line})"

    def short_str(self) -> str:
        return f"{self.file_short_name}:l{self.line}"

    def __repr__(self) -> str:
        return f"{os.path.basename(self.file)}:{self.line}"


@dataclass(slots=True, eq=False)
class MacroName:
    """
    Unique for every macro definition.
    """

    name: str
    parameter_num: int = 0
    _hash: int = dataclasses.field(init=False, repr=False)  # of (name, parameter_num), computed once

    def __post_init__(self) -> None:
        self._hash = hash((self.name, self.parameter_num))

    def __str__(self) -> str:
        if 0 == self.parameter_num:
            return self.name
        return f"{self.name}({self.parameter_num})"

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, MacroName):
            return self.name == other.name and self.parameter_num == other.parameter_num
        return False

    def __repr__(self) -> str:
        return str(self)


# one MacroName per (name, parameter_num): a program has millions of macro calls
_macro_names: Dict[Tuple[str, int], MacroName] = {}


def get_macro_name(name: str, parameter_num: int) -> MacroName:
    """the shared MacroName for (name, parameter_num)"""
    key = (name, parameter_num)
    shared = _macro_names.get(key)
    if shared is None:
        shared = _macro_names[key] = MacroName(name, parameter_num)
    return shared


# The macro that holds the ops that are outside any macro.
INITIAL_MACRO_NAME = MacroName('')
INITIAL_ARGS: List[Expr] = []
INITIAL_LABELS_PREFIX = ''


@dataclass(slots=True, eq=False)
class FlipJump:
    """
    The python representation of the "flip; jump" fj-assembly op.
    """

    flip: Expr
    jump: Expr
    code_position: CodePosition

    def __str__(self) -> str:
        return f"Flip: {self.flip}, Jump: {self.jump}, at {self.code_position}"

    def eval_new(self, labels_dict: Dict[str, Expr]) -> FlipJump:
        flip, jump = self.flip.eval_new(labels_dict), self.jump.eval_new(labels_dict)
        if flip is self.flip and jump is self.jump:
            return self  # FlipJump ops are immutable - share them (data ops dominate LUT programs)
        return FlipJump(flip, jump, self.code_position)

    def all_unknown_labels(self) -> Set[str]:
        return {label for expr in (self.flip, self.jump) for label in expr.all_unknown_labels()}


@dataclass(slots=True, eq=False)
class WordFlip:
    """
    The python representation of the "wflip address, value [, return_address]" fj-assembly op.
    """

    word_address: Expr
    flip_value: Expr
    return_address: Expr
    code_position: CodePosition

    def __str__(self) -> str:
        return (
            f"Flip Word {self.word_address} by {self.flip_value}, and return to {self.return_address}. "
            f"at {self.code_position}"
        )

    def eval_new(self, labels_dict: Dict[str, Expr]) -> WordFlip:
        word_address = self.word_address.eval_new(labels_dict)
        flip_value = self.flip_value.eval_new(labels_dict)
        return_address = self.return_address.eval_new(labels_dict)
        if (
            word_address is self.word_address
            and flip_value is self.flip_value
            and return_address is self.return_address
        ):
            return self  # WordFlip ops are immutable - share them
        return WordFlip(word_address, flip_value, return_address, self.code_position)

    def all_unknown_labels(self) -> Set[str]:
        return {
            label
            for expr in (self.word_address, self.flip_value, self.return_address)
            for label in expr.all_unknown_labels()
        }


@dataclass(slots=True, eq=False)
class Pad:
    """
    The python representation of the "pad ops_alignment" fj-assembly op.
    """

    ops_alignment: Expr
    code_position: CodePosition

    def __str__(self) -> str:
        return f"Pad {self.ops_alignment} ops, at {self.code_position}"

    def eval_new(self, labels_dict: Dict[str, Expr]) -> Pad:
        return Pad(self.ops_alignment.eval_new(labels_dict), self.code_position)

    def all_unknown_labels(self) -> Set[str]:
        return self.ops_alignment.all_unknown_labels()

    def calculate_ops_alignment(self, labels: Dict[str, int]) -> int:
        try:
            return self.ops_alignment.exact_eval(labels)
        except FlipJumpExprException as e:
            raise FlipJumpExprException(f"Can't calculate pad ops_alignment on {self.code_position}") from e


@dataclass(slots=True, eq=False)
class Segment:
    """
    The python representation of the "segment start_address" fj-assembly op.
    """

    start_address: Expr
    code_position: CodePosition

    def __str__(self) -> str:
        return f"Segment {self.start_address}, at {self.code_position}"

    def eval_new(self, labels_dict: Dict[str, Expr]) -> Segment:
        return Segment(self.start_address.eval_new(labels_dict), self.code_position)

    def all_unknown_labels(self) -> Set[str]:
        return {label for label in self.start_address.all_unknown_labels()}

    def calculate_address(self, labels: Dict[str, int]) -> int:
        try:
            return self.start_address.exact_eval(labels)
        except FlipJumpExprException as e:
            raise FlipJumpExprException(f"Can't calculate segment address on {self.code_position}") from e


@dataclass(slots=True, eq=False)
class Reserve:
    """
    The python representation of the "reserve bit_size" fj-assembly op.
    """

    reserved_bit_size: Expr
    code_position: CodePosition

    def __str__(self) -> str:
        return f"Reserve {self.reserved_bit_size}, at {self.code_position}"

    def eval_new(self, labels_dict: Dict[str, Expr]) -> Reserve:
        return Reserve(self.reserved_bit_size.eval_new(labels_dict), self.code_position)

    def all_unknown_labels(self) -> Set[str]:
        return {label for label in self.reserved_bit_size.all_unknown_labels()}

    def calculate_reserved_bit_size(self, labels: Dict[str, int]) -> int:
        try:
            return self.reserved_bit_size.exact_eval(labels)
        except FlipJumpExprException as e:
            raise FlipJumpExprException(f"Can't calculate reserved bits size on {self.code_position}") from e


@dataclass(slots=True, eq=False, init=False)
class MacroCall:
    """
    The python representation of the "macro-call [args...]" fj-assembly op.
    """

    macro_name: MacroName
    arguments: List[Expr]
    code_position: CodePosition

    def __init__(self, macro_name: str, arguments: List[Expr], code_position: CodePosition):
        self.macro_name = get_macro_name(macro_name, len(arguments))
        self.arguments = arguments
        self.code_position = code_position

    def __str__(self) -> str:
        return f"macro call. {self.macro_name.name} {', '.join(map(str, self.arguments))}. at {self.code_position}"

    def eval_new(self, labels_dict: Dict[str, Expr]) -> MacroCall:
        return MacroCall(
            self.macro_name.name, [arg.eval_new(labels_dict) for arg in self.arguments], self.code_position
        )

    def all_unknown_labels(self) -> Set[str]:
        return {label for expr in self.arguments for label in expr.all_unknown_labels()}

    def trace_str(self) -> str:
        return f'macro {self.macro_name} ({self.code_position})'


@dataclass(slots=True, eq=False, init=False)
class RepCall:
    """
    The python representation of the "rep(n, i) macro_call [args...]" fj-assembly op.
    """

    current_index: int  # the iteration being expanded (set by the preprocessor)
    repeat_times: Expr
    iterator_name: str
    source_iterator_name: str  # the user-visible iterator name, kept for error traces even after a hygienic rename
    macro_name: MacroName
    arguments: List[Expr]
    code_position: CodePosition

    def __init__(
        self,
        repeat_times: Expr,
        iterator_name: str,
        macro_name: str,
        arguments: List[Expr],
        code_position: CodePosition,
    ):
        self.current_index = 0
        self.repeat_times = repeat_times
        self.iterator_name = iterator_name
        self.source_iterator_name = iterator_name
        self.macro_name = get_macro_name(macro_name, len(arguments))
        self.arguments = arguments
        self.code_position = code_position

    def __str__(self) -> str:
        return (
            f"rep call. rep({self.repeat_times}, {self.iterator_name}) {self.macro_name.name} "
            f"{', '.join(map(str, self.arguments))}. at {self.code_position}"
        )

    def eval_new(self, labels_dict: Dict[str, Expr]) -> RepCall:
        evaluated = RepCall(
            self.repeat_times.eval_new(labels_dict),
            self.iterator_name,
            self.macro_name.name,
            [expr.eval_new(labels_dict) for expr in self.arguments],
            self.code_position,
        )
        evaluated.source_iterator_name = self.source_iterator_name  # keep the source name for traces
        return evaluated

    def rename_iterator(self, new_iterator_name: str) -> RepCall:
        """
        Hygienic rename: return a new RepCall whose iterator is `new_iterator_name`, with every
        reference to the old iterator inside the rep's arguments rewritten to the new name.
        Must run BEFORE eval_new(params_dict) so the iterator name is unique per macro-expansion and
        can never be captured by - or capture - a macro parameter / @-label of the same source name.
        repeat_times is left untouched: the repeat count cannot reference the iterator.
        """
        rename_dict = {self.iterator_name: Expr(new_iterator_name)}
        renamed = RepCall(
            self.repeat_times,
            new_iterator_name,
            self.macro_name.name,
            [expr.eval_new(rename_dict) for expr in self.arguments],
            self.code_position,
        )
        renamed.source_iterator_name = self.source_iterator_name  # keep the source name for traces
        return renamed

    def all_unknown_labels(self) -> Set[str]:
        times = self.repeat_times
        arguments = self.arguments
        arguments_labels = set(label for e in arguments for label in e.all_unknown_labels())
        return times.all_unknown_labels() | (arguments_labels - {self.iterator_name})

    def calculate_times(self, labels: Dict[str, int]) -> int:
        try:
            times = self.repeat_times.exact_eval(labels)
            self.repeat_times = Expr(times)
            return times
        except FlipJumpExprException as e:
            raise FlipJumpExprException(f"Can't calculate rep times on {self.code_position}") from e

    def calculate_arguments(self, iterator_value: int) -> Tuple[Expr, ...]:
        iterator_dict = {self.iterator_name: Expr(iterator_value)}
        try:
            return tuple(expr.eval_new(iterator_dict) for expr in self.arguments)
        except FlipJumpExprException as e:
            raise FlipJumpExprException(f"Can't calculate rep arguments on {self.code_position}") from e

    def trace_str(self) -> str:
        """
        @note assumes calculate_times successfully called before
        """
        return (
            f'rep({self.source_iterator_name}={self.current_index}, out of 0..{int(self.repeat_times)-1}) '
            f'macro {self.macro_name}  ({self.code_position})'
        )


@dataclass(slots=True, eq=False)
class Label:
    """
    The python representation of the "label:" fj-assembly op.
    """

    name: str
    code_position: CodePosition

    def __str__(self) -> str:
        return f'Label "{self.name}:", at {self.code_position}'

    def eval_name(self, labels_dict: Dict[str, Expr]) -> str:
        if self.name in labels_dict:
            new_name = labels_dict[self.name].value
            if isinstance(new_name, str):
                return new_name
            raise FlipJumpExprException(
                f'Bad label swap (from {self.name} to {labels_dict[self.name]}) in {self.code_position}.'
            )
        return self.name


def get_used_labels(ops: List[Op]) -> Set[str]:
    used_labels = set()
    for op in ops:
        if not isinstance(op, Label):
            used_labels.update(op.all_unknown_labels())
    return used_labels


def get_declared_labels(ops: List[Op]) -> Set[str]:
    return set(op.name for op in ops if isinstance(op, Label))


# The input for the preprocessor
Op = Union[FlipJump, WordFlip, Pad, Label, MacroCall, RepCall, Segment, Reserve]


@dataclasses.dataclass
class Macro:
    """
    The python representation of a .fj macro (macro declaration).
    """

    params: List[str]
    local_params: List[str]
    ops: List[Op]
    namespace: str
    code_position: CodePosition

    # [(namespace.name, name)] of every param, built once per macro, not per expansion
    namespaced_params: List[Tuple[str, str]] = dataclasses.field(init=False, repr=False, default_factory=list)

    def __post_init__(self) -> None:
        if self.namespace:
            namespace = self.namespace
            self.namespaced_params = [(f'{namespace}.{name}', name) for name in (*self.params, *self.local_params)]

    def __repr__(self) -> str:
        return f'{self.namespace}.MACRO({", ".join(self.params)})  ({repr(self.code_position)})'


WFLIP_NOT_INSERTED_YET = -1


@dataclass(slots=True, eq=False)
class NewSegment:
    """
    The python expressions-resolved (all compilation data is known) representation
     of the "segment start_address" fj-assembly op.
    """

    start_address: int  # the first address of the new segment
    # a stub, to be resolved later with the start of the wflip area address
    wflip_start_address: int = WFLIP_NOT_INSERTED_YET


@dataclass(slots=True, eq=False)
class ReserveBits:
    """
    The python expressions-resolved (all compilation data is known) representation
     of the "reserve bit_size" fj-assembly op.
    """

    first_address_after_reserved: int  # the address right after the reserved "segment"


@dataclass(slots=True, eq=False)
class Padding:
    """
    The python expressions-resolved (all compilation data is known) representation
     of the "pad ops_alignment" fj-assembly op.
    """

    ops_count: int  # the number of fj-ops to pad


# The input to the labels-resolve
LastPhaseOp = Union[FlipJump, WordFlip, Padding, NewSegment, ReserveBits]
