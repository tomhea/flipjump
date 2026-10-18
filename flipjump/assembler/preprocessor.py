"""
the preprocessor (macro-resolution stage).
expands macro calls and rep (repetition) blocks recursively, starting from the main
macro, and produces the flat op-list that the assembler then resolves into addresses.
"""

from __future__ import annotations

import bisect
import collections
import dataclasses
import re
import sys
from array import array
from itertools import islice
from typing import (
    Any,
    Dict,
    Tuple,
    Iterable,
    Iterator,
    Union,
    Deque,
    Set,
    List,
    Optional,
    NoReturn,
    Callable,
    Collection,
    Mapping,
    MutableSequence,
    Sequence,
)

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
    Op,
    INITIAL_MACRO_NAME,
    INITIAL_ARGS,
    INITIAL_LABELS_PREFIX,
)

CurrTree = Deque[Union[MacroCall, RepCall]]
OpsQueue = Deque[LastPhaseOp]
LabelsDict = Dict[str, int]

wflip_start_label = '_.wflip_area_start_'

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

    Kept as parallel arrays, not an object per table: a big program queues hundreds of thousands of
    tables (doom-flipjump ~622K), and the per-table cost is what one pass adds over a placing pass.
    MEASURED (tests/unit/test_table_pool.py::test_a_deferred_table_costs_tens_of_bytes, 20,000
    tables): 43 bytes per table with one label and no site path, +13 per further label, +9 for a
    kept site path - plus the path string it keeps alive: pin protection keeps the interned heat
    key (shared by every site with that key), a subclass that overrides reserve() the full path.
    That is on top of what a placing pass holds too: the op pointers, the NewSegment, the label
    strings. (An object per table, with its own op list and (label, offset) tuples, cost ~540.)
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


def table_names_next_address(ops: Sequence[Op], pad_index: int, end_index: int) -> bool:
    """does an entry of the table after `ops[pad_index]` (through `ops[end_index]`) use `$`, the
    address of the op after it? Its value is computed while the macro expands, so such a table must
    have its address by then. (A jump to `$` already keeps relocatable_table_end from accepting it;
    this is about a FLIP of an address computed from `$`.)"""
    return any(
        isinstance(op, FlipJump) and '$' in op.flip.all_unknown_labels()
        for op in ops[pad_index + 1 : end_index + 1]  # noqa: E203
    )


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


