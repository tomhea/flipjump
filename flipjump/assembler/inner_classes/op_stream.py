"""
the compact op stream: what the preprocessor (macro resolve) emits and labels-resolve consumes.

One python object per op (and an Expr tree per operand) cost ~250 bytes. This stream keeps an op in
~17-60 bytes:

    kinds: bytearray        - one byte per op (FJ, FJ_POS, WFLIP, PAD, OBJ below)
    vals:  array('q')       - the op's slots, in order (see each kind)
    refs:  list             - what a slot cannot hold as a number: a label name, an Expr, a
                              CodePosition, a NewSegment / ReserveBits. Shared by every stream of
                              one preprocessor, so a run of ops moves between streams by slicing.

An OPERAND slot is either a literal (0 <= v < 2**63) or a deferred reference, ~((r << 2) | tag)
(always negative), naming refs[r]:
    TAG_LABEL       refs[r] is a label name:        the value is labels[name]
    TAG_LABEL_PLUS  refs[r] is a label name, and the NEXT slot holds a constant c (any int64):
                                                    the value is labels[name] + c
    TAG_EXPR        refs[r] is an Expr:             the value is expr.exact_eval(labels)

Every value is the one the Expr would evaluate to, exactly: an operand is folded into a literal only
when it is an int, or a sum of ONE label (coefficient +1) and integer constants whose label is
already declared - labels are never reassigned, so a declared label's value is final (one-pass
blocking's deferred tables declare theirs only when the expansion ends, so a reference to one stays
deferred). A sum of one undeclared label and constants is kept as (label, constant) - integer +/- is
exact, so `((x + 64) + 33) + 3` and `x + 100` evaluate alike. Anything else is kept as its Expr.

An op whose operands are not all literal carries its CodePosition, so an error in labels-resolve
still says "... in op <op>." with the op's position. The op is rebuilt for that message: a folded
sum prints in its canonical form `(x + 100)`; the exception text itself (`Can't evaluate label x in
expression x`) is the one the nested tree raised, since its innermost node is the same label.
"""

from __future__ import annotations

from array import array
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.ops import (
    CodePosition,
    FlipJump,
    NewSegment,
    Padding,
    ReserveBits,
    WordFlip,
)

# op kinds. Slots per kind:
FJ = 0  # flip, jump - both literal
FJ_POS = 1  # flip, jump (operands; at least one deferred), position-ref
WFLIP = 2  # word_address, flip_value, return_address (operands), position-ref
PAD = 3  # ops_count
OBJ = 4  # ref to a NewSegment / ReserveBits (mutable: NewSegment's wflip address is patched later)
FJ_PACKED = 5  # flip << 32 | jump - both literal, flip < 2**31 and jump < 2**32 (the common op: one slot, not two)

PACKED_FLIP_MAX = (1 << 31) - 1
PACKED_JUMP_MAX = (1 << 32) - 1
PACKED_JUMP_MASK = PACKED_JUMP_MAX

TAG_LABEL = 0
TAG_LABEL_PLUS = 1
TAG_EXPR = 2

LITERAL_MAX = (1 << 63) - 1
CONST_MIN = -(1 << 63)

_ADD_SUB = ('+', '-')


_NO_PARAMS: Dict[str, Expr] = {}


def _linear_in(expr: Expr, params: Dict[str, Expr]) -> Optional[Tuple[Optional[str], int]]:
    """(label, constant) if expr.eval_new(params) is a +/- tree of at most one label (coefficient +1) and
    ints, else None - without building the substituted tree: a name is replaced by its params value once
    (as eval_new does - the values are already minimal), and the +/- tree is summed. None also where
    eval_new would fold something else (the caller then takes the eval_new path, so every other operator -
    and its errors - stays eval_new's)."""
    value: Any = expr.value
    if value.__class__ is int:
        return None, value
    if value.__class__ is str:
        substitute = params.get(value)
        if substitute is None:
            return value, 0
        return _linear_memo(substitute)
    op, args = value
    if len(args) != 2 or op not in _ADD_SUB:
        return None
    left = _linear_in(args[0], params)
    if left is None:
        return None
    right = _linear_in(args[1], params)
    if right is None:
        return None
    if op == '+':
        if left[0] is not None and right[0] is not None:
            return None
        return (left[0] if left[0] is not None else right[0]), left[1] + right[1]
    if right[0] is not None:  # `c - x` / `x - y`: not a +1 coefficient
        return None
    return left[0], left[1] - right[1]


