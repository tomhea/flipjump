"""
The table pool: moves the stl's lookup tables (see preprocessor.relocatable_call) to addresses where arming them
is cheap.

It never changes what the program does. A table whose move would change an operand stays inline (OffsetGuard), a
pin that cannot hold is dropped (assembler.unpin), and a program whose code runs into the pool is refused.

Words used here:
- table: a lookup table the stl declares relocatable (an expansion of a `__relocatable__` macro): ops a dispatch
  jumps into.
- jump word: the word a dispatch writes a table's address into (with `wflip`) and then jumps through.
- arm: write a table's address into its jump word (so the next jump through the word lands in that table).
- group: all the tables dispatched through the same jump word.
- block: a power-of-two-sized, aligned area of the pool that one group's tables share.
- slot: one table's place in a block. Slots are power-of-two sized; a table's index is its slot number.
- pin: make a jump word rest at its block's base, so arming it flips only the table's index bits, not its whole
  address.
- rest: the value a word holds before any flip (its initial value).
- broken group: a group with a table that got no slot; its word is normally left unpinned.
- hot: named in the heat list (a profile). Hot tables get their group's cheapest indices.
- raw word: a word the program reads or writes directly (through a pointer); pinning it would break the program.
- inline: left in the program's own code, not moved to the pool.
- overflow region: the area right after the blocks, for the tables of groups that got no block.
- anchor, piece, stranded (OffsetGuard): an anchor is the address a moved table left; anchors cut the inline code
  into pieces; a stranded label was declared at an anchor before its table moved out.
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import re
import sys
from array import array
from typing import (
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    NoReturn,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from flipjump.utils.exceptions import FlipJumpAssemblerException, FlipJumpExprException, FlipJumpPreprocessorException
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.op_stream import linear_parts
from flipjump.assembler.inner_classes.ops import NewSegment

LabelsDict = Dict[str, int]
# A cell the program reads or writes raw: its label (the cell runs to the next label), or (label, length in ops).
UnpinnedCell = Union[str, Tuple[str, int]]

# The runtime's words (like stl.IO) lie below this bit address and are never pinned. 1024 covers any w <= 256.
RESERVED_BELOW = 1024

# A BlockPool pool_base that means: start the pool at the first power of two above the program.
AFTER_PROGRAM = 'after_program'

# A macro that names one of these reads or writes words raw, through a pointer. If one expands and pin_all_but is
# not given, no word is pinned.
POINTER_RUNTIME_LABELS = frozenset(
    {'bit.pointers.to_jump', 'bit.pointers.to_flip', 'hex.pointers.to_jump', 'hex.pointers.to_flip'}
)

# a call site's coordinates in a macro-expansion path: `f3:l120:` or `s1:l40:`
_SITE_COORDINATES = re.compile(r'(?<![\w.])[fs]\d+:l\d+:')


def heat_key(path: str) -> str:
    """Return a macro-expansion path (or a group expression) without its call-site coordinates. A heat list matches
    groups and sites by this key (see BlockPool's `heat`)."""
    return _SITE_COORDINATES.sub('', path)


@dataclasses.dataclass
class BlockCensus:
    """The tables a BlockPool met, counted by group. A group is keyed by its jump word's expression."""

    counts: Dict[str, int]
    widths: Dict[str, int]
    width_hist: Dict[str, Dict[int, int]]
    alias: Dict[str, str]


class HotSite(NamedTuple):
    """A hot table in a BlockPool's heat list. It is named by the macro path that emitted it (`site`), how many tables
    of its group that site emitted before it (`occurrence`), and its width (`slot_ops`). TableSite has the same
    three."""

    site: str
    occurrence: int
    slot_ops: int


@dataclasses.dataclass(frozen=True, slots=True)
class TableSite:
    """A table the pool met (BlockPool.table_sites). A profile of the run at these addresses gives a heat list's
    HotSites."""

    group: str  # its group: the word it is dispatched through
    site: str  # the macro path that emitted it (heat_key)
    occurrence: int  # how many tables of its group came from the same site before it
    slot_ops: int  # the slot width it takes
    address: int  # where it went; 0 means it stayed inline


# What BlockPool._defer decides for a table the expansion reached
DEFER_OUT = 'out'  # it leaves the op stream, and is placed once the expansion is done
DEFER_TOO_WIDE = 'too_wide'  # it stays inline: no slot is wide enough
DEFER_INLINE = 'inline'  # it stays inline: it has no group


class DeferredTables:
    """The tables the pool queued, in expansion order, kept as compact parallel arrays (a big program has ~10^5).
    A moved table's labels wait here, as bit offsets from its start, until it is placed."""

    def __init__(self) -> None:
        self.groups: List[str] = []
        self.aligns = array('I')
        self.sizes = array('I')
        self.segments: List[Optional[NewSegment]] = []  # None for a table that stayed inline
        self.sites: List[str] = []  # each one's heat key
        self.label_names: List[str] = []
        self.label_offsets = array('I')
        self.label_ends = array('I')  # table i's labels are label_names[label_ends[i-1]:label_ends[i]]

    def add(self, group: str, ops_alignment: int, table_ops: int, site: str, segment: Optional[NewSegment]) -> None:
        self.groups.append(sys.intern(group))
        self.aligns.append(ops_alignment)
        self.sizes.append(table_ops)
        self.segments.append(segment)
        self.sites.append(site)
        self.label_ends.append(len(self.label_names))

    def add_label(self, label: str, offset: int) -> None:
        self.label_names.append(label)
        self.label_offsets.append(offset)
        self.label_ends[-1] = len(self.label_names)

    def labels_of(self, index: int) -> Iterator[Tuple[str, int]]:
        for at in range(self.label_ends[index - 1] if index else 0, self.label_ends[index]):
            yield self.label_names[at], self.label_offsets[at]


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
        index = bisect.bisect_right(self.tables, (address, 1 << 64)) - 1
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


def _moves_with(expr: Expr, label: str, address: int, value: int) -> bool:
    """does `expr` (naming only `label`, at `address`) move as far as its label does?"""
    far = 1 << (max(address, value).bit_length() + 1)
    try:
        return expr.exact_eval({label: address + far}) == value + far
    except FlipJumpExprException:
        return False


def _operand(label: str, offset: int) -> str:
    return f'{label} + {offset}' if offset > 0 else f'{label} - {-offset}' if offset else label


def _cheapest(bits: int) -> Iterator[int]:
    """the values below 2**bits, fewest set bits first, then ascending (lazily)"""
    yield 0
    for ones in range(1, bits + 1):
        value = (1 << ones) - 1
        while value < 1 << bits:
            yield value
            lowest = value & -value  # the next larger value with as many set bits (Gosper's hack)
            ripple = value + lowest
            value = ripple | (((value ^ ripple) >> 2) // lowest)


@functools.lru_cache(maxsize=None)
def _cheapest_order(slots: int) -> List[int]:
    """Return range(slots) in _cheapest's order. The list is cached and shared: don't change it."""
    return sorted(range(slots), key=lambda v: (bin(v).count('1'), v))


class BlockPool:
    """A pool that gives each group a block, so that arming a jump word flips only the table's index.

    `wflip src+w, switch` arms a hex variable's jump word. If the word is pinned at a block base B and its tables live
    in that block, every wflip of the word flips `value ^ B` instead of `value` (assembler.BinaryData.pinned_value);
    for an arm, that is just the table's index. A pinned word holds `B ^ value`, so a raw word (POINTER_RUNTIME_LABELS)
    must not be pinned: `pin_all_but` names the cells to leave out.

    One pass: the preprocessor takes each table out of the op stream (`_defer`), and once the expansion is done the
    pool places them all (`place_deferred`), sizing the blocks from its census. A block has one sub-block per table
    width (_bucket_layout). Groups whose jump words resolve to one address share a block (alias). If the blocks do not
    all fit, the last groups go to the overflow region. A `heat` list (pin protection) lays the hot groups out first,
    always pins them, and gives each width's k-th listed site the k-th cheapest index.

    A pool lays out one assembly: construct a fresh one for every assemble().
    """

    def __init__(
        self,
        memory_width: int,
        pool_base: Union[int, str] = AFTER_PROGRAM,
        *,
        pool_end: Optional[int] = None,
        max_slot_ops: int = 32,
        spread_factor: int = 1,
        spread_min_tables: int = 256,
        pin_incomplete_groups: bool = False,
        heat: Optional[Mapping[str, Sequence[HotSite]]] = None,
        pin_all_but: Optional[Iterable[UnpinnedCell]] = None,
    ):
        """
        @param pool_base: the address of the pool's first block, or AFTER_PROGRAM: the first power of two above the
            program. With AFTER_PROGRAM the wflip areas go after the pool.
        @param pool_end: the pool ends below this address. None means the top of memory.
        @param max_slot_ops: the widest table a slot holds (32 fits the stl's widest, `hex.cmp`'s 31 ops)
        @param spread_factor: a group of at least `spread_min_tables` tables gets this many times the slots it needs,
            and uses the cheapest indices
        @param pin_incomplete_groups: pin a broken group's word too (see pinned_words)
        @param heat: the heat list, `{group: [HotSite, ...]}`, each group's hottest site first. It comes from a
            profile of `table_sites`; groups and sites are matched by heat_key. The listed tables get their group's
            cheapest indices.
        @param pin_all_but: pin every word except the words of these cells. List every cell the program reads or
            writes raw (through a pointer, or by jumping through its word), as a label (the cell runs to the next
            label) or as (label, length in ops). Leaving such a cell out makes the program run WRONG, not slower.
            [] pins every word. None (the default) means the cells are not known: a program that uses the pointer
            runtime then pins no word (its tables still go to cheap addresses). A label the program does not define
            is skipped, and listed in pin_all_but_missing.
        """
        self.memory_width = memory_width
        self.op_bits = 2 * memory_width
        self.after_program = pool_base == AFTER_PROGRAM  # the base is chosen once the program's end is known
        self.pool_base = 1 << memory_width if isinstance(pool_base, str) else pool_base
        if isinstance(pool_base, str) and not self.after_program:
            raise FlipJumpPreprocessorException(f'pool_base is an address or {AFTER_PROGRAM!r}, not {pool_base!r}')
        self.pool_end = pool_end
        self.max_slot_ops = max_slot_ops
        self.spread_factor = max(1, spread_factor)
        self.spread_min_tables = spread_min_tables
        self.pin_incomplete_groups = pin_incomplete_groups
        self.heat = heat
        self.pin_all_but = None if pin_all_but is None else tuple(pin_all_but)
        self.pin_all_but_missing: List[str] = []  # the labels of pin_all_but the program does not define
        self.reads_words_raw = False  # set by the preprocessor when a macro naming POINTER_RUNTIME_LABELS expands
        self.offset_guard: Optional[OffsetGuard] = None  # set by the preprocessor that lays this pool out
        # tables kept inline because moving them would change an operand, counted by why
        self.unsafe_declines: Dict[str, int] = {}
        self.unpins: Dict[str, int] = {}  # words left unpinned because a pin could not hold, by why (assembler.unpin)
        self.pinned: Dict[int, int] = {}  # the words the assembly pinned: {jump-word address: block base}
        # table counts: taken out of the op stream, placed in a block, placed in the overflow region, kept inline as
        # too wide for a slot, kept inline with no group
        self.deferred = self.allocated = self.overflowed = self.declined = self.ungrouped = 0
        # groups whose jump word has no address yet when the pool is sized (it names a queued table's label)
        self.alias_unresolved = 0
        self.end = 0  # the end of the last table placed
        # the census, by group: the widths' histogram, unmerged while the expansion runs; place_deferred merges it by
        # `alias` and counts it into `counts`
        self.counts: Dict[str, int] = {}
        self.width_hist: Dict[str, Dict[int, int]] = {}
        self.exprs: Dict[str, Expr] = {}  # unmerged group -> its jump word's Expr
        self.alias: Dict[str, str] = {}
        self.groups: Dict[str, Tuple[int, Optional[Expr]]] = {}  # group -> (block base, its jump word's Expr)
        self.broken_groups: Set[str] = set()  # a group that could not place every table is not pinned (pinned_words)
        self._used = 0  # the bits the blocks take, from pool_base
        self._layout_cache: Dict[str, Tuple[Dict[int, Tuple[int, int, int]], int]] = {}
        self._next_index: Dict[Tuple[str, int], int] = {}  # (group, slot width) -> tables placed
        # hot group -> (site, occurrence) -> (width, rank)
        self.hot_sites: Dict[str, Dict[Tuple[str, int], Tuple[int, int]]] = {}
        self.hot_ranks: Dict[Tuple[str, int], int] = {}  # (group, width) -> ranks reserved for its sites
        self._hot_seen: Dict[Tuple[str, str], int] = {}  # (group, site) -> occurrences reached so far
        self._hot_next: Dict[Tuple[str, int], int] = {}  # (group, width) -> unlisted tables so far
        self.hot_missing: List[str] = []  # listed groups this program does not have
        self.hot_ambiguous: List[str] = []  # listed keys that match more than one of the program's groups
        self.hot_sites_matched = self.hot_sites_width_changed = 0
        self.table_sites: List[TableSite] = []  # every table met, in expansion order

    def _defer(self, ops_alignment: int, table_ops: int, group_expr: Optional[Expr]) -> str:
        """Count a table the expansion reached (at its pad) in the census, and return what becomes of it (a DEFER_*)."""
        width = max(table_ops, ops_alignment)
        if group_expr is not None:
            group = str(group_expr)
            hist = self.width_hist.setdefault(group, {})
            hist[width] = hist.get(width, 0) + 1
            self.exprs.setdefault(group, group_expr)
        elif not self._raw():  # (a raw program's tables need no group: see _place_cheapest)
            self.ungrouped += 1
            return DEFER_INLINE
        if width > self.max_slot_ops and not self._raw():
            return DEFER_TOO_WIDE
        self.deferred += 1
        return DEFER_OUT

    def place_deferred(self, tables: DeferredTables, labels: LabelsDict, end: int) -> Sequence[int]:
        """Place every queued table and return their addresses; 0 means the table stayed inline. First merge the groups
        whose jump words resolve to one address. `end` is where the program ends."""
        # the first power of two above the program, or right after the program if that power is past the top of memory
        if self.after_program:
            self.pool_base = 1 << max(0, (end - 1).bit_length())
            if self.pool_base >= 1 << self.memory_width:
                self.pool_base = end
        by_address: Dict[int, str] = {}
        for group in sorted(self.exprs):
            try:
                word = self.exprs[group].exact_eval(labels)
            except FlipJumpExprException:
                self.alias_unresolved += 1
                continue
            first = by_address.setdefault(word, group)
            if first != group:
                self.alias[group] = first
        counts: Dict[str, int] = {}
        width_hist: Dict[str, Dict[int, int]] = {}
        for group, hist in self.width_hist.items():
            canonical = self.alias.get(group, group)
            counts[canonical] = counts.get(canonical, 0) + sum(hist.values())
            into = width_hist.setdefault(canonical, {})
            for width, count in hist.items():
                into[width] = into.get(width, 0) + count
        if self._raw():
            self.counts, self.width_hist = counts, width_hist
            return self._place_cheapest(tables)
        self.lay_out(counts, width_hist)
        addresses = array('Q', bytes(8 * len(tables.sizes)))  # 0 means the table stayed inline
        overflow = []  # (slot bits, index) of the tables whose group got no block
        for index, group in enumerate(tables.groups):
            address = self._reserve(tables.aligns[index], tables.sizes[index], group, tables.sites[index])
            if address is not None:
                addresses[index] = address
            elif tables.segments[index] is not None:
                overflow.append((self._slot_bits(max(tables.sizes[index], tables.aligns[index])), index))
        cursor = self.pool_base + self._used  # the overflow region, right after the blocks: widest slot first
        for slot_bits, index in sorted(overflow, key=lambda entry: -entry[0]):
            addresses[index] = cursor = -(-cursor // slot_bits) * slot_bits
            cursor += slot_bits
        self.overflowed = len(overflow)
        return self._ended(tables, addresses)

    def _ended(self, tables: DeferredTables, addresses: Sequence[int]) -> Sequence[int]:
        occurrences: Dict[Tuple[str, str], int] = {}
        for index, address in enumerate(addresses):
            if address:
                self.end = max(self.end, address + tables.sizes[index] * self.op_bits)
            group, site = self.alias.get(tables.groups[index], tables.groups[index]), tables.sites[index]
            occurrence = occurrences.get((group, site), 0)
            occurrences[(group, site)] = occurrence + 1
            slot_ops = 1 << (max(tables.sizes[index], tables.aligns[index], 1) - 1).bit_length()
            self.table_sites.append(TableSite(group, site, occurrence, slot_ops, address))
        return addresses

    def _raw(self) -> bool:
        """Return whether the program reads words raw and no pin_all_but says which. Then no word is pinned
        (_place_cheapest)."""
        return self.reads_words_raw and self.pin_all_but is None

    def unpinned_words(self, labels: LabelsDict) -> Set[int]:
        """Return the addresses of the words in pin_all_but's cells: the words the assembler must not pin."""
        if not self.pin_all_but:
            return set()
        addresses = sorted(set(labels.values()))
        words: Set[int] = set()
        for cell in self.pin_all_but:
            label, ops = (cell, None) if isinstance(cell, str) else cell
            if label not in labels:
                self.pin_all_but_missing.append(label)
                continue
            start = labels[label]
            if ops is None:
                after = bisect.bisect_right(addresses, start)
                end = addresses[after] if after < len(addresses) else start + self.op_bits
            else:
                end = start + ops * self.op_bits
            words.update(range(start, end, self.memory_width))
        return words

    def _place_cheapest(self, tables: DeferredTables) -> Sequence[int]:
        """Place the tables of a raw program (_raw), whose words cannot be pinned, at cheap addresses. Each width's
        tables take the offsets with the fewest set bits in a region with 256 times the slots they need, or just the
        slots they need if the pool cannot hold that."""
        by_slot: Dict[int, List[int]] = {}
        for index, segment in enumerate(tables.segments):
            if segment is not None:
                by_slot.setdefault(self._slot_bits(max(tables.sizes[index], tables.aligns[index])), []).append(index)
        for spread_bits in (8, 0):
            cursor, placed = self.pool_base, []
            for slot_bits in sorted(by_slot, reverse=True):
                indices = by_slot[slot_bits]
                index_bits = (len(indices) - 1).bit_length() + spread_bits
                base = -(-cursor // (slot_bits << index_bits)) * (slot_bits << index_bits)
                placed += [(i, base + offset * slot_bits) for i, offset in zip(indices, _cheapest(index_bits))]
                cursor = base + (slot_bits << index_bits)
            if cursor <= self._pool_limit():
                break
        else:
            raise self._no_room()
        addresses = array('Q', bytes(8 * len(tables.sizes)))
        for index, address in placed:
            addresses[index] = address
        self.allocated = len(placed)
        if self.counts:
            self.unpins['the pointer runtime'] = len(self.counts)  # the words a block would have pinned
        return self._ended(tables, addresses)

    def _no_room(self) -> FlipJumpPreprocessorException:
        return FlipJumpPreprocessorException(
            f'table-placement: the pool [{hex(self.pool_base)}, {hex(self._pool_limit())}) has no room for its '
            f'tables. Raise pool_end, or assemble without the pool.'
        )

    def lay_out(self, counts: Dict[str, int], width_hist: Dict[str, Dict[int, int]]) -> None:
        """give each group of the census a block, aligned to its size: the hot groups first, then biggest first. Where
        they do not all fit, the last groups get none (their tables go to the overflow region)."""
        self.counts, self.width_hist = counts, width_hist
        if self.heat:
            self._resolve_heat(self.heat)
        bits = {group: self._block_bits(group) for group in counts}
        order = list(self.hot_sites) + sorted(
            (g for g in counts if g not in self.hot_sites), key=lambda g: (-bits[g], g)
        )
        ends = [self.pool_base]  # where the first k blocks end
        for group in order:
            ends.append(-(-ends[-1] // bits[group]) * bits[group] + bits[group])
        # if the groups from k on overflow: the widest slot they need, and the bits all their slots take
        widest, room = [1] * (len(order) + 1), [0] * (len(order) + 1)
        for k in range(len(order) - 1, -1, -1):
            hist = width_hist[order[k]]
            widest[k] = max([widest[k + 1]] + [self._slot_bits(width) for width in hist])
            room[k] = room[k + 1] + sum(self._slot_bits(width) * count for width, count in hist.items())
        kept = range(len(order), len(self.hot_sites) - 1, -1)  # a hot group always gets a block
        limit = self._pool_limit()
        blocked = next((k for k in kept if -(-ends[k] // widest[k]) * widest[k] + room[k] <= limit), None)
        if blocked is None:
            raise self._no_room()
        for k in range(blocked):
            self.groups[order[k]] = (ends[k + 1] - bits[order[k]], None)
        self._used = ends[blocked] - self.pool_base

    def _reserve(self, ops_alignment: int, table_ops: int, group: str, site: str) -> Optional[int]:
        """Return the table's address in its group's block. None if the group has no block, or if the table is too wide
        for a slot (this marks the group broken)."""
        group_expr = self.exprs.get(group)
        group = self.alias.get(group, group)
        hot_site = None
        if group in self.hot_sites:  # (its site key, occurrence): counted whether it is placed or not
            site = heat_key(site)
            hot_site = site, self._hot_seen.get((group, site), 0)
            self._hot_seen[(group, site)] = hot_site[1] + 1
        if group not in self.groups:
            return None  # no block: place_deferred puts it in the overflow region
        if max(table_ops, ops_alignment) > self.max_slot_ops:
            self.declined += 1
            self.broken_groups.add(group)
            return None
        slot_ops = 1 << (max(table_ops, ops_alignment, 1) - 1).bit_length()
        offset, slots, _ = self._bucket_layout(group)[0][slot_ops]
        key = (group, slot_ops)
        raw = self._next_index.get(key, 0)
        if hot_site is not None:
            index = self._hot_index(group, slot_ops, slots, hot_site)
        elif self.spread_factor > 1 and self.counts[group] >= self.spread_min_tables:
            index = _cheapest_order(slots)[raw]
        else:
            index = raw
        base, previous_expr = self.groups[group]
        self.groups[group] = (base, group_expr if group_expr is not None else previous_expr)
        self._next_index[key] = raw + 1
        self.allocated += 1
        return base + offset + index * slot_ops * self.op_bits

    def census(self) -> BlockCensus:
        return BlockCensus(
            {group: sum(hist.values()) for group, hist in self.width_hist.items()},
            {group: max(hist) for group, hist in self.width_hist.items()},
            {group: dict(hist) for group, hist in self.width_hist.items()},
            dict(self.alias),
        )

    def heat_report(self) -> Dict[str, int]:
        """what the heat list found in this program"""
        return {
            'hot_groups': len(self.hot_sites),
            'hot_groups_missing': len(self.hot_missing),
            'hot_groups_ambiguous': len(self.hot_ambiguous),
            'hot_sites_listed': sum(len(sites) for sites in self.hot_sites.values()),
            'hot_sites_matched': self.hot_sites_matched,
            'hot_sites_width_changed': self.hot_sites_width_changed,
        }

    def _resolve_heat(self, heat: Mapping[str, Sequence[HotSite]]) -> None:
        """match the heat list's groups to this program's, and rank each group's sites per width"""
        by_key: Dict[str, List[str]] = {}
        for group in self.counts:
            by_key.setdefault(heat_key(group), []).append(group)
        for listed_key, sites in heat.items():
            key = heat_key(listed_key)
            groups = by_key.get(key, [])
            if len(groups) != 1:
                (self.hot_ambiguous if groups else self.hot_missing).append(key)
                continue
            ranked: Dict[Tuple[str, int], Tuple[int, int]] = {}
            for listed_site, occurrence, slot_ops in sites:
                site = heat_key(listed_site)
                if (site, occurrence) in ranked:
                    raise FlipJumpPreprocessorException(
                        f'pin protection: the heat list names {site!r} #{occurrence} of {key} twice'
                    )
                rank = self.hot_ranks.get((groups[0], slot_ops), 0)
                ranked[(site, occurrence)] = (slot_ops, rank)
                self.hot_ranks[(groups[0], slot_ops)] = rank + 1
            self.hot_sites[groups[0]] = ranked

    def _hot_index(self, group: str, width: int, slots: int, site: Tuple[str, int]) -> int:
        """Return the index of a hot group's table. A listed site takes the index of its rank in cheapest-first order;
        an unlisted one takes the next index after all the ranks."""
        listed = self.hot_sites[group].get(site)
        if listed is not None and listed[0] != width:
            self.hot_sites_width_changed += 1  # its table changed width since the profile
            listed = None
        if listed is not None:
            self.hot_sites_matched += 1
            rank = listed[1]
        else:
            unlisted = self._hot_next.get((group, width), 0)
            self._hot_next[(group, width)] = unlisted + 1
            rank = self.hot_ranks.get((group, width), 0) + unlisted
        return _cheapest_order(slots)[rank]

    def _bucket_layout(self, group: str) -> Tuple[Dict[int, Tuple[int, int, int]], int]:
        """Lay out a group's block: one sub-block per power-of-two table width, biggest first, each aligned to its
        size (so `base + offset == base ^ offset`).
        Returns: `({width: (offset, slots, tables)}, block size in bits)`."""
        cached = self._layout_cache.get(group)
        if cached is not None:
            return cached
        buckets: Dict[int, int] = {}
        for width, count in self.width_hist[group].items():
            slot_ops = 1 << (min(max(width, 1), self.max_slot_ops) - 1).bit_length()
            buckets[slot_ops] = buckets.get(slot_ops, 0) + count
        spread = self.spread_factor if self.counts[group] >= self.spread_min_tables else 1
        sized: Dict[int, Tuple[int, int]] = {}
        for slot_ops, count in buckets.items():
            slots = (1 << (count - 1).bit_length()) * spread
            # a hot width also needs room for the ranks its listed sites reserve
            while slots < count + self.hot_ranks.get((group, slot_ops), 0):
                slots *= 2
            sized[slot_ops] = (slots, slots * slot_ops * self.op_bits)
        layout: Dict[int, Tuple[int, int, int]] = {}
        offset = 0
        for slot_ops in sorted(sized, key=lambda k: (-sized[k][1], -k)):
            slots, bits = sized[slot_ops]
            offset = -(-offset // bits) * bits
            layout[slot_ops] = (offset, slots, buckets[slot_ops])
            offset += bits
        result = self._layout_cache[group] = (layout, 1 << max(0, (offset - 1).bit_length()))
        return result

    def _slot_bits(self, width: int) -> int:
        return (1 << (max(width, 1) - 1).bit_length()) * self.op_bits

    def _block_bits(self, group: str) -> int:
        return self._bucket_layout(group)[1]

    def _pool_limit(self) -> int:
        top = 1 << self.memory_width
        return top if self.pool_end is None else min(top, self.pool_end)

    def pool_start(self) -> int:
        """Return the pool's first address. Before an AFTER_PROGRAM pool places a table it has none, so return the top
        of memory."""
        return 1 << self.memory_width if self.after_program and not self.end else self.pool_base

    def wflips_at(self) -> Optional[int]:
        """Return where the wflip areas go: after the pool, if it sits right after the program and holds tables.
        Otherwise None (their usual place)."""
        return self.end if self.after_program and self.end else None

    def pinned_words(self) -> Dict[Expr, int]:
        """Return the jump words to pin, `{word: block base}`. Skip groups whose tables didn't all fit in their block,
        unless `pin_incomplete_groups` is set or the group is hot (an inline table at `A` is then armed with
        `A ^ base`: slower, still right)."""
        pinned: Dict[Expr, int] = {}
        for group, (base, expr) in self.groups.items():
            if expr is not None and (
                self.pin_incomplete_groups or group in self.hot_sites or group not in self.broken_groups
            ):
                pinned[expr] = base
        return pinned
