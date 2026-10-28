"""
the table pool: places the declared lookup tables where arming them is cheap (`assemble(table_pool=...)`,
`fj --pool-tables`).

A table is one expansion of a macro whose innermost namespace is `__relocatable__` (hex.exact_xor's switch and the
like), called right after the `pad` that aligns it (see preprocessor.relocatable_call). It is reached by a jump
through a word that its arm writes the table's address into, one executed op per set bit. BlockPool gives the tables
dispatched through one word a block, and rests the word at the block's base, so the arm flips only an index.

The pool never changes what the program does: where moving a table would change an operand, the table stays inline
(OffsetGuard), and where a pin cannot hold, the word is left unpinned (assembler.unpin); both are counted by reason
(`unsafe_declines`, `unpins`). A program segment, reserve or wflip area that reaches the pool is an error.
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import re
import sys
from array import array
from typing import Callable, Dict, Iterator, List, Mapping, NoReturn, Optional, Sequence, Set, Tuple

from flipjump.utils.exceptions import FlipJumpAssemblerException, FlipJumpExprException, FlipJumpPreprocessorException
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.op_stream import linear_parts
from flipjump.assembler.inner_classes.ops import NewSegment

LabelsDict = Dict[str, int]

# bit addresses below this are the runtime's (`stl.IO` is op 1, bits [2w, 4w); 1024 covers every w <= 256): a word
# there is neither relocated nor pinned - a block base baked into it would corrupt the program's startup.
RESERVED_BELOW = 1024

# the pointer runtime's words: a macro that names one reads or writes a cell through a pointer, on the word's bare
# value. A pinned word holds `base + value`, so such a program must say which cells a pointer can reach
# (BlockPool.pin_exclude), or no word is pinned.
POINTER_RUNTIME_LABELS = frozenset(
    {'bit.pointers.to_jump', 'bit.pointers.to_flip', 'hex.pointers.to_jump', 'hex.pointers.to_flip'}
)

# a call site's coordinates in a macro-expansion path: `f3:l120:` (a program file) or `s1:l40:` (an stl file)
_SITE_COORDINATES = re.compile(r'(?<![\w.])[fs]\d+:l\d+:')


def heat_key(path: str) -> str:
    """a macro-expansion path (or a group expression) without its call-site coordinates (see BlockPool's `heat`)"""
    return _SITE_COORDINATES.sub('', path)


@dataclasses.dataclass
class BlockCensus:
    """the tables a BlockPool met, by group (the source word's expression): `counts`, `widths` (the widest table,
    in ops), `width_hist` ({width: tables}) and `alias` ({group: the canonical group of the same word})"""

    counts: Dict[str, int]
    widths: Dict[str, int]
    width_hist: Dict[str, Dict[int, int]]
    alias: Dict[str, str]


# BlockPool.defer's verdicts on a table the expansion reached
DEFER_OUT = 'out'  # it leaves the op stream, and is placed once the expansion is done
DEFER_TOO_WIDE = 'too_wide'  # it stays inline (no slot can hold it), and is declined in its turn
DEFER_INLINE = 'inline'  # it stays inline: it has no group


class DeferredTables:
    """Every table the pool queued, in the order the expansion reached them, as parallel arrays (a big program
    queues hundreds of thousands). A table that left the op stream has its ops in the preprocessor's pool stream
    behind a NewSegment whose address is set once it is placed; its labels wait here as bit offsets from its start.
    One that stayed inline (too wide) is queued so the pool declines it in its turn."""

    def __init__(self) -> None:
        self.groups: List[str] = []
        self.aligns = array('I')
        self.sizes = array('I')
        self.segments: List[Optional[NewSegment]] = []  # None for a table that stayed inline
        self.sites: List[str] = []  # each one's heat key (kept only for a pool with a heat list)
        self.label_names: List[str] = []
        self.label_offsets = array('I')
        self.label_ends = array('I')  # table i's labels are label_names[label_ends[i-1]:label_ends[i]]

    def __len__(self) -> int:
        return len(self.groups)

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
        """a label of the table added last, `offset` bits from its start"""
        self.label_names.append(label)
        self.label_offsets.append(offset)
        self.label_ends[-1] = len(self.label_names)

    def labels_of(self, index: int) -> Iterator[Tuple[str, int]]:
        """(label, bit offset) of table `index`"""
        for at in range(self.label_ends[index - 1] if index else 0, self.label_ends[index]):
            yield self.label_names[at], self.label_offsets[at]


def reached_the_pool(address: int, pool_start: int) -> FlipJumpPreprocessorException:
    """the refusal of a program whose own code or data reaches the pool, at `address`"""
    return FlipJumpPreprocessorException(
        f'table-placement: the program reached {hex(address)}, which is inside the table pool at {hex(pool_start)}. '
        f'Raise pool_base, or relocate fewer tables.'
    )


_SEGMENT_START = 'the start of the segment'


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
    violation is refused, `where()` saying where the op is.
    """

    def __init__(self, inline_end: int, labels: LabelsDict):
        self.inline_end = inline_end  # the inline stream lies below it, the moved tables above (the pool's start)
        self.labels = labels  # the preprocessor's (a moved table's are there once it is placed)
        self.anchors: List[int] = []  # the cuts of the inline stream, ascending
        self._anchor_tables: List[str] = []  # ...and each one's table (its start label), or what cut it
        self.moved_tables: List[str] = []  # the moved tables' start labels, in the order they moved
        # a label declared at an anchor before its table's pad -> (the anchor, the tables that moved out of it before)
        self.stranded: Dict[str, Tuple[int, int]] = {}
        self.watched: Set[str] = set()  # the labels checked with no offset too: the stranded ones, and `$`
        self.falls_to = -1  # where the last op that falls through to the next op falls to
        self._pending: List[Tuple[int, int, str]] = []  # (label address, target, label) reaching past the code
        self._starts: List[int] = []  # the moved tables' [start, end), by start
        self._ends: List[int] = []
        self._names: List[str] = []
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
        """the table (its start label) left the inline stream at `anchor`"""
        self._cut(anchor, table)
        self.moved_tables.append(table)

    def pad_realigned(self, address: int, ops_alignment: int) -> None:
        """the `pad` at the inline `address` pads differently than without the pool (a table moved out before it):
        the distance across it changes, so an operand already emitted across it is refused"""
        cut = (
            f'the `pad {ops_alignment}` at {hex(address)}, which pads to another op once a table before it moved out: '
            f'it would no longer point where it did'
        )
        if self.falls_to == address:
            self._refuse('$', f'falls through to {cut}')
        self._cut_across(address, cut, False)

    def segment_started(self, address: int) -> None:
        """a segment starts at the inline `address`, where it does without the pool: an operand already emitted from
        moved code that reaches into it is refused"""
        cut = (
            f'{_SEGMENT_START} at {hex(address)}, which stays where it is while code a table moved out of moves: it '
            f'would no longer be the same distance'
        )
        self._cut_across(address, cut, True)

    def _cut_across(self, address: int, cut: str, moved_only: bool) -> None:
        """refuse the pending operands that reach across `address` (`moved_only`: from moved code), then cut there"""
        for label_address, target, label in self._pending:
            if label_address <= address <= target:
                if not moved_only or self.shift_class(self.piece(label, label_address)) >= 0:
                    self._refuse(_operand(label, target - label_address), f'reaches across {cut}')
        self._cut(address, cut)

    def shift_class(self, piece: int) -> int:
        """-1 for a piece whose code is where it is without the pool (the program's first, or one a segment starts),
        else the piece: two addresses keep their distance only in one piece, or both in a -1 piece"""
        return -1 if piece == 0 or self._anchor_tables[piece - 1].startswith(_SEGMENT_START) else piece

    def _cut(self, anchor: int, table: str) -> None:
        """cut the inline stream into pieces at `anchor`"""
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
        self._anchor_tables.insert(index, table)
        self._pending = [entry for entry in self._pending if entry[1] >= anchor]

    def table_placed(self, start: int, end: int, table: str) -> None:
        """a moved table is at [start, end)"""
        index = bisect.bisect_right(self._starts, start)
        self._starts.insert(index, start)
        self._ends.insert(index, end)
        self._names.insert(index, table)

    def table_at(self, address: int) -> int:
        """the index of the moved table at `address`, or -1"""
        index = bisect.bisect_right(self._starts, address) - 1
        return index if index >= 0 and address < self._ends[index] else -1

    def alignment(self, address: int) -> int:
        """the bits the table at `address` is aligned to: a moved table's size, an inline one's `pad` (else 0)"""
        table = self.table_at(address)
        if table >= 0:
            return (self._ends[table] - self._starts[table] - 1).bit_length()
        return self.pads.get(address, 0)

    def piece(self, label: str, address: int) -> int:
        """the piece of the inline label `label`, at `address`: the anchors below it"""
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
            if table < 0:
                raise reached_the_pool(address, self.inline_end)
            if not self._starts[table] <= target < self._ends[table]:
                self._refuse(
                    operand or _operand(label, offset),
                    f'leaves the moved table {self._names[table]} at {hex(self._starts[table])}: an offset from a '
                    f'moved table\'s label must stay inside it',
                )
            return
        if target >= self.inline_end:
            self._refuse_inside(operand or _operand(label, offset), target)
        piece = self.piece(label, address)
        aim = bisect.bisect_right(self.anchors, target)
        if aim != piece and not self.shift_class(aim) == self.shift_class(piece) == -1:
            index = piece if aim > piece else piece - 1
            self._refuse(operand or _operand(label, offset), self._across(index, label, offset))
        if (offset > 0 or label in self.watched) and self.emitting is not None and target >= self.emitting():
            self._pending.append((address, target, label))  # a table may move out below it: it stays inline

    def check_expr(self, expr: Expr, value: int, is_address: bool = True) -> None:
        """the operand `expr`, whose value is `value`, every label of it declared. An address computed from labels
        (`is_address`: not a wflip's value, which may be `T1 ^ T2`) may not land in the pool either."""
        parts = linear_parts(expr)
        names_a_table = False
        for coefficients, constant in parts:
            names_a_table = self._check_part(coefficients, constant, str(expr)) or names_a_table
        if is_address and not _is_one_sum(expr):
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
        """the operand `expr` kept for labels_resolve: checked now if its labels are declared, so a table it reaches
        over can still stay inline"""
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
            if len(tables) != 1 or -1 in tables or total not in (0, 1):
                self._refuse(
                    operand,
                    f'computes from {moved[0]}, a label of a moved table, with labels outside it: what it computes '
                    f'changes with the move',
                )
            if total == 1:
                self.check(moved[0], labels[moved[0]], value - labels[moved[0]], operand)
            return True
        if len(coefficients) == 1:
            return False  # `2 * L`, the `L` of `L >> 6`: an address as a number
        pieces = sorted(self.piece(label, labels[label]) for label in coefficients)
        if len({self.shift_class(piece) for piece in pieces}) > 1:
            self._refuse(operand, f'takes the distance across {self._cut_name(pieces[0], "be the same")}')
        if total == 1:
            label = next(iter(coefficients))
            self.check(label, labels[label], value - labels[label], operand)
        return False

    def label_below(self, address: int) -> str:
        """the label nearest at or below `address` (slow - a refusal asks)"""
        return max(((value, name) for name, value in self.labels.items() if value <= address), default=(0, ''))[1]

    def _cut_name(self, index: int, no_longer: str) -> str:
        """the cut at anchors[index], as a refusal names it (a moved table's place: it would `no_longer` do)"""
        cut = self._anchor_tables[index]
        if cut.startswith('the '):  # a pad that realigned, or a segment's start
            return cut
        return (
            f'the place of the table {cut}, which moved out of {hex(self.anchors[index])}, so it would no longer '
            + (no_longer)
        )

    def _across(self, index: int, label: str, offset: int) -> str:
        """why `label + offset` may not reach across the cut at anchors[index]"""
        if offset:
            return f'reaches across {self._cut_name(index, "point where it did")}'
        cut = self._anchor_tables[index]
        if cut.startswith('the '):
            return f'names {label}, declared before {cut}'
        return (
            f'names {label}, declared at {hex(self.anchors[index])} before the `pad` of the table {cut}: it named the '
            f'table\'s start, which moved, while the label stays with the code after it'
        )

    def _refuse_inside(self, operand: str, target: int) -> NoReturn:
        """an operand that is not one of a moved table's labels plus a constant lands in the pool, at `target`"""
        table = self.table_at(target)
        where = f'inside the moved table {self._names[table]} at {hex(self._starts[table])}' if table >= 0 else ''
        self._refuse(
            operand,
            f'lands at {hex(target)}, {where or "in the table pool"}, and is not one of its labels plus a constant',
        )

    def _refuse(self, operand: str, what: str) -> NoReturn:
        if self.emitting is not None and self.emitting() > self.inline_end:
            raise reached_the_pool(self.emitting(), self.inline_end)  # the program's own code did: that is the cause
        raise FlipJumpAssemblerException(
            f'table-placement: the operand `{operand}`{self.where()} {what}. Address a moved table by its own labels, '
            f'or assemble without a table pool.'
        )


def _is_one_sum(expr: Expr) -> bool:
    """is all of `expr` one sum - of labels and numbers, by +, - and * by a number (see linear_parts)?"""
    value = expr.value
    if not isinstance(value, tuple):
        return True
    op, args = value
    if len(args) != 2 or op not in ('+', '-', '*') or not all(_is_one_sum(arg) for arg in args):
        return False
    return op != '*' or not args[0].all_unknown_labels() or not args[1].all_unknown_labels()


def _moves_with(expr: Expr, label: str, address: int, value: int) -> bool:
    """does `expr` (naming only `label`, at `address`; its value `value`) move as far as its label does?"""
    far = 1 << (max(address, value).bit_length() + 1)
    try:
        return expr.exact_eval({label: address + far}) == value + far
    except FlipJumpExprException:
        return False


def _operand(label: str, offset: int) -> str:
    """`label + offset` as the source would say it"""
    return f'{label} + {offset}' if offset > 0 else f'{label} - {-offset}' if offset else label


def _cheapest(bits: int) -> Iterator[int]:
    """the values below 2**bits, fewest set bits first, ascending within a popcount (generated lazily)"""
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
    """every index of a block of `slots`, lowest popcount first (shared: read only)"""
    return sorted(range(slots), key=lambda v: (bin(v).count('1'), v))


class BlockPool:
    """Give the tables dispatched through ONE jump word a block, so that arming one flips only its INDEX.

    A hex variable is an op `;val*dw`: its jump word holds the value, and `wflip src+w, switch` makes it `switch +
    digit`. If the word RESTS at a block base B (it is PINNED) and its tables live in the block at B, the arm flips
    `switch ^ B`, the table's index. Every writer of the word installs an address V by flipping `V ^ B` (see
    assembler.BinaryData.pinned_value), so stl.comp_if1, hex.shifts.* and hex.tables.* keep working. A pinned word
    cannot be read or written raw: see POINTER_RUNTIME_LABELS and `pin_exclude(address, labels) -> bool`, which vetoes
    the pin of a cell's jump word that a pointer can reach.

    ONE PASS: the preprocessor takes each table out of the op stream as it reaches it (`defer`), and has the pool
    place them all when it is done (`place_deferred`), sizing the blocks from that expansion's census (`census()`).
    Blocks are power-of-two sized and aligned to their own size, one sub-block per table width (_bucket_layout);
    groups whose words resolve to one address share a block (alias). A table too wide for any slot stays inline;
    where the blocks do not fit, the last groups go to an overflow region.

    PIN PROTECTION (`heat`, for re-placing a profiled program): the hot groups are laid out first, in heat order, are
    always pinned, and in each slot width the k-th listed site gets the k-th cheapest index (_hot_index).

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
    ):
        """
        @param pool_base: the pool's first address, above everything the program reaches
        @param span_bits: how far above pool_base the pool may reach (None: to the top of memory)
        @param max_slot_ops: the widest table a slot holds (32 fits the stl's widest, `hex.cmp`'s 31 ops)
        @param spread: give a group of at least `spread_min_count` tables `spread` times the slots, and hand out only
        the cheapest indices (an arm costs `2 * popcount(index)`)
        @param pin_broken: pin a group although some of its tables got no slot - correct because every arm of the word
        is flipped xor its base, an inline table's too (see pinned_words)
        @param heat: pin protection: {heat_key(group): [(heat_key(site path), occurrence, slot_ops), ...]}, each
        group's hot sites hottest first (`occurrence` numbers the sites that share a key, in the order reached)
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
        self.auto_base = False  # (for_flat) pool_base is chosen once the program's end is known
        self.pin_exclude: Optional[Callable[[int, LabelsDict], bool]] = None
        self.pin_conflicts = 0
        self.reads_words_raw = False  # set by the preprocessor when a macro naming POINTER_RUNTIME_LABELS expands
        self.laid_out = False  # set by the preprocessor that lays this pool out
        self.offset_guard: Optional[OffsetGuard] = None
        self.unsafe_declines: Dict[str, int] = {}  # tables kept inline: moving them would change an operand
        self.unpins: Dict[str, int] = {}  # words left unpinned: a pin could not hold (assembler.unpin)
        self.reserved_words = 0  # tables through the runtime's words, never relocated
        self.deferred = 0  # tables taken out of the op stream
        self.allocated = 0  # ...placed in a block
        self.overflowed = 0  # ...placed in the overflow region
        self.declined = 0  # tables kept inline: too wide for any slot
        self.ungrouped = 0
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
        self.hot_sites_matched = 0
        self.hot_sites_width_changed = 0

    @classmethod
    def for_flat(cls, memory_width: int) -> BlockPool:
        """`fj --pool-tables`'s pool: based at the first power of two above the program, once its end is known, with
        the program's wflip areas after it (wflips_at)"""
        pool = cls(memory_width, 1 << memory_width)
        pool.auto_base = True
        return pool

    def defer(
        self, ops_alignment: int, table_ops: int, group: Optional[str], group_expr: Optional[Expr], labels_prefix: str
    ) -> str:
        """what becomes of a table the expansion reached, at its pad (DEFER_*), counted in the census first. A
        subclass that overrides it sees every table at its pad, in order, with its site path."""
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
        """the address of every queued table (0 for one that stayed inline), in the order the expansion reached them.
        `labels`: the program's (a deferred table's are not in yet), `end`: where the program ends.

        Merges the groups whose words these labels resolve to one address (a word that names a deferred table's label
        cannot be resolved, and is counted in alias_unresolved), lays the blocks out, then places every table in turn.
        """
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
        addresses = array('Q', bytes(8 * len(tables)))  # 0: it stayed inline (too wide, declined in its turn)
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
        """`addresses`, the end of the last table placed at them noted"""
        for index, address in enumerate(addresses):
            if address:
                self.end = max(self.end, address + tables.sizes[index] * self.op_bits)
        return addresses

    def _raw(self) -> bool:
        """does the program read words raw (the pointer runtime, and no pin_exclude)? Then no word can be pinned
        (assembler.unpin), and a heat list aside, the tables are placed by _place_cheapest"""
        return self.reads_words_raw and self.pin_exclude is None and not self.heat

    def _place_cheapest(self, tables: DeferredTables) -> Sequence[int]:
        """place_deferred for a raw program: no word is pinned, so each table takes a cheap ADDRESS instead - a slot of
        its width's power of two at the offsets with the fewest set bits of a region of 256 times their slots, if the
        pool has room for it (unused slots emit no data), else of their slots"""
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
        addresses = array('Q', bytes(8 * len(tables)))
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
        """Size the blocks of the census (`counts` and `width_hist`, by canonical group) and give each a base: the hot
        groups first, in heat order, then biggest first - each aligned to its own size, so in descending size each
        lands on an address the ones before already aligned past. Where they do not all fit below the pool's end, the
        groups at the end of that order get no block: their tables go to the overflow region after the blocks (see
        place_deferred), and their words are not pinned."""
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
        """the address of a table (of the raw `group`, at the site `labels_prefix`) in its group's block, or None: its
        group got no block, or it is too wide for any slot - then its group breaks (see pinned_words)."""
        group = self.alias.get(group, group)
        hot_site = self._hot_site(group, labels_prefix) if group in self.hot_sites else None
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
        """the census of every table this pool was offered, merged by the alias its blocks were sized with (placed)"""
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
        """match the heat list's groups to this program's and rank each group's sites per width"""
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

    def _hot_site(self, group: str, labels_prefix: str) -> Tuple[str, int]:
        """(site key, occurrence) of the table being placed in a hot group, counted for every table of it"""
        site = heat_key(labels_prefix)
        occurrence = self._hot_seen.get((group, site), 0)
        self._hot_seen[(group, site)] = occurrence + 1
        return site, occurrence

    def _hot_index(self, group: str, width: int, slots: int, site: Tuple[str, int]) -> int:
        """A table's index in a hot group: a listed site takes the index its rank reserved, every other table the next
        one after all the reserved ranks - positions in ONE cheapest-first order, so no two tables share a slot (each
        width is sized for its tables plus its ranks)."""
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
        each aligned to its own size and laid out biggest first, so a sub-block's offset bits sit above the bits its
        slots use and `base ^ offset` is `base + offset`"""
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
        """the bits of an overflow region's slot for a table `width` ops wide (or aligned)"""
        return (1 << (max(width, 1) - 1).bit_length()) * self.op_bits

    def _block_bits(self, group: str) -> int:
        return self._bucket_layout(group)[1]

    def _pool_limit(self) -> int:
        """the first address past the pool: pool_base + span_bits, and never past the memory"""
        top = 1 << self.memory_width
        return top if self.span_bits is None else min(top, self.pool_base + self.span_bits)

    def pool_start(self) -> int:
        """the lowest address the pool places a table at (for_flat before it placed one: the top of memory)"""
        return 1 << self.memory_width if self.auto_base and not self.end else self.pool_base

    def wflips_at(self) -> Optional[int]:
        """where the wflip areas go (assembler.BinaryData): after a for_flat pool that placed a table, else None"""
        return self.end if self.auto_base and self.end else None

    def pinned_words(self) -> Dict[Expr, int]:
        """{source-word Expr: block base} - resolved by the caller once labels are known.

        A group that failed to place every table is excluded (broken_groups) unless `pin_broken` is set or the group
        is hot: the word rests at `value*dw ^ base` and an inline table at `A` is armed with `A ^ base`, which cancels
        to `A + value*dw` - so a declined table then costs more, but still dispatches right."""
        return {
            expr: base
            for group, (base, expr) in self.groups.items()
            if expr is not None and (self.pin_broken or group in self.hot_sites or group not in self.broken_groups)
        }