class _FreeSpace:
    """`pack`'s free list: disjoint [lo, hi) bit intervals, kept by the bit length of their size so a
    search skips every interval too short to hold a block.

    A search reads at most SCAN intervals of each size class (lowest first): one long enough may still
    hold no aligned base, and on a 63K-group census an unbounded scan of such intervals cost 64 of the
    layout's 67 seconds. A block that finds none in the window goes to a larger class or higher up -
    the layout stays a pure function of the census. Measured on doom-flipjump's E1M2 census (spread 1
    and 2): the same extents to the word as the unbounded search, in 2 s and 5 s instead of 73 s and
    203 s."""

    SCAN = 64

    def __init__(self, lo: int, hi: int) -> None:
        self.by_size: Dict[int, List[Tuple[int, int]]] = {}
        self.give(lo, hi)

    def give(self, lo: int, hi: int) -> None:
        if hi > lo:
            bisect.insort(self.by_size.setdefault((hi - lo).bit_length(), []), (lo, hi))

    def take(self, lo: int, hi: int) -> None:
        intervals = self.by_size[(hi - lo).bit_length()]
        intervals.pop(bisect.bisect_left(intervals, (lo, hi)))

    def find(self, need: int, align: int) -> Optional[Tuple[int, int, int]]:
        """(base, lo, hi): the lowest `align`-aligned base with [base, base + need) inside one free
        interval [lo, hi), or None"""
        best: Optional[Tuple[int, int, int]] = None
        for size in sorted(self.by_size):
            if size < need.bit_length():
                continue  # every interval of this size class is shorter than `need`
            for lo, hi in islice(self.by_size[size], self.SCAN):
                if best is not None and lo >= best[0]:
                    break
                base = -(-lo // align) * align
                if base + need <= hi:
                    best = (base, lo, hi)
                    break
        return best


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
        pack: bool = False,
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
        @param pack: lay the blocks out by the slots their tables USE (see _layout_packed): a block keeps
        its size, its alignment and every table's index - so every arm flips what it flips without
        pack - but the space inside it that no table of its own uses (a bucket's tail, the
        power-of-two round-up, the slots the cheapest-index order skips) holds other, smaller blocks.
        Only where blocks sit changes. Off by default: the layout is unchanged.
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
        if pack and evict_by_value:
            raise ValueError('pack: evict_by_value prices blocks by their whole size; the two are not combined')
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
        self._pack_runs: Dict[str, Dict[int, Tuple[int, List[Tuple[int, int]]]]] = {}
        self._bucket_next: Dict[Tuple[str, int], int] = {}
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
        # opt-in: blocks share the space their own tables leave unused (_layout_packed)
        self.pack = pack
        # opt-in: pin a group even though some of its tables were declined (see pinned_words)
        self.pin_broken = pin_broken
        self.groups: Dict[str, Tuple[int, Optional[Expr]]] = {}  # group -> (base, source-word Expr)
        # A group that could not place EVERY one of its tables is not pinned: a pinned word makes
        # every writer flip `V ^ base`, which sends the arm of a DECLINED (inline) table to
        # `switch ^ base`. Un-pinned, the word rests at `digit`, the full address is written, and
        # both the blocked and the inline tables in it stay correct; only the saving is lost.
        self.broken_groups: Set[str] = set()
        self._used = 0  # bits handed out, from pool_base
        self._next_index: Dict[str, int] = {}
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
        keeps the alias it was SIZED with instead (see census)."""
        if not (self.sizes_from_census and self.merge_aliases and self._placed):
            self.seen_alias = self.recording.canonical_alias(labels)

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

    def _cheap_indices(self, slots: int, need: int) -> List[int]:
        """the `need` lowest-popcount indices inside a block of `slots`, cheapest first"""
        return self._cheapest_order(slots)[:need]

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
        self._pack_runs.clear()

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
                slots = (1 << max(0, (count - 1).bit_length())) * spread
                need = count + self.hot_ranks.get((group, slot_ops), 0)  # a hot width holds its ranks too
                while slots < need:  # double only when they do not fit: a spread width has room
                    slots *= 2
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
        slots = 1 << max(0, (count - 1).bit_length())
        if self.spread > 1 and count >= self.spread_min_count:
            slots *= self.spread
        # a hot group holds its reserved ranks too; bucket mode (whose group without a width
        # histogram lands here) keys them by the slot width, uniform mode by 0
        need = count + self.hot_ranks.get((group, slot_ops if self.width_buckets else 0), 0)
        while slots < need:  # double only when they do not fit: a spread group has room
            slots *= 2
        return slots, slot_ops * self.op_bits

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
        if self.pack:
            return self._layout_packed(order, bits, limit)
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

    def _bucket_runs(self, group: str) -> Dict[int, Tuple[int, List[Tuple[int, int]]]]:
        """{slot_ops: (bucket offset in bits, [(lo, hi) index runs its tables can take])} - the model
        `pack` lays out by and holds `reserve` to (_pack_refuses). Per bucket (one implicit bucket on the
        uniform path): a hot group takes the first `count + ranks` of the cheapest-first order (its
        listed sites their ranks, the others after them: _hot_index); a spread group the first `count`
        of it; every other group the raw indices 0 .. count-1, one run."""
        cached = self._pack_runs.get(group)
        if cached is not None:
            return cached
        ob = self.op_bits
        if self.width_buckets:
            layout = self._bucket_layout(group)[0]
        else:
            n_slots, slot_bits = self._uniform_shape(group)
            layout = {slot_bits // ob: (0, n_slots, self.counts.get(group, 1))}
        spread = self.spread > 1 and self.counts.get(group, 1) >= self.spread_min_count
        hot = group in self.hot_sites
        result: Dict[int, Tuple[int, List[Tuple[int, int]]]] = {}
        for slot_ops, (offset, slots, count) in layout.items():
            if hot or spread:
                ranks = self.hot_ranks.get((group, slot_ops if self.width_buckets else 0), 0) if hot else 0
                used = sorted(self._cheapest_order(slots)[: count + ranks])
                runs: List[Tuple[int, int]] = []
                start = prev = used[0]
                for index in used[1:]:
                    if index != prev + 1:
                        runs.append((start, prev + 1))
                        start = index
                    prev = index
                runs.append((start, prev + 1))
            else:
                runs = [(0, count)]
            result[slot_ops] = (offset, runs)
        self._pack_runs[group] = result
        return result

    def _used_slots(self, group: str) -> List[Tuple[int, int]]:
        """[(lo, hi)] bit offsets inside `group`'s block its tables can occupy, sorted (_bucket_runs)"""
        ob = self.op_bits
        out = [
            (offset + lo * slot_ops * ob, offset + hi * slot_ops * ob)
            for slot_ops, (offset, runs) in self._bucket_runs(group).items()
            for lo, hi in runs
        ]
        out.sort()
        return out

    def _pack_refuses(self, group: str, slot_ops: int, index: int) -> bool:
        """pack: a table whose index lies outside its group's modelled runs (counts that are stale - more
        tables than the census) must decline: another block may sit in that slot"""
        if not self.pack:
            return False
        entry = self._bucket_runs(group).get(slot_ops)
        return entry is None or not any(lo <= index < hi for lo, hi in entry[1])

    def _layout_packed(
        self, order: Sequence[str], bits: Mapping[str, int], limit: int
    ) -> Tuple[Dict[str, int], List[str], int]:
        """`_layout` for `pack`: each block, in `order`, at the LOWEST base aligned to its own size whose
        [base, base + end of its used slots) is free; then only its used slots are taken - the space
        between them, and above them up to the block's size, goes back to the free list for the blocks
        after it. A table keeps its index, so its address is `base + the same offset`, and no two blocks'
        used slots overlap (each is placed inside free space). The end is the highest used bit."""
        free = _FreeSpace(self.pool_base, limit)
        bases: Dict[str, int] = {}
        unfit: List[str] = []
        end = self.pool_base
        for group in order:
            runs = self._used_slots(group)
            need = runs[-1][1]
            found = free.find(need, bits[group])
            if found is None:
                unfit.append(group)
                continue
            base, lo, hi = found
            free.take(lo, hi)
            free.give(lo, base)
            cursor = base
            for run_lo, run_hi in runs:
                free.give(cursor, base + run_lo)
                cursor = base + run_hi
            free.give(base + need, hi)
            bases[group] = base
            end = max(end, base + need)
        return bases, unfit, end

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
            # leave holes, see the docstring); with no list it stays 1.5.1's plain sum, byte for
            # byte, even where an unaligned pool base makes the layout end later
            hot_end = self.pool_base
            for group in self.hot_sites:
                hot_end = -(-hot_end // bits[group]) * bits[group] + bits[group]
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
            self._next_index[group] = 0
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
        if self.width_buckets:
            return self._reserve_bucketed(ops_alignment, table_ops, group, group_expr, hot_site)
        slots, slot_bits = self._uniform_shape(group)
        if table_ops * self.op_bits > slot_bits or ops_alignment * self.op_bits > slot_bits:
            self._decline(group, self._width_cause(max(table_ops, ops_alignment)))  # wider than its slot
            return None
        if hot_site is not None:
            hot = self._hot_index(group, 0, slots, hot_site)
            if hot is None:
                self._decline(group, 'overflow')
                return None
            index = hot
            if self._pack_refuses(group, slot_bits // self.op_bits, index):
                self._decline(group, 'overflow')
                return None
        else:
            index = self._next_index[group]
            count = self.counts.get(group, 1)
            if self.spread > 1 and count >= self.spread_min_count:
                # an overflow table must decline, never fall back to a raw index: the cheap-index
                # list is a subset of range(slots), so a raw index past its end can collide with a
                # mapped one already handed out (two tables at one address)
                if index >= count:
                    self._decline(group, 'overflow')
                    return None
                index = self._cheap_indices(slots, count)[index]
            if index >= slots or self._pack_refuses(group, slot_bits // self.op_bits, index):
                self._decline(group, 'overflow')  # more tables than the counting pass saw
                return None
        base, previous_expr = self.groups[group]
        self.groups[group] = (base, group_expr if group_expr is not None else previous_expr)
        self._next_index[group] = self._next_index[group] + 1
        self.allocated += 1
        address = base + index * slot_bits
        self.run_starts.append(address)
        return address, True, 0  # its own segment; blocks are sparse by construction

    def _reserve_bucketed(
        self,
        ops_alignment: int,
        table_ops: int,
        group: str,
        group_expr: Optional[Expr],
        hot_site: Optional[Tuple[str, int]] = None,
    ) -> Optional[Tuple[int, bool, int]]:
        """`reserve` for width-bucketed blocks: the table picks the sub-block matching ITS width.

        Mirrors the uniform path exactly - same decline reasons, same sparse-index rule, same
        raw-counter increment - differing only in that the slot size and offset come from the
        bucket rather than from the group's widest table.
        """
        width = max(table_ops, ops_alignment, 1)
        if width > self.max_slot_ops:
            self._decline(group, 'too_wide')
            return None
        slot_ops = 1 << max(0, (width - 1).bit_length())
        layout, _ = self._bucket_layout(group)
        entry = layout.get(slot_ops)
        if entry is None:  # a width the counting pass never saw for this group
            self._decline(group, 'width_overflow')
            return None
        offset, slots, bucket_count = entry
        key = (group, slot_ops)
        raw = self._bucket_next.get(key, 0)
        if hot_site is not None:
            hot = self._hot_index(group, slot_ops, slots, hot_site)
            if hot is None:
                self._decline(group, 'overflow')
                return None
            index = hot
            if self._pack_refuses(group, slot_ops, index):
                self._decline(group, 'overflow')
                return None
        else:
            index = raw
            total_count = self.counts.get(group, 1)
            if self.spread > 1 and total_count >= self.spread_min_count:
                if raw >= bucket_count:  # same rule as the uniform path: decline, never a raw index
                    self._decline(group, 'overflow')
                    return None
                index = self._cheap_indices(slots, bucket_count)[raw]
            if index >= slots or self._pack_refuses(group, slot_ops, index):
                self._decline(group, 'overflow')
                return None
        base, previous_expr = self.groups[group]
        self.groups[group] = (base, group_expr if group_expr is not None else previous_expr)
        self._bucket_next[key] = raw + 1
        self.allocated += 1
        address = base + offset + index * slot_ops * self.op_bits
        self.run_starts.append(address)
        return address, True, 0

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
            self._pack_runs.clear()
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
            hot_end = self.pool_base  # where the hot blocks alone end, with no limit
            for group in self.hot_sites:
                hot_end = -(-hot_end // bits[group]) * bits[group] + bits[group]
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


# a macro-expansion's progress slice thinner than this is not tracked any deeper (see PreprocessorData)
_PROGRESS_MIN_SLICE = 1e-5
# ...and progress is reported only when it moved at least this much (one call per macro call is too many)
_PROGRESS_MIN_STEP = 1e-4


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
        __slots__ = (
            'curr_tree',
            'calling_op',
            'macros',
            'max_recursion_depth',
        )

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
        table_pool: Optional[TablePool] = None,
    ):
        """
        @param memory_width: the memory-width
        @param macros: parser's result; the dictionary from the macro names to the macro declaration
        @param max_recursion_depth: The compiler supports macros that recursively uses other macros,
        up to the specified recursion depth.
        @param save_debug_labels: whether to record the per-expansion `...---:start:` macro-start
        labels. See insert_macro_start_label for why they can be skipped.
        @param table_pool: if given, relocate the lookup tables it wants (see TablePool)
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

        # TABLE PLACEMENT (off unless a pool is given). Relocated tables are emitted into a
        # SEPARATE stream and flushed after the main one in finish(), so the main stream stays a
        # single ascending segment and the pool's runs become segments of their own.
        self.table_pool = table_pool
        self.pool_ops: Deque[LastPhaseOp] = collections.deque()
        self.emit_target: MutableSequence[LastPhaseOp] = self.result_ops
        self._reloc_stack: List[Tuple[int, MutableSequence[LastPhaseOp]]] = []
        self._pool_segment: Optional[NewSegment] = None

        # ONE-PASS BLOCKING (off unless the pool is a BlockPool(one_pass=True)): a table the pool
        # takes is emitted into the pool's stream behind a NewSegment with no address yet, and waits
        # in `_deferred` until finish() has the pool place them all
        self._one_pass: Optional[BlockPool] = (
            table_pool if isinstance(table_pool, BlockPool) and table_pool.one_pass else None
        )
        self._deferred: Optional[DeferredTables] = (
            None if self._one_pass is None else DeferredTables(self._one_pass.keeps_site_paths)
        )
        self._deferring = False  # emitting a deferred table's ops
        self._next_address_cache: Dict[Tuple[int, int], bool] = {}

        # PROGRESS (off unless on_progress is given): each macro expansion owns a slice [lo, hi) of
        # the [0, 1) progress line, split evenly among its ops; a macro call / rep iteration takes its
        # op's sub-slice. Only CALLS look at it -- a plain op pays nothing -- and a slice thinner than
        # _PROGRESS_MIN_SLICE is not tracked (None) below it, so the bookkeeping stays tiny.
        self.on_progress: Optional[Callable[[float], None]] = None
        self.progress_slice: Optional[Tuple[float, float]] = None
        self._progress_reported = -1.0

        self.max_recursion_depth = max_recursion_depth
        # set python's recursion-limit so the preprocessor's own depth-check (at max_recursion_depth)
        #  triggers before python's RecursionError. Set it unconditionally - whether it's higher or lower than the
        #  current limit - so that raising max_recursion_depth actually allows deeper macro-recursion.
        sys.setrecursionlimit(max_recursion_depth + GAP_BETWEEN_PYTHONS_AND_PREPROCESSOR_MACRO_RECURSION_DEPTH)

    def enter_progress_slice(
        self, macro_slice: Tuple[float, float], op_index: int, ops_count: int, part: int, parts: int
    ) -> None:
        """a call at op `op_index` of a macro owning `macro_slice` (its `part`-th of `parts` rep
        iterations): report where it starts, and make its sub-slice the callee's"""
        lo, hi = macro_slice
        op_width = (hi - lo) / ops_count
        width = op_width / parts
        start = lo + op_width * op_index + width * part
        if start - self._progress_reported >= _PROGRESS_MIN_STEP:
            self._progress_reported = start
            assert self.on_progress is not None
            self.on_progress(start)
        self.progress_slice = (start, start + width) if width >= _PROGRESS_MIN_SLICE else None

    def patch_last_wflip_address(self) -> None:
        self.last_new_segment.wflip_start_address = self.curr_address

    def emit(self, op: LastPhaseOp) -> None:
        """append to whichever stream is being built -- the main one, or the table pool"""
        self.emit_target.append(op)

    def begin_relocation(
        self,
        macro_name: MacroName,
        ops_alignment: int,
        table_ops: int,
        labels_prefix: str = '',
        group: Optional[str] = None,
        group_expr: Optional[Expr] = None,
        table_source: Optional[Tuple[Macro, int, int]] = None,
    ) -> bool:
        """Move emission to a pool address instead of aligning in place.

        A table is reached only by jump, so relocating it changes nothing but its address -- and
        the address is what the arming wflip pays for. Returns False when there is no pool, the
        pool does not want this macro, or the pool is full; the caller then pads normally.

        `table_source` is (the macro, the pad's index, the table's last op index): a one-pass pool
        needs it to see whether an entry uses `$` (see _begin_deferral).
        """
        pool = self.table_pool
        if pool is None or not pool.wants(macro_name, labels_prefix):
            return False
        if group_expr is not None and self._is_runtime_word(group_expr):
            pool.reserved_words += 1
            return False
        if self._one_pass is not None:
            return self._begin_deferral(
                self._one_pass, ops_alignment, table_ops, labels_prefix, group, group_expr, table_source
            )
        reserved = pool.reserve(ops_alignment, table_ops, group, group_expr, labels_prefix)
        if reserved is None:
            return False
        address, starts_new_run, gap_ops = reserved
        self._reloc_stack.append((self.curr_address, self.emit_target))
        self.emit_target = self.pool_ops
        if starts_new_run:
            segment = NewSegment(address)
            self.pool_ops.append(segment)
            self._pool_segment = segment
        elif gap_ops:
            # alignment skipped inside this run -- the segment writes its words consecutively, so
            # the gap has to exist in the op stream too
            self.pool_ops.append(Padding(gap_ops))
        self.curr_address = address
        return True

    def _is_runtime_word(self, word: Expr) -> bool:
        """is `word` one of the runtime's (see RESERVED_BELOW)? Those are declared before any code
        dispatches through them, so they resolve here; a program variable, declared after the
        code, does not yet."""
        try:
            return word.exact_eval(self.labels) < RESERVED_BELOW
        except FlipJumpExprException:
            return False

    def _names_next_address(self, table_source: Optional[Tuple[Macro, int, int]]) -> bool:
        """does the table use `$` (table_names_next_address)? Cached per macro and pad. A caller that
        gives no source is assumed to (and its table stays inline)."""
        if table_source is None:
            return True
        macro, pad_index, end_index = table_source
        key = (id(macro), pad_index)  # a macro lives as long as the expansion: its id is stable
        names = self._next_address_cache.get(key)
        if names is None:
            names = table_names_next_address(macro.ops, pad_index, end_index)
            self._next_address_cache[key] = names
        return names

    def _begin_deferral(
        self,
        pool: BlockPool,
        ops_alignment: int,
        table_ops: int,
        labels_prefix: str,
        group: Optional[str],
        group_expr: Optional[Expr],
        table_source: Optional[Tuple[Macro, int, int]],
    ) -> bool:
        """one pass: take the table out of the op stream (see BlockPool's ONE PASS). Its ops go into
        the pool's stream behind a NewSegment whose address is set once it is placed, and its labels
        are recorded as offsets from its start, so curr_address counts from 0 inside it - nothing in
        the table can read that number, since an entry that uses `$` keeps its table inline. A table
        that stays inline (too wide for any slot, or using `$`) is still queued, so the pool declines
        it in its turn."""
        verdict = pool.defer(
            ops_alignment,
            table_ops,
            group,
            group_expr,
            labels_prefix,
            can_defer=not self._names_next_address(table_source),
        )
        if verdict == DEFER_INLINE:
            return False
        assert group is not None and self._deferred is not None  # defer() keeps an ungrouped table inline
        site = pool.kept_site(labels_prefix)
        if verdict != DEFER_OUT:
            self._deferred.add(group, group_expr, ops_alignment, table_ops, site, None, verdict)
            return False  # aligned and emitted in place, exactly as a placing pass declines it
        segment = NewSegment(0)  # its address is decided once the expansion is done
        self.pool_ops.append(segment)
        self._deferred.add(group, group_expr, ops_alignment, table_ops, site, segment)
        self._deferring = True
        self._reloc_stack.append((self.curr_address, self.emit_target))
        self.emit_target = self.pool_ops
        self.curr_address = 0
        return True

    def _place_deferred_tables(self, pool: BlockPool) -> None:
        """one pass, after the expansion: have the pool place every queued table, then give each
        that left the stream its labels and its segment's address (flush_table_pool sorts them)"""
        tables = self._deferred
        assert tables is not None
        op_bits = 2 * self.memory_width
        addresses = pool.place_deferred(tables, self.labels)
        for index, address in enumerate(addresses):
            if not address:
                continue  # it stayed inline: its labels are in place already
            for label, offset in tables.labels_of(index):
                self.labels[label] = address + offset
                if self.save_debug_labels:
                    self.addresses_with_labels.add(address + offset)
            segment = tables.segments[index]
            assert segment is not None
            segment.start_address = address
            segment.wflip_start_address = address + tables.sizes[index] * op_bits  # a table has no wflips
        self._deferred = DeferredTables(pool.keeps_site_paths)  # release them

    def end_relocation(self) -> None:
        """close the table and resume the interrupted stream where it left off.

        The run's segment ends wherever the last table in it ended, so its wflip_start_address is
        moved forward here rather than guessed: a relocated table contains no wflips, so that
        address is simply the segment's end.
        """
        if self._deferring:
            self._deferring = False
            assert self._deferred is not None
            promised = self._deferred.sizes[-1]
            if self.curr_address != promised * 2 * self.memory_width:
                raise FlipJumpPreprocessorException(
                    f'one-pass blocking: a deferred table holds {self.curr_address // (2 * self.memory_width)} '
                    f'ops where its pad promised {promised}'
                )
            self.curr_address, self.emit_target = self._reloc_stack.pop()
            return
        if self.table_pool is not None:
            self.table_pool.commit(self.curr_address)
        if self._pool_segment is not None:
            self._pool_segment.wflip_start_address = self.curr_address
        saved_address, saved_target = self._reloc_stack.pop()
        self.curr_address = saved_address
        self.emit_target = saved_target

    def flush_table_pool(self) -> None:
        """append the pool's segments after the main stream.

        Each run opened its own NewSegment, and the runs were allocated in ascending popcount --
        NOT ascending address -- so they are sorted here. The main stream's wflip area has already
        been patched, and the pool sits above it by construction (pool_base), so they cannot
        collide.
        """
        if not self.pool_ops:
            return
        runs: List[Tuple[NewSegment, List[LastPhaseOp]]] = []
        for op in self.pool_ops:
            if isinstance(op, NewSegment):
                runs.append((op, [op]))
            elif runs:
                runs[-1][1].append(op)
        runs.sort(key=lambda run: run[0].start_address)
        for segment, run_ops in runs:
            self.last_new_segment = segment
            self.result_ops.extend(run_ops)
        self.pool_ops.clear()

    def finish(self, show_statistics: bool) -> None:
        self.patch_last_wflip_address()
        if self.table_pool is not None and self.curr_address > self.table_pool.pool_base:
            # The pool sits above the main stream by construction. If the program grew into it, a
            # relocated table is sitting on top of code and the program is silently wrong -- so
            # this is an error, not a warning.
            raise FlipJumpPreprocessorException(
                'table-placement: the program reached %s, which is inside the table pool based at '
                '%s. Raise pool_base, or relocate fewer tables.'
                % (hex(self.curr_address), hex(self.table_pool.pool_base))
            )
        if self._one_pass is not None:
            self._place_deferred_tables(self._one_pass)
        self.flush_table_pool()
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

        # (one pass: a deferred table's label is in labels_code_positions before it is in labels)
        if label in self.labels or (self._one_pass is not None and label in self.labels_code_positions):
            other_position = self.labels_code_positions[label]
            macro_resolve_error(
                self.curr_tree, f'label declared twice - "{label}" on ' f'{code_position} and {other_position}'
            )
        self.labels_code_positions[label] = code_position
        if self._deferring:
            # a deferred table's label: an offset now, an address once the table is placed
            assert self._deferred is not None
            self._deferred.add_label(label, address)
            return
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
        self.emit(Padding(ops_to_pad))

    def deferred_labels_note(self, op: Union[RepCall, Pad, Segment, Reserve]) -> str:
        """why an expression evaluated during the expansion may have failed: it names the label of a
        table a one-pass BlockPool places only after it ('' when it names none). Only the expression
        that is evaluated is searched - a rep's count, not its arguments."""
        if self._one_pass is None:
            return ''
        if isinstance(op, RepCall):
            expr = op.repeat_times
        elif isinstance(op, Pad):
            expr = op.ops_alignment
        elif isinstance(op, Segment):
            expr = op.start_address
        else:
            expr = op.reserved_bit_size
        # a deferred label is declared (it has a code position) but has no address yet
        named = sorted(
            label
            for label in expr.all_unknown_labels()
            if label in self.labels_code_positions and label not in self.labels
        )
        if not named:
            return ''
        return (
            f' It names {", ".join(named)}, the label of a table that one-pass blocking places only after '
            f'the expansion: no address exists yet. Assemble this program with the two-pass BlockPool.'
        )


def get_rep_times(op: RepCall, preprocessor_data: PreprocessorData) -> int:
    try:
        return op.calculate_times(preprocessor_data.labels)
    except FlipJumpExprException as e:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f"Can't evaluate how many times to repeat in "
            f"'rep {op.macro_name}'. In {op.code_position}."
            f"{preprocessor_data.deferred_labels_note(op)}",
            orig_exception=e,
        )


def get_pad_ops_alignment(op: Pad, preprocessor_data: PreprocessorData) -> int:
    try:
        ops_alignment = op.calculate_ops_alignment(preprocessor_data.labels)
    except FlipJumpExprException as e:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f"Can't evaluate how much to pad in "
            f"'pad {op.ops_alignment}'. In {op.code_position}."
            f"{preprocessor_data.deferred_labels_note(op)}",
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
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f'segment failed. In {op.code_position}.{preprocessor_data.deferred_labels_note(op)}',
            orig_exception=e,
        )


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
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f'reserve failed. In {op.code_position}.{preprocessor_data.deferred_labels_note(op)}',
            orig_exception=e,
        )


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


def relocatable_table_end(ops: List[Op], pad_index: int) -> Optional[Tuple[int, int, Optional[Expr]]]:
    """(index of the table's last op, number of ops in it, the source-word Expr) after
    `ops[pad_index]`, or None if the run must not be moved.

    THE TABLE is the maximal run of plain `a;b` ops after the `pad`, INCLUDING labels interleaved
    among them, but with TRAILING labels trimmed off. Both halves of that rule are load-bearing,
    and stl contains a counter-example to each:

      * `hex.exact_xor` ends `... d3;switch+7*dw` / `end:` / `wflip src+w, switch`. `end:` names
        the DISARM, which stays inline, so a trailing label must NOT be relocated -- otherwise
        every table entry jumps into empty pool space.

      * `hex.cmp` ends `__lt: ;lt` / `__eq: jumper+dbit ;eq` / `__gt: jumper+dbit+1 ;gt`. Those
        three are ONE table selected by flipping address bits of the jumper, so an interior label
        MUST be relocated with it -- splitting them apart breaks the compare.

    The run is refused when
      * an entry has an EMPTY jump target (`dst;`, continue to the next address): `bit.exact_xor`
        is that shape, and in the pool the next address is another table's slot;
      * the pad is reached by falling into it rather than by a jump (the op before it is a `dst;`
        or a `wflip` with no return address), since the pool is not the next address;
      * the run ends at a `wflip` that is not the DISARM of the run's head label. A dispatch table
        is armed and disarmed through one word (`wflip src+w, switch, src` ... `wflip src+w,
        switch`); a `wflip` that installs anything else is an ENTRY of a multi-op table
        (`hex.input_hex`'s `flip3: wflip stl.IO+w, flip3, end`, `hex.pointers.xor_hex_to_flip_ptr`'s
        `after_flip_bit2: wflip to_flip, ..., cleanup`), and moving the plain ops before it away
        from it breaks the table.
    """
    if not _reached_only_by_jump(ops, pad_index):
        return None
    head: Optional[str] = None
    last_flipjump = None
    count = 0
    source_word: Optional[Expr] = None
    for offset, op in enumerate(ops[pad_index + 1 :], start=pad_index + 1):  # noqa: E203
        if isinstance(op, FlipJump):
            if _falls_through(op):
                return None
            last_flipjump = offset
            count += 1
        elif isinstance(op, Label):
            if count == 0 and head is None:
                head = op.name
        else:
            if isinstance(op, WordFlip):
                # the disarm names the head label; its word is the jump word the table is
                # dispatched through, and that word's EXPRESSION (the address is unknown this
                # early) groups the tables that share a word (see BlockPool)
                if head is None or not _same_local_label(op.flip_value, head):
                    return None
                source_word = op.word_address
            break
    if last_flipjump is None:
        return None
    return last_flipjump, count, source_word


def _same_local_label(value: Expr, label: str) -> bool:
    """does `value` name `label`? a macro body's labels are parsed namespaced (`hex.switch`) while
    a reference to one is the bare local name (`switch`), so the local part is what is compared"""
    return isinstance(value.value, str) and value.value.rpartition('.')[2] == label.rpartition('.')[2]


def _falls_through(op: Op) -> bool:
    """does control leave `op` by falling to the next address? (`dst;`, or a `wflip` with no
    return address)"""
    if isinstance(op, FlipJump):
        return '$' in op.jump.all_unknown_labels()
    if isinstance(op, WordFlip):
        return '$' in op.return_address.all_unknown_labels()
    return False


def _reached_only_by_jump(ops: List[Op], pad_index: int) -> bool:
    """is the pad at `ops[pad_index]` preceded by an op that jumps away? labels in between do not
    count, and a pad that opens the macro body is reached from whatever came before the call"""
    for op in reversed(ops[:pad_index]):
        if isinstance(op, Label):
            continue
        return not _falls_through(op) and isinstance(op, (FlipJump, WordFlip))
    return False


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
    relocated = False
    table_end = -1
    current_macro = preprocessor_data.macros[macro_name]
    if preprocessor_data.table_pool is not None and macro_name.name in RAW_READERS:
        preprocessor_data.table_pool.reads_words_raw = True
    params_dict = get_params_dictionary(current_macro, args, current_macro.namespace, labels_prefix)

    preprocessor_data.insert_macro_start_label(labels_prefix, current_macro.code_position)

    for op_index, op in enumerate(current_macro.ops):
        # a relocated table ends at its last `a;b` op (see relocatable_table_end); everything after
        # it - `exact_xor`'s `end:` label and its disarm wflip - belongs to the inline stream
        if relocated and op_index > table_end:
            preprocessor_data.end_relocation()
            relocated = False

        if isinstance(op, Label):
            preprocessor_data.insert_label(op.eval_name(params_dict), op.code_position)

        elif isinstance(op, FlipJump) or isinstance(op, WordFlip):
            preprocessor_data.curr_address += 2 * preprocessor_data.memory_width
            params_dict['$'] = Expr(preprocessor_data.curr_address)
            preprocessor_data.emit(op.eval_new(params_dict))
            del params_dict['$']

        elif isinstance(op, Pad):
            op = op.eval_new(params_dict)
            ops_alignment = get_pad_ops_alignment(op, preprocessor_data)
            # a `pad` marks a lookup table: reached only by jump, so instead of aligning here it may
            # be emitted at a pool address, and the interrupted stream resumes after it (TablePool)
            found = relocatable_table_end(current_macro.ops, op_index)
            group = None
            group_expr: Optional[Expr] = None
            if found is not None and found[2] is not None:
                # bind the macro's parameters (`src + w` names the caller's variable); unknown
                # labels stay as names, so two sites on the same variable compare equal
                group_expr = found[2].eval_new(params_dict)
                group = str(group_expr)
            if (
                not relocated
                and found is not None
                and preprocessor_data.begin_relocation(
                    macro_name,
                    ops_alignment,
                    found[1],
                    labels_prefix,
                    group,
                    group_expr,
                    table_source=(current_macro, op_index, found[0]),
                )
            ):
                table_end = found[0]
                relocated = True
            else:
                preprocessor_data.align_current_address(ops_alignment)

        elif isinstance(op, MacroCall):
            op = op.eval_new(params_dict)
            next_macro_path = (
                f"{labels_prefix}{MACRO_SEPARATOR_STRING}" if labels_prefix else ""
            ) + f"{op.code_position.short_str()}:{op.macro_name}"
            progress_slice = preprocessor_data.progress_slice
            if progress_slice is not None:
                preprocessor_data.enter_progress_slice(progress_slice, op_index, len(current_macro.ops), 0, 1)
            with preprocessor_data.prepare_macro_call(op):
                resolve_macro_aux(preprocessor_data, op.macro_name, op.arguments, next_macro_path)
            if progress_slice is not None:
                preprocessor_data.progress_slice = progress_slice

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
            progress_slice = preprocessor_data.progress_slice
            with preprocessor_data.prepare_macro_call(op):
                for i in range(rep_times):
                    op.current_index = i
                    if progress_slice is not None:
                        preprocessor_data.enter_progress_slice(
                            progress_slice, op_index, len(current_macro.ops), i, rep_times
                        )
                    resolve_macro_aux(
                        preprocessor_data, op.macro_name, op.calculate_arguments(i), next_macro_path.format(i)
                    )
            if progress_slice is not None:
                preprocessor_data.progress_slice = progress_slice

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

    if relocated:
        preprocessor_data.end_relocation()

    preprocessor_data.register_macro_code_size(labels_prefix, init_curr_address)


def resolve_macros(
    memory_width: int,
    macros: Dict[MacroName, Macro],
    *,
    show_statistics: bool = False,
    max_recursion_depth: int = DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    save_debug_labels: bool = True,
    table_pool: Optional[TablePool] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> Tuple[OpsQueue, LabelsDict]:
    """
    unwind the macro tree to a serialized-queue of ops,
    and creates a dictionary from label's name to its address.
    @param memory_width: the memory-width
    @param macros: parser's result; the dictionary from the macro names to the macro declaration
    @param show_statistics: if True then prints the macro-usage statistics
    @param max_recursion_depth: The compiler supports macros that recursively uses other macros,
    up to the specified recursion depth.
    @param table_pool: if given, relocate the lookup tables it wants to low-popcount addresses.
    See TablePool -- a table is reached only by jump, so its address is free to choose, and that
    address is what the arming wflip pays for on every call.
    @param save_debug_labels: record the per-expansion `...---:start:` macro-start labels. They are
    unreachable from fj source (see PreprocessorData.insert_macro_start_label), so a caller that is
    not writing a debugging file can pass False. It cannot change the emitted .fjm.
    @param on_progress: if given, called with the expansion's progress, a fraction in [0, 1) - by how
    far into the main macro (and the calls in it) the expansion is. It cannot change the emitted .fjm.
    @return: tuple of the queue of ops, and the labels' dictionary
    """
    preprocessor_data = PreprocessorData(
        memory_width, macros, max_recursion_depth, save_debug_labels=save_debug_labels, table_pool=table_pool
    )
    if on_progress is not None:
        preprocessor_data.on_progress = on_progress
        preprocessor_data.progress_slice = (0.0, 1.0)
    resolve_macro_aux(preprocessor_data, INITIAL_MACRO_NAME, INITIAL_ARGS, INITIAL_LABELS_PREFIX)

    preprocessor_data.finish(show_statistics)
    return preprocessor_data.get_result_ops_and_labels()
