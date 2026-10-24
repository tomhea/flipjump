"""
the table pool: an opt-in placement of the lookup tables, used only when assemble() is given one (`table_pool=`).

A lookup table is the code after a `pad` that is only ever entered by a jump (`hex.exact_xor`'s switch and the
like). Arming one writes its address into a word, one executed op per set bit, so a table at an address that is
cheap to arm makes every dispatch through it cost fewer ops. The pool moves such tables there:
    TablePool - each to the lowest-popcount free address above pool_base;
    BlockPool - the tables dispatched through one word share a block, and the word rests at the block's base, so
                arming flips only the table's index in it.
Only code entered by a jump, never by falling through, is moved, so a program reaching the table through its own
labels runs the same. It costs a bigger address span (the pool sits above the program) and .fjm segments: one per
placed table (BlockPool) or per run of tables (TablePool).
"""

from __future__ import annotations

import collections
import dataclasses
import re
import sys
from array import array
from typing import Any, Callable, Collection, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from flipjump.utils.exceptions import FlipJumpAssemblerException, FlipJumpExprException, FlipJumpPreprocessorException
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.ops import MacroName, NewSegment

LabelsDict = Dict[str, int]

# bit addresses below this are the runtime's, not the program's: `stl.IO` sits at bit address 64
# and the interpreter intercepts flips there, yet `bit.output` dispatches through it like an
# ordinary hex source. Such a word is neither relocated (PreprocessorData.begin_relocation) nor pinned
# (assembler.resolve_pinned) - a block base baked into it would corrupt the program's startup.
RESERVED_BELOW = 1024

# the macros that read a cell RAW: they flip a bit of the pointed word and jump through it,
# which lands on the pointer decoder only while the word holds exactly `value*dw`. A word that
# BlockPool pinned holds `base + value*dw`, so a program that expands one of these must say which
# cells a pointer can reach (TablePool.pin_exclude) before any word is pinned; see BlockPool.
RAW_READERS = frozenset({'hex.pointers.read_cell_from_inners_ptrs'})

# a call site's coordinates in a macro-expansion path or a group expression: `f3:l120:` (a program
# file) or `s1:l40:` (an stl file) - CodePosition.short_str() and its separator
_SITE_COORDINATES = re.compile(r'(?<![\w.])[fs]\d+:l\d+:')


def heat_key(path: str) -> str:
    """a macro-expansion path (or a group expression) without its call-site coordinates. A heat list
    names sites and groups this way, so a site keeps its heat when lines above it move; sites that
    then share a key are told apart by the order the program reaches them (BlockPool's `heat`)."""
    return _SITE_COORDINATES.sub('', path)


# Why BlockPool declined a table (BlockPool.declines_by_group).
# DECLINE_STALE - only counts that are not the program's own produce these:
#   'new_group'      - the counts do not name the table's group, so it has no block
#   'width_overflow' - the table is wider than the counts said its group's (or width's) tables are
#   'overflow'       - the group has more tables (of this width) than the counts said
# DECLINE_OTHER - these happen with the program's own counts too:
#   'no_block'       - the group is counted, but the pool had no room for its block (or evicted it).
#                      Counts that are too BIG cause it as well: they inflate the blocks.
#   'too_wide'       - the table is wider than any slot `max_slot_ops` allows
#   'next_address'   - (one pass) an entry computes an address from `$`, so the table stays inline
# Neither list tells whether the counts were stale - counts with room to spare decline nothing and
# still size the blocks differently. That test is `pool.census() == the census it was given`.
DECLINE_STALE = ('new_group', 'width_overflow', 'overflow')
DECLINE_OTHER = ('no_block', 'too_wide', 'next_address')


@dataclasses.dataclass
class BlockCensus:
    """What a placing BlockPool must be told before it can size and place blocks, in the shape it
    takes them: `BlockPool(w, base, **census.pool_kwargs(), ...)`.

    `counts` (tables per group), `widths` (each group's widest table, in ops), `width_hist` (each
    group's {width: tables}) and `alias` ({group: canonical group}), all keyed by the group
    EXPRESSION string BlockPool keys groups by. A counting pass produces one (BlockPool.census on a
    counting pool), and so does a PLACING pass - for the program it actually placed, so a build can
    cache it as the next build's counts instead of running a counting expansion first.

    `unblocked` lists the groups a one-pass pool left WITHOUT a block on purpose, to make room for
    its overflow region (BlockPool.sizing). A pool given it leaves them without a block too, so the
    two lay out alike. A program's census (BlockPool.census) has none.
    """

    counts: Dict[str, int]
    widths: Dict[str, int]
    width_hist: Dict[str, Dict[int, int]]
    alias: Dict[str, str]
    unblocked: List[str] = dataclasses.field(default_factory=list)

    def pool_kwargs(self) -> Dict[str, Any]:
        """BlockPool's `counts`, `widths`, `width_hist`, `alias` and `unblocked` arguments (copies)"""
        return {
            'counts': dict(self.counts),
            'widths': dict(self.widths),
            'width_hist': {group: dict(hist) for group, hist in self.width_hist.items()},
            'alias': dict(self.alias),
            'unblocked': list(self.unblocked),
        }

    def to_json(self) -> Dict[str, Any]:
        """a JSON-safe form (a histogram's widths become strings); from_json reverses it"""
        return {
            'counts': dict(self.counts),
            'widths': dict(self.widths),
            'width_hist': {group: {str(w): n for w, n in hist.items()} for group, hist in self.width_hist.items()},
            'alias': dict(self.alias),
            'unblocked': list(self.unblocked),
        }

    @classmethod
    def from_json(cls, blob: Mapping[str, Any]) -> BlockCensus:
        return cls(
            counts={group: int(n) for group, n in blob['counts'].items()},
            widths={group: int(w) for group, w in blob['widths'].items()},
            width_hist={group: {int(w): int(n) for w, n in hist.items()} for group, hist in blob['width_hist'].items()},
            alias={group: str(canonical) for group, canonical in (blob.get('alias') or {}).items()},
            unblocked=[str(group) for group in blob.get('unblocked') or []],
        )


def merge_aliased_groups(
    counts: Mapping[str, int],
    widths: Mapping[str, int],
    width_hist: Mapping[str, Mapping[int, int]],
    alias: Mapping[str, str],
) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, Dict[int, int]]]:
    """(counts, widths, width_hist) with every aliased group folded into its canonical group: the
    counts add, the widths take the max, the histograms add. A pool given an `alias` keys its blocks
    by the canonical group, so these are the counts it needs (see BlockPool.canonical_alias)."""
    merged_counts: Dict[str, int] = {}
    merged_widths: Dict[str, int] = {}
    for group, count in counts.items():
        canonical = alias.get(group, group)
        merged_counts[canonical] = merged_counts.get(canonical, 0) + count
        merged_widths[canonical] = max(merged_widths.get(canonical, 0), widths.get(group, 16))
    merged_hist: Dict[str, Dict[int, int]] = {}
    for group, hist in width_hist.items():
        into = merged_hist.setdefault(alias.get(group, group), {})
        for width, count in hist.items():
            into[width] = into.get(width, 0) + count
    return merged_counts, merged_widths, merged_hist


# BlockPool.defer's verdicts on a table the expansion reached (one pass)
DEFER_OUT = 'out'  # it leaves the op stream, and is placed once the expansion is done
DEFER_TOO_WIDE = 'too_wide'  # it stays inline - no slot can hold it - and is declined in its turn
DEFER_NEXT_ADDRESS = 'next_address'  # it stays inline - an entry uses `$` - and is declined in its turn
DEFER_INLINE = 'inline'  # it stays inline and is never offered again: it has no group


