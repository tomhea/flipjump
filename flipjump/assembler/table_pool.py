"""
the table pool: places the declared lookup tables (see preprocessor.relocatable_call) where arming them is cheap.
It never changes what the program does: a table whose move would change an operand stays inline (OffsetGuard), a pin
that cannot hold is dropped (assembler.unpin), and a program that reaches the pool is refused.
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import re
import sys
from array import array
from typing import Callable, Dict, Iterable, Iterator, List, Mapping, NoReturn, Optional, Sequence, Set, Tuple, Union

from flipjump.utils.exceptions import FlipJumpAssemblerException, FlipJumpExprException, FlipJumpPreprocessorException
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.op_stream import linear_parts
from flipjump.assembler.inner_classes.ops import NewSegment

LabelsDict = Dict[str, int]
# a cell the program reads or writes raw: its label, alone (the cell runs to the next label) or with its length in ops
UnpinnedCell = Union[str, Tuple[str, int]]

# the runtime's words lie below this bit address (`stl.IO` is op 1; 1024 covers every w <= 256): never pinned
RESERVED_BELOW = 1024

# a macro naming these reads or writes words raw through a pointer: without pin_all_but, no word is pinned
POINTER_RUNTIME_LABELS = frozenset(
    {'bit.pointers.to_jump', 'bit.pointers.to_flip', 'hex.pointers.to_jump', 'hex.pointers.to_flip'}
)

# a call site's coordinates in a macro-expansion path: `f3:l120:` or `s1:l40:`
_SITE_COORDINATES = re.compile(r'(?<![\w.])[fs]\d+:l\d+:')


def heat_key(path: str) -> str:
    """a macro-expansion path (or a group expression) without its call-site coordinates (see BlockPool's `heat`)"""
    return _SITE_COORDINATES.sub('', path)


@dataclasses.dataclass
class BlockCensus:
    """the tables a BlockPool met, by group (the source word's expression)"""

    counts: Dict[str, int]
    widths: Dict[str, int]
    width_hist: Dict[str, Dict[int, int]]
    alias: Dict[str, str]


# BlockPool.defer's verdicts on a table the expansion reached
DEFER_OUT = 'out'  # it leaves the op stream, and is placed once the expansion is done
DEFER_TOO_WIDE = 'too_wide'  # it stays inline (no slot can hold it), and is declined in its turn
DEFER_INLINE = 'inline'  # it stays inline: it has no group


class DeferredTables:
    """the tables the pool queued, in expansion order, as compact parallel arrays (a big program has ~10^5); a moved
    table's labels wait here as bit offsets from its start"""

    def __init__(self) -> None:
        self.groups: List[str] = []
        self.aligns = array('I')
        self.sizes = array('I')
        self.segments: List[Optional[NewSegment]] = []  # None for a table that stayed inline
        self.sites: List[str] = []  # each one's heat key (kept only for a pool with a heat list)
        self.label_names: List[str] = []
        self.label_offsets = array('I')
        self.label_ends = array('I')  # table i's labels are label_names[label_ends[i-1]:label_ends[i]]

    def add(
        self, group: str, ops_alignment: int, table_ops: int, site: Optional[str], segment: Optional[NewSegment]
    ) -> None:
        self.groups.append(sys.intern(group))
        self.aligns.append(ops_alignment)
        self.sizes.append(table_ops)
        self.segments.append(segment)
        if site is not None:
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
    """Keeps an operand from pointing elsewhere once the tables moved.

    A moved table leaves the inline stream at its ANCHOR, and the code after it moves up. The anchors (and the pads
    that pad differently, and the segment starts) cut the inline stream into PIECES; a label at an anchor lies before
    it if it was declared before the table's `pad`. Every +/- sum of an operand (linear_parts) must keep its meaning:
    a moved table's label plus a constant must stay inside that table, an inline label plus a constant must stay in
    its piece, labels whose distance is taken must lie in one piece, and an address computed from labels may not land
    in the pool. `$` is an inline label at the next op, and an op that falls through (`a;`) reaches the next op. A
    label times another coefficient (`2 * L`, `(L >> 6) & 15`) is an address used as a number, and is not checked.

    The preprocessor checks the operands whose labels it knows, labels_resolve the rest. An operand reaching past the
    code emitted so far is PENDING: the table whose place it reaches over stays inline (may_move). Every other
    violation is refused (`refuse`), `where()` saying where the op is.
    """

    def __init__(self, inline_end: int, labels: LabelsDict):
        self.inline_end = inline_end  # the inline stream lies below it, the moved tables above (the pool's start)
        self.labels = labels  # the preprocessor's (a moved table's are there once it is placed)
        self.anchors: List[int] = []  # the cuts of the inline stream, ascending
        self.cuts: List[Tuple[str, bool]] = []  # ...each one as a refusal names it, and whether a segment starts there
        self.moved_tables: List[str] = []  # the moved tables' start labels, in the order they moved
        # a label declared at an anchor before its table's pad -> (the anchor, the tables that moved out of it before)
        self.stranded: Dict[str, Tuple[int, int]] = {}
        self.watched: Set[str] = set()  # the labels checked with no offset too: the stranded ones, and `$`
        self.falls_to = -1  # where the last op that falls through to the next op falls to
        self._pending: List[Tuple[int, int, str]] = []  # (label address, target, label) reaching past the code
        self.tables: List[Tuple[int, int, str]] = []  # the placed tables' (start, end, start label), by start
        self.where: Callable[[], str] = lambda: ''
        self.emitting: Optional[Callable[[], int]] = None  # the next inline address the preprocessor emits at
        self.pads: Dict[int, int] = {}  # inline address -> the bits a `pad` aligned it to (0: a segment starts)
        self.reach = 0  # the highest address an operand checked so far lands at

    def may_move(self, anchor: int) -> Optional[str]:
        """why the table about to move out of `anchor` must stay inline, or None"""
        if self.falls_to == anchor:
            return 'the op before it falls through to it'
        if any(address <= anchor <= target for address, target, _ in self._pending):
            return 'an operand reaches over its place'
        return None

    def table_moved(self, anchor: int, table: str) -> None:
        self._cut(anchor, f'the place of the table {table}, which moved out of {hex(anchor)}', False)
        self.moved_tables.append(table)

    def pad_realigned(self, address: int, ops_alignment: int) -> None:
        """the `pad` at `address` pads differently than without the pool: an operand across it is refused"""
        cut = f'the `pad {ops_alignment}` at {hex(address)}, which pads to another op once a table before it moved out'
        if self.falls_to == address:
            self.refuse('$', f'falls through to {cut}')
        self._cut_across(address, cut, False)

    def segment_started(self, address: int) -> None:
        """a segment starts at `address`, as without the pool: an operand from moved code into it is refused"""
        self._cut_across(address, f'the start of the segment at {hex(address)}, which stays where it is', True)

    def _cut_across(self, address: int, cut: str, segment: bool) -> None:
        for label_address, target, label in self._pending:
            if label_address <= address <= target:
                if not segment or self.shift_class(self.piece(label, label_address)) >= 0:
                    self.refuse(_operand(label, target - label_address), f'reaches across {cut}')
        self._cut(address, cut, segment)

    def shift_class(self, piece: int) -> int:
        """-1 for a piece whose code does not move (the first, or a segment's), else the piece itself"""
        return -1 if piece == 0 or self.cuts[piece - 1][1] else piece

    def _cut(self, anchor: int, cut: str, segment: bool) -> None:
        anchors = self.anchors
        index = bisect.bisect_right(anchors, anchor)
        before = index - bisect.bisect_left(anchors, anchor)
        labels = self.labels
        for name in reversed(labels):  # the labels declared at the anchor so far: before the table's pad
            address = labels[name]
            if address == anchor:
                if name in self.stranded:
                    break  # declared before an earlier table that moved out of the same address
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
        """the bits the table at `address` is aligned to: a moved table's size, an inline one's `pad`"""
        table = self.table_at(address)
        return (table[1] - table[0] - 1).bit_length() if table else self.pads.get(address, 0)

    def piece(self, label: str, address: int) -> int:
        stranded = self.stranded.get(label)
        if stranded is not None:
            return bisect.bisect_left(self.anchors, stranded[0]) + stranded[1]
        return bisect.bisect_right(self.anchors, address)

    def check(self, label: str, address: int, offset: int, operand: Optional[str] = None) -> None:
        """the operand `label + offset` (or a sum that is one: `operand`), where `label` is at `address`"""
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
            self._pending.append((address, target, label))  # a table may move out below it: it stays inline

    def check_expr(self, expr: Expr, value: int, is_address: bool = True) -> None:
        """the operand `expr` (= `value`); `is_address`: not a wflip's value, so it may not land in the pool"""
        parts = linear_parts(expr)
        names_a_table = False
        for coefficients, constant in parts:
            names_a_table = self._check_part(coefficients, constant, str(expr)) or names_a_table
        if is_address:  # (a sum of one label is checked as its part was, again)
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
        """an operand kept for labels_resolve, checked now if its labels are declared"""
        if all(label in self.labels for label in expr.all_unknown_labels()):
            self.check_expr(expr, expr.exact_eval(self.labels), is_address)

    def _check_part(self, coefficients: Dict[str, int], constant: int, operand: str) -> bool:
        """one +/- sum of `operand`; True if it names a moved table's label"""
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
            return False  # `2 * L`, the `L` of `L >> 6`: an address as a number
        pieces = sorted(self.piece(label, labels[label]) for label in coefficients)
        if len({self.shift_class(piece) for piece in pieces}) > 1:
            self.refuse(operand, f'takes the distance across {self.cuts[pieces[0]][0]}')
        if total == 1:
            label = next(iter(coefficients))
            self.check(label, labels[label], value - labels[label], operand)
        return False

    def label_below(self, address: int) -> str:
        """the label nearest at or below `address` (slow - a refusal asks)"""
        return max(((value, name) for name, value in self.labels.items() if value <= address), default=(0, ''))[1]

    def _refuse_inside(self, operand: str, target: int) -> None:
        table = self.table_at(target)
        where = f'inside the moved table {table[2]} at {hex(table[0])}' if table else 'in the table pool'
        self.refuse(operand, f'lands at {hex(target)}, {where}, and is not one of its labels plus a constant')

    def refuse(self, operand: str, why: str) -> NoReturn:
        """refuse the program: moving the tables would change what `operand` (of the op `where()` names) means"""
        if self.emitting is not None and self.emitting() > self.inline_end:
            raise reached_the_pool(self.emitting(), self.inline_end)  # the program's own code did: that is the cause
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
    """range(slots) in _cheapest's order (shared: read only)"""
    return sorted(range(slots), key=lambda v: (bin(v).count('1'), v))


class BlockPool:
    """Give the tables dispatched through ONE jump word a block, so that arming one flips only its INDEX.

    `wflip src+w, switch` arms a hex variable's jump word. If the word RESTS at a block base B (it is PINNED) and its
    tables live in the block, the arm flips `switch ^ B`, the table's index, and every writer of the word flips
    `V ^ B` (assembler.BinaryData.pinned_value). A pinned word holds `B ^ value`, so a word the program reads raw
    (through a pointer, POINTER_RUNTIME_LABELS) must not be pinned: `pin_all_but` names the cells to leave out.

    ONE PASS: the preprocessor takes each table out of the op stream (`defer`) and has the pool place them all once
    the expansion is done (`place_deferred`), the blocks sized from its census. Blocks are power-of-two sized and
    aligned, one sub-block per table width (_bucket_layout); groups of one word share a block (alias); where the
    blocks do not fit, the last groups go to an overflow region. `heat` (pin protection) lays the hot groups out
    first, always pins them, and gives each width's k-th listed site the k-th cheapest index.

    A pool lays out one assembly: construct a fresh one for every assemble().
    """

    def __init__(
        self,
        memory_width: int,
        pool_base: int,
        *,
        span_bits: Optional[int] = None,
        max_slot_ops: int = 32,
        spread: int = 1,
        spread_min_count: int = 256,
        pin_broken: bool = False,
        heat: Optional[Mapping[str, Sequence[Tuple[str, int, int]]]] = None,
        pin_all_but: Optional[Iterable[UnpinnedCell]] = None,
    ):
        """
        @param span_bits: how far above pool_base the pool may reach (None: to the top of memory)
        @param max_slot_ops: the widest table a slot holds (32 fits the stl's widest, `hex.cmp`'s 31 ops)
        @param spread: a group of >= `spread_min_count` tables gets `spread` times the slots, the cheapest indices used
        @param pin_broken: pin a group although some of its tables got no slot (see pinned_words)
        @param heat: {heat_key(group): [(heat_key(site path), occurrence, slot_ops), ...]}, hottest site first
        @param pin_all_but: pin every word but the words of these cells: each cell the program reads or writes RAW
            (through a pointer, or by jumping through its word), as a label (the cell runs to the next label) or
            (label, length in ops). Leaving such a cell out makes the program run WRONG, not slower. [] pins every
            word. None (the default): the cells are not known - a program that uses the pointer runtime then pins
            no word (its tables still go to cheap addresses). A label the program does not define is skipped
            (listed in pin_all_but_missing).
        """
        self.memory_width = memory_width
        self.op_bits = 2 * memory_width
        self.pool_base = pool_base
        self.span_bits = span_bits
        self.max_slot_ops = max_slot_ops
        self.spread = max(1, spread)
        self.spread_min_count = spread_min_count
        self.pin_broken = pin_broken
        self.heat = heat
        self.pin_all_but = None if pin_all_but is None else tuple(pin_all_but)
        self.pin_all_but_missing: List[str] = []  # the labels of pin_all_but the program does not define
        self.auto_base = False  # (for_flat) pool_base is chosen once the program's end is known
        self.pin_conflicts = 0
        self.reads_words_raw = False  # set by the preprocessor when a macro naming POINTER_RUNTIME_LABELS expands
        self.laid_out = False  # set by the preprocessor that lays this pool out
        self.offset_guard: Optional[OffsetGuard] = None
        self.unsafe_declines: Dict[str, int] = {}  # tables kept inline: moving them would change an operand
        self.unpins: Dict[str, int] = {}  # words left unpinned: a pin could not hold (assembler.unpin)
        # tables: through the runtime's words (never moved), taken out of the op stream, placed in a block, placed in
        # the overflow region, kept inline as too wide for any slot, kept inline with no group
        self.reserved_words = self.deferred = self.allocated = self.overflowed = self.declined = self.ungrouped = 0
        self.alias_unresolved = 0  # group words that name a deferred table's label: unknown when the pool is sized
        self.end = 0  # the end of the last table placed
        # the census, by group: RAW while the expansion runs, merged by `alias` once it is placed
        self.counts: Dict[str, int] = {}
        self.width_hist: Dict[str, Dict[int, int]] = {}
        self.exprs: Dict[str, Expr] = {}  # raw group -> its source-word Expr
        self.alias: Dict[str, str] = {}
        self.groups: Dict[str, Tuple[int, Optional[Expr]]] = {}  # group -> (block base, source-word Expr)
        self.broken_groups: Set[str] = set()  # a group that could not place every table is not pinned (pinned_words)
        self._used = 0  # the blocks' bits, from pool_base
        self._layout_cache: Dict[str, Tuple[Dict[int, Tuple[int, int, int]], int]] = {}
        self._next_index: Dict[Tuple[str, int], int] = {}  # (group, slot width) -> tables placed
        self.hot_sites: Dict[str, Dict[Tuple[str, int], Tuple[int, int]]] = {}  # group -> site -> (width, rank)
        self.hot_ranks: Dict[Tuple[str, int], int] = {}  # (group, width) -> ranks reserved for its sites
        self._hot_seen: Dict[Tuple[str, str], int] = {}  # (group, site) -> occurrences reached so far
        self._hot_next: Dict[Tuple[str, int], int] = {}  # (group, width) -> unlisted tables so far
        self.hot_missing: List[str] = []  # listed groups this program does not have
        self.hot_ambiguous: List[str] = []  # listed keys that name two of its groups
        self.hot_sites_matched = self.hot_sites_width_changed = 0

    @classmethod
    def for_flat(cls, memory_width: int, pin_all_but: Optional[Iterable[UnpinnedCell]] = None) -> BlockPool:
        """`fj --pool-tables`'s pool: at the first power of two above the program, its wflip areas after it"""
        pool = cls(memory_width, 1 << memory_width, pin_all_but=pin_all_but)
        pool.auto_base = True
        return pool

    def defer(
        self, ops_alignment: int, table_ops: int, group: Optional[str], group_expr: Optional[Expr], labels_prefix: str
    ) -> str:
        """what becomes of a table the expansion reached, at its pad (DEFER_*), counted in the census first"""
        width = max(table_ops, ops_alignment)
        if group is not None:
            self.counts[group] = self.counts.get(group, 0) + 1
            hist = self.width_hist.setdefault(group, {})
            hist[width] = hist.get(width, 0) + 1
            if group_expr is not None:
                self.exprs.setdefault(group, group_expr)
        elif not self._raw():  # (a raw program's tables need no group: see _place_cheapest)
            self.ungrouped += 1
            return DEFER_INLINE
        if width > self.max_slot_ops and not self._raw():
            return DEFER_TOO_WIDE
        self.deferred += 1
        return DEFER_OUT

    def place_deferred(self, tables: DeferredTables, labels: LabelsDict, end: int) -> Sequence[int]:
        """the address of every queued table (0: it stayed inline), after merging the groups whose words `labels`
        resolve to one address; `end`: where the program ends"""
        if self.auto_base:  # the first power of two above the program, or right above it where there is none
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
            counts[canonical] = counts.get(canonical, 0) + self.counts[group]
            into = width_hist.setdefault(canonical, {})
            for width, count in hist.items():
                into[width] = into.get(width, 0) + count
        if self._raw():
            self.counts, self.width_hist = counts, width_hist
            return self._place_cheapest(tables)
        self.lay_out(counts, width_hist)
        addresses = array('Q', bytes(8 * len(tables.sizes)))  # 0: it stayed inline (too wide, declined in its turn)
        overflow = []  # (slot bits, index) of the tables whose group got no block
        for index, group in enumerate(tables.groups):
            site = tables.sites[index] if self.heat else ''
            address = self.reserve(tables.aligns[index], tables.sizes[index], group, self.exprs.get(group), site)
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
        for index, address in enumerate(addresses):
            if address:
                self.end = max(self.end, address + tables.sizes[index] * self.op_bits)
        return addresses

    def _raw(self) -> bool:
        """does the program read words raw with no pin_all_but to say which? (then none is pinned: _place_cheapest)"""
        return self.reads_words_raw and self.pin_all_but is None

    def unpinned_words(self, labels: LabelsDict) -> Set[int]:
        """the addresses of the words of pin_all_but's cells (resolved by `labels`): words the assembler must not pin"""
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
        """place_deferred for a raw program, whose words cannot be pinned: each table takes a cheap ADDRESS - a slot at
        the offsets with the fewest set bits of a region of 256 times its width's slots if it fits, else of its slots"""
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
            f'tables. Raise span_bits, or assemble without the pool.'
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
        # the overflow region of the groups from k on: its widest slot, and all its slots
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

    def reserve(
        self, ops_alignment: int, table_ops: int, group: str, group_expr: Optional[Expr], labels_prefix: str
    ) -> Optional[int]:
        """the table's address in its group's block, or None: no block, or too wide (its group breaks)"""
        group = self.alias.get(group, group)
        hot_site = None
        if group in self.hot_sites:  # (its site key, occurrence): counted whether it is placed or not
            site = heat_key(labels_prefix)
            hot_site = site, self._hot_seen.get((group, site), 0)
            self._hot_seen[(group, site)] = hot_site[1] + 1
        if group not in self.groups:
            return None  # no block: the overflow region (place_deferred)
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
        elif self.spread > 1 and self.counts[group] >= self.spread_min_count:
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
            dict(self.counts),
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

    def _resolve_heat(self, heat: Mapping[str, Sequence[Tuple[str, int, int]]]) -> None:
        """match the heat list's groups to this program's, and rank each group's sites per width"""
        by_key: Dict[str, List[str]] = {}
        for group in self.counts:
            by_key.setdefault(heat_key(group), []).append(group)
        for key, sites in heat.items():
            groups = by_key.get(key, [])
            if len(groups) != 1:
                (self.hot_ambiguous if groups else self.hot_missing).append(key)
                continue
            ranked: Dict[Tuple[str, int], Tuple[int, int]] = {}
            for site, occurrence, slot_ops in sites:
                if (site, occurrence) in ranked:
                    raise FlipJumpPreprocessorException(
                        f'pin protection: the heat list names {site!r} #{occurrence} of {key} twice'
                    )
                rank = self.hot_ranks.get((groups[0], slot_ops), 0)
                ranked[(site, occurrence)] = (slot_ops, rank)
                self.hot_ranks[(groups[0], slot_ops)] = rank + 1
            self.hot_sites[groups[0]] = ranked

    def _hot_index(self, group: str, width: int, slots: int, site: Tuple[str, int]) -> int:
        """a hot group's table's index: a listed site's rank, else the next after every rank, in cheapest-first order"""
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
        """({slot_ops: (offset in the block, slots, tables)}, the block's bits): a sub-block per power-of-two width,
        each aligned to its size, biggest first (so `base ^ offset` is `base + offset`)"""
        cached = self._layout_cache.get(group)
        if cached is not None:
            return cached
        buckets: Dict[int, int] = {}
        for width, count in self.width_hist[group].items():
            slot_ops = 1 << (min(max(width, 1), self.max_slot_ops) - 1).bit_length()
            buckets[slot_ops] = buckets.get(slot_ops, 0) + count
        spread = self.spread if self.counts[group] >= self.spread_min_count else 1
        sized: Dict[int, Tuple[int, int]] = {}
        for slot_ops, count in buckets.items():
            slots = (1 << (count - 1).bit_length()) * spread
            while slots < count + self.hot_ranks.get((group, slot_ops), 0):  # a hot width holds its ranks too
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
        return top if self.span_bits is None else min(top, self.pool_base + self.span_bits)

    def pool_start(self) -> int:
        """the lowest address the pool places a table at (an unplaced for_flat pool: the top of memory)"""
        return 1 << self.memory_width if self.auto_base and not self.end else self.pool_base

    def wflips_at(self) -> Optional[int]:
        """where the wflip areas go (assembler.BinaryData): after a for_flat pool that placed a table, else None"""
        return self.end if self.auto_base and self.end else None

    def pinned_words(self) -> Dict[Expr, int]:
        """{source-word Expr: block base}; a broken group's word too if `pin_broken` or hot (an inline table at `A` is
        armed with `A ^ base`: slower, still right)"""
        return {
            expr: base
            for group, (base, expr) in self.groups.items()
            if expr is not None and (self.pin_broken or group in self.hot_sites or group not in self.broken_groups)
        }
