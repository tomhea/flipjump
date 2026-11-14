"""
The pins of a table pool: which jump words rest at their block's base (see "pin" in inner_classes/table_pool.py).

The pool proposes the words (BlockPool.pinned_words). `pins_that_hold` resolves them to addresses and keeps only the
pins that can hold: a pinned word holds `value ^ base`, so every use of the word must be one that a pin leaves right
(see unpin).
"""

import bisect
from operator import itemgetter
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from flipjump.utils.exceptions import FlipJumpAssemblerException, FlipJumpExprException
from flipjump.utils.progress import clear_progress_line
from flipjump.assembler.inner_classes import op_stream
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.offset_guard import OffsetGuard
from flipjump.assembler.inner_classes.op_stream import OpStream, VALUE_ADDRESS, VALUE_NUMBER
from flipjump.assembler.inner_classes.ops import NewSegment
from flipjump.assembler.inner_classes.table_pool import BlockPool, reserved_below


def pins_that_hold(ops: OpStream, labels: Dict[str, int], pool: BlockPool, warning_as_errors: bool) -> Dict[int, int]:
    """Return the words to pin, {jump-word address: block base}: the pool's candidates resolved by `labels`, minus
    the words of pin_all_but's cells, words two groups pin with different bases, and pins that can't hold (see
    unpin)."""
    unpinned_cells, missing = pool.unpinned_cells(labels)
    if missing:
        pin_all_but_warning(missing, warning_as_errors)
    pinned, conflicts = resolve_pinned(pool.pinned_words(), labels, pool.memory_width, unpinned_cells)
    if conflicts:
        pool.unpins['two groups pin it at different bases'] = conflicts
    if pinned:
        try:
            unpin(ops, labels, pinned, pool)
        except FlipJumpExprException:
            # an operand has no value. Labels-resolve runs next, and says which op it is and where.
            pinned = {}
    pool.pinned = pinned
    return pinned


def pin_all_but_warning(missing: List[str], warning_as_errors: bool) -> None:
    """Warn (or fail, with warnings as errors) when pin_all_but names labels the program doesn't define. A typo there
    would leave a raw cell pinned."""
    shown = ', '.join(missing[:5]) + (f' and {len(missing) - 5} more' if len(missing) > 5 else '')
    message = f'pin_all_but names labels the program does not define: {shown}'
    if warning_as_errors:
        raise FlipJumpAssemblerException(message)
    clear_progress_line()
    print(f'Warning: {message}')


def resolve_pinned(
    pinned_exprs: Dict[Expr, int],
    labels: Dict[str, int],
    memory_width: int,
    unpinned_cells: Sequence[Tuple[int, int]] = (),
) -> Tuple[Dict[int, int], int]:
    """Resolve the pinned words' expressions to addresses. Skip a word that two groups pin with different bases
    (like `(x + 32)` and `(x + w)`), and a word in one of `unpinned_cells` (the program reads them raw; they are
    (start, end) address ranges, as BlockPool.unpinned_cells returns them).
    Returns: ({jump-word address: block base}, how many addresses were skipped for having two bases)."""
    runtime_end = reserved_below(memory_width)
    pinned: Dict[int, int] = {}
    conflicts = set()
    for expr, base in pinned_exprs.items():
        try:
            address = expr.exact_eval(labels)
        except FlipJumpExprException:
            continue  # an unresolvable word simply is not pinned
        if address < runtime_end:
            continue  # the runtime's word (stl.IO), not the program's
        if _in_a_cell(address, unpinned_cells):
            continue  # the program reads this word raw
        if address in pinned and pinned[address] != base:
            conflicts.add(address)
        pinned[address] = base
    for address in conflicts:
        del pinned[address]
    return pinned, len(conflicts)


def _in_a_cell(address: int, cells: Sequence[Tuple[int, int]]) -> bool:
    """Is `address` inside one of `cells`: sorted (start, end) ranges that do not overlap?"""
    index = bisect.bisect_right(cells, address, key=itemgetter(0)) - 1
    return index >= 0 and address < cells[index][1]