class DeferredTables:
    """Every table a one-pass BlockPool queued, in the order the expansion reached them.

    A table that LEFT the op stream has its ops in the preprocessor's pool stream already, behind a
    NewSegment whose address is filled in once it is placed (`segments`); its labels wait here as
    bit offsets from its start. A table that stayed inline (too wide for any slot, or an entry uses
    `$`) is queued only so the pool can decline it in its turn (`inline`: index -> cause).

    ~43 bytes a table: parallel arrays, not an object per table (a big program queues hundreds of
    thousands of tables). A kept site path is the interned heat key, or for a subclass that overrides
    reserve() the full path.
    """

    __slots__ = (
        'groups',
        'exprs',
        'aligns',
        'sizes',
        'segments',
        'sites',
        'keeps_sites',
        'inline',
        'label_names',
        'label_offsets',
        'label_ends',
    )

    def __init__(self, keeps_sites: bool):
        self.groups: List[str] = []  # each table's (raw) group; one string object per group
        self.exprs: Dict[str, Optional[Expr]] = {}  # group -> its source-word Expr, shared by its tables
        self.aligns = array('I')  # ops_alignment
        self.sizes = array('I')  # table_ops
        self.segments: List[Optional[NewSegment]] = []  # None for a table that stayed inline
        self.sites: List[str] = []  # kept only when `keeps_sites` (see BlockPool.keeps_site_paths)
        self.keeps_sites = keeps_sites
        self.inline: Dict[int, str] = {}  # index -> DEFER_TOO_WIDE / DEFER_NEXT_ADDRESS
        self.label_names: List[str] = []  # the deferred labels, table after table
        self.label_offsets = array('I')  # ...each one's bit offset inside its table
        self.label_ends = array('I')  # table i's labels are label_names[label_ends[i-1]:label_ends[i]]

    def __len__(self) -> int:
        return len(self.groups)

    def add(
        self,
        group: str,
        group_expr: Optional[Expr],
        ops_alignment: int,
        table_ops: int,
        site: str,
        segment: Optional[NewSegment],
        inline_cause: Optional[str] = None,
    ) -> None:
        group = sys.intern(group)  # one string object per group, however many tables it has
        if group not in self.exprs:
            self.exprs[group] = group_expr  # its tables' Exprs print alike, so they resolve alike
        self.groups.append(group)
        self.aligns.append(ops_alignment)
        self.sizes.append(table_ops)
        self.segments.append(segment)
        if self.keeps_sites:
            self.sites.append(site)
        if inline_cause is not None:
            self.inline[len(self.groups) - 1] = inline_cause
        self.label_ends.append(len(self.label_names))

    def add_label(self, label: str, offset: int) -> None:
        """a label of the table added last, `offset` bits from its start"""
        self.label_names.append(label)
        self.label_offsets.append(offset)
        self.label_ends[-1] = len(self.label_names)

    def __iter__(self) -> Iterator[Tuple[int, str, Optional[Expr], int, int, str, Optional[str]]]:
        """(index, group, group Expr, ops_alignment, table_ops, site path, inline cause or None)"""
        sites = self.sites if self.keeps_sites else None
        for index, group in enumerate(self.groups):
            yield (
                index,
                group,
                self.exprs[group],
                self.aligns[index],
                self.sizes[index],
                sites[index] if sites is not None else '',
                self.inline.get(index),
            )

    def labels_of(self, index: int) -> Iterator[Tuple[str, int]]:
        """(label, bit offset) of table `index`"""
        start = self.label_ends[index - 1] if index else 0
        for at in range(start, self.label_ends[index]):
            yield self.label_names[at], self.label_offsets[at]