# the linear sums of the macro arguments (the params values): an argument such as `va + 2*dw` is referenced by
# many ops of its expansion, so its sum is computed once. Keyed by the Expr object (Expr has identity
# hashing, and the key keeps it alive, so an id cannot be reused); bounded by clearing.
_LINEAR_MEMO: Dict[Expr, Optional[Tuple[Optional[str], int]]] = {}
_LINEAR_MEMO_LIMIT = 1 << 16
_MISSING: Any = object()


def _linear_memo(expr: Expr) -> Optional[Tuple[Optional[str], int]]:
    value: Any = expr.value
    if value.__class__ is int:
        return None, value
    if value.__class__ is str:
        return value, 0
    linear = _LINEAR_MEMO.get(expr, _MISSING)
    if linear is _MISSING:
        if len(_LINEAR_MEMO) >= _LINEAR_MEMO_LIMIT:
            _LINEAR_MEMO.clear()
        linear = _LINEAR_MEMO[expr] = _linear_in(expr, _NO_PARAMS)
    return linear  # type: ignore[no-any-return]


class OpStream:
    """an ordered stream of last-phase ops, stored compactly (see the module docstring)"""

    __slots__ = ('kinds', 'vals', 'refs', 'labels', 'marks')

    def __init__(self, refs: List[Any], labels: Dict[str, int]):
        """
        @param refs: the reference list, shared by every stream of one preprocessor
        @param labels: the preprocessor's labels: a declared label is folded into a literal
        """
        self.kinds = bytearray()
        self.vals = array('q')
        self.refs = refs
        self.labels = labels
        # (op index, vals index, ref) of every OBJ op, in order: flush_table_pool cuts the pool's
        # stream at its NewSegments without decoding it
        self.marks: List[Tuple[int, int, Any]] = []

    def __len__(self) -> int:
        return len(self.kinds)

    # ---- writing ----

    def _operand(self, expr: Expr) -> bool:
        """append expr's slot(s); True if it is deferred (not a literal)"""
        value: Any = expr.value
        vals = self.vals
        if value.__class__ is int:
            if 0 <= value <= LITERAL_MAX:
                vals.append(value)
                return False
        elif value.__class__ is str:
            known = self.labels.get(value)
            if known is not None and 0 <= known <= LITERAL_MAX:
                vals.append(known)
                return False
            refs = self.refs
            vals.append(~(len(refs) << 2 | TAG_LABEL))
            refs.append(value)
            return True
        else:
            linear = _linear_in(expr, _NO_PARAMS)
            if linear is not None and linear[0] is not None and self._label_plus(linear[0], linear[1]):
                return linear[0] not in self.labels
        refs = self.refs
        vals.append(~(len(refs) << 2 | TAG_EXPR))
        refs.append(expr)
        return True

    def _label_plus(self, name: str, const: int) -> bool:
        """append the slot(s) of `labels[name] + const`; False (nothing appended) if it does not fit"""
        vals = self.vals
        known = self.labels.get(name)
        if known is not None:
            if 0 <= known + const <= LITERAL_MAX:
                vals.append(known + const)
                return True
            return False
        if CONST_MIN <= const <= LITERAL_MAX:
            refs = self.refs
            if const == 0:
                vals.append(~(len(refs) << 2 | TAG_LABEL))
            else:
                vals.append(~(len(refs) << 2 | TAG_LABEL_PLUS))
                vals.append(const)
            refs.append(name)
            return True
        return False

    def _operand_in(self, expr: Expr, params: Dict[str, Expr]) -> bool:
        """_operand(expr.eval_new(params)) - the same slots - building no Expr for a +/- operand.
        (The hot path of the expansion: the common shapes are spelled out.)"""
        value: Any = expr.value
        if value.__class__ is str:
            substitute = params.get(value)
            if substitute is None:
                name, const = value, 0
            else:
                value = substitute.value
                if value.__class__ is int:
                    if 0 <= value <= LITERAL_MAX:
                        self.vals.append(value)
                        return False
                    return self._operand(substitute)
                if value.__class__ is str:
                    name, const = value, 0
                else:
                    linear = _linear_memo(substitute)
                    if linear is None or linear[0] is None:
                        return self._operand(substitute)
                    name, const = linear  # type: ignore[assignment]
        elif value.__class__ is int:
            if 0 <= value <= LITERAL_MAX:
                self.vals.append(value)
                return False
            return self._operand(expr)
        else:
            linear = _linear_in(expr, params)
            if linear is None:
                return self._operand(expr.eval_new(params))
            name, const = linear  # type: ignore[assignment]
            if name is None:
                if 0 <= const <= LITERAL_MAX:
                    self.vals.append(const)
                    return False
                return self._operand(expr.eval_new(params))
        known = self.labels.get(name)
        if known is not None:
            known += const
            if 0 <= known <= LITERAL_MAX:
                self.vals.append(known)
                return False
        elif CONST_MIN <= const <= LITERAL_MAX:
            refs = self.refs
            if const == 0:
                self.vals.append(~(len(refs) << 2 | TAG_LABEL))
            else:
                self.vals.append(~(len(refs) << 2 | TAG_LABEL_PLUS))
                self.vals.append(const)
            refs.append(name)
            return True
        return self._operand(expr.eval_new(params))

    def add_flip_jump_in(self, flip: Expr, jump: Expr, params: Dict[str, Expr], code_position: CodePosition) -> None:
        """add_flip_jump(flip.eval_new(params), jump.eval_new(params), code_position), the same slots"""
        deferred = self._operand_in(flip, params)
        deferred = self._operand_in(jump, params) or deferred
        self._end_flip_jump(deferred, code_position)

    def _end_flip_jump(self, deferred: bool, code_position: CodePosition) -> None:
        """the op's operand slots were just appended: end it as FJ_POS (with its position) or as a literal"""
        if deferred:
            self.kinds.append(FJ_POS)
            self.vals.append(len(self.refs))
            self.refs.append(code_position)
        else:
            self._literal_flip_jump()

    def _end_word_flip(self, code_position: CodePosition) -> None:
        """the op's three operands were just appended: end it with its position"""
        self.kinds.append(WFLIP)
        self.vals.append(len(self.refs))
        self.refs.append(code_position)

    def _literal_flip_jump(self) -> None:
        """the op's two literal slots were just appended: one FJ_PACKED slot instead, where they fit"""
        vals = self.vals
        jump = vals[-1]
        flip = vals[-2]
        if flip <= PACKED_FLIP_MAX and jump <= PACKED_JUMP_MAX:
            vals.pop()
            vals[-1] = flip << 32 | jump
            self.kinds.append(FJ_PACKED)
        else:
            self.kinds.append(FJ)

    def add_word_flip_in(
        self,
        word_address: Expr,
        flip_value: Expr,
        return_address: Expr,
        params: Dict[str, Expr],
        code_position: CodePosition,
    ) -> None:
        """add_word_flip of the three operands' eval_new(params), the same slots"""
        self._operand_in(word_address, params)
        self._operand_in(flip_value, params)
        self._operand_in(return_address, params)
        self._end_word_flip(code_position)

    def add_flip_jump(self, flip: Expr, jump: Expr, code_position: CodePosition) -> None:
        f: Any = flip.value
        j: Any = jump.value
        if f.__class__ is int and j.__class__ is int and 0 <= f <= LITERAL_MAX and 0 <= j <= LITERAL_MAX:
            self.vals.append(f)
            self.vals.append(j)
            self._literal_flip_jump()
            return
        deferred = self._operand(flip)
        deferred = self._operand(jump) or deferred
        self._end_flip_jump(deferred, code_position)

    def add_word_flip(
        self, word_address: Expr, flip_value: Expr, return_address: Expr, code_position: CodePosition
    ) -> None:
        self._operand(word_address)
        self._operand(flip_value)
        self._operand(return_address)
        self._end_word_flip(code_position)

    def add_padding(self, ops_count: int) -> None:
        self.kinds.append(PAD)
        self.vals.append(ops_count)

    def add_object(self, op: Union[NewSegment, ReserveBits]) -> None:
        self.marks.append((len(self.kinds), len(self.vals), op))
        self.kinds.append(OBJ)
        self.vals.append(len(self.refs))
        self.refs.append(op)

    def append(self, op: Any) -> None:
        """append any last-phase op object (the deque-compatible entry point)"""
        if isinstance(op, FlipJump):
            self.add_flip_jump(op.flip, op.jump, op.code_position)
        elif isinstance(op, WordFlip):
            self.add_word_flip(op.word_address, op.flip_value, op.return_address, op.code_position)
        elif isinstance(op, Padding):
            self.add_padding(op.ops_count)
        elif isinstance(op, (NewSegment, ReserveBits)):
            self.add_object(op)
        else:
            raise TypeError(f"not a last-phase op: {op!r}")

    def extend_slice(
        self, other: OpStream, op_start: int, op_end: int, val_start: int, val_end: int, mark_start: int, mark_end: int
    ) -> None:
        """append other's ops [op_start, op_end) - whose slots are other.vals[val_start:val_end], and whose
        OBJ ops are other.marks[mark_start:mark_end] - to this stream. Both must share `refs` (a reference
        is an index into it)."""
        assert other.refs is self.refs
        base_ops = len(self.kinds)
        base_vals = len(self.vals)
        for op_index, val_index, ref in other.marks[mark_start:mark_end]:
            self.marks.append((base_ops + op_index - op_start, base_vals + val_index - val_start, ref))
        self.kinds += other.kinds[op_start:op_end]
        self.vals += other.vals[val_start:val_end]

    def clear(self) -> None:
        del self.kinds[:]
        del self.vals[:]
        self.marks.clear()

    # ---- reading ----

    def first_object(self) -> Any:
        """the first op if it is an OBJ (the program's first NewSegment), else None"""
        if not self.kinds or self.kinds[0] != OBJ:
            return None
        return self.refs[self.vals[0]]

    def rebuild_operand(self, slots: Iterator[int]) -> Expr:
        """the Expr of the operand whose slots come next in `slots` (for messages and iteration)"""
        slot = next(slots)
        if slot >= 0:
            return Expr(slot)
        encoded = ~slot
        ref = self.refs[encoded >> 2]
        tag = encoded & 3
        if tag == TAG_LABEL:
            return Expr(ref)
        if tag == TAG_LABEL_PLUS:
            return Expr(('+', (Expr(ref), Expr(next(slots)))))
        return ref  # type: ignore[no-any-return]

    def iter_ops(self, start_op: int = 0) -> Iterator[Any]:
        """rebuild the ops as objects, from op `start_op` on (slow; for messages, debugging and tests).
        A literal FlipJump keeps no position: it is rebuilt with code_position None."""
        slots = iter(self.vals)
        refs = self.refs
        for index, kind in enumerate(self.kinds):
            if kind == FJ_PACKED:
                packed = next(slots)
                op: Any = FlipJump(Expr(packed >> 32), Expr(packed & PACKED_JUMP_MASK), None)  # type: ignore[arg-type]
            elif kind == FJ:
                op = FlipJump(Expr(next(slots)), Expr(next(slots)), None)  # type: ignore[arg-type]
            elif kind == FJ_POS:
                flip = self.rebuild_operand(slots)
                jump = self.rebuild_operand(slots)
                op = FlipJump(flip, jump, refs[next(slots)])
            elif kind == WFLIP:
                word_address = self.rebuild_operand(slots)
                flip_value = self.rebuild_operand(slots)
                return_address = self.rebuild_operand(slots)
                op = WordFlip(word_address, flip_value, return_address, refs[next(slots)])
            elif kind == PAD:
                op = Padding(next(slots))
            else:
                op = refs[next(slots)]
            if index >= start_op:
                yield op

    def op_at(self, index: int) -> Any:
        """the op at `index`, rebuilt (slow: decodes the stream up to it)"""
        return next(self.iter_ops(index))

    def __iter__(self) -> Iterator[Any]:
        return self.iter_ops()
