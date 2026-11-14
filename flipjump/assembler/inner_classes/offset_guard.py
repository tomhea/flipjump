"""
The offset guard: keeps every operand pointing at the same place once a table pool moves tables (see OffsetGuard).

Words used here (the pool's own words are in table_pool.py):
- anchor: the address a moved table left.
- piece: the anchors cut the inline code into pieces.
- stranded: a stranded label was declared at an anchor before its table moved out.
"""

from __future__ import annotations

import bisect
from typing import Callable, Dict, List, NoReturn, Optional, Set, Tuple

from flipjump.utils.exceptions import FlipJumpAssemblerException, FlipJumpExprException, FlipJumpPreprocessorException
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.op_stream import linear_parts

LabelsDict = Dict[str, int]


def reached_the_pool(address: int, pool_start: int) -> FlipJumpPreprocessorException:
    return FlipJumpPreprocessorException(
        f'table-placement: the program reached {hex(address)}, which is inside the table pool at {hex(pool_start)}. '
        f'Raise pool_base, or relocate fewer tables.'
    )


class OffsetGuard:
    """A guard that keeps every operand pointing at the same place once the tables move.

    A moved table leaves the inline stream at its anchor, and the code after it moves up. The anchors, the `pad`s that
    now pad differently, and the segment starts cut the inline stream into pieces. A stranded label belongs to the
    piece before its anchor.

    Each +/- sum in an operand (linear_parts) must keep its meaning:
    - a moved table's label plus a constant must stay inside that table;
    - an inline label plus a constant must stay inside its piece;
    - labels whose distance is taken must lie in one piece;
    - an address computed from labels must not land in the pool.
    `$` counts as an inline label at the next op, and an op that falls through (`a;`) reaches the next op. Inside
    another operation, each +/- sum is checked on its own (`L + 15*dw` in `(L + 15*dw) | 0`). An inline label times a
    number other than 1 (`2 * L`) is an address used as a number, and is not checked; a moved table's label is refused.

    The preprocessor checks the operands whose labels it knows; labels-resolve checks the rest. An operand that reaches
    past the code emitted so far is pending: a table about to move out of a place it reaches over stays inline
    (may_move). Any other violation refuses the program (`refuse`), and `where()` says where the op is.
    """

    def __init__(
        self,
        inline_end: int,
        labels: LabelsDict,
        *,
        where: Callable[[], str] = lambda: '',
        emitting: Optional[Callable[[], int]] = None,
    ):
        """
        @param where: returns where the op being checked is, for a refusal's message
        @param emitting: returns the next inline address the preprocessor emits at. None once every table has moved.
        """
        self.inline_end = inline_end  # the pool's start: the inline stream lies below it, the moved tables above
        self.labels = labels  # the preprocessor's labels (a moved table's labels join them once it is placed)
        self.anchors: List[int] = []  # the addresses where the inline stream is cut, ascending
        # for each anchor: how a refusal describes it, and whether a segment starts there
        self.cuts: List[Tuple[str, bool]] = []
        self.moved_tables: List[str] = []  # the moved tables' start labels, in the order they moved
        # stranded label -> (its anchor, how many tables had moved out of that anchor before it)
        self.stranded: Dict[str, Tuple[int, int]] = {}
        self.watched: Set[str] = set()  # labels checked even with no offset: the stranded ones, and the `$` labels
        self.falls_to = -1  # the address the last op that falls through (`a;`) falls to
        # pending operands, reaching past the code emitted so far: (label address, target, label)
        self._pending: List[Tuple[int, int, str]] = []
        self.tables: List[Tuple[int, int, str]] = []  # the placed tables' (start, end, start label), by start
        self.where = where
        self.emitting = emitting
        # inline address -> log2 of the alignment (in bits) a `pad` gave it; 0 where a segment starts
        self.pads: Dict[int, int] = {}
        self.reach = 0  # the highest address an operand checked so far lands at

    def resolving(self, where: Callable[[], str]) -> None:
        """Start the labels-resolve phase. Every table has moved by now, so an operand that reaches over one is
        refused."""
        self.where = where
        self.emitting = None

    def may_move(self, anchor: int) -> Optional[str]:
        """Return why the table about to move out of `anchor` must stay inline, or None if it may move."""
        if self.falls_to == anchor:
            return 'the op before it falls through to it'
        if any(address <= anchor <= target for address, target, _ in self._pending):
            return 'an operand reaches over its place'
        return None

    def table_moved(self, anchor: int, table: str) -> None:
        self._cut(anchor, f'the place of the table {table}, which moved out of {hex(anchor)}', False)
        self.moved_tables.append(table)

    def pad_realigned(self, address: int, ops_alignment: int) -> None:
        """Record that the `pad` at `address` pads differently than without the pool. An operand across it is
        refused."""
        cut = f'the `pad {ops_alignment}` at {hex(address)}, which pads to another op once a table before it moved out'
        if self.falls_to == address:
            self.refuse('$', f'falls through to {cut}')
        self._cut_across(address, cut, False)

    def segment_started(self, address: int) -> None:
        """Record that a segment starts at `address`, the same place as without the pool. An operand from moved code
        into it is refused."""
        self._cut_across(address, f'the start of the segment at {hex(address)}, which stays where it is', True)

    def _cut_across(self, address: int, cut: str, segment: bool) -> None:
        for label_address, target, label in self._pending:
            if label_address <= address <= target:
                if not segment or self.shift_class(self.piece(label, label_address)) >= 0:
                    self.refuse(_operand(label, target - label_address), f'reaches across {cut}')
        self._cut(address, cut, segment)

    def shift_class(self, piece: int) -> int:
        """Return -1 for a piece whose code does not move (the first piece, or one that starts a segment), else the
        piece's number."""
        return -1 if piece == 0 or self.cuts[piece - 1][1] else piece

    def _cut(self, anchor: int, cut: str, segment: bool) -> None:
        anchors = self.anchors
        index = bisect.bisect_right(anchors, anchor)
        before = index - bisect.bisect_left(anchors, anchor)
        labels = self.labels
        # walk the labels from the newest: the ones at the anchor were declared before the table's pad (stranded)
        for name in reversed(labels):
            address = labels[name]
            if address == anchor:
                if name in self.stranded:
                    # this label and the older ones were stranded by an earlier table that moved out of the same address
                    break
                self.stranded[name] = (anchor, before)
                self.watched.add(name)
            elif address < self.inline_end:
                break
        anchors.insert(index, anchor)
        self.cuts.insert(index, (cut, segment))
        self._pending = [entry for entry in self._pending if entry[1] >= anchor]

    def table_at(self, address: int) -> Optional[Tuple[int, int, str]]:
        """the placed table that holds `address`, or None"""
        index = bisect.bisect_right(self.tables, address, key=_table_start) - 1
        return self.tables[index] if index >= 0 and address < self.tables[index][1] else None

    def alignment(self, address: int) -> int:
        """Return log2 of the alignment (in bits) of the table at `address`: a moved table's size, an inline table's
        `pad`."""
        table = self.table_at(address)
        return (table[1] - table[0] - 1).bit_length() if table else self.pads.get(address, 0)

    def piece(self, label: str, address: int) -> int:
        stranded = self.stranded.get(label)
        if stranded is not None:
            return bisect.bisect_left(self.anchors, stranded[0]) + stranded[1]
        return bisect.bisect_right(self.anchors, address)

    def check_label(self, label: str, offset: int) -> None:
        """Check `label + offset` now if `label` is declared. Otherwise labels-resolve checks it."""
        address = self.labels.get(label)
        if address is not None:
            self.check(label, address, offset)

    def check(self, label: str, address: int, offset: int, operand: Optional[str] = None) -> None:
        """Check the operand `label + offset`, where `label` is at `address`. `operand`, if given, is the longer sum it
        came from, for a refusal's message."""
        target = address + offset
        self.reach = max(self.reach, target)
        if address >= self.inline_end:
            table = self.table_at(address)
            if table is None:
                raise reached_the_pool(address, self.inline_end)
            if not table[0] <= target < table[1]:
                self.refuse(
                    operand or _operand(label, offset),
                    f'leaves the moved table {table[2]} at {hex(table[0])}: an offset from a moved table\'s label '
                    f'must stay inside it',
                )
            return
        if target >= self.inline_end:
            self._refuse_inside(operand or _operand(label, offset), target)
        piece = self.piece(label, address)
        aim = bisect.bisect_right(self.anchors, target)
        if aim != piece and not self.shift_class(aim) == self.shift_class(piece) == -1:
            cut = self.cuts[piece if aim > piece else piece - 1][0]
            why = f'reaches across {cut}' if offset else f'names {label}, declared before {cut}'
            self.refuse(operand or _operand(label, offset), why)
        if (offset > 0 or label in self.watched) and self.emitting is not None and target >= self.emitting():
            # pending: a table about to move out of a place it reaches over stays inline (may_move)
            self._pending.append((address, target, label))

    def check_expr(self, expr: Expr, value: int, is_address: bool = True) -> None:
        """Check the operand `expr`, whose value is `value`. `is_address` is False for a wflip's value; an address may
        not land in the pool."""
        parts = linear_parts(expr)
        names_a_table = False
        for coefficients, constant in parts:
            names_a_table = self._check_part(coefficients, constant, str(expr)) or names_a_table
        if is_address:  # an operand that names one label is also checked whole, as that label plus an offset
            named = expr.all_unknown_labels()
            if len(named) == 1:
                label = next(iter(named))
                address = self.labels[label]
                if _moves_with(expr, label, address, value):  # `(L | 0) + 15*dw`: checked as L plus what it adds
                    self.check(label, address, value - address, str(expr))
        if is_address and parts and not names_a_table:
            if value >= self.inline_end:
                self._refuse_inside(str(expr), value)
            self.reach = max(self.reach, value)

    def check_emitted(self, expr: Expr, is_address: bool = False) -> None:
        """Check an operand kept for labels-resolve now, if all its labels are declared."""
        if all(label in self.labels for label in expr.all_unknown_labels()):
            self.check_expr(expr, expr.exact_eval(self.labels), is_address)

    def _check_part(self, coefficients: Dict[str, int], constant: int, operand: str) -> bool:
        """Check one +/- sum of `operand`. Return True if it names a moved table's label."""
        labels = self.labels
        if len(coefficients) == 1:
            label, coefficient = next(iter(coefficients.items()))
            if coefficient == 1:
                self.check(label, labels[label], constant, operand)
                return labels[label] >= self.inline_end
        total = sum(coefficients.values())
        moved = [label for label in coefficients if labels[label] >= self.inline_end]
        value = constant + sum(coefficient * labels[label] for label, coefficient in coefficients.items())
        if moved:
            tables = {self.table_at(labels[label]) for label in coefficients}
            if len(tables) != 1 or None in tables or total not in (0, 1):
                self.refuse(operand, f'computes from {moved[0]}, a moved table\'s label, and labels outside that table')
            if total == 1:
                self.check(moved[0], labels[moved[0]], value - labels[moved[0]], operand)
            return True
        if len(coefficients) == 1:
            return False  # one label times a number other than 1 (`2 * L`): an address used as a number
        pieces = sorted(self.piece(label, labels[label]) for label in coefficients)
        if len({self.shift_class(piece) for piece in pieces}) > 1:
            self.refuse(operand, f'takes the distance across {self.cuts[pieces[0]][0]}')
        if total == 1:
            label = next(iter(coefficients))
            self.check(label, labels[label], value - labels[label], operand)
        return False

    def label_below(self, address: int) -> str:
        """Return the nearest label at or below `address`. Slow: only error messages use it."""
        _, label = max(((value, name) for name, value in self.labels.items() if value <= address), default=(0, ''))
        return label

    def _refuse_inside(self, operand: str, target: int) -> None:
        table = self.table_at(target)
        where = f'inside the moved table {table[2]} at {hex(table[0])}' if table else 'in the table pool'
        self.refuse(operand, f'lands at {hex(target)}, {where}, and is not one of its labels plus a constant')

    def refuse(self, operand: str, why: str) -> NoReturn:
        """Refuse the program: moving the tables would change what `operand` means. `where()` names its op."""
        emitting = None if self.emitting is None else self.emitting()
        if emitting is not None and emitting > self.inline_end:
            # the program's own code ran into the pool: report that as the cause
            raise reached_the_pool(emitting, self.inline_end)
        raise FlipJumpAssemblerException(
            f'table-placement: the operand `{operand}`{self.where()} {why}. Address a moved table by its own labels, '
            f'or assemble without a table pool.'
        )


def _table_start(table: Tuple[int, int, str]) -> int:
    return table[0]


def _moves_with(expr: Expr, label: str, address: int, value: int) -> bool:
    """does `expr` (naming only `label`, at `address`) move as far as its label does?"""
    far = 1 << (max(address, value).bit_length() + 1)
    try:
        return expr.exact_eval({label: address + far}) == value + far
    except FlipJumpExprException:
        return False


def _operand(label: str, offset: int) -> str:
    return f'{label} + {offset}' if offset > 0 else f'{label} - {-offset}' if offset else label
