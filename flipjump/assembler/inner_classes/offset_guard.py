"""
The offset guard: keeps every operand pointing at the same place once a table pool moves tables (see OffsetGuard).

Words used here (the pool's own words are in table_pool.py):
- plain: of the same program assembled without the pool. Every op has a pooled address and a plain address.
- anchor: an address where the inline code is cut: the place a moved table left, a `pad` that now pads another number
  of ops, or the start of a segment.
- piece: the inline code between two anchors. A moved table is a piece too.
- shift: how far a piece is from its plain place: plain address minus pooled address, the same for the whole piece.
- stranded: a stranded label was declared at an anchor before its table moved out. It belongs to the piece before it.
- emitted: an address is emitted once the expansion wrote the code that lies there.
"""

from __future__ import annotations

import bisect
from operator import itemgetter
from typing import Callable, Dict, Iterable, List, NamedTuple, NoReturn, Optional, Set, Tuple

from flipjump.utils.exceptions import FlipJumpAssemblerException, FlipJumpExprException, FlipJumpPreprocessorException
from flipjump.utils.functions import readable_int
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.op_stream import linear_parts

LabelsDict = Dict[str, int]


class MovedTable(NamedTuple):
    """A moved table, once it is placed."""

    start: int
    end: int
    label: str  # its start label
    plain_start: int  # its address without the pool

    @property
    def shift(self) -> int:
        return self.plain_start - self.start


class Cut(NamedTuple):
    """What happens at an anchor."""

    description: str  # how a refusal describes it
    shift: int  # the shift of the piece that starts there
    pad_end: int  # where its padding ops end, if it is a `pad`; else the anchor itself


class Stranded(NamedTuple):
    """Where a stranded label belongs."""

    anchor: int
    cuts_before: int  # how many cuts that anchor already had when the label's table moved


class Pending(NamedTuple):
    """An operand that lands on code not emitted yet."""

    target: int  # the address it lands on
    shift: int  # the shift that place must end up with
    operand: str
    where: str  # where its op is


_start = itemgetter(0)


def reached_the_pool_exception(address: int, pool_start: int) -> FlipJumpPreprocessorException:
    return FlipJumpPreprocessorException(
        f'table-placement: the program reached {hex(address)}, which is inside the table pool at {hex(pool_start)}. '
        f'Raise pool_base, or relocate fewer tables.'
    )


def _nowhere() -> str:
    return ''