def unpin(stream: OpStream, labels: Dict[str, int], pinned: Dict[int, int], pool: BlockPool) -> None:
    """Unpin each word of `pinned` whose pin can't hold, counting why in `pool.unpins`. Unpinning is always safe,
    only slower.
    A pinned word holds its value xor its base, and is right again only while it is armed: a table's address is
    flipped into it xor the base, so the bases cancel. So a pin holds when:
    - the word has its own op (the pin changes what it rests at);
    - every address flipped into it is a table's (a label plus a constant; the label's a moved table's, or an inline
      one's that a `pad` aligned), and so is an arm or a disarm;
    - every other flip of it - a number, a value computed from labels (`T1 ^ T2`), an `a;b` flip - stays inside its
      window: such a flip changes a value or moves within the armed table;
    - no wflip flips across it and the next word;
    - the only ops that jump through it are its arms (`wflip word, table, word's op`): any other jump may find it
      unarmed, holding its base too. With pin_all_but, an `a;b` op may jump through it as well: pin_all_but names
      the cells that are jumped through at rest, so such a jump comes after an arm (as `stl.fret` after `stl.fcall`).

    Not checked - a program that depends on these is not kept the same:
    - which wflips arm a word and which disarm it: `wflip word, table, word's op` is taken as an arm, as the stl's
      dispatches are. A program that arms a word without jumping, and then jumps with the wflip that disarms it,
      must name the word's cell in pin_all_but.
    - with pin_all_but, a number that arms a word which an `a;b` op then jumps through (`wflip x+w, 0x200` and
      `;x`): a number flipped into a word inside its window is taken as a change of its value, and is flipped as
      it is. Name such a word's cell in pin_all_but.
    - a flip of an armed word at or above its table's `pad` alignment: it leaves the table only where the table's
      address has that bit set, which the pool changes (it aligns a moved table to its whole size)."""
    guard = pool.offset_guard
    assert guard is not None  # a pool's preprocessor made it
    uses = WordUses(pinned, pool.memory_width, jumps_follow_an_arm=pool.pin_all_but is not None)
    uses.scan(stream, labels, guard)
    for word, base in list(pinned.items()):
        why = uses.why_unpin(word, base)
        if why is not None:
            del pinned[word]
            pool.unpins[why] = pool.unpins.get(why, 0) + 1


