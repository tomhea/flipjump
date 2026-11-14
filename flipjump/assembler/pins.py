"""
The pins of a table pool: which jump words rest at their block's base (see "pin" in inner_classes/table_pool.py).

The pool proposes the words (BlockPool.pinned_words). `pins` resolves them to addresses and keeps only the pins
that can hold: a pinned word holds `value ^ base`, so every use of the word must be one that a pin leaves right
(see unpin).
"""

from typing import AbstractSet, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from flipjump.utils.exceptions import FlipJumpAssemblerException, FlipJumpExprException
from flipjump.utils.progress import clear_progress_line
from flipjump.assembler.inner_classes import op_stream
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.offset_guard import OffsetGuard
from flipjump.assembler.inner_classes.op_stream import OpStream, VALUE_ADDRESS, VALUE_NUMBER
from flipjump.assembler.inner_classes.ops import NewSegment
from flipjump.assembler.inner_classes.table_pool import BlockPool, reserved_below


def pins(
    ops: OpStream, labels: Dict[str, int], memory_width: int, pool: BlockPool, warning_as_errors: bool
) -> Dict[int, int]:
    """Return the words to pin, {jump-word address: block base}: the pool's candidates resolved by `labels`, minus
    pin_all_but's words, words two groups pin with different bases, and pins that can't hold (see unpin)."""
    unpinned_words, missing = pool.unpinned_words(labels)
    if missing:
        pin_all_but_warning(missing, warning_as_errors)
    pinned, _ = resolve_pinned(pool.pinned_words(), labels, memory_width, unpinned_words)
    if pinned:
        unpin(ops, labels, memory_width, pinned, pool)
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
    unpinned_words: AbstractSet[int] = frozenset(),
) -> Tuple[Dict[int, int], int]:
    """Resolve the pinned words' expressions to addresses. Skip a word that two groups pin with different bases
    (like `(x + 32)` and `(x + w)`), and a word of `unpinned_words` (the program reads it raw).
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
        if address in unpinned_words:
            continue  # the program reads this word raw
        if address in pinned and pinned[address] != base:
            conflicts.add(address)
        pinned[address] = base
    for address in conflicts:
        del pinned[address]
    return pinned, len(conflicts)


def unpin(stream: OpStream, labels: Dict[str, int], memory_width: int, pinned: Dict[int, int], pool: BlockPool) -> None:
    """Unpin each word of `pinned` whose pin can't hold, counting why in `pool.unpins`. Unpinning is always safe,
    only slower.
    A pin holds when the word has its own op, and every flip of it is an arm (a label plus a constant, flipped xor
    the base) or stays inside the word's window. So these unpin it: a number or computed value above the window
    (`wflip x+w, 0x4000`, `T1 ^ T2`), an `a;b` flip above it, or a wflip across two words. And when an op that is
    not an arm jumps through the word, no flip may be able to point it into a table below its window
    (tables_armed_below). With the pointer runtime and no pin_all_but, no pin holds."""
    guard = pool.offset_guard
    assert guard is not None  # a pool's preprocessor made it
    if pool.reads_words_raw and pool.pin_all_but is None:
        reasons: Dict[int, Optional[str]] = dict.fromkeys(pinned, 'the pointer runtime')
    else:
        uses = WordUses(pinned, memory_width)
        uses.scan(stream, labels, guard)
        low_tables = sorted(guard.pads.items())
        reasons = {word: uses.why_unpin(word, base, low_tables) for word, base in pinned.items()}
    for word, why in reasons.items():
        if why is not None:
            del pinned[word]
            pool.unpins[why] = pool.unpins.get(why, 0) + 1


class WordUses:
    """What a program does with the words a pool wants to pin, gathered in one pass over its op stream."""

    def __init__(self, words: Iterable[int], memory_width: int):
        self.memory_width = memory_width
        # word -> log2 of the alignment (in bits) of each table it is armed with. Its keys are the words.
        self.arms: Dict[int, List[int]] = {word: [] for word in words}
        self.rests: Dict[int, int] = {}  # word -> its initial value (the jump of its op)
        self.flipped_bits: Dict[int, int] = {}  # word -> the bits that `a;b` ops flip in it
        self.numbers: Dict[int, int] = {}  # word -> the bits that wflips of plain numbers flip in it
        self.computed: Dict[int, int] = {}  # word -> the bits that wflips of computed values (`T1 ^ T2`) flip in it
        self.jumped: Set[int] = set()  # words that an op jumps through without being one of their arms
        self.split: Set[int] = set()  # words that a wflip across two words flips

    def scan(self, stream: OpStream, labels: Dict[str, int], guard: OffsetGuard) -> None:
        """Walk the op stream, noting every use of the words."""
        w, arms = self.memory_width, self.arms
        address = 0
        next_slot = iter(stream.vals).__next__
        value_of, kind_of, refs = stream.deferred_value, stream.value_kind, stream.refs
        FJ_POS, WFLIP, PAD, OBJ, FJ_PACKED = (
            op_stream.FJ_POS,
            op_stream.WFLIP,
            op_stream.PAD,
            op_stream.OBJ,
            op_stream.FJ_PACKED,
        )
        for kind in stream.kinds:
            if kind == WFLIP:
                word = next_slot()
                if word < 0:
                    word = value_of(~word, next_slot, labels)
                value = next_slot()
                value_kind = VALUE_NUMBER
                if value < 0:
                    value_kind = kind_of(~value)
                    if value_kind == VALUE_ADDRESS and word in arms:
                        arms[word].append(guard.alignment(stream.label_address(~value, labels)))
                    value = value_of(~value, next_slot, labels)
                return_address = next_slot()
                if return_address < 0:
                    return_address = value_of(~return_address, next_slot, labels)
                next_slot()  # the position
                self._wflip(word, value, value_kind, return_address)
                address += 2 * w
            elif kind == PAD:
                address += next_slot() * 2 * w
            elif kind == OBJ:
                obj = refs[next_slot()]
                address = obj.start_address if isinstance(obj, NewSegment) else obj.first_address_after_reserved
            else:
                # an `a;b` op. Most ops are these, so it is noted right here, with no function call.
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
                    self.rests[address + w] = jump
                if jump + w in arms:
                    self.jumped.add(jump + w)
                word = flip - flip % w
                if word in arms:
                    self.flipped_bits[word] = self.flipped_bits.get(word, 0) | 1 << (flip - word)
                address += 2 * w

    def _wflip(self, word: int, value: int, value_kind: int, return_address: int) -> None:
        """Note `wflip word, value, return_address`."""
        w, arms = self.memory_width, self.arms
        if return_address + w in arms and (value_kind != VALUE_ADDRESS or word != return_address + w):
            self.jumped.add(return_address + w)
        if word % w:
            first = word - word % w
            self.split.update(touched for touched in (first, first + w) if touched in arms)
        elif word in arms and value_kind != VALUE_ADDRESS:
            into = self.numbers if value_kind == VALUE_NUMBER else self.computed
            into[word] = into.get(word, 0) | value

    def why_unpin(self, word: int, base: int, low_tables: Sequence[Tuple[int, int]]) -> Optional[str]:
        """Return why `word`, pinned at `base`, can't stay pinned, or None if its pin holds.
        `low_tables` is tables_armed_below's `pads`."""
        if word in self.split:
            return 'a wflip across two words'
        if word not in self.rests:
            return 'no op of its own'
        # The word's window: its low bits, below its base's alignment and below its widest armed table. A flip inside
        # the window changes a value or moves within the armed table, the same with or without the pool. Only an arm
        # may flip a bit above it.
        top = (base & -base).bit_length() - 1 if base else self.memory_width
        if self.arms[word]:
            top = min(top, max(self.arms[word]))
        above = ~((1 << top) - 1)
        flipped, numbers, computed = (uses.get(word, 0) for uses in (self.flipped_bits, self.numbers, self.computed))
        if flipped & above:
            return 'a bit flip above its window'
        if numbers & above:
            return 'armed with a number'
        if computed & above:
            return 'armed with a value computed from labels'
        if word in self.jumped and tables_armed_below(low_tables, top, self.rests[word], flipped | numbers | computed):
            return 'a flip below its window could arm a table there'
        return None


def tables_armed_below(pads: Sequence[Tuple[int, int]], top: int, rest: int, flips: int) -> bool:
    """Could a word that starts at `rest`, flipped by some of the bits of `flips`, point into a table of `pads` below
    2^top? (`pads` is [(table address, log2 of its alignment in bits)], ascending.)"""
    for address, bits in pads:
        if address >> top:
            break
        above = ~((1 << bits) - 1)  # the bits above the table's alignment (all its entries share them)
        if address & flips & above and not address & ~(rest | flips) and not rest & ~flips & above & ~address:
            return True
    return False