class OffsetGuard:
    """A guard that keeps every operand pointing at the same place once the tables move.

    A moved table leaves the inline code at its anchor, and the code after it moves up. The guard knows the shift of
    every piece, so it knows where the op at any pooled address is in the plain program.

    An operand is right when it lands on the op it lands on in the plain program. Each +/- sum in an operand
    (linear_parts) is checked on its own (`L + 15*dw` in `(L + 15*dw) | 0`):
    - `label + constant` (validate_offset): the place it lands on must have the label's shift;
    - a distance (`b - a`: labels whose coefficients sum to 0) must be the same as in the plain program;
    - a label plus distances (the coefficients sum to 1) is an address: the place it lands on must be as far from its
      plain place as the sum is from its plain value.
    An operand that names one label and moves as far as it does (`(L | 0) + 15*dw`) is also checked whole.
    A number the expansion evaluates from labels - a rep count, a pad, a reserve, a segment's address - shapes the
    program, so it must be the same number as in the plain program (validate_number).
    `$` counts as a label at the next op, and an op that falls through (`a;`) reaches the next op.

    Not checked - a program that depends on these is not kept the same:
    - an address used as a number: what an operand computes from an address beyond its sums (`2 * L`, `(L >> 6) & 15`,
      `a ^ b`). The stl stores pointers and retargets jumps this way, and those stay right: every use of the address
      is of the pooled one.
    - a number used as an address (`;0x280` names no label);
    - what the padding ops of a `pad` hold.

    The preprocessor checks the operands whose labels it knows; labels-resolve checks the rest. An operand that lands
    on code not emitted yet is pending. Until that code is emitted, a table about to move out of a place below it
    stays inline (why_stay_inline), and a `pad` there that pads another number of ops refuses the program. Any other
    violation refuses the program too (`refuse`), and `where()` says where the op is.
    """

    def __init__(
        self,
        inline_end: int,
        labels: LabelsDict,
        *,
        where: Callable[[], str] = _nowhere,
        emitting: Optional[Callable[[], int]] = None,
    ):
        """
        @param where: returns where the op being checked is, for a refusal's message
        @param emitting: returns the next inline address the preprocessor emits at. None once every table has moved.
        """
        self.inline_end = inline_end  # the pool's start: the inline code lies below it, the moved tables above
        self.labels = labels  # the preprocessor's labels (a moved table's labels join them once it is placed)
        self.anchors: List[int] = []  # ascending
        self.cuts: List[Cut] = []  # what happens at each anchor
        self.stranded: Dict[str, Stranded] = {}  # by label
        self.watched: Set[str] = set()  # labels checked even with no offset: the stranded ones, and the `$` labels
        self.falls_to = -1  # the address the last op that falls through (`a;`) falls to
        self._pending: List[Pending] = []
        self._closed: List[Tuple[int, int]] = []  # the inline segments emitted in full: (start, end), by start
        self._segment_start = 0  # where the inline segment being emitted starts
        self._segment_limit = inline_end  # the start of the next emitted segment above it (the pool, if none)
        self.moved_tables: List[Tuple[str, int]] = []  # (start label, plain start) of the moved tables, as they moved
        self.tables: List[MovedTable] = []  # the placed tables, by start
        self.where = where
        self.emitting = emitting
        # inline address -> log2 of the alignment (in bits) a `pad` gave it
        self.pads: Dict[int, int] = {}
        self.reach = 0  # the highest address an operand checked so far lands at

    # ---- what the expansion tells it ----

    def resolving(self, where: Callable[[], str]) -> None:
        """Start the labels-resolve phase. Every table has moved by now, so nothing is pending any more."""
        self.where = where
        self.emitting = None

    def resolved(self) -> None:
        """End the labels-resolve phase: forget where its ops are (the function that says so refers to the binary
        being built, and back to this guard)."""
        self.where = _nowhere

    def watch(self, label: str) -> None:
        """Check `label` even where it is used with no offset (a `$` label)."""
        self.watched.add(label)

    def fell_through(self, address: int) -> None:
        """Record that the op just emitted falls through (`a;`) to `address`."""
        self.falls_to = address

    def pad_aligned(self, address: int, bits: int) -> None:
        """Record that a `pad` aligned the inline `address` to 2^bits bits."""
        if address:
            self.pads[address] = max(self.pads.get(address, 0), bits)

    def why_stay_inline(self, anchor: int) -> Optional[str]:
        """Return why the table about to move out of `anchor` must stay inline, or None if it may move."""
        if self.falls_to == anchor:
            return 'the op before it falls through to it'
        if any(pending.target >= anchor and not self._emitted(pending.target) for pending in self._pending):
            return 'an operand reaches over its place'
        return None

    def table_moved(self, anchor: int, table: str, shift: int, plain_start: int) -> None:
        """Record that a table moved out of `anchor`. `table` is its start label, `plain_start` its plain address;
        the code after it is now `shift` bits below its plain place."""
        self._cut(anchor, Cut(f'the place of the table {table}, which moved out of {hex(anchor)}', shift, anchor))
        self.moved_tables.append((table, plain_start))

    def tables_placed(self, extents: Iterable[Tuple[int, int]], inline_end: int) -> None:
        """Record where the moved tables went: `extents` are their (start, end), in the order they moved.
        `inline_end` is where the pool starts, now that it is placed."""
        self.inline_end = inline_end
        self.tables = sorted(
            MovedTable(start, end, label, plain) for (start, end), (label, plain) in zip(extents, self.moved_tables)
        )

    def pad_realigned(self, address: int, ops_alignment: int, shift: int, pad_end: int) -> None:
        """Record that the `pad` at `address` pads another number of ops than without the pool: its padding ends at
        `pad_end`, and the code after it is `shift` bits below its plain place. A pending operand above it is
        refused."""
        pad = f'`pad {readable_int(ops_alignment, separators=False)}`'
        cut = f'the {pad} at {hex(address)}, which pads to another op once a table before it moved out'
        if self.falls_to == address:
            self.refuse('$', f'falls through to {cut}')
        for pending in self._pending:
            if pending.target >= address and not self._emitted(pending.target):
                self.refuse(pending.operand, f'reaches across {cut}', pending.where)
        self._cut(address, Cut(cut, shift, pad_end))

    def segment_started(self, address: int, previous_end: int) -> None:
        """Record that the inline segment that was being emitted ended at `previous_end`, and the next starts at
        `address`: the same place as without the pool. A pending operand that needs moved code there is refused."""
        bisect.insort(self._closed, (self._segment_start, previous_end))
        above = bisect.bisect_right(self._closed, address, key=_start)
        self._segment_start = address
        self._segment_limit = self._closed[above][0] if above < len(self._closed) else self.inline_end
        cut = f'the start of the segment at {hex(address)}, which stays where it is'
        for pending in self._pending:
            if pending.shift and pending.target >= address and not self._emitted(pending.target):
                self.refuse(pending.operand, f'reaches across {cut}', pending.where)
        self._cut(address, Cut(cut, 0, address))

    def _cut(self, anchor: int, cut: Cut) -> None:
        """Cut the inline code at `anchor`: the labels declared right at it stay with the piece before it, and the
        operands whose code is emitted by now are pending no more."""
        index = bisect.bisect_right(self.anchors, anchor)
        self._strand_labels_at(anchor, index - bisect.bisect_left(self.anchors, anchor))
        self.anchors.insert(index, anchor)
        self.cuts.insert(index, cut)
        self._pending = [pending for pending in self._pending if not self._emitted(pending.target)]

    def _strand_labels_at(self, anchor: int, cuts_before: int) -> None:
        """Strand the labels at `anchor` that are not stranded yet: they were declared before the cut being made
        there (which is the anchor's cut number `cuts_before`)."""
        labels = self.labels
        for name in reversed(labels):  # from the newest: the labels at the anchor are the last ones declared
            address = labels[name]
            if address == anchor:
                if name in self.stranded:
                    break  # this label and the older ones were stranded by an earlier cut at the same address
                self.stranded[name] = Stranded(anchor, cuts_before)
                self.watched.add(name)
            elif address < self.inline_end:
                break

    # ---- where things are ----

    def _emitted(self, address: int) -> bool:
        """Did the expansion already write the inline code at `address`?"""
        if self.emitting is None or self._segment_start <= address < self.emitting():
            return True
        index = bisect.bisect_right(self._closed, address, key=_start) - 1
        return index >= 0 and address < self._closed[index][1]

    def table_at(self, address: int) -> Optional[MovedTable]:
        """the placed table that holds `address`, or None"""
        index = bisect.bisect_right(self.tables, address, key=_start) - 1
        return self.tables[index] if index >= 0 and address < self.tables[index].end else None

    def alignment(self, address: int) -> int:
        """Return log2 of the alignment (in bits) of the table at `address`: a moved table's size, an inline table's
        `pad`."""
        table = self.table_at(address)
        return (table.end - table.start - 1).bit_length() if table else self.pads.get(address, 0)

    def _piece(self, label: str, address: int) -> int:
        """Return the number of the inline piece that `label`, at `address`, belongs to."""
        stranded = self.stranded.get(label)
        if stranded is not None:
            return bisect.bisect_left(self.anchors, stranded.anchor) + stranded.cuts_before
        return bisect.bisect_right(self.anchors, address)

    def _shift_at(self, address: int) -> Optional[int]:
        """Return the shift of the op at `address`. None if no op of the plain program is there: a place in the pool
        outside its tables, or the padding of a `pad` that pads another number of ops."""
        if address >= self.inline_end:
            table = self.table_at(address)
            return None if table is None else table.shift
        piece = bisect.bisect_right(self.anchors, address)
        if piece == 0:
            return 0
        cut = self.cuts[piece - 1]
        return None if address < cut.pad_end else cut.shift

    def _label_shift(self, label: str, address: int) -> int:
        """Return the shift of `label`, which is at `address`."""
        if address >= self.inline_end:
            table = self.table_at(address)
            if table is None:
                raise reached_the_pool_exception(address, self.inline_end)
            return table.shift
        piece = self._piece(label, address)
        return self.cuts[piece - 1].shift if piece else 0

    def label_below(self, address: int) -> str:
        """Return the nearest label at or below `address`. Slow: only error messages use it."""
        _, label = max(((value, name) for name, value in self.labels.items() if value <= address), default=(0, ''))
        return label

    # ---- checking operands ----

    def validate_label(self, label: str, offset: int) -> None:
        """Check `label + offset` now if `label` is declared. Otherwise labels-resolve checks it."""
        address = self.labels.get(label)
        if address is not None:
            self.validate_offset(label, address, offset)

    def validate_offset(self, label: str, address: int, offset: int, operand: Optional[str] = None) -> None:
        """Check the operand `label + offset`, where `label` is at `address`: it must land on a place with the label's
        shift. `operand`, if given, is the longer expression it came from, for a refusal's message."""
        target = address + offset
        if target > self.reach:
            self.reach = target
        shift = self._label_shift(label, address)
        if self._shift_at(target) != shift:
            self.refuse(operand or _operand(label, offset), self._why_misses(label, address, offset))
        if self.emitting is not None and (
            # (a label is declared at an emitted address, so in the segment being emitted a reach back is emitted too)
            offset > 0
            or target < self._segment_start
            or address >= self._segment_limit
            or label in self.watched
        ):
            self._keep(target, shift, operand or _operand(label, offset))

    def _keep(self, address: int, shift: int, operand: str) -> None:
        """Remember that `operand` needs the place at `address` to end up with `shift`, if its code is not emitted
        yet (a pending operand)."""
        if self.emitting is not None and not self._emitted(address):
            self._pending.append(Pending(address, shift, operand, self.where()))

    def _why_misses(self, label: str, address: int, offset: int) -> str:
        """Return why `label + offset` does not land on the op it lands on without the pool."""
        target = address + offset
        if address >= self.inline_end:
            table = self.table_at(address)
            assert table is not None
            return (
                f"leaves the moved table {table.label} at {hex(table.start)}: an offset from a moved table's label "
                f"must stay inside it"
            )
        if target >= self.inline_end:
            return self._lands_in_the_pool(target)
        piece, aim = self._piece(label, address), bisect.bisect_right(self.anchors, target)
        if aim and target < self.cuts[aim - 1].pad_end:
            return f'lands in the padding of {self.cuts[aim - 1].description}'
        cut = self.cuts[piece if aim > piece else piece - 1].description
        return f'reaches across {cut}' if offset else f'names {label}, declared before {cut}'

    def _lands_in_the_pool(self, target: int) -> str:
        table = self.table_at(target)
        where = f'inside the moved table {table.label} at {hex(table.start)}' if table else 'in the table pool'
        return f'lands at {hex(target)}, {where}, and is not one of its labels plus a constant'

    def validate_expr(self, expr: Expr, value: int, is_address: bool = True) -> None:
        """Check the operand `expr`, whose value is `value`: each of its +/- sums, and the whole of it if it moves as
        far as the one label it names. `is_address` is False for a wflip's value; an address may not land in the
        pool."""
        operand = str(expr)
        parts = linear_parts(expr)
        names_a_table = False
        for coefficients, constant in parts:
            names_a_table = self._validate_sum(coefficients, constant, operand) or names_a_table
        named = expr.all_unknown_labels()
        if len(named) == 1:
            label = next(iter(named))
            address = self.labels[label]
            if _moves_with(expr, label, address, value):  # `(L | 0) + 15*dw`: checked as L plus what it adds
                self.validate_offset(label, address, value - address, operand)
        if is_address and parts and not names_a_table:
            if value >= self.inline_end:
                self.refuse(operand, self._lands_in_the_pool(value))
            self.reach = max(self.reach, value)

    def validate_number(self, expr: Expr, value: int) -> None:
        """Check a number the expansion evaluates from labels, whose value is `value`: a rep count, a pad, a reserve, a
        segment's address. It shapes the program, so it must be the same number as without the pool."""
        self.validate_expr(expr, value, False)
        plain_labels = {}
        for label in expr.all_unknown_labels():
            address = self.labels[label]
            plain_labels[label] = address + self._label_shift(label, address)
        try:
            plain: Optional[int] = expr.exact_eval(plain_labels)
        except FlipJumpExprException:
            plain = None
        if plain != value:
            was = 'has no value' if plain is None else f'is {readable_int(plain, separators=False)}'
            now = readable_int(value, separators=False)
            self.refuse(str(expr), f'{was} without the pool and is {now} with it: it is computed from code that moved')

    def validate_emitted(self, expr: Expr, is_address: bool = False) -> None:
        """Check an operand kept for labels-resolve now, if all its labels are declared."""
        if all(label in self.labels for label in expr.all_unknown_labels()):
            self.validate_expr(expr, expr.exact_eval(self.labels), is_address)

    def _validate_sum(self, coefficients: Dict[str, int], constant: int, operand: str) -> bool:
        """Check one +/- sum of `operand`: `constant` plus each label times its coefficient. Return True if it names a
        moved table's label."""
        addresses = {label: self.labels[label] for label in coefficients}
        names_a_table = any(address >= self.inline_end for address in addresses.values())
        if len(coefficients) == 1:
            self._validate_one_label_sum(coefficients, addresses, constant, operand, names_a_table)
        else:
            self._validate_many_labels_sum(coefficients, addresses, constant, operand, names_a_table)
        return names_a_table

    def _validate_one_label_sum(
        self, coefficients: Dict[str, int], addresses: Dict[str, int], constant: int, operand: str, names_a_table: bool
    ) -> None:
        """Check a sum of one label: `label + constant` lands where the label's piece is, and a moved table's label
        takes no other coefficient (an inline label times a number, `2 * L`, is an address used as a number)."""
        label, coefficient = next(iter(coefficients.items()))
        if coefficient == 1:
            self.validate_offset(label, addresses[label], constant, operand)
        elif names_a_table:
            self.refuse(operand, self._mixes_a_table(addresses))

    def _validate_many_labels_sum(
        self, coefficients: Dict[str, int], addresses: Dict[str, int], constant: int, operand: str, names_a_table: bool
    ) -> None:
        """Check a sum of several labels by what their coefficients add up to: 0 is a distance (it must not change),
        1 is an address (it must land on a place that moved as far as the sum did), and anything else is an address
        used as a number (right while its labels moved alike)."""
        shifts = {label: self._label_shift(label, addresses[label]) for label in coefficients}
        for label in coefficients:
            if label in self.watched:
                self._keep(addresses[label], shifts[label], operand)
        shift = sum(coefficient * shifts[label] for label, coefficient in coefficients.items())  # of the whole sum
        value = constant + sum(coefficient * addresses[label] for label, coefficient in coefficients.items())
        total = sum(coefficients.values())
        if total == 0:
            right = shift == 0
        elif total == 1:
            right = self._shift_at(value) == shift
        else:
            right = len(set(shifts.values())) == 1 and not names_a_table
        if not right:
            self.refuse(operand, self._mixes_a_table(addresses) if names_a_table else self._why_distance(addresses))
        if total == 1:
            self._keep(value, shift, operand)

    def _mixes_a_table(self, addresses: Dict[str, int]) -> str:
        moved = next(label for label, address in addresses.items() if address >= self.inline_end)
        if len(addresses) == 1:
            return f"uses {moved}, a moved table's label, as a number: only a constant may be added to it"
        return f"computes from {moved}, a moved table's label, and labels outside that table"

    def _why_distance(self, addresses: Dict[str, int]) -> str:
        """Return why a sum of the labels at `addresses` is not what it is without the pool."""
        pieces = sorted({self._piece(label, address) for label, address in addresses.items()})
        if len(pieces) == 1:
            return 'does not land where the place it names moved to'
        return f'takes the distance across {self.cuts[pieces[0]].description}'

    def refuse(self, operand: str, why: str, where: Optional[str] = None) -> NoReturn:
        """Refuse the program: moving the tables would change what `operand` means. `where` says where its op is: it
        is given for a pending operand (whose op is behind by now), else `where()` says it."""
        emitting = None if self.emitting is None else self.emitting()
        if emitting is not None and emitting > self.inline_end:
            # the program's own code ran into the pool: report that as the cause
            raise reached_the_pool_exception(emitting, self.inline_end)
        raise FlipJumpAssemblerException(
            f'table-placement: the operand `{operand}`{self.where() if where is None else where} {why}. '
            f'Address a moved table by its own labels, '
            f'or assemble without a table pool.'
        )


def _moves_with(expr: Expr, label: str, address: int, value: int) -> bool:
    """does `expr` (naming only `label`, at `address`) move as far as its label does?"""
    far = 1 << (max(address, value).bit_length() + 1)
    try:
        return expr.exact_eval({label: address + far}) == value + far
    except FlipJumpExprException:
        return False


def _operand(label: str, offset: int) -> str:
    if not offset:
        return label
    return f"{label} {'+' if offset > 0 else '-'} {readable_int(abs(offset), separators=False)}"