class WordUses:
    """What a program does with the words a pool wants to pin, gathered in one pass over its op stream."""

    def __init__(self, words: Iterable[int], memory_width: int, jumps_follow_an_arm: bool = False):
        """`jumps_follow_an_arm`: an `a;b` op that jumps through a word finds it armed (see unpin)"""
        self.memory_width = memory_width
        self.jumps_follow_an_arm = jumps_follow_an_arm
        # word -> log2 of the alignment (in bits) of each table it is armed with. Its keys are the words.
        self.arms: Dict[int, List[int]] = {word: [] for word in words}
        self.own_op: Set[int] = set()  # words that are the jump word of an op of their own
        self.flipped_bits: Dict[int, int] = {}  # word -> the bits that `a;b` ops flip in it
        self.numbers: Dict[int, int] = {}  # word -> the bits that wflips of plain numbers flip in it
        self.computed: Dict[int, int] = {}  # word -> the bits that wflips of computed values (`T1 ^ T2`) flip in it
        self.jumped: Set[int] = set()  # words that an `a;b` op jumps through
        self.misarmed: Set[int] = set()  # words that a wflip returns through without arming them with an address
        self.split: Set[int] = set()  # words that a wflip across two words flips
        self.not_tables: Set[int] = set()  # words that an address which is no table's is flipped into

    def scan(self, stream: OpStream, labels: Dict[str, int], guard: OffsetGuard) -> None:
        """Walk the op stream, noting every use of the words."""
        w, arms = self.memory_width, self.arms
        address = 0
        next_slot = iter(stream.vals).__next__
        value_of, refs = stream.deferred_value, stream.refs
        FJ_POS, WFLIP, PAD, OBJ, FJ_PACKED = (
            op_stream.FJ_POS,
            op_stream.WFLIP,
            op_stream.PAD,
            op_stream.OBJ,
            op_stream.FJ_PACKED,
        )
        for kind in stream.kinds:
            if kind == WFLIP:
                self._scan_wflip(stream, next_slot, labels, guard)
                address += 2 * w
            elif kind == PAD:
                address += next_slot() * 2 * w
            elif kind == OBJ:
                obj = refs[next_slot()]
                address = obj.start_address if isinstance(obj, NewSegment) else obj.first_address_after_reserved
            else:
                # an `a;b` op. Most ops are these, so its slots are read and it is noted right here, in place.
                if kind == FJ_PACKED:
                    packed = next_slot()
                    flip, jump = packed >> op_stream.PACKED_JUMP_BITS, packed & op_stream.PACKED_JUMP_MASK
                else:
                    flip = next_slot()
                    if flip < 0:
                        flip = value_of(~flip, next_slot, labels)
                    jump = next_slot()
                    if jump < 0:
                        jump = value_of(~jump, next_slot, labels)
                    if kind == FJ_POS:
                        next_slot()  # the position
                if address + w in arms:
                    self.own_op.add(address + w)
                if jump + w in arms:
                    self.jumped.add(jump + w)
                word = flip - flip % w
                if word in arms:
                    self.flipped_bits[word] = self.flipped_bits.get(word, 0) | 1 << (flip - word)
                address += 2 * w

    def _scan_wflip(
        self, stream: OpStream, next_slot: Callable[[], int], labels: Dict[str, int], guard: OffsetGuard
    ) -> None:
        """Read a `wflip word, value, return_address` op from `next_slot`, and note it."""
        word = stream.read_value(next_slot, labels)
        value = next_slot()
        value_kind = VALUE_NUMBER
        if value < 0:
            value_kind = stream.value_kind(~value)
            if value_kind == VALUE_ADDRESS and word in self.arms:
                table_bits = guard.alignment(stream.label_address(~value, labels))
                if table_bits:
                    self.arms[word].append(table_bits)
                else:
                    self.not_tables.add(word)
            value = stream.deferred_value(~value, next_slot, labels)
        return_address = stream.read_value(next_slot, labels)
        next_slot()  # the position
        self._wflip(word, value, value_kind, return_address)

    def _wflip(self, word: int, value: int, value_kind: int, return_address: int) -> None:
        """Note `wflip word, value, return_address`."""
        w, arms = self.memory_width, self.arms
        if return_address + w in arms and (value_kind != VALUE_ADDRESS or word != return_address + w):
            self.misarmed.add(return_address + w)
        if word % w:
            first = word - word % w
            self.split.update(touched for touched in (first, first + w) if touched in arms)
        elif word in arms and value_kind != VALUE_ADDRESS:
            into = self.numbers if value_kind == VALUE_NUMBER else self.computed
            into[word] = into.get(word, 0) | value

    def why_unpin(self, word: int, base: int) -> Optional[str]:
        """Return why `word`, pinned at `base`, can't stay pinned, or None if its pin holds (see unpin)."""
        if word in self.split:
            return 'a wflip across two words'
        if word not in self.own_op:
            return 'no op of its own'
        if word in self.not_tables:
            return 'armed with an address that is no table'
        # The word's window: its low bits, below its base's alignment and below its widest armed table.
        top = (base & -base).bit_length() - 1 if base else self.memory_width
        if self.arms[word]:
            top = min(top, max(self.arms[word]))
        above = ~((1 << top) - 1)
        if self.flipped_bits.get(word, 0) & above:
            return 'a bit flip above its window'
        if self.numbers.get(word, 0) & above:
            return 'armed with a number'
        if self.computed.get(word, 0) & above:
            return 'armed with a value computed from labels'
        if word in self.misarmed or (word in self.jumped and not self.jumps_follow_an_arm):
            return 'an op jumps through it without arming it'
        return None