class TablePool:
    """Place lookup tables at LOW-POPCOUNT addresses, because that address is a per-call cost.

    A `pad`-aligned dispatch table (`hex.exact_xor`'s switch, and the like) is reached only by a
    jump, so it can live anywhere. But the `wflip`s that arm and disarm it write the table's
    ADDRESS into the hex variable's jump word, and `assembler.insert_wflip_ops` emits one executed
    op per set bit - so each call costs `2 * popcount(table_address)` on top of the table walk.
    Moving the table to a cheap address makes every call through it cheaper and changes no
    semantics: the word still rests at 0, so every other user of that word is untouched.

    Addresses are handed out cheapest-popcount-first from a pool based at `pool_base`, in runs of
    `run_ops` ops so the .fjm does not need one segment per table. A table's total popcount is
    `popcount(pool_base) + popcount(run_offset) + popcount(index_in_run)`, so a short run is
    cheaper per table but costs more segments.

    A pool is STATEFUL (a cursor, the consumed slots, an allocation count), so construct a fresh
    one for every assemble() call: a reused pool continues where the first assembly stopped and
    hands every table a different address.
    """

    def __init__(
        self,
        memory_width: int,
        pool_base: int,
        *,
        run_ops: int = 256,
        capacity: Optional[int] = None,
        span_bits: Optional[int] = None,
        wants: Optional[Callable[[MacroName, str], bool]] = None,
    ):
        """
        @param memory_width: the memory-width
        @param pool_base: first address of the pool. MUST be above everything the main stream can
        reach, or a relocated table will overwrite code -- nothing here checks that.
        @param run_ops: ops per contiguous run. One .fjm segment per run, so a short run buys a
        lower mean popcount and costs more segments.
        @param capacity: stop relocating after this many tables (None = unlimited)
        @param span_bits: how far above pool_base the pool may reach (None = to the top of memory).
        Worth setting: offsets are handed out cheapest-popcount-first, so an unbounded pool scatters
        to the top of the address space and the .fjm's span approaches 2^w while its data stays small.
        @param wants: given (macro name, call-site macro path), whether that table is relocated
        """
        self.memory_width = memory_width
        self.op_bits = 2 * memory_width
        self.run_bits = run_ops * self.op_bits
        if self.run_bits & (self.run_bits - 1):
            raise ValueError('run_ops * 2 * memory_width must be a power of two')
        self.pool_base = pool_base
        self.capacity = capacity
        self.span_bits = span_bits
        self._wants = wants
        # `pin_exclude(address, labels) -> bool` lets the caller veto pinning a word it owns (see
        # assembler.resolve_pinned); a plain pool pins nothing, so both only matter for BlockPool
        self.pin_exclude: Optional[Callable[[int, LabelsDict], bool]] = None
        self.pin_conflicts = 0
        self.reads_words_raw = False  # set by the preprocessor when a RAW_READERS macro expands
        self.allocated = 0
        self.declined = 0
        self.reserved_words = 0  # tables through the runtime's words, never relocated
        self._offsets = self._cheapest_offsets()
        self._cursor = 0  # bit address of the next free spot inside the current run
        self._run_end = 0
        # A table can be LONGER than the run it started in -- `hex.cmp`'s is 31 ops behind a
        # `pad 4` -- and then it spills into the slots above. Those slots are recorded here so a
        # later (cheaper-popcount) run is never opened on top of one.
        self._consumed: Set[int] = set()
        self.run_starts: List[int] = []

    def _cheapest_offsets(self) -> Iterator[int]:
        """every run-aligned offset in the pool, cheapest popcount first and ascending within a
        popcount -- generated lazily, since an unbounded pool at w=64 has 2^49 of them"""
        reach = (1 << self.memory_width) - self.pool_base
        if self.span_bits is not None:
            reach = min(reach, self.span_bits)
        count = reach // self.run_bits
        if count <= 0:
            return
        yield 0
        for bits in range(1, count.bit_length() + 1):
            offset = (1 << bits) - 1  # the smallest value with `bits` set bits
            while offset < count:
                yield offset
                lowest = offset & -offset  # the next larger value with the same popcount
                ripple = offset + lowest
                offset = ripple | (((offset ^ ripple) >> 2) // lowest)

    def wants(self, macro_name: MacroName, labels_prefix: str = '') -> bool:
        """`labels_prefix` is the full macro-expansion path, i.e. the CALL SITE -- so a caller can
        relocate the hot sites a profile named rather than every expansion of a macro. Cheap
        addresses are finite, and spending them on cold tables is how a placement pass ends up
        costing more than it saves."""
        if self.capacity is not None and self.allocated >= self.capacity:
            return False
        return True if self._wants is None else self._wants(macro_name, labels_prefix)

    def reserve(
        self,
        ops_alignment: int,
        table_ops: int,
        group: Optional[str] = None,
        group_expr: Optional[Expr] = None,
        labels_prefix: str = '',
    ) -> Optional[Tuple[int, bool, int]]:
        """(address, starts_a_new_run, gap_ops) for a table of `table_ops` ops. `labels_prefix` is the
        table's macro-expansion path (only BlockPool's `heat` reads it).

        The SIZE is passed in rather than discovered afterwards, because a table may be bigger than
        the run it would start in -- `hex.cmp`'s is 31 ops behind a `pad 4` -- and a table that
        spills past its run lands on a run allocated EARLIER (offsets are handed out in popcount
        order, not address order), which the .fjm writer rejects as overlapping segments. Knowing
        the size up front turns that into a placement decision instead of a crash.

        `gap_ops` is alignment skipped INSIDE the current run. The caller must emit it as Padding:
        a segment's words are written consecutively from its start address, so a gap the op stream
        does not know about shifts everything after it.
        """
        align_bits = ops_alignment * self.op_bits
        size_bits = max(table_ops, 1) * self.op_bits
        new_run = False
        gap_ops = 0
        if not self.run_starts:
            new_run = True
        else:
            candidate = -(-self._cursor // align_bits) * align_bits
            if candidate + size_bits > self._run_end:
                new_run = True  # does not fit in what is left of this run
            else:
                gap_ops = (candidate - self._cursor) // self.op_bits
                self._cursor = candidate
        if new_run:
            slots_needed = -(-size_bits // self.run_bits)
            slot = self._find_free_slots(slots_needed)
            if slot is None:
                self.declined += 1
                return None
            for extra in range(slots_needed):
                self._consumed.add(slot + extra)
            base = self.pool_base + slot * self.run_bits
            if base % align_bits:
                self.declined += 1
                return None
            self._cursor = base
            self._run_end = base + slots_needed * self.run_bits
            self.run_starts.append(base)
            gap_ops = 0
        self.allocated += 1
        return self._cursor, new_run, gap_ops

    def _find_free_slots(self, slots_needed: int) -> Optional[int]:
        """the cheapest run offset with `slots_needed` consecutive free slots from it"""
        for slot in self._offsets:
            if all((slot + extra) not in self._consumed for extra in range(slots_needed)):
                if self.pool_base + (slot + slots_needed) * self.run_bits <= (1 << self.memory_width):
                    return slot
        return None

    def commit(self, end_address: int) -> None:
        """the table ended here; the next one starts at or after it.

        A table that ran past its run's end has claimed the slots it spilled into, so record them:
        the .fjm writer rejects overlapping segments, and a cheaper-popcount run opened later would
        land inside this one.
        """
        if end_address > self._run_end:
            raise FlipJumpPreprocessorException(
                'table-placement: a table ran past the run reserved for it '
                f'({hex(end_address)} > {hex(self._run_end)}). reserve() was given the wrong size.'
            )
        self._cursor = end_address

    def pinned_words(self) -> Dict[Expr, int]:
        """{source-word Expr: block base} to bake into the program; a plain pool pins nothing"""
        return {}

    def labels_resolved(self, labels: LabelsDict) -> None:
        """assemble() calls this once every label of the program is known, before the labels are
        resolved into the binary. A plain pool has no use for them."""


class BlockPool(TablePool):
    """Give every table dispatched through ONE jump word a block, so the arm flips an INDEX.

    TablePool moves a table to a cheap ADDRESS; the arm still writes that whole address, so the
    saving is capped by how many low-popcount addresses exist. BlockPool changes what is written.

    A hex variable is a single op `;val*dw`, so its jump word holds the VALUE and
    `wflip src+w, switch` makes it `switch + digit`. If that word RESTS at a block base B and every
    table it dispatches to lives in the block at B, then `switch ^ B` is just the table's index
    inside the block. The same XOR serves every writer of the word: `insert_wflip_ops` rewrites
    `flip_value ^= B` for any address-sized flip of a pinned word, so `stl.comp_if1`,
    `hex.shifts.*` and `hex.tables.*` keep working by construction - they wanted the word to become
    V, and (B + digit) ^ (V ^ B) is V + digit.

    THE ONE THING A PINNED WORD CANNOT DO is be read raw. `hex.pointers`' read dance flips a bit
    of the POINTED word and jumps through it, which reaches the decoder table only from
    `value*dw`, not from `base + value*dw`; anything else that jumps through a cell expecting its
    bare value has the same problem. Which cells a pointer will reach is runtime data, so the
    caller declares it: `pin_exclude(address, labels) -> bool`, given the bit address of a cell's
    JUMP word (`cell + w`), vetoes the pin of every cell a pointer (or any raw reader) can reach.
    A program that expands a RAW_READERS macro and gives no `pin_exclude` is refused by assemble()
    rather than miscompiled; `pin_exclude` may veto nothing when the caller knows no pointer
    reaches an armed cell.

    TWO PASSES. A block's size must be known before its base is chosen, and the tables of a group
    are scattered through the program, so counting comes first:

        counting = BlockPool(w, base)                 # counts=None -> relocates nothing
        resolve_macros(..., table_pool=counting)
        placing = BlockPool(w, base, counts=counting.counts, widths=counting.widths)
        resolve_macros(..., table_pool=placing)

    Blocks are power-of-two sized and aligned to their own size, so the base has no bits where the
    index goes and the arming XOR adds rather than subtracts.

    THE COUNTS OF THE LAST BUILD ARE GOOD ENOUGH. A placing pool records every table it is offered,
    placed or declined, exactly as a counting pool would (`recording`, a counting pool fed the same
    reservations), so after its assembly `census()` is what a counting expansion of THIS program
    reports - in the shape the next placing pool takes (BlockCensus). A build can therefore cache it
    and place straight from the previous build's counts:

        placing = BlockPool(w, base, **cached.pool_kwargs())    # the last build's census
        assemble(..., table_pool=placing)
        fresh = placing.census()                                # cache it for the next build

    Stale counts cost speed, never correctness: a table they have no room for is DECLINED and stays
    inline, where it still dispatches correctly (see broken_groups), and `declines_by_group` says
    which and why (DECLINE_STALE / DECLINE_OTHER). Whether the counts WERE stale is `fresh ==
    cached`, not the declines: counts with room to spare decline nothing, yet size the blocks
    differently. When `fresh == cached` the binary is the one the two-pass build makes.

    ONE PASS (`one_pass=True`, opt-in). Nothing during an expansion needs a table's address: the
    arm and disarm name its head label, which resolves only after the expansion. So the
    preprocessor can take every table this pool would take out of the op stream as it reaches it
    (`defer`), expand on, and place them all when it is done (`place_deferred`) - sizing the blocks
    from that very expansion's census:

        pool = BlockPool(w, base, one_pass=True)       # counts=None: sized from its own census
        resolve_macros(..., table_pool=pool)

    The op stream then never depends on placement: a table that gets no block goes to an OVERFLOW
    region right after the blocks and inside the pool's span (`overflow_base` moves it), never
    inline, in the order the expansion reached them - so code appended later never moves an earlier
    one. Two kinds of table stay inline, decided at their pad and declined in their turn: one too
    wide for any slot (as in the two-pass build), and one whose entries compute an address from `$`,
    which is unknown while they expand ('next_address'; no stl table does this).

    Given the same counts, one pass decides every table exactly as the placing pass of the two-pass
    build, and the binaries are identical - EXCEPT where (a) the pool lacks room for a block: the
    two-pass build keeps the groups that do not fit inline, while one pass needs room in the span
    for their tables, so a pool sized from its own census (with no overflow_base) also leaves
    without a block the groups whose blocks free the most room per table, net of their tables' room
    in the overflow region (their power-of-two or spread padding: _choose_unblocked) - which can be
    more tables than the two-pass build leaves inline or, where spread pads the blocks, fewer - and
    lays out exactly that plan (so `evict_by_value` drops nothing more), while a pool given counts,
    or an overflow_base, drops what the two-pass build drops, to the overflow region; (b) an
    entry uses `$`: the two-pass build places that table; (c) with merge_aliases, a group's word
    names a deferred table's label: that alias is unknown when the blocks are sized
    (`alias_unresolved`); or (d) the counts given were stale: every table they decline stays
    inline in the two-pass build and overflows here. The debugging file lists a deferred table's
    labels after the others; the label addresses, and the debugger's address -> label choice, are
    the same.

    Given `counts`, a one-pass pool sizes from them instead. Assemblies that must lay out alike -
    the same program twice, or a program and the same program with code appended - share the
    census the first was sized with: `BlockPool(..., one_pass=True, **first.sizing().pool_kwargs())`
    (its `unblocked` groups stay without a block, so the blocks and the overflow region come out the
    same). Appended code's tables that find no slot (their group's block is full, or the group is
    new) go to the overflow region after the first assembly's, so that region needs ROOM: a first
    assembly that had to leave groups out planned its region to end near the end of its span, and
    a second with the same span refuses appended tables that do not fit in what is left ('ends past
    the end of the pool'). Give both an `overflow_base` with room, or give the first a span_bits
    smaller than the second's by the room to keep: the second lays out the same blocks and region,
    then the appended tables, up to its own span. A table's labels have no address until the
    expansion ends, so an expression evaluated DURING it (a `rep` count, a `pad`, a `segment`, a
    `reserve`) cannot name one: that is refused, saying so.

    A subclass sees every table in `defer`, at its pad, with its site path. `reserve` is called at
    placement, in encounter order, for the tables that left the stream or stayed inline as too wide
    (not for ungrouped or `$` ones): with the full site path when the subclass overrides `reserve`,
    with just its heat key when only pin protection reads it, else with '' (see kept_site).

    PIN PROTECTION (`heat`, opt-in). Two things re-roll a hot word's cost when the program changes:
    its group can lose its block (and so its pin), and indices follow ENCOUNTER order, so the
    cheapest ones go to whichever code the program happens to reach first, not to the hottest
    tables. A profile's heat list fixes both for the groups it names: hot groups are placed FIRST,
    in heat order, from pool_base (so no new group moves them, and their bases stay low - a writer
    that jumps outside the block, or arms a declined table, flips `V ^ base`); a hot group is never
    evicted, raises if it gets no block, and is always pinned; and in each slot width its k-th
    listed site gets the k-th cheapest index (see _hot_index).
    """

    def __init__(
        self,
        memory_width: int,
        pool_base: int,
        *,
        counts: Optional[Dict[str, int]] = None,
        widths: Optional[Dict[str, int]] = None,
        span_bits: Optional[int] = None,
        max_slot_ops: int = 32,
        alias: Optional[Dict[str, str]] = None,
        spread: int = 1,
        spread_min_count: int = 256,
        evict_by_value: bool = False,
        pin_broken: bool = False,
        width_hist: Optional[Dict[str, Dict[int, int]]] = None,
        width_buckets: bool = False,
        wants: Optional[Callable[[MacroName, str], bool]] = None,
        heat: Optional[Mapping[str, Sequence[Tuple[str, int, int]]]] = None,
        one_pass: bool = False,
        merge_aliases: bool = False,
        overflow_base: Optional[int] = None,
        unblocked: Optional[Sequence[str]] = None,
    ):
        """
        (see TablePool for memory_width, pool_base, span_bits and wants, and the class docstring)
        @param counts: tables per group. None makes a COUNTING pool - or, with one_pass, sizes the
        blocks from the very expansion it places.
        @param one_pass: ONE expansion (see the class docstring): tables wait out of the op stream
        and are placed when it is done; a table that gets no block goes to the overflow region, not
        inline (one too wide for any slot stays inline). Off by default: the two-pass mode is
        unchanged.
        @param merge_aliases: one_pass with counts=None only - merge the groups whose words the
        expansion's labels resolve to one address (canonical_alias), as `alias` does for a placing
        pool. Otherwise the census is merged by the `alias` given, if any; with counts given, pass
        the `alias` that goes with them.
        @param overflow_base: one_pass only - where the overflow region starts (None: right after
        the last block, bounded by the pool's span). It must not overlap the blocks, the main stream
        or its wflip area.
        @param unblocked: with counts - groups to leave without a block (BlockCensus.unblocked: what
        a one-pass pool sized from its own census left out to make room for its overflow region).
        """
        if merge_aliases and not (one_pass and counts is None):
            raise ValueError(
                'merge_aliases: only a one-pass pool sized from its own census (counts=None) merges aliases; '
                'give a placing pool the `alias` of the census its counts come from'
            )
        if merge_aliases and alias:
            raise ValueError('merge_aliases computes the alias: pass it or `alias`, not both')
        if overflow_base is not None and not one_pass:
            raise ValueError('overflow_base: only a one-pass pool has an overflow region')
        if overflow_base is not None and overflow_base <= 0:
            raise ValueError('overflow_base: the program starts at 0, the overflow region must be above it')
        if unblocked and counts is None:
            raise ValueError('unblocked: only a pool given counts leaves groups of them without a block')
        super().__init__(memory_width, pool_base, run_ops=16, span_bits=span_bits, wants=wants)
        self.counts = {} if counts is None else dict(counts)
        # a slot must hold the BIGGEST table of its group (`hex.cmp`'s is 31 ops behind a `pad 4`),
        # so the counting pass records each group's widest table, keyed like `counts`
        self.widths = {} if widths is None else dict(widths)
        # the per-group width distribution, for `width_buckets` and for accounting: one wide table
        # in a group of narrow ones sets the slot width for all of them
        self.width_hist: Dict[str, Dict[int, int]] = (
            {} if width_hist is None else {group: dict(hist) for group, hist in width_hist.items()}
        )
        # opt-in: give each width its own sub-block instead of widening every slot in the group to
        # the widest table (see _bucket_layout)
        self.width_buckets = width_buckets
        self._layout_cache: Dict[str, Tuple[Dict[int, Tuple[int, int, int]], int]] = {}
        # ONE PASS (opt-in): tables are deferred during the expansion and placed after it (see the
        # class docstring); with no counts the blocks are sized from that expansion's own census
        self.one_pass = one_pass
        self.sizes_from_census = one_pass and counts is None
        self.merge_aliases = merge_aliases
        self.overflow_base = overflow_base
        self._placed = False
        self._heat = heat
        # a deferred table keeps its site path only when something reads it at placement: a
        # subclass's own reserve() (a site recorder) gets the full path, pin protection - which
        # only compares heat keys - the heat key, interned, so the sites that share one share it
        self._full_site_paths = type(self).reserve is not BlockPool.reserve
        self.keeps_site_paths = bool(heat) or self._full_site_paths
        self.deferred = 0  # tables taken out of the op stream (one_pass)
        self.overflowed = 0  # ...placed in the overflow region, having got no block
        self.overflow_tables: List[Tuple[int, int]] = []  # ...each one's (address, ops_alignment)
        self.kept_inline = 0  # ...left inline: an entry computes an address from `$`
        self.alias_unresolved = 0  # one pass, merge_aliases: group words unknown when it was sized
        # the groups left without a block on purpose: given, or (one pass, sized from its census) the
        # ones left out to make room for the overflow region and the ones there was no room for
        # anyway (_choose_unblocked) - and how many layouts that choice took
        self.unblocked: List[str] = list(unblocked or [])
        self.overflow_rounds = 0
        self.counting = counts is None and not one_pass
        # {group key: canonical group key}. Groups are keyed by the source word's EXPRESSION, and
        # two expressions can name one word (`(x + 32)` and `(x + w)` at w=32); without merging
        # them resolve_pinned has to un-pin both. The caller derives this from a counting pass
        # with canonical_alias, once the labels are known.
        self.alias = alias or {}
        self.exprs: Dict[str, Expr] = {}
        # what a placing pass actually met (see the class docstring): a counting pool, keyed by the
        # RAW group like any counting pool, fed every reservation this pool is offered
        self._recording: Optional[BlockPool] = (
            None
            if self.counting
            else BlockPool(
                memory_width,
                pool_base,
                span_bits=span_bits,
                max_slot_ops=max_slot_ops,
                spread=spread,
                spread_min_count=spread_min_count,
                wants=wants,
            )
        )
        # the alias the labels of this pool's own assembly imply (labels_resolved), for census()
        self.seen_alias: Optional[Dict[str, str]] = None
        # why a table was declined, by cause
        self.declined_no_block = 0  # its group got no block at all: pool exhausted, or a group the counts lack
        self.declined_too_wide = 0  # wider than its slot: than max_slot_ops allows, or than the counts said
        self.declined_overflow = 0  # more tables in the group than the counting pass saw
        self.declined_next_address = 0  # (one pass) an entry uses `$`: it stays inline
        # ...and by group: {group: {cause: tables}}, the causes of DECLINE_STALE and DECLINE_OTHER
        self.declines_by_group: Dict[str, Dict[str, int]] = {}
        # opt-in: when the pool cannot hold every block, drop the least valuable groups (tables per
        # bit) before allocating, instead of whichever groups the biggest-first cursor reaches last
        self.evict_by_value = evict_by_value
        self.evicted_low_value = 0
        # opt-in: pin a group even though some of its tables were declined (see pinned_words)
        self.pin_broken = pin_broken
        self.groups: Dict[str, Tuple[int, Optional[Expr]]] = {}  # group -> (base, source-word Expr)
        # A group that could not place EVERY one of its tables is not pinned: a pinned word makes
        # every writer flip `V ^ base`, which sends the arm of a DECLINED (inline) table to
        # `switch ^ base`. Un-pinned, the word rests at `digit`, the full address is written, and
        # both the blocked and the inline tables in it stay correct; only the saving is lost.
        self.broken_groups: Set[str] = set()
        self._used = 0  # bits handed out, from pool_base
        self._next_index: Dict[Tuple[str, int], int] = {}  # (group, slot width - 0 for uniform) -> tables placed
        self.ungrouped = 0
        # a block's slots are uniform, so one wide table sets the width for its whole group and a
        # single 514-op table could claim the entire pool. Wider tables are declined and stay
        # inline, which is correct because a consistent base cancels: `(B + digit) ^ (switch ^ B)`
        # is `switch + digit` wherever the table sits.
        self.max_slot_ops = max_slot_ops
        # sparse indices: the arm costs `2 * popcount(index)`, and a group of 19,015 tables needs a
        # 15-bit index. Giving such a group `spread` times the slots and handing out only the
        # cheapest indices cuts that; unused slots emit no data, so it costs span, not data.
        # Applied only above `spread_min_count`, where the index is wide enough to matter.
        self.spread = max(1, spread)
        self.spread_min_count = spread_min_count
        self._cheap_index_cache: Dict[int, List[int]] = {}
        # pin protection: {heat_key(group): [(heat_key(site path), occurrence, slot_ops), ...]}, each
        # group's hot sites hottest first; `occurrence` numbers the sites that share a key, in the
        # order the program reaches them. None (the default) changes nothing.
        self.hot_sites: Dict[str, Dict[Tuple[str, int], Tuple[int, int]]] = {}  # group -> site -> (width, rank)
        self.hot_ranks: Dict[Tuple[str, int], int] = {}  # (group, width) -> ranks reserved for its sites
        self._hot_seen: Dict[Tuple[str, str], int] = {}  # (group, site) -> occurrences reached so far
        self._hot_next: Dict[Tuple[str, int], int] = {}  # (group, width) -> unlisted tables so far
        self.hot_missing: List[str] = []  # listed groups this program does not have
        self.hot_ambiguous: List[str] = []  # listed keys that name two of its groups
        self.hot_sites_matched = 0
        self.hot_sites_width_changed = 0
        if not self.counting and not self.one_pass:  # one_pass sizes its blocks in place_deferred
            if heat:
                self._resolve_heat(heat)
            self._preallocate(self.unblocked)

    def canonical_alias(self, labels: LabelsDict) -> Dict[str, str]:
        """{group -> canonical group} for groups whose expressions resolve to the SAME address.

        Run it on a COUNTING pool once its assembly's labels are known, and pass the result as the
        placing pool's `alias`; without it every alias pair loses both its pins.
        """
        return self._alias_and_unresolved(labels)[0]

    def _alias_and_unresolved(self, labels: LabelsDict) -> Tuple[Dict[str, str], int]:
        """(canonical_alias, how many groups' words these labels cannot resolve)"""
        by_address: Dict[int, str] = {}
        alias: Dict[str, str] = {}
        unresolved = 0
        for group in sorted(self.exprs):
            try:
                address = self.exprs[group].exact_eval(labels)
            except FlipJumpExprException:
                unresolved += 1
                continue
            first = by_address.setdefault(address, group)
            if first != group:
                alias[group] = first
        return alias, unresolved

    @property
    def recording(self) -> BlockPool:
        """the counting pool of what this pool was offered: itself when counting, and for a placing
        pool the shadow counting pool fed every reservation it got - placed or declined, ungrouped
        ones included - before anything could decline it"""
        return self if self._recording is None else self._recording

    def labels_resolved(self, labels: LabelsDict) -> None:
        """record the alias this assembly's labels imply among the groups it met (census reads it).
        Only a dict of aliases is kept, never the labels. A one-pass pool that merged its own census
        keeps the alias it was SIZED with instead (see census). Refuses a program that reads a pinned word
        raw (RAW_READERS) when no pin_exclude says which cells it reads."""
        if not (self.sizes_from_census and self.merge_aliases and self._placed):
            self.seen_alias = self.recording.canonical_alias(labels)
        if self.pinned_words() and self.reads_words_raw and self.pin_exclude is None:
            raise FlipJumpAssemblerException(
                "the program reads cells through hex.pointers, and a word the BlockPool pinned cannot be "
                "read that way (it rests at base + value). Which cells a pointer reaches is runtime data: "
                "pass pin_exclude=(lambda address, labels: ...) vetoing every cell a pointer can read "
                "(it may veto nothing if none is an armed hex), or use TablePool."
            )

    def census(self, merge_aliases: bool = False) -> BlockCensus:
        """The counts, widths, width histogram and alias of every table this pool was offered: what a
        counting expansion of the program just assembled reports, in the shape a placing pool takes
        (see the class docstring). Cache it as the next build's counts.

        The groups are keyed as a counting pool keys them, with the `alias` a counting pool was
        given folded in (it counted canonical groups).

        @param merge_aliases: also fold the groups whose words resolved to one address into their
        canonical group (canonical_alias), as a placing pool given that `alias` needs them: by the
        labels of this assembly (so only after assemble(), which hands them over) - or, for a
        one-pass pool that merged its own census, by exactly the alias its blocks were sized with,
        so a second assembly given this census lays out alike (see alias_unresolved).
        """
        recording = self.recording
        given = dict(recording.alias)  # a counting pool given an alias counted canonical groups
        seen: Dict[str, str] = {}
        if merge_aliases:
            if self.seen_alias is None:
                raise FlipJumpPreprocessorException(
                    'census(merge_aliases=True) needs the assembly\'s labels: call it after assemble() '
                    '(which hands them to the pool), or merge with an alias of your own'
                )
            seen = self.seen_alias
        counts, widths, width_hist = merge_aliased_groups(
            recording.counts, recording.widths, recording.width_hist, seen
        )
        # one lookup reaches the final group: a raw group the given alias named goes on to where
        # `seen` sends its canonical group
        alias = {group: seen.get(canonical, canonical) for group, canonical in given.items()}
        alias.update(seen)
        return BlockCensus(counts, widths, width_hist, alias)

    def sizing(self) -> BlockCensus:
        """the census this pool's blocks were sized from: the counts it was given, or - a one-pass
        pool sized from its own census, once placed - the census it computed, with the groups it left
        without a block (`unblocked`). A second assembly that must lay out the same tables alike
        takes this: `BlockPool(..., one_pass=True, **sizing().pool_kwargs())` - one pass, since a
        placing pool would keep the unblocked groups' tables inline and so move the op stream. Code
        appended to the second needs room for its tables in the overflow region: see the class
        docstring."""
        if self.counting or (self.sizes_from_census and not self._placed):
            raise FlipJumpPreprocessorException('sizing(): this pool has not sized its blocks')
        return BlockCensus(
            dict(self.counts),
            dict(self.widths),
            {group: dict(hist) for group, hist in self.width_hist.items()},
            dict(self.alias),
            list(self.unblocked),
        )

    def decline_report(self) -> Dict[str, int]:
        """{cause: tables declined} over every group (DECLINE_STALE + DECLINE_OTHER)"""
        report = {cause: 0 for cause in DECLINE_STALE + DECLINE_OTHER}
        for causes in self.declines_by_group.values():
            for cause, count in causes.items():
                report[cause] += count
        return report

    @property
    def stale_declines(self) -> int:
        """tables declined for a DECLINE_STALE cause - one only counts that are not this program's
        own produce. Zero does NOT mean the counts were fresh: counts with room to spare decline
        nothing (and stale counts that are too big can cause a 'no_block'). The test for stale
        counts is `census() == the census the pool was given`."""
        report = self.decline_report()
        return sum(report[cause] for cause in DECLINE_STALE)

    def _cheapest_order(self, slots: int) -> List[int]:
        """every index of a block of `slots`, lowest popcount first (cached and shared: read only)"""
        cached = self._cheap_index_cache.get(slots)
        if cached is None:
            cached = sorted(range(slots), key=lambda v: (bin(v).count('1'), v))
            self._cheap_index_cache[slots] = cached
        return cached

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
            group = groups[0]
            ranked: Dict[Tuple[str, int], Tuple[int, int]] = {}
            for site, occurrence, slot_ops in sites:
                if (site, occurrence) in ranked:
                    raise FlipJumpPreprocessorException(
                        f'pin protection: the heat list names {site!r} #{occurrence} of {key} twice'
                    )
                width = slot_ops if self.width_buckets else 0  # uniform slots are all one width
                rank = self.hot_ranks.get((group, width), 0)
                ranked[(site, occurrence)] = (width, rank)
                self.hot_ranks[(group, width)] = rank + 1
            self.hot_sites[group] = ranked

    def _hot_site(self, group: str, labels_prefix: str) -> Tuple[str, int]:
        """(site key, occurrence) of the table being reserved in a hot group. Counted for EVERY
        table, before anything can decline it, because the heat list numbers occurrences over every
        table the counting pass saw, a too-wide one included."""
        site = heat_key(labels_prefix)
        occurrence = self._hot_seen.get((group, site), 0)
        self._hot_seen[(group, site)] = occurrence + 1
        return site, occurrence

    def _hot_index(self, group: str, width: int, slots: int, site: Tuple[str, int]) -> Optional[int]:
        """A table's index in a HOT group (None: this width is full).

        A listed site takes the cheapest index its rank reserved; every other table takes the next
        one after all the reserved ranks. Both are positions in ONE cheapest-first order of the
        block's slots and the two ranges are disjoint, so no two tables share a slot. A listed site
        the program no longer has leaves its index unused, and since each width is sized for its
        tables PLUS its reserved ranks (_uniform_shape, _bucket_layout), that never pushes a table
        out.
        """
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
        return self._cheapest_order(slots)[rank] if rank < slots else None

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

    def _bucket_layout(self, group: str) -> Tuple[Dict[int, Tuple[int, int, int]], int]:
        """{slot_ops -> (offset_bits_within_block, slots, count)}, total_block_bits.

        A block's slots are uniform, so ONE wide table sets the width of every slot in its group,
        and most of a mixed group's block is width padding. Arming only needs `base ^ offset` to
        equal `base + offset`, which holds whenever the base is aligned to the block size and the
        offset lies inside it - uniform slots are merely the easy way to keep popcount(offset) low.
        Per-width sub-blocks keep that property: each bucket is aligned to its OWN size and they
        are laid out biggest-first, so a bucket's offset bits sit strictly above the bits its own
        slots use. A group with one width lays out exactly as the uniform path does.
        """
        cached = self._layout_cache.get(group)
        if cached is not None:
            return cached
        layout: Dict[int, Tuple[int, int, int]] = {}
        hist = self.width_hist.get(group)
        if not hist:
            slots, slot_bits = self._uniform_shape(group)
            layout[slot_bits // self.op_bits] = (0, slots, self.counts.get(group, 1))
            result = (layout, slots * slot_bits)
        else:
            buckets: Dict[int, int] = {}
            for width, count in hist.items():
                capped = min(max(width, 1), self.max_slot_ops)
                slot_ops = 1 << max(0, (capped - 1).bit_length())
                buckets[slot_ops] = buckets.get(slot_ops, 0) + count
            total_count = sum(hist.values())
            spread = self.spread if (self.spread > 1 and total_count >= self.spread_min_count) else 1
            sized: Dict[int, Tuple[int, int]] = {}
            for slot_ops, count in buckets.items():
                need = count + self.hot_ranks.get((group, slot_ops), 0)  # a hot width holds its ranks too
                slots = self._slots_for(count, need, spread)
                sized[slot_ops] = (slots, slots * slot_ops * self.op_bits)
            offset = 0
            for slot_ops in sorted(sized, key=lambda k: (-sized[k][1], -k)):
                slots, bits = sized[slot_ops]
                offset = -(-offset // bits) * bits  # align each bucket to its own size
                layout[slot_ops] = (offset, slots, buckets[slot_ops])
                offset += bits
            result = (layout, 1 << max(0, (offset - 1).bit_length()))
        self._layout_cache[group] = result
        return result

    def _block_bits(self, group: str) -> int:
        if self.width_buckets:
            return self._bucket_layout(group)[1]
        slots, slot_bits = self._uniform_shape(group)
        return slots * slot_bits

    def _uniform_shape(self, group: str) -> Tuple[int, int]:
        """(slots, slot_bits) for a group -- both powers of two, so index*slot_bits is a clean
        bit field and the arming XOR adds rather than subtracts."""
        count = self.counts.get(group, 1)
        width = min(max(self.widths.get(group, 16), 1), self.max_slot_ops)
        slot_ops = 1 << max(0, (width - 1).bit_length())
        spread = self.spread if (self.spread > 1 and count >= self.spread_min_count) else 1
        # a hot group holds its reserved ranks too; bucket mode (whose group without a width
        # histogram lands here) keys them by the slot width, uniform mode by 0
        need = count + self.hot_ranks.get((group, slot_ops if self.width_buckets else 0), 0)
        return self._slots_for(count, need, spread), slot_ops * self.op_bits

    @staticmethod
    def _slots_for(count: int, need: int, spread: int) -> int:
        """`count` rounded up to a power of two, times `spread`, then doubled only while `need` does not fit
        (a spread block has room)"""
        slots = (1 << max(0, (count - 1).bit_length())) * spread
        while slots < need:
            slots *= 2
        return slots

    def _hot_blocks_end(self, bits: Mapping[str, int]) -> int:
        """where the hot groups' blocks end when they are laid out first, from pool_base, with no limit"""
        end = self.pool_base
        for group in self.hot_sites:
            end = -(-end // bits[group]) * bits[group] + bits[group]
        return end

    def _pool_limit(self) -> int:
        """the first address past the pool: pool_base + span_bits, and never past the memory"""
        if self.span_bits is None:
            return 1 << self.memory_width
        return min(1 << self.memory_width, self.pool_base + self.span_bits)

    def _layout_order(self, keep: Collection[str], bits: Mapping[str, int]) -> List[str]:
        """the order blocks are laid out in: the hot groups first, in heat order, then biggest first"""
        return [g for g in self.hot_sites if g in keep] + sorted(
            (g for g in keep if g not in self.hot_sites), key=lambda g: (-bits[g], g)
        )

    def _layout(
        self, order: Sequence[str], bits: Mapping[str, int], limit: int
    ) -> Tuple[Dict[str, int], List[str], int]:
        """({group: base} of the blocks in `order` that fit below `limit`, the groups that do not, and
        the end of the last block placed): each block aligned to its own size, from pool_base up"""
        cursor = self.pool_base
        bases: Dict[str, int] = {}
        unfit: List[str] = []
        for group in order:
            block_bits = bits[group]
            base = -(-cursor // block_bits) * block_bits
            if base + block_bits > limit:
                unfit.append(group)
                continue
            bases[group] = base
            cursor = base + block_bits
        return bases, unfit, cursor

    def _preallocate(self, unblocked: Collection[str] = ()) -> None:
        """Assign every block a base up front: the hot groups first, in heat order, then the rest
        BIGGEST FIRST.

        Blocks are power-of-two sized and aligned to their own size, so allocating them in
        encounter order leaves a hole in front of each one, up to a whole block's worth; in
        descending size each block lands on an address the previous ones already aligned past.
        Hot-first placement can therefore leave holes among the hot blocks and one in front of the
        biggest block after them, and `evict_by_value` counts them. Doing it here rather than
        during expansion also makes allocation independent of the order macros are reached, which
        two assemblies of the same program need in order to agree.

        `unblocked` groups get no block (BlockCensus.unblocked); a hot group always does.
        """
        bits = {g: self._block_bits(g) for g in self.counts}
        limit = self._pool_limit()

        keep = set(self.counts)
        for group in unblocked:
            if group in keep and group not in self.hot_sites:
                keep.discard(group)
                self.broken_groups.add(group)  # same contract as a no-room break
        if self.evict_by_value:
            # something is dropped either way; dropping by ascending tables-per-bit spends the pool
            # on the groups with the most dispatch sites (`counts` counts SITES, not calls)
            capacity = limit - self.pool_base
            # with hot groups the demand is where the layout would END (hot-first placement can
            # leave holes, see the docstring); with no list it is the plain sum, even where an
            # unaligned pool base makes the layout end later
            hot_end = self._hot_blocks_end(bits)
            sizes = collections.Counter(bits[g] for g in keep if g not in self.hot_sites)
            rest = sum(size * n for size, n in sizes.items())

            def demand() -> int:
                if not self.hot_sites:
                    return rest
                biggest = max(sizes) if sizes else 1
                return -(-hot_end // biggest) * biggest + rest - self.pool_base

            if demand() > capacity:
                evictable = keep - set(self.hot_sites)  # a hot group is never evicted
                for group in sorted(evictable, key=lambda g: (self.counts.get(g, 1) / bits[g], g)):
                    if demand() <= capacity:
                        break
                    keep.discard(group)
                    self.broken_groups.add(group)  # same contract as a no-room break
                    self.evicted_low_value += 1
                    rest -= bits[group]
                    sizes[bits[group]] -= 1
                    if not sizes[bits[group]]:
                        del sizes[bits[group]]

        bases, unfit, cursor = self._layout(self._layout_order(keep, bits), bits, limit)
        self.broken_groups.update(unfit)  # no room: these groups stay inline everywhere
        for group, base in bases.items():
            self.groups[group] = (base, None)
        self._used = cursor - self.pool_base
        lost = sorted(group for group in self.hot_sites if group not in self.groups)
        if lost:
            raise FlipJumpPreprocessorException(
                f'pin protection: {len(lost)} hot group(s) got no block - the pool is full: {lost[:3]}'
            )

    def _decline(self, group: str, cause: str) -> None:
        """a table the pass cannot place, for `cause` (DECLINE_STALE / DECLINE_OTHER). Except for a
        group that got no block at all, this breaks the group: see broken_groups."""
        self.declined += 1
        causes = self.declines_by_group.setdefault(group, {})
        causes[cause] = causes.get(cause, 0) + 1
        if cause in ('no_block', 'new_group'):
            self.declined_no_block += 1
        elif cause in ('too_wide', 'width_overflow'):
            self.declined_too_wide += 1
            self.broken_groups.add(group)
        elif cause == 'overflow':
            self.declined_overflow += 1
            self.broken_groups.add(group)
        elif cause == 'next_address':
            self.declined_next_address += 1
            self.broken_groups.add(group)
        else:
            raise ValueError(f'unknown decline cause {cause!r}')

    def _width_cause(self, width: int) -> str:
        """why a table of `width` ops did not fit its slot: 'too_wide' when no counts could give it
        one (it is wider than max_slot_ops allows), else 'width_overflow' (the counts said its
        group's tables are narrower). A uniform slot is the group's width capped at max_slot_ops and
        rounded up to a power of two; a width bucket holds at most max_slot_ops."""
        widest = max(self.max_slot_ops, 1)
        if not self.width_buckets:
            widest = 1 << (widest - 1).bit_length()
        return 'too_wide' if width > widest else 'width_overflow'

    def reserve(
        self,
        ops_alignment: int,
        table_ops: int,
        group: Optional[str] = None,
        group_expr: Optional[Expr] = None,
        labels_prefix: str = '',
    ) -> Optional[Tuple[int, bool, int]]:
        if self._recording is not None and not self.one_pass:  # one_pass recorded it in defer()
            # first, so a table is recorded whatever happens to it below (census)
            self._recording.reserve(ops_alignment, table_ops, group, group_expr, labels_prefix)
        if group is None:
            self.ungrouped += 1  # no disarm wflip found: cannot be blocked
            return None
        group = self.alias.get(group, group)
        if self.counting:
            self.counts[group] = self.counts.get(group, 0) + 1
            if group_expr is not None and group not in self.exprs:
                self.exprs[group] = group_expr  # so the caller can resolve and canonicalise
            width = max(table_ops, ops_alignment)
            self.widths[group] = max(self.widths.get(group, 0), width)
            hist = self.width_hist.setdefault(group, {})
            hist[width] = hist.get(width, 0) + 1
            return None  # the counting pass must not change the layout
        hot_site = self._hot_site(group, labels_prefix) if group in self.hot_sites else None
        if group not in self.groups:
            # no block was reserved for it (see _preallocate): the pool had no room, or the counts
            # do not have the group at all
            self._decline(group, 'no_block' if group in self.counts else 'new_group')
            return None
        width = max(table_ops, ops_alignment)
        slot = self._bucket_slot(group, width) if self.width_buckets else self._uniform_slot(group, width)
        if slot is None:
            return None
        offset, slots, slot_ops, count = slot
        key = (group, slot_ops if self.width_buckets else 0)
        raw = self._next_index.get(key, 0)
        if hot_site is not None:
            index = self._hot_index(group, key[1], slots, hot_site)
            if index is None:
                self._decline(group, 'overflow')
                return None
        else:
            index = raw
            if self.spread > 1 and self.counts.get(group, 1) >= self.spread_min_count:
                # an overflow table must decline, never fall back to a raw index: the cheap-index
                # list is a subset of range(slots), so a raw index past its end can collide with a
                # mapped one already handed out (two tables at one address)
                if index >= count:
                    self._decline(group, 'overflow')
                    return None
                index = self._cheapest_order(slots)[index]
            if index >= slots:
                self._decline(group, 'overflow')  # more tables than the counting pass saw
                return None
        base, previous_expr = self.groups[group]
        self.groups[group] = (base, group_expr if group_expr is not None else previous_expr)
        self._next_index[key] = raw + 1
        self.allocated += 1
        address = base + offset + index * slot_ops * self.op_bits
        self.run_starts.append(address)
        return address, True, 0  # its own segment; blocks are sparse by construction

    def _uniform_slot(self, group: str, width: int) -> Optional[Tuple[int, int, int, int]]:
        """(offset in the block, slots, slot_ops, tables counted) of the group's uniform slots for a table of
        `width` ops, or None: declined, wider than its slot"""
        slots, slot_bits = self._uniform_shape(group)
        if width * self.op_bits > slot_bits:
            self._decline(group, self._width_cause(width))
            return None
        return 0, slots, slot_bits // self.op_bits, self.counts.get(group, 1)

    def _bucket_slot(self, group: str, width: int) -> Optional[Tuple[int, int, int, int]]:
        """_uniform_slot for width-bucketed blocks: the table picks the sub-block matching ITS width"""
        width = max(width, 1)
        if width > self.max_slot_ops:
            self._decline(group, 'too_wide')
            return None
        slot_ops = 1 << max(0, (width - 1).bit_length())
        entry = self._bucket_layout(group)[0].get(slot_ops)
        if entry is None:  # a width the counting pass never saw for this group
            self._decline(group, 'width_overflow')
            return None
        offset, slots, bucket_count = entry
        return offset, slots, slot_ops, bucket_count

    def commit(self, end_address: int) -> None:
        pass  # each table owns its slot; no shared cursor

    def defer(
        self,
        ops_alignment: int,
        table_ops: int,
        group: Optional[str],
        group_expr: Optional[Expr],
        labels_prefix: str = '',
        *,
        can_defer: bool = True,
    ) -> str:
        """one_pass: what becomes of a table the expansion reached, at its pad (DEFER_OUT,
        DEFER_TOO_WIDE, DEFER_NEXT_ADDRESS or DEFER_INLINE). It is recorded first (census), exactly
        as a counting pass counts it. This is the hook a subclass overrides to see every table at
        its pad, in order, with its site path.

        A table with no group cannot be blocked and stays inline, as in the two-pass mode. Two more
        stay inline but are queued, so place_deferred declines them in their turn (pin protection
        counts a hot group's tables in that order): one wider than any slot max_slot_ops allows -
        declined whatever the counts, exactly as a placing pass declines it at its pad - and one
        the caller cannot defer (`can_defer`: an entry computes an address from `$`).
        """
        self.recording.reserve(ops_alignment, table_ops, group, group_expr, labels_prefix)
        if group is None:
            self.ungrouped += 1
            return DEFER_INLINE
        if not can_defer:
            self.kept_inline += 1
            return DEFER_NEXT_ADDRESS
        if self._width_cause(max(table_ops, ops_alignment, 1)) == 'too_wide':
            return DEFER_TOO_WIDE
        self.deferred += 1
        return DEFER_OUT

    def kept_site(self, labels_prefix: str) -> str:
        """the site path a queued table keeps for its reserve() at placement (keeps_site_paths)"""
        if self._full_site_paths:
            return labels_prefix
        if self._heat:
            return sys.intern(heat_key(labels_prefix))  # _hot_site compares nothing else
        return ''

    def place_deferred(self, tables: DeferredTables, labels: LabelsDict) -> Sequence[int]:
        """one_pass: the address of every queued table (0 for one that stayed inline: no table can
        sit at 0, the program's start), in the order the expansion reached them.

        Sizes the blocks - from this expansion's own census when the pool was given no counts (merged
        by the alias `labels` imply among its groups, if merge_aliases; a group word that names a
        deferred table's label cannot be resolved yet and is counted in `alias_unresolved`), else
        from the counts it was given - and then decides every table exactly as a placing pass
        decides it at its pad, in the same order. A table that gets no block goes to the overflow
        region (_place_overflow) instead of inline, so nothing in the op stream depends on these
        decisions.
        """
        if self._placed:
            raise FlipJumpPreprocessorException('one-pass blocking: a pool places its tables once')
        self._placed = True
        if self.sizes_from_census:
            recording = self.recording
            if self.merge_aliases:  # else the `alias` it was given, if any
                self.alias, self.alias_unresolved = recording._alias_and_unresolved(labels)
                self.seen_alias = dict(self.alias)  # census(merge_aliases=True) reports what it was sized with
            self.counts, self.widths, self.width_hist = merge_aliased_groups(
                recording.counts, recording.widths, recording.width_hist, self.alias
            )
            self._layout_cache.clear()
        if self._heat:
            self._resolve_heat(self._heat)
        if self.sizes_from_census and self.overflow_base is None:
            self.unblocked = self._choose_unblocked(tables)
        self._preallocate(self.unblocked)
        addresses = array('Q', bytes(8 * len(tables)))  # 0: it stayed inline
        overflow: List[Tuple[int, int, int]] = []  # (index, ops_alignment, table_ops), encounter order
        for index, group, group_expr, ops_alignment, table_ops, site, inline_cause in tables:
            if inline_cause == DEFER_NEXT_ADDRESS:
                self._decline_next_address(group, site)
                continue
            reserved = self.reserve(ops_alignment, table_ops, group, group_expr, site)
            if inline_cause is not None:  # too wide: reserve() declines it, exactly as at its pad
                if reserved is not None:  # defer() and reserve() disagree on what is too wide
                    raise FlipJumpPreprocessorException(
                        f'one-pass blocking: a table kept inline as too wide got a slot ({group})'
                    )
            elif reserved is None:
                overflow.append((index, ops_alignment, table_ops))
            else:
                addresses[index] = reserved[0]
        for index, address in self._place_overflow(overflow):
            addresses[index] = address
        return addresses

    def _decline_next_address(self, group: str, site: str) -> None:
        """a table whose entries use `$` stayed inline: decline it in its turn, as reserve() declines
        a table at its pad - a hot group counts it among its sites first (see _hot_site)"""
        group = self.alias.get(group, group)
        if group in self.hot_sites:
            self._hot_site(group, site)
        self._decline(group, 'next_address')

    def _choose_unblocked(self, tables: DeferredTables) -> List[str]:
        """The groups to leave without a block so that the blocks AND the overflow region - every
        table of a group with no block, packed as _place_overflow packs them - fit in the pool's span
        (one pass, sized from its own census, with no overflow_base).

        When every block fits, nothing overflows and nothing is left out: the tables are not even
        read. Otherwise each ROUND lays out the blocks exactly as _preallocate lays them out and the
        region exactly as _place_overflow fills it, the groups that do not fit (the ones the two-pass
        build keeps inline) overflowing too; and while that layout is short, more groups are left out.
        Leaving a group out frees its block but costs its tables' room in the region, so on balance
        it frees only the block's padding (power-of-two or spread): groups are left out by that NET
        room per table, most first, until it covers the shortfall - and the next round's exact layout
        says whether it did (`overflow_rounds` counts the layouts; each round leaves at least one more
        group out). A hot group is never left out, nor is a group whose tables need all of its block
        by the estimate (every slot holds a table as wide as the slot).

        The net room is an ESTIMATE - a table counts at its size rounded up to its `pad` - and the
        choice is greedy: it never puts a group back. In the region a table packs against its
        neighbours' pads in encounter order, so leaving a group out can free more room than the
        estimate says (a 1-op table behind `pad 16` that ends the region takes 1 op, not 16) or less
        (a table that leaves an alignment hole before the next), and another set of groups may fit
        where this choice falls short. So it refuses only on the EXACT layout - short, with no group
        left that the estimate says frees room - and says by how much. A hot group that does not
        fit is refused at once: the hot blocks are laid out first, so no group left out makes room.
        """
        bits = {group: self._block_bits(group) for group in self.counts}
        limit = self._pool_limit()
        order = self._layout_order(self.counts, bits)  # a subset's layout order is this one, filtered
        self.overflow_rounds += 1
        bases, unfit, end = self._layout(order, bits, limit)
        if not unfit:
            return []  # every block fits, so nothing overflows: the plain layout
        if any(group in self.hot_sites for group in unfit):
            hot_end = self._hot_blocks_end(bits)
            raise FlipJumpPreprocessorException(
                f"one-pass blocking: the pool [{hex(self.pool_base)}, {hex(limit)}) cannot hold its hot groups' "
                f'blocks and the overflow region of the groups it has no block for - short by {hex(hot_end - limit)} '
                f'bits for the hot blocks alone, which end at {hex(hot_end)}: a hot group always gets a block and '
                f'the hot blocks are laid out first, so no group left without a block makes room for them. Raise '
                f'span_bits.'
            )
        canonical = {raw: self.alias.get(raw, raw) for raw in tables.exprs}  # every raw group queued
        need: Dict[str, int] = {}  # a group's room in the overflow region, by the estimate
        count: Dict[str, int] = {}
        for index, raw in enumerate(tables.groups):  # the arrays themselves: nothing is built per table
            if index not in tables.inline:
                group = canonical[raw]
                align = tables.aligns[index] * self.op_bits
                need[group] = need.get(group, 0) + -(-tables.sizes[index] * self.op_bits // align) * align
                count[group] = count.get(group, 0) + 1
        unblocked: List[str] = []
        left_out: Set[str] = set()
        while True:
            end = self._overflow_end(tables, canonical, left_out.union(unfit), end)
            short = end - limit
            if short <= 0:
                # with the groups that do not fit named too, _preallocate lays out exactly this
                # layout whatever else it does (evict_by_value), and so does a pool given sizing()
                return unblocked + unfit
            candidates = sorted(
                (g for g in bases if g not in self.hot_sites and bits[g] > need.get(g, 0)),
                key=lambda g: (count.get(g, 0) / (bits[g] - need.get(g, 0)), g),
            )
            if not candidates:
                raise FlipJumpPreprocessorException(
                    f'one-pass blocking: the pool [{hex(self.pool_base)}, {hex(limit)}) cannot hold the overflow '
                    f'region of the groups it has no block for - short by {hex(short)} bits with every group whose '
                    f'block frees room by the estimate left without a block ({len(unblocked)} left out; the '
                    f'estimate counts a table at its padded size, so another choice may fit). Raise span_bits, or '
                    f'pass an overflow_base outside it.'
                )
            freed = 0
            for group in candidates:
                if freed >= short:
                    break
                left_out.add(group)
                unblocked.append(group)
                freed += bits[group] - need.get(group, 0)
            self.overflow_rounds += 1
            bases, unfit, end = self._layout([g for g in order if g not in left_out], bits, limit)

    def _overflow_end(
        self, tables: DeferredTables, canonical: Mapping[str, str], without: Collection[str], start: int
    ) -> int:
        """where the overflow region ends if it starts at `start` and holds every queued table of the
        groups `without` (`canonical` maps each raw group to its group), in encounter order, each
        aligned as its `pad` asks - as _place_overflow fills it. A table that stayed inline takes no
        room there."""
        raws = {raw for raw, group in canonical.items() if group in without}
        end = start
        if raws:
            aligns, sizes, inline, op_bits = tables.aligns, tables.sizes, tables.inline, self.op_bits
            for index, raw in enumerate(tables.groups):
                if raw in raws and index not in inline:
                    align = aligns[index] * op_bits
                    end = -(-end // align) * align + sizes[index] * op_bits
        return end

    def _place_overflow(self, overflow: Sequence[Tuple[int, int, int]]) -> List[Tuple[int, int]]:
        """[(index, address)] for the tables that got no block, in the overflow region: in the order
        the expansion reached them, each aligned as its `pad` asks - so code appended to a program
        never moves the overflow tables of the program before it.

        The region starts right after the last block, inside the pool's span, unless `overflow_base`
        says otherwise (then it may reach the top of memory, and must not overlap the blocks). It
        can never sit right after the main stream, whose wflip area is laid out only once labels
        resolve. A declined table dispatches from here exactly as it would inline: an un-pinned word
        is armed with its full address, a pinned one with `address ^ base` (see pinned_words).
        """
        if not overflow:
            return []
        blocks_end = self.pool_base + self._used
        start = blocks_end if self.overflow_base is None else self.overflow_base
        limit = self._pool_limit() if self.overflow_base is None else 1 << self.memory_width
        cursor = start
        placed: List[Tuple[int, int]] = []
        for index, align, ops in overflow:
            align_bits = align * self.op_bits
            address = -(-cursor // align_bits) * align_bits
            cursor = address + ops * self.op_bits
            placed.append((index, address))
            self.overflow_tables.append((address, align))
        if cursor > limit:
            bound = 'the end of the pool' if self.overflow_base is None else 'the end of memory'
            raise FlipJumpPreprocessorException(
                f'one-pass blocking: the overflow region [{hex(start)}, {hex(cursor)}) for {len(placed)} table(s) '
                f'ends past {bound} at {hex(limit)} - short by {hex(cursor - limit)} bits. Raise span_bits, '
                f'or pass an overflow_base with room.'
            )
        if self.overflow_base is not None and start < blocks_end and cursor > self.pool_base:
            raise FlipJumpPreprocessorException(
                f'one-pass blocking: the overflow region [{hex(start)}, {hex(cursor)}) overlaps the blocks '
                f'[{hex(self.pool_base)}, {hex(blocks_end)}). Move overflow_base.'
            )
        self.overflowed += len(placed)
        return placed

    def pinned_words(self) -> Dict[Expr, int]:
        """{source-word Expr: block base} - the caller resolves the Expr once labels are known.

        Groups that failed to place every table are EXCLUDED (see broken_groups) unless
        `pin_broken` is set, or the group is hot (`heat`). The arithmetic says an inline table
        survives pinning - the word rests at `value*dw ^ base`, an inline table at `A` is armed with
        `A ^ base`, and `(value*dw ^ base) ^ (A ^ base)` is `A + value*dw` - so a declined table then
        pays a worse popcount rather than becoming unreachable; the exclusion is the conservative
        default.
        """
        return {
            expr: base
            for group, (base, expr) in self.groups.items()
            if expr is not None and (self.pin_broken or group in self.hot_sites or group not in self.broken_groups)
        }
