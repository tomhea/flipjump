"""
the preprocessor (macro-resolution stage).
expands macro calls and rep (repetition) blocks recursively, starting from the main
macro, and produces the flat op-list that the assembler then resolves into addresses.
"""

from __future__ import annotations

import collections
import sys
from typing import Dict, Tuple, Iterable, Union, Deque, Set, List, Optional, NoReturn, Callable

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
        self.allocated = 0
        self.declined = 0
        self.reserved_words = 0  # tables through the runtime's words, never relocated
        self._offsets = self._cheapest_offsets()
        self._next_offset = 0
        self._cursor = 0  # bit address of the next free spot inside the current run
        self._run_end = 0
        # A table can be LONGER than the run it started in -- `hex.cmp`'s is 31 ops behind a
        # `pad 4` -- and then it spills into the slots above. Those slots are recorded here so a
        # later (cheaper-popcount) run is never opened on top of one.
        self._consumed: Set[int] = set()
        self.run_starts: List[int] = []

    def _cheapest_offsets(self) -> List[int]:
        """every run-aligned offset in the pool, cheapest popcount first"""
        reach = (1 << self.memory_width) - self.pool_base
        if self.span_bits is not None:
            reach = min(reach, self.span_bits)
        count = reach // self.run_bits
        return sorted(range(count), key=lambda v: (bin(v).count('1'), v))

    def wants(self, macro_name: MacroName, labels_prefix: str = '') -> bool:
        """`labels_prefix` is the full macro-expansion path, i.e. the CALL SITE -- so a caller can
        relocate the hot sites a profile named rather than every expansion of a macro. Cheap
        addresses are finite, and spending them on cold tables is how a placement pass ends up
        costing more than it saves."""
        if self.capacity is not None and self.allocated >= self.capacity:
            return False
        return True if self._wants is None else self._wants(macro_name, labels_prefix)

    def reserve(
        self, ops_alignment: int, table_ops: int, group: Optional[str] = None, group_expr: Optional[Expr] = None
    ) -> Optional[Tuple[int, bool, int]]:
        """(address, starts_a_new_run, gap_ops) for a table of `table_ops` ops.

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
        while self._next_offset < len(self._offsets):
            slot = self._offsets[self._next_offset]
            self._next_offset += 1
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

    TWO PASSES. A block's size must be known before its base is chosen, and the tables of a group
    are scattered through the program, so counting comes first:

        counting = BlockPool(w, base)                 # counts=None -> relocates nothing
        resolve_macros(..., table_pool=counting)
        placing = BlockPool(w, base, counts=counting.counts, widths=counting.widths)
        resolve_macros(..., table_pool=placing)

    Blocks are power-of-two sized and aligned to their own size, so the base has no bits where the
    index goes and the arming XOR adds rather than subtracts.
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
    ):
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
        self._bucket_next: Dict[Tuple[str, int], int] = {}
        self.counting = counts is None
        # {group key: canonical group key}. Groups are keyed by the source word's EXPRESSION, and
        # two expressions can name one word (`(x + 32)` and `(x + w)` at w=32); without merging
        # them resolve_pinned has to un-pin both. The caller derives this from a counting pass
        # with canonical_alias, once the labels are known.
        self.alias = alias or {}
        self.exprs: Dict[str, Expr] = {}
        # why a table was declined, by cause
        self.declined_no_block = 0  # its group got no block at all (pool exhausted)
        self.declined_too_wide = 0  # wider than max_slot_ops, so it cannot share a uniform slot
        self.declined_overflow = 0  # more tables in the group than the counting pass saw
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
        if not self.counting:
            self._preallocate()

    def canonical_alias(self, labels: LabelsDict) -> Dict[str, str]:
        """{group -> canonical group} for groups whose expressions resolve to the SAME address.

        Run it on a COUNTING pool once its assembly's labels are known, and pass the result as the
        placing pool's `alias`; without it every alias pair loses both its pins.
        """
        by_address: Dict[int, str] = {}
        alias: Dict[str, str] = {}
        for group in sorted(self.exprs):
            try:
                address = self.exprs[group].exact_eval(labels)
            except FlipJumpExprException:
                continue
            first = by_address.setdefault(address, group)
            if first != group:
                alias[group] = first
        return alias

    def _cheap_indices(self, slots: int, need: int) -> List[int]:
        """the `need` lowest-popcount indices inside a block of `slots`, cheapest first"""
        cached = self._cheap_index_cache.get(slots)
        if cached is None:
            cached = sorted(range(slots), key=lambda v: (bin(v).count('1'), v))
            self._cheap_index_cache[slots] = cached
        return cached[:need]

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
        slots = 1 << max(0, (count - 1).bit_length())
        if self.spread > 1 and count >= self.spread_min_count:
            slots *= self.spread
        width = min(max(self.widths.get(group, 16), 1), self.max_slot_ops)
        slot_ops = 1 << max(0, (width - 1).bit_length())
        return slots, slot_ops * self.op_bits

    def _preallocate(self) -> None:
        """Assign every block a base up front, BIGGEST FIRST.

        Blocks are power-of-two sized and aligned to their own size, so allocating them in
        encounter order leaves a hole in front of each one, up to a whole block's worth; in
        descending size each block lands on an address the previous ones already aligned past.
        Doing it here rather than during expansion also makes allocation independent of the order
        macros are reached, which two assemblies of the same program need in order to agree.
        """
        bits = {g: self._block_bits(g) for g in self.counts}
        cursor = self.pool_base
        limit = (
            (1 << self.memory_width)
            if self.span_bits is None
            else min(1 << self.memory_width, self.pool_base + self.span_bits)
        )

        keep = set(self.counts)
        if self.evict_by_value:
            # something is dropped either way; dropping by ascending tables-per-bit spends the pool
            # on the groups with the most dispatch sites (`counts` counts SITES, not calls)
            capacity = limit - self.pool_base
            demand = sum(bits.values())
            if demand > capacity:
                for group in sorted(keep, key=lambda g: (self.counts.get(g, 1) / bits[g], g)):
                    if demand <= capacity:
                        break
                    keep.discard(group)
                    self.broken_groups.add(group)  # same contract as a no-room break
                    self.evicted_low_value += 1
                    demand -= bits[group]

        order = sorted(keep, key=lambda g: (-bits[g], g))
        for group in order:
            block_bits = bits[group]
            base = -(-cursor // block_bits) * block_bits
            if base + block_bits > limit:
                self.broken_groups.add(group)  # no room: this group stays inline everywhere
                continue
            self.groups[group] = (base, None)
            self._next_index[group] = 0
            cursor = base + block_bits
        self._used = cursor - self.pool_base

    def _decline(self, group: str, cause: str) -> None:
        """a table the pass cannot place. Except for a group that got no block at all, this breaks
        the group: see broken_groups."""
        self.declined += 1
        if cause == 'no_block':
            self.declined_no_block += 1
        elif cause == 'too_wide':
            self.declined_too_wide += 1
            self.broken_groups.add(group)
        else:
            self.declined_overflow += 1
            self.broken_groups.add(group)

    def reserve(
        self, ops_alignment: int, table_ops: int, group: Optional[str] = None, group_expr: Optional[Expr] = None
    ) -> Optional[Tuple[int, bool, int]]:
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
        if group not in self.groups:
            self._decline(group, 'no_block')  # no block was reserved for it (see _preallocate)
            return None
        if self.width_buckets:
            return self._reserve_bucketed(ops_alignment, table_ops, group, group_expr)
        slots, slot_bits = self._uniform_shape(group)
        if table_ops * self.op_bits > slot_bits or ops_alignment * self.op_bits > slot_bits:
            self._decline(group, 'too_wide')  # wider than the counting pass saw
            return None
        index = self._next_index[group]
        count = self.counts.get(group, 1)
        if self.spread > 1 and count >= self.spread_min_count:
            # an overflow table must decline, never fall back to a raw index: the cheap-index list
            # is a subset of range(slots), so a raw index past its end can collide with a mapped
            # one already handed out (two tables at one address)
            if index >= count:
                self._decline(group, 'overflow')
                return None
            index = self._cheap_indices(slots, count)[index]
        if index >= slots:
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
        self, ops_alignment: int, table_ops: int, group: str, group_expr: Optional[Expr]
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
            self._decline(group, 'too_wide')
            return None
        offset, slots, bucket_count = entry
        key = (group, slot_ops)
        raw = self._bucket_next.get(key, 0)
        index = raw
        total_count = self.counts.get(group, 1)
        if self.spread > 1 and total_count >= self.spread_min_count:
            if raw >= bucket_count:  # same rule as the uniform path: decline, never a raw index
                self._decline(group, 'overflow')
                return None
            index = self._cheap_indices(slots, bucket_count)[raw]
        if index >= slots:
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

    def pinned_words(self) -> Dict[Expr, int]:
        """{source-word Expr: block base} - the caller resolves the Expr once labels are known.

        Groups that failed to place every table are EXCLUDED (see broken_groups) unless
        `pin_broken` is set. The arithmetic says an inline table survives pinning - the word rests
        at `value*dw ^ base`, an inline table at `A` is armed with `A ^ base`, and
        `(value*dw ^ base) ^ (A ^ base)` is `A + value*dw` - so a declined table then pays a worse
        popcount rather than becoming unreachable; the exclusion is the conservative default.
        """
        return {
            expr: base
            for group, (base, expr) in self.groups.items()
            if expr is not None and (self.pin_broken or group not in self.broken_groups)
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
        self.emit_target: Deque[LastPhaseOp] = self.result_ops
        self._reloc_stack: List[Tuple[int, Deque[LastPhaseOp]]] = []
        self._pool_segment: Optional[NewSegment] = None

        self.max_recursion_depth = max_recursion_depth
        # set python's recursion-limit so the preprocessor's own depth-check (at max_recursion_depth)
        #  triggers before python's RecursionError. Set it unconditionally - whether it's higher or lower than the
        #  current limit - so that raising max_recursion_depth actually allows deeper macro-recursion.
        sys.setrecursionlimit(max_recursion_depth + GAP_BETWEEN_PYTHONS_AND_PREPROCESSOR_MACRO_RECURSION_DEPTH)

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
    ) -> bool:
        """Move emission to a pool address instead of aligning in place.

        A table is reached only by jump, so relocating it changes nothing but its address -- and
        the address is what the arming wflip pays for. Returns False when there is no pool, the
        pool does not want this macro, or the pool is full; the caller then pads normally.
        """
        pool = self.table_pool
        if pool is None or not pool.wants(macro_name, labels_prefix):
            return False
        if group_expr is not None and self._is_runtime_word(group_expr):
            pool.reserved_words += 1
            return False
        reserved = pool.reserve(ops_alignment, table_ops, group, group_expr)
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

    def end_relocation(self) -> None:
        """close the table and resume the interrupted stream where it left off.

        The run's segment ends wherever the last table in it ended, so its wflip_start_address is
        moved forward here rather than guessed: a relocated table contains no wflips, so that
        address is simply the segment's end.
        """
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

        if label in self.labels:
            other_position = self.labels_code_positions[label]
            macro_resolve_error(
                self.curr_tree, f'label declared twice - "{label}" on ' f'{code_position} and {other_position}'
            )
        self.labels_code_positions[label] = code_position
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


def get_rep_times(op: RepCall, preprocessor_data: PreprocessorData) -> int:
    try:
        return op.calculate_times(preprocessor_data.labels)
    except FlipJumpExprException as e:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f"Can't evaluate how many times to repeat in " f"'rep {op.macro_name}'. In {op.code_position}.",
            orig_exception=e,
        )


def get_pad_ops_alignment(op: Pad, preprocessor_data: PreprocessorData) -> int:
    try:
        ops_alignment = op.calculate_ops_alignment(preprocessor_data.labels)
    except FlipJumpExprException as e:
        macro_resolve_error(
            preprocessor_data.curr_tree,
            f"Can't evaluate how much to pad in " f"'pad {op.ops_alignment}'. In {op.code_position}.",
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
        macro_resolve_error(preprocessor_data.curr_tree, f'segment failed. In {op.code_position}.', orig_exception=e)


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
        macro_resolve_error(preprocessor_data.curr_tree, f'reserve failed. In {op.code_position}.', orig_exception=e)


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
                    macro_name, ops_alignment, found[1], labels_prefix, group, group_expr
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
            with preprocessor_data.prepare_macro_call(op):
                resolve_macro_aux(preprocessor_data, op.macro_name, op.arguments, next_macro_path)

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
            with preprocessor_data.prepare_macro_call(op):
                for i in range(rep_times):
                    op.current_index = i
                    resolve_macro_aux(
                        preprocessor_data, op.macro_name, op.calculate_arguments(i), next_macro_path.format(i)
                    )

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
    @return: tuple of the queue of ops, and the labels' dictionary
    """
    preprocessor_data = PreprocessorData(
        memory_width, macros, max_recursion_depth, save_debug_labels=save_debug_labels, table_pool=table_pool
    )
    resolve_macro_aux(preprocessor_data, INITIAL_MACRO_NAME, INITIAL_ARGS, INITIAL_LABELS_PREFIX)

    preprocessor_data.finish(show_statistics)
    return preprocessor_data.get_result_ops_and_labels()
