"""
The table pool: moves the stl's lookup tables (see preprocessor.relocatable_call) to addresses where arming them
is cheap.

It never changes what the program does. A table whose move would change an operand stays inline (OffsetGuard, in
offset_guard.py), a pin that cannot hold is dropped (pins.unpin), and a program whose code runs into the pool
is refused.

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

Every address here is a python int, so the pool works at any memory width. Only the column that holds the
addresses of a big program's tables is a typed array, where an address fits one (w <= 64).
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import re
import sys
from array import array
from typing import (
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    MutableSequence,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from flipjump.utils.constants import MACRO_SEPARATOR_STRING
from flipjump.utils.exceptions import FlipJumpExprException, FlipJumpPreprocessorException
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.offset_guard import OffsetGuard
from flipjump.assembler.inner_classes.ops import NewSegment

LabelsDict = Dict[str, int]
# A cell the program reads or writes raw: its label (the cell runs to the next label), or (label, length in ops).
UnpinnedCell = Union[str, Tuple[str, int]]


def reserved_below(memory_width: int) -> int:
    """Return the bit address the runtime's words lie below: the first op and the IO op (stl.IO). They are never
    pinned."""
    return 4 * memory_width


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


# Why a table stayed inline (the keys of BlockPool.kept_inline; OffsetGuard.may_move gives two more)
KEPT_NO_JUMP_WORD = 'it is dispatched through no word the pool can name'
KEPT_RUNTIME_WORD = "it is dispatched through one of the runtime's words"
KEPT_TOO_WIDE = 'it is wider than any slot'


class DeferredTables:
    """The tables the pool queued, in expansion order, kept as compact parallel columns (a big program has ~10^5).
    A moved table's labels wait here, as offsets from its start, until it is placed.
    No column holds an address: they count ops and labels, so they do not depend on the memory width."""

    def __init__(self) -> None:
        self.groups: List[str] = []
        self.aligns: List[int] = []  # in ops. A list: a `pad` may be any size (the usual small ones cost no object)
        self.sizes = array('I')  # in ops
        self.segments: List[Optional[NewSegment]] = []  # None for a table that stayed inline
        self.sites: List[str] = []  # each one's heat key
        self.label_names: List[str] = []
        self.label_offsets = array('I')  # in ops, from the table's start
        self.label_ends = array('I')  # table i's labels are label_names[label_ends[i-1]:label_ends[i]]

    def add(self, group: str, ops_alignment: int, table_ops: int, site: str, segment: Optional[NewSegment]) -> None:
        self.groups.append(sys.intern(group))
        self.aligns.append(ops_alignment)
        self.sizes.append(table_ops)
        self.segments.append(segment)
        self.sites.append(site)
        self.label_ends.append(len(self.label_names))

    def add_label(self, label: str, offset_ops: int) -> None:
        """Add a label to the last table added, `offset_ops` ops from its start."""
        self.label_names.append(label)
        self.label_offsets.append(offset_ops)
        self.label_ends[-1] = len(self.label_names)

    def labels_of(self, index: int) -> Iterator[Tuple[str, int]]:
        """Yield table `index`'s labels, each with its offset in ops from the table's start."""
        for at in range(self.label_ends[index - 1] if index else 0, self.label_ends[index]):
            yield self.label_names[at], self.label_offsets[at]

    def width(self, index: int) -> int:
        """Return how many ops table `index` needs in a slot: its ops, or its alignment if that is more."""
        return max(self.sizes[index], self.aligns[index])


def _written_in_source(label: str) -> bool:
    """Is `label` one the program's source declares? Not one the assembler adds, whose own name has a `:` (a macro's
    `:start:` label for the debugging file, a `$:` label)."""
    return ':' not in label.rpartition(MACRO_SEPARATOR_STRING)[2]


def _pow2_ceil(value: int) -> int:
    """the smallest power of two that is at least `value` (1 for 0)"""
    return 1 << (max(value, 1) - 1).bit_length()


def _align_up(value: int, alignment: int) -> int:
    return -(-value // alignment) * alignment


def _zero_addresses(memory_width: int, length: int) -> MutableSequence[int]:
    """Return `length` zero addresses: a 64-bit array where an address fits one (w <= 64), else a list of ints."""
    return array('Q', bytes(8 * length)) if memory_width <= 64 else [0] * length


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


class GroupCensus(NamedTuple):
    """The tables of one group that the expansion met."""

    word: Expr  # the group's jump word
    widths: Dict[int, int]  # a table's width in ops (DeferredTables.width) -> how many tables have it


class HotGroup:
    """A group the heat list names. Each width's k-th listed site takes that width's k-th cheapest index; the group's
    other tables take the indices after the listed ones."""

    __slots__ = ('ranks', 'listed', 'seen', 'unlisted')

    def __init__(self, sites: Sequence[HotSite], group_key: str):
        # (site, occurrence) -> (its slot width, its rank among the listed sites of that width)
        self.ranks: Dict[Tuple[str, int], Tuple[int, int]] = {}
        self.listed: Dict[int, int] = {}  # slot width -> how many listed sites have it
        self.seen: Dict[str, int] = {}  # site -> how many of its tables came so far
        self.unlisted: Dict[int, int] = {}  # slot width -> how many unlisted tables took an index
        for listed_site, occurrence, slot_ops in sites:
            site = heat_key(listed_site)
            if (site, occurrence) in self.ranks:
                raise FlipJumpPreprocessorException(
                    f'pin protection: the heat list names {site!r} #{occurrence} of {group_key} twice'
                )
            rank = self.listed.get(slot_ops, 0)
            self.ranks[(site, occurrence)] = (slot_ops, rank)
            self.listed[slot_ops] = rank + 1

    def next_occurrence(self, site: str) -> Tuple[str, int]:
        """Count one more table from `site`, and return (its site key, how many came from that site before it)."""
        site = heat_key(site)
        occurrence = self.seen.get(site, 0)
        self.seen[site] = occurrence + 1
        return site, occurrence

    def rank(self, site_occurrence: Tuple[str, int], slot_ops: int) -> int:
        """Return the rank, among its width's tables, of the table `site_occurrence` names. A listed site keeps its
        listed rank, unless its table changed width since the profile; any other takes the next rank after the
        listed ones."""
        listed = self.ranks.get(site_occurrence)
        if listed is not None and listed[0] == slot_ops:
            return listed[1]
        unlisted = self.unlisted.get(slot_ops, 0)
        self.unlisted[slot_ops] = unlisted + 1
        return self.listed.get(slot_ops, 0) + unlisted


@dataclasses.dataclass(slots=True)
class Block:
    """A group's block: one sub-block per slot width, each aligned to its own size (so `base + offset` is
    `base ^ offset`)."""

    sub_blocks: Dict[int, Tuple[int, int]]  # slot width in ops -> (the sub-block's bit offset in the block, its slots)
    bits: int  # the block's size in bits: a power of two
    cheapest_first: bool  # hand each width's slots out fewest-set-bits first, not in order (a spread group)
    hot: Optional[HotGroup]  # the heat list's ranks, for a hot group
    base: Optional[int] = None  # its address. None: the pool has no room for it (its tables go to the overflow region)
    taken: Dict[int, int] = dataclasses.field(default_factory=dict)  # slot width -> slots handed out
    broken: bool = False  # a table of the group got no slot (it is too wide for one)

    def take_slot(self, slot_ops: int, op_bits: int, hot_site: Optional[Tuple[str, int]]) -> int:
        """Hand out the next slot of `slot_ops` ops and return its address. `hot_site` is the table's (site,
        occurrence), in a hot group."""
        assert self.base is not None
        offset, slots = self.sub_blocks[slot_ops]
        taken = self.taken.get(slot_ops, 0)
        self.taken[slot_ops] = taken + 1
        if self.hot is not None and hot_site is not None:
            index = _cheapest_order(slots)[self.hot.rank(hot_site, slot_ops)]
        elif self.cheapest_first:
            index = _cheapest_order(slots)[taken]
        else:
            index = taken
        return self.base + offset + index * slot_ops * op_bits


class BlockPool:
    """A pool that gives each group a block, so that arming a jump word flips only the table's index.

    `wflip src+w, switch` arms a hex variable's jump word. If the word is pinned at a block base B and its tables live
    in that block, every wflip of the word flips `value ^ B` instead of `value` (assembler.BinaryData.pinned_value);
    for an arm, that is just the table's index. A pinned word holds `B ^ value`, so a raw word (POINTER_RUNTIME_LABELS)
    must not be pinned: `pin_all_but` names the cells to leave out.

    One pass: the preprocessor takes each table out of the op stream (`defer`), and once the expansion is done the
    pool places them all (`place_deferred`), sizing the blocks from its census. A block has one sub-block per table
    width (Block). Groups whose jump words resolve to one address share a block. If the blocks do not all fit, the
    last groups go to the overflow region. A `heat` list (pin protection) lays the hot groups out first, always pins
    them, and gives each width's k-th listed site the k-th cheapest index (HotGroup).

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
            is skipped (unpinned_words returns it, and the assembler warns).
        """
        if isinstance(pool_base, str) and pool_base != AFTER_PROGRAM:
            raise FlipJumpPreprocessorException(f'pool_base is an address or {AFTER_PROGRAM!r}, not {pool_base!r}')
        if max_slot_ops < 1:
            raise FlipJumpPreprocessorException(f'max_slot_ops is the widest table a slot holds, not {max_slot_ops}')
        # the options
        self.memory_width = memory_width
        self.after_program = pool_base == AFTER_PROGRAM  # the base is chosen once the program's end is known
        self.pool_base = 1 << memory_width if isinstance(pool_base, str) else pool_base
        self.pool_end = pool_end
        self.max_slot_ops = max_slot_ops
        self.spread_factor = max(1, spread_factor)
        self.spread_min_tables = spread_min_tables
        self.pin_incomplete_groups = pin_incomplete_groups
        self.heat = heat
        self.pin_all_but = None if pin_all_but is None else tuple(pin_all_but)
        # what the preprocessor tells it
        self.reads_words_raw = False  # set when a macro naming POINTER_RUNTIME_LABELS expands
        self.offset_guard: Optional[OffsetGuard] = None  # the guard of the preprocessor that lays this pool out
        self.census: Dict[str, GroupCensus] = {}  # the tables met, by group (a group is its jump word's text)
        # what it did
        self.alias: Dict[str, str] = {}  # group -> the group whose block it shares (their jump words are one word)
        self.blocks: Dict[str, Block] = {}  # each group's block, in layout order (place_deferred)
        self.end = 0  # the end of the last table placed
        self.table_sites: List[TableSite] = []  # every table met and queued, in expansion order
        self.pinned: Dict[int, int] = {}  # the words the assembly pinned: {jump-word address: block base}
        # what it left as it was, counted by why: tables kept inline (a KEPT_*, or why moving one would change an
        # operand), and words left unpinned because a pin could not hold (pins.unpin)
        self.kept_inline: Dict[str, int] = {}
        self.unpins: Dict[str, int] = {}
        self.heat_unmatched: List[str] = []  # the heat list's groups this program has none of, or several of

    @property
    def op_bits(self) -> int:
        return 2 * self.memory_width

    @property
    def moved(self) -> int:
        """how many tables the pool moved"""
        return sum(1 for site in self.table_sites if site.address)

    def _raw(self) -> bool:
        """Return whether the program reads words raw and no pin_all_but says which. Then no word is pinned
        (_place_cheapest)."""
        return self.reads_words_raw and self.pin_all_but is None

    # ---- while the expansion runs ----

    def defer(self, group: Optional[str], word: Optional[Expr], ops_alignment: int, table_ops: int) -> Optional[str]:
        """Count a table the expansion reached (at its pad) in the census. Return None if it leaves the op stream (it
        is placed once the expansion is done), else why it stays inline: KEPT_NO_JUMP_WORD or KEPT_TOO_WIDE.
        `group` is the text of `word`, its group's jump word; both are None for a table with no group."""
        width = max(table_ops, ops_alignment)
        raw = self._raw()
        if group is not None and word is not None:
            census = self.census.get(group)
            if census is None:
                census = self.census[group] = GroupCensus(word, {})
            census.widths[width] = census.widths.get(width, 0) + 1
        elif not raw:  # (a raw program's tables need no group: see _place_cheapest)
            return KEPT_NO_JUMP_WORD
        if width > self.max_slot_ops and not raw:
            return KEPT_TOO_WIDE
        return None

    # ---- once the expansion is done ----

    def place_deferred(self, tables: DeferredTables, labels: LabelsDict, end: int) -> Sequence[int]:
        """Place every queued table and return their addresses; 0 means the table stayed inline. `end` is where the
        program ends."""
        if not len(tables.sizes):
            return ()
        self._choose_base(end)
        self.alias = self._groups_sharing_a_word(labels)
        groups = self._merged_census()
        if self._raw():
            addresses = self._place_cheapest(tables, len(groups))
        else:
            blocks_end = self._lay_out_blocks(groups)
            addresses = self._place_in_blocks(tables, blocks_end)
        self._record_sites(tables, addresses)
        return addresses

    def _choose_base(self, end: int) -> None:
        """Start an AFTER_PROGRAM pool at the first power of two above the program, or right after the program if
        that power is past the top of memory."""
        if self.after_program:
            self.pool_base = _pow2_ceil(end)
            if self.pool_base >= 1 << self.memory_width:
                self.pool_base = end

    def _groups_sharing_a_word(self, labels: LabelsDict) -> Dict[str, str]:
        """Return {group: the group whose block it shares}, for the groups whose jump words resolve to one address
        (the first by name keeps the block). A word that names a queued table's label has no address yet, and shares
        nothing."""
        alias: Dict[str, str] = {}
        by_address: Dict[int, str] = {}
        for group in sorted(self.census):
            try:
                address = self.census[group].word.exact_eval(labels)
            except FlipJumpExprException:
                continue
            first = by_address.setdefault(address, group)
            if first != group:
                alias[group] = first
        return alias

    def _merged_census(self) -> Dict[str, Dict[int, int]]:
        """Return {group: {table width: how many}}, with the groups that share a block counted as one."""
        merged: Dict[str, Dict[int, int]] = {}
        for group, census in self.census.items():
            widths = merged.setdefault(self.alias.get(group, group), {})
            for width, count in census.widths.items():
                widths[width] = widths.get(width, 0) + count
        return merged

    def _record_sites(self, tables: DeferredTables, addresses: Sequence[int]) -> None:
        """Record where every table went (table_sites), and where the last one ends."""
        occurrences: Dict[Tuple[str, str], int] = {}
        op_bits, alias = self.op_bits, self.alias
        for index, address in enumerate(addresses):
            if address:
                self.end = max(self.end, address + tables.sizes[index] * op_bits)
            group, site = alias.get(tables.groups[index], tables.groups[index]), tables.sites[index]
            occurrence = occurrences.get((group, site), 0)
            occurrences[(group, site)] = occurrence + 1
            self.table_sites.append(TableSite(group, site, occurrence, _pow2_ceil(tables.width(index)), address))

    # ---- placing in blocks ----

    def _lay_out_blocks(self, groups: Dict[str, Dict[int, int]]) -> int:
        """Give each group a block, aligned to its size: the hot groups first, then biggest first. Where they do not
        all fit, the last groups get none (their tables go to the overflow region). Return where the blocks end."""
        hot = self._hot_groups(groups)
        blocks = {group: self._block(widths, hot.get(group)) for group, widths in groups.items()}
        order = list(hot) + sorted((group for group in groups if group not in hot), key=lambda g: (-blocks[g].bits, g))
        self.blocks = {group: blocks[group] for group in order}
        ends = self._block_ends(order)
        fitting = self._how_many_fit(order, ends, groups, len(hot))
        for group, end in zip(order[:fitting], ends[1:]):
            blocks[group].base = end - blocks[group].bits
        return ends[fitting]

    def _hot_groups(self, groups: Iterable[str]) -> Dict[str, HotGroup]:
        """Match the heat list's groups to this program's, by heat_key. Return {group: its HotGroup}, in the list's
        order. A listed group that matches none of the program's groups, or several, goes to heat_unmatched."""
        by_key: Dict[str, List[str]] = {}
        for group in groups:
            by_key.setdefault(heat_key(group), []).append(group)
        hot: Dict[str, HotGroup] = {}
        for listed_key, sites in (self.heat or {}).items():
            key = heat_key(listed_key)
            matches = by_key.get(key, [])
            if len(matches) == 1:
                hot[matches[0]] = HotGroup(sites, key)
            else:
                self.heat_unmatched.append(key)
        return hot

    def _block(self, widths: Dict[int, int], hot: Optional[HotGroup]) -> Block:
        """Size a group's block from its tables' widths: one sub-block per slot width, the biggest sub-block first."""
        tables = sum(widths.values())
        spread = self.spread_factor if tables >= self.spread_min_tables else 1
        counts: Dict[int, int] = {}  # slot width -> tables
        for width, count in widths.items():
            slot_ops = _pow2_ceil(min(width, self.max_slot_ops))
            counts[slot_ops] = counts.get(slot_ops, 0) + count
        sizes: Dict[int, Tuple[int, int]] = {}  # slot width -> (slots, the sub-block's bits)
        for slot_ops, count in counts.items():
            slots = _pow2_ceil(count) * spread
            # a hot width also needs room for the ranks its listed sites reserve
            while hot is not None and slots < count + hot.listed.get(slot_ops, 0):
                slots *= 2
            sizes[slot_ops] = (slots, slots * slot_ops * self.op_bits)
        sub_blocks: Dict[int, Tuple[int, int]] = {}
        offset = 0
        for slot_ops in sorted(sizes, key=lambda k: (-sizes[k][1], -k)):
            slots, bits = sizes[slot_ops]
            offset = _align_up(offset, bits)
            sub_blocks[slot_ops] = (offset, slots)
            offset += bits
        return Block(sub_blocks, _pow2_ceil(offset), spread > 1, hot)

    def _block_ends(self, order: Sequence[str]) -> List[int]:
        """Return where the first k blocks of `order` end, for every k: each block is aligned to its own size."""
        ends = [self.pool_base]
        for group in order:
            bits = self.blocks[group].bits
            ends.append(_align_up(ends[-1], bits) + bits)
        return ends

    def _how_many_fit(
        self, order: Sequence[str], ends: Sequence[int], groups: Dict[str, Dict[int, int]], hot_groups: int
    ) -> int:
        """Return how many of the first groups of `order` get a block: as many as leave room in the pool, after
        their blocks, for the tables of all the others (widest slot first). A hot group always gets one."""
        # of the groups from k on: the widest slot they need, and the bits all their slots take
        widest, room = [1] * (len(order) + 1), [0] * (len(order) + 1)
        for k in range(len(order) - 1, -1, -1):
            widths = groups[order[k]]
            widest[k] = max([widest[k + 1]] + [self._slot_bits(width) for width in widths])
            room[k] = room[k + 1] + sum(self._slot_bits(width) * count for width, count in widths.items())
        limit = self._pool_limit()
        for k in range(len(order), hot_groups - 1, -1):
            if _align_up(ends[k], widest[k]) + room[k] <= limit:
                return k
        raise self._no_room()

    def _place_in_blocks(self, tables: DeferredTables, blocks_end: int) -> Sequence[int]:
        """Return every table's address: a slot of its group's block, or a place in the overflow region (from
        `blocks_end` on) if its group has no block. 0 for a table that stays inline."""
        addresses = _zero_addresses(self.memory_width, len(tables.sizes))
        overflow: List[Tuple[int, int]] = []  # (slot bits, index) of the tables whose group has no block
        alias = self.alias
        for index, group in enumerate(tables.groups):
            address = self._slot_in_block(alias.get(group, group), tables.width(index), tables.sites[index])
            if address is not None:
                addresses[index] = address
            elif tables.segments[index] is not None:
                overflow.append((self._slot_bits(tables.width(index)), index))
        cursor = blocks_end
        for slot_bits, index in sorted(overflow, key=lambda entry: -entry[0]):  # widest slot first
            addresses[index] = cursor = _align_up(cursor, slot_bits)
            cursor += slot_bits
        return addresses

    def _slot_in_block(self, group: str, width: int, site: str) -> Optional[int]:
        """Return the address of a slot in the group's block, for a table `width` ops wide. None if the group has no
        block, or if the table is too wide for a slot (the group is then broken)."""
        block = self.blocks[group]
        # a hot group counts every table of a site, placed or not
        hot_site = None if block.hot is None else block.hot.next_occurrence(site)
        if block.base is None:
            return None
        if width > self.max_slot_ops:
            block.broken = True
            return None
        return block.take_slot(_pow2_ceil(width), self.op_bits, hot_site)

    # ---- placing a raw program's tables ----

    def _place_cheapest(self, tables: DeferredTables, groups: int) -> Sequence[int]:
        """Place the tables of a raw program (_raw), whose words cannot be pinned, at cheap addresses. Each width's
        tables take the offsets with the fewest set bits in a region with 256 times the slots they need, or just the
        slots they need if the pool cannot hold that. `groups` is how many words a block would have pinned."""
        by_slot: Dict[int, List[int]] = {}  # slot bits -> the tables that take such a slot
        for index, segment in enumerate(tables.segments):
            if segment is not None:
                by_slot.setdefault(self._slot_bits(tables.width(index)), []).append(index)
        for spread_bits in (8, 0):
            cursor, placed = self.pool_base, []
            for slot_bits in sorted(by_slot, reverse=True):
                indices = by_slot[slot_bits]
                index_bits = (len(indices) - 1).bit_length() + spread_bits
                base = _align_up(cursor, slot_bits << index_bits)
                placed += [(i, base + offset * slot_bits) for i, offset in zip(indices, _cheapest(index_bits))]
                cursor = base + (slot_bits << index_bits)
            if cursor <= self._pool_limit():
                break
        else:
            raise self._no_room()
        addresses = _zero_addresses(self.memory_width, len(tables.sizes))
        for index, address in placed:
            addresses[index] = address
        if groups:
            self.unpins['the pointer runtime'] = groups
        return addresses

    def _no_room(self) -> FlipJumpPreprocessorException:
        if self.pool_end is not None and self.pool_end < 1 << self.memory_width:
            advice = 'Raise pool_end'
        elif self.after_program:
            advice = 'The program leaves them no room below the top of memory: move fewer tables (max_slot_ops)'
        else:
            advice = 'Lower pool_base'
        return FlipJumpPreprocessorException(
            f'table-placement: the pool [{hex(self.pool_base)}, {hex(self._pool_limit())}) has no room for its '
            f'tables. {advice}, or assemble without the pool.'
        )

    def _slot_bits(self, width: int) -> int:
        return _pow2_ceil(width) * self.op_bits

    def _pool_limit(self) -> int:
        top = 1 << self.memory_width
        return top if self.pool_end is None else min(top, self.pool_end)

    # ---- what the assembler asks ----

    def pool_start(self) -> int:
        """Return the pool's first address. Before an AFTER_PROGRAM pool places a table it has none, so return the top
        of memory."""
        return 1 << self.memory_width if self.after_program and not self.end else self.pool_base

    def wflips_at(self) -> Optional[int]:
        """Return where the wflip areas go: after the pool, if it sits right after the program and holds tables.
        Otherwise None (their usual place)."""
        return self.end if self.after_program and self.end else None

    def pinned_words(self) -> Dict[Expr, int]:
        """Return the jump words to pin, `{word: block base}`: the words of the groups that have a table in their
        block. Skip groups whose tables didn't all fit in their block, unless `pin_incomplete_groups` is set or the
        group is hot (an inline table at `A` is then armed with `A ^ base`: slower, still right)."""
        pinned: Dict[Expr, int] = {}
        for group, block in self.blocks.items():
            if block.base is not None and block.taken:
                if self.pin_incomplete_groups or block.hot is not None or not block.broken:
                    pinned[self.census[group].word] = block.base
        return pinned

    def unpinned_words(self, labels: LabelsDict) -> Tuple[Set[int], List[str]]:
        """Return the addresses of the words in pin_all_but's cells (the words the assembler must not pin), and the
        labels pin_all_but names that the program does not define. A cell named by its label alone runs to the next
        label the source declares in the program's own code (not in a moved table)."""
        words: Set[int] = set()
        missing: List[str] = []
        if not self.pin_all_but:
            return words, missing
        pool_start = self.pool_start()
        addresses = sorted({a for label, a in labels.items() if a < pool_start and _written_in_source(label)})
        for cell in self.pin_all_but:
            label, ops = (cell, None) if isinstance(cell, str) else cell
            if label not in labels:
                missing.append(label)
                continue
            start = labels[label]
            if ops is None:
                after = bisect.bisect_right(addresses, start)
                end = addresses[after] if after < len(addresses) else start + self.op_bits
            else:
                end = start + ops * self.op_bits
            words.update(range(start, end, self.memory_width))
        return words, missing
