"""
the core assembly pipeline.
orchestrates the three stages that turn .fj source into a .fjm binary:
parsing the macro-tree, resolving macros into a flat op-list (the preprocessor),
and resolving labels into addresses while writing the result with the fjm Writer.
"""

import dataclasses
import gc
import itertools
from array import array
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Deque, Iterable, List, Dict, MutableSequence, Sequence, Set, Tuple, Optional, Union

from flipjump.fjm.fjm_writer import Writer, new_word_buffer
from flipjump.utils.constants import WFLIP_LABEL_PREFIX, DEFAULT_MAX_MACRO_RECURSION_DEPTH
from flipjump.utils.functions import save_debugging_labels
from flipjump.utils.progress import ProgressBar
from flipjump.assembler.fj_parser import parse_macro_tree
from flipjump.utils.exceptions import (
    FlipJumpAssemblerException,
    FlipJumpException,
    FlipJumpExprException,
    FlipJumpWriteFjmException,
)
from flipjump.assembler.inner_classes.ops import (
    FlipJump,
    WordFlip,
    LastPhaseOp,
    Macro,
    MacroName,
    NewSegment,
    ReserveBits,
    Padding,
)
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes import op_stream
from flipjump.assembler.inner_classes.op_stream import OpStream
from flipjump.assembler.preprocessor import resolve_macros
from flipjump.assembler.table_pool import BlockPool, OffsetGuard, TablePool, RESERVED_BELOW, reached_the_pool


def assert_address_in_memory(memory_width: int, address: int) -> None:
    if address < 0 or address >= (1 << memory_width):
        raise FlipJumpAssemblerException(f"Not enough space with the {memory_width}-bits memory-width.")


def validate_addresses(memory_width: int, first_address: int, last_address: int) -> None:
    if first_address % memory_width != 0 or last_address % memory_width != 0:
        raise FlipJumpAssemblerException(
            f'segment boundaries are unaligned: ' f'[{hex(first_address)}, {hex(last_address - 1)}].'
        )

    assert_address_in_memory(memory_width, first_address)
    assert_address_in_memory(memory_width, last_address - 1)


def add_segment_to_fjm(
    memory_width: int,
    fjm_writer: Writer,
    first_address: int,
    last_address: int,
    fj_words: MutableSequence[int],
    wflip_words: MutableSequence[int],
    *,
    take: bool = False,
) -> None:
    """
    The new segment will be placed in [first_address, last_address),
    And will include the next data: fj_words + wflip_words.
    @param take: the caller hands fj_words over (the writer may keep it as its data rather than copy it)
    and will use new buffers from now on; neither list is cleared then.
    """
    validate_addresses(memory_width, first_address, last_address)
    if first_address == last_address:
        return

    # two appends, not `fj_words + wflip_words` (a copy of every word): they land contiguously
    data_length = len(fj_words) + len(wflip_words)  # (before add_data: a writer that takes fj_words grows it)
    data_start = fjm_writer.add_data(fj_words, take=take)
    fjm_writer.add_data(wflip_words)

    segment_start_address = first_address // memory_width
    segment_length = (last_address - first_address) // memory_width

    try:
        fjm_writer.add_segment(segment_start_address, segment_length, data_start, data_length)
    except FlipJumpWriteFjmException as e:
        exception_message = (
            f"failed to add the segment: "
            f"{fjm_writer.get_segment_addresses_repr(segment_start_address, segment_length)}."
        )
        raise FlipJumpAssemblerException(exception_message) from e

    if take:
        return
    # `del x[:]` empties both an array (which has no clear()) and a list
    del fj_words[:]
    del wflip_words[:]


@dataclasses.dataclass(slots=True)
class WFlipSpot:
    list: MutableSequence[int]
    index: int
    address: int


class BinaryData:
    __slots__ = (
        'memory_width',
        'first_address',
        'next_wflip_address',
        'labels',
        'wflips_so_far',
        'save_wflip_labels',
        'fj_words',
        'wflip_words',
        'padding_ops_indices',
        'wflips_dict',
        'pinned',
        'baked',
        'pool_start',
        'wflips_at',
    )

    def __init__(
        self,
        memory_width: int,
        first_segment: NewSegment,
        labels: Dict[str, int],
        *,
        save_wflip_labels: bool = True,
        pinned: Optional[Dict[int, int]] = None,
        pool_start: Optional[int] = None,
        wflips_at: Optional[int] = None,
    ):
        """
        @param save_wflip_labels: whether to record a `:wflips:N` label per generated wflip-chain op
        @param pinned: {jump-word address: block base} of BlockPool's pinned words
        @param pool_start: where a table pool starts: a segment below it may not reach it
        @param wflips_at: if given, every segment's wflip area is laid out from here, one after the other, rather
        than right after its code (a pool based right above the program: TablePool.wflips_at)
        """
        self.memory_width = memory_width

        # {jump-word address: block base} for BlockPool; None for every other build, so the use site
        # below costs one truthiness test per op when off
        self.pinned = pinned
        self.baked: Set[int] = set()  # the pinned words whose op got its base (see unbaked_pins)
        self.pool_start = pool_start

        self.first_address = first_segment.start_address
        self.wflips_at = wflips_at
        self.next_wflip_address = first_segment.wflip_start_address if wflips_at is None else wflips_at

        self.labels = labels
        self.wflips_so_far = 0
        self.save_wflip_labels = save_wflip_labels

        # typed word arrays (a plain list where no typecode fits the width): they hold every word
        self.fj_words = new_word_buffer(memory_width)
        self.wflip_words = new_word_buffer(memory_width)

        # indices in self.fj_words. MEMORY: a typed array (8 bytes an index), not a list of int objects
        # (40): a program's alignment padding can be millions of ops waiting for a wflip op to fill them
        self.padding_ops_indices = array('q')

        # return_address -> { a chain tail (its addresses, as one int: see insert_wflip_ops) -> start_flip_address }
        self.wflips_dict: Dict[int, Dict[int, int]] = defaultdict(dict)

    def get_wflip_spot(self) -> WFlipSpot:
        if self.padding_ops_indices:
            index = self.padding_ops_indices.pop()
            return WFlipSpot(self.fj_words, index, self.first_address + self.memory_width * index)

        if self.next_wflip_address + 2 * self.memory_width > 1 << self.memory_width:
            raise FlipJumpAssemblerException(
                f'Not enough space with the {self.memory_width}-bits memory-width: the wflip area of the segment at '
                f'{hex(self.first_address)} reaches {hex(self.next_wflip_address + 2 * self.memory_width)}.'
            )
        wflip_spot = WFlipSpot(self.wflip_words, len(self.wflip_words), self.next_wflip_address)
        self.wflip_words.extend((0, 0))
        self.next_wflip_address += 2 * self.memory_width
        return wflip_spot

    def close_and_add_segment(self, fjm_writer: Writer) -> None:
        if self.wflips_at is not None:  # the code here, and its wflip area after the pool
            code_end = self.first_address + len(self.fj_words) * self.memory_width
            if self.pool_start is not None and self.first_address < self.pool_start < code_end:
                raise reached_the_pool(code_end, self.pool_start)
            fj_words, wflip_words = self.fj_words, self.wflip_words
            add_segment_to_fjm(self.memory_width, fjm_writer, self.first_address, code_end, fj_words, [], take=True)
            add_segment_to_fjm(self.memory_width, fjm_writer, self.wflips_at, self.next_wflip_address, wflip_words, [])
            self.wflips_at = self.next_wflip_address
            self._new_buffers()
            return
        if self.next_wflip_address == self.first_address:
            return
        if self.pool_start is not None and self.first_address < self.pool_start < self.next_wflip_address:
            raise FlipJumpAssemblerException(
                f'table-placement: the segment at {hex(self.first_address)} and its wflip area reach '
                f'{hex(self.next_wflip_address)}, inside the table pool at {hex(self.pool_start)}. Raise pool_base.'
            )

        add_segment_to_fjm(
            self.memory_width,
            fjm_writer,
            self.first_address,
            self.next_wflip_address,
            self.fj_words,
            self.wflip_words,
            take=True,
        )
        self._new_buffers()

    def _new_buffers(self) -> None:
        """fresh word buffers: the writer took the old ones (MEMORY: it keeps the first segment's words as
        its data instead of copying them - see Writer.add_data)"""
        self.fj_words = new_word_buffer(self.memory_width)
        self.wflip_words = new_word_buffer(self.memory_width)

    def _insert_wflip_label(self, address: int) -> None:
        """the `:wflips:N` labels are read only by the debugging file (fj source cannot name them)"""
        if self.save_wflip_labels:
            self.labels[f'{WFLIP_LABEL_PREFIX}{self.wflips_so_far}'] = address
            self.wflips_so_far += 1

    def insert_fj_op(self, flip: int, jump: int) -> None:
        if self.pinned:
            # A pinned hex variable RESTS at `base + value` rather than at `value`, so the block's
            # base is baked into its declaration here. Only the variable's own op can match: a
            # wflip-chain op's jump word is at its own address, never at a pinned one.
            jump_word = self.first_address + (len(self.fj_words) + 1) * self.memory_width
            base = self.pinned.get(jump_word)
            if base:
                jump ^= base
                self.baked.add(jump_word)
        # `+=` on an array only accepts another array; extend() takes any iterable of ints.
        self.fj_words.extend((flip, jump))

    def pinned_value(self, word_address: int, flip_value: int, value_kind: int) -> int:
        """the value a wflip flips into `word_address`, which may be a pinned word: it rests at `base + digit`,
        so a writer installing an address V (a label, or a label plus a constant) flips `V ^ base` - for a
        table's own arm just its index in the block, and the same rule for every other dispatcher through the
        word (stl.comp_if1, hex.shifts.*, hex.tables.*) - while any other value is flipped as it is (unpin left
        pinned only the words whose other values touch just their window)"""
        base = self.pinned.get(word_address) if self.pinned else None
        if not base or value_kind != VALUE_ADDRESS:
            return flip_value
        return flip_value ^ base

    def unbaked_pins(self) -> List[int]:
        """the pinned words no op was emitted for (a `reserve`d word): they rest at 0, not at their base"""
        return sorted(set(self.pinned or ()) - self.baked)

    def insert_wflip_ops(self, word_address: int, flip_value: int, return_address: int) -> None:
        if 0 == flip_value:
            self.insert_fj_op(0, return_address)
        else:
            assert_address_in_memory(self.memory_width, flip_value)

            return_dict = self.wflips_dict[return_address]

            # The chain flips the word's set bits from the LOWEST address up (this is the order
            # (tested with many other orders) that produces the best found-statistic for searching a
            # chain's tail in return_dict), and every tail is shared through return_dict: a tail is
            # the SET of addresses still to flip.
            # A tail {a + i : bit i of m} (a its lowest address, bit 0 of m set, m < 2**w) is keyed by
            # one int, `a << w | m`, which is equal for two tails exactly when their address sets are.
            width = self.memory_width
            remaining = flip_value

            # insert the first op
            lowest = (remaining & -remaining).bit_length() - 1
            remaining &= remaining - 1
            self.insert_fj_op(word_address + lowest, 0)
            last_return_address_index: Tuple[MutableSequence[int], int] = self.fj_words, len(self.fj_words) - 1

            while remaining:
                lowest = (remaining & -remaining).bit_length() - 1
                flips_key = (word_address + lowest) << width | remaining >> lowest
                ops_list, last_address_index = last_return_address_index

                if flips_key in return_dict:
                    # connect the last op to the already created wflip-chain
                    ops_list[last_address_index] = return_dict[flips_key]
                    return
                else:
                    # insert a new wflip op, and connect the last one to it
                    wflip_spot = self.get_wflip_spot()
                    self._insert_wflip_label(wflip_spot.address)

                    ops_list[last_address_index] = wflip_spot.address
                    return_dict[flips_key] = wflip_spot.address

                    wflip_spot.list[wflip_spot.index] = word_address + lowest
                    remaining &= remaining - 1
                    last_return_address_index = wflip_spot.list, wflip_spot.index + 1

            ops_list, last_address_index = last_return_address_index
            ops_list[last_address_index] = return_address

    def insert_padding(self, ops_count: int) -> None:
        for i in range(len(self.fj_words), len(self.fj_words) + 2 * ops_count, 2):
            self.padding_ops_indices.append(i)
            self.fj_words.extend((0, 0))

    def insert_new_segment(self, fjm_writer: Writer, first_address: int, wflip_first_address: int) -> None:
        self.close_and_add_segment(fjm_writer)

        self.first_address = first_address
        self.next_wflip_address = wflip_first_address if self.wflips_at is None else self.wflips_at

        del self.padding_ops_indices[:]  # (an array has no clear())

    def insert_reserve_bits(self, fjm_writer: Writer, new_first_address: int) -> None:
        add_segment_to_fjm(
            self.memory_width, fjm_writer, self.first_address, new_first_address, self.fj_words, [], take=True
        )
        # only the ops' buffer was handed over: the wflip ops gathered so far belong to the segment's
        # wflip area, after the reserved region, and are written when the segment closes
        self.fj_words = new_word_buffer(self.memory_width)

        self.first_address = new_first_address

        del self.padding_ops_indices[:]


def resolve_pinned(
    pinned_exprs: Dict[Expr, int],
    labels: Dict[str, int],
    reserved_below: int = RESERVED_BELOW,
    exclude: Optional[Callable[[int, Dict[str, int]], bool]] = None,
) -> Tuple[Dict[int, int], int]:
    """({jump-word address: block base}, number of aliased addresses dropped).

    BlockPool keys groups by the source word's EXPRESSION, because its address is unknown while
    macros expand, and two different expressions can resolve to the SAME address (`(x + 32)` and
    `(x + w)` at w=32) with different block bases. Pinning that address to either base sends every
    table of the other block to `switch ^ wrong_base`, so a collision drops the pin instead: an
    un-pinned word rests at `digit`, the full address is written, and both its blocked and inline
    tables still work.
    `exclude(address, labels) -> bool` lets the caller veto a pin on a word it owns (a pinned word
    holds `base + value`, which anything reading it raw no longer recognises as the value).
    """
    pinned: Dict[int, int] = {}
    conflicts = set()
    for expr, base in pinned_exprs.items():
        try:
            address = expr.exact_eval(labels)
        except FlipJumpExprException:
            continue  # an unresolvable word simply is not pinned
        if address < reserved_below:
            continue  # the runtime's word (stl.IO), not the program's
        if exclude is not None and exclude(address, labels):
            continue  # the caller owns this word
        if address in pinned and pinned[address] != base:
            conflicts.add(address)
        pinned[address] = base
    for address in conflicts:
        del pinned[address]
    return pinned, len(conflicts)


# labels_resolve reports its progress once per this many ops
_PROGRESS_CHUNK = 1 << 16


def window_top(base: int, alignments: Iterable[int]) -> int:
    """the lowest bit of a pinned word (resting at `base`, armed with tables aligned to `alignments` bits) that holds
    an address: a flip below it is a value, or a move inside the table the word is armed with - the same with the
    pool as without (the base, and the widest table it is armed with, is aligned past it; a flip of an armed word is
    assumed to stay inside the table armed). A flip at or above it changes an address, which only an arm may, xored
    with the base."""
    top = (base & -base).bit_length() - 1 if base else 1 << 30
    alignments = list(alignments)
    return min(top, max(alignments)) if alignments else top


def unpin(
    stream: OpStream,
    labels: Dict[str, int],
    memory_width: int,
    pinned: Dict[int, int],
    reasons: Dict[str, int],
    table_alignment: Callable[[int], int] = lambda address: 0,
    *,
    raw: bool = False,
    pads: Optional[Dict[int, int]] = None,
) -> None:
    """Un-pin every word of `pinned` ({jump-word address: block base}) whose pin could not hold, counting each in
    `reasons` by why. An un-pinned word rests at its value and is armed with the whole address - always right, only
    slower. A pin holds when the word has an op of its own (to rest at base + value) and every flip of it either
    arms it - a wflip by a label plus a constant, flipped xor the base - or stays below its window_top: not a
    number or a value computed from labels above it (`wflip x+w, 0x4000`, `T1 ^ T2`), no `a;b` flip above it (arming
    bit by bit), no wflip across two words; and a flip below it (a number is a value there, as `hex.set`'s are) may
    not arm the word either: it is not pinned while an op that is no arm of it jumps to its op (through it) and a
    table below its window - an inline address a `pad` aligned or a segment starts at (`pads`: {address: the bits
    the pad aligned it to}) - is a value its flips and its rest value can make (tables_armed_below). (A pinned word
    is assumed never to be jumped through unarmed but by such an op, nor armed with a number to an op no pad or
    segment aligned.) `table_alignment(address)`: the
    bits the table armed at `address` is aligned to (a moved table: its size; an inline one: its `pad`, else 0).
    `raw`: the program reads or writes words through the pointer runtime, and no pin_exclude says which - no pin
    can hold."""

    def drop(word: int, reason: str) -> None:
        if pinned.pop(word, None) is not None:
            reasons[reason] = reasons.get(reason, 0) + 1

    if raw:
        for word in list(pinned):
            drop(word, 'the pointer runtime')
        return
    w = memory_width
    arms: Dict[int, List[int]] = {word: [] for word in pinned}  # each word's arms' alignments
    flipped_bits: Dict[int, int] = {}  # word -> the bits `a;b` ops flip
    numbers: Dict[int, int] = {}  # word -> the bits numbers flip into it
    computed: Dict[int, int] = {}  # word -> the bits values computed from labels flip into it
    with_op: Set[int] = set()
    rests: Dict[int, int] = {}  # word -> the value its op rests at (its jump)
    jumped: Set[int] = set()  # the words whose op an op jumps to that is no arm of the word (jumping through it)
    address = 0
    slots = iter(stream.vals)
    next_slot = slots.__next__
    FJ_POS, WFLIP, PAD, FJ_PACKED = op_stream.FJ_POS, op_stream.WFLIP, op_stream.PAD, op_stream.FJ_PACKED
    for kind in stream.kinds:
        if kind == WFLIP:
            word = next_slot()
            if word < 0:
                word = _deferred_value(stream, ~word, next_slot, labels, None)
            value = next_slot()
            kind_of_value = VALUE_NUMBER
            if value < 0:
                kind_of_value = _deferred_value_kind(stream, ~value)
                if kind_of_value == VALUE_ADDRESS and word in arms:
                    arms[word].append(table_alignment(_label_address(stream, ~value, labels)))
                value = _deferred_value(stream, ~value, next_slot, labels, None)
            return_address = next_slot()
            if return_address < 0:
                return_address = _deferred_value(stream, ~return_address, next_slot, labels, None)
            next_slot()  # the position
            if return_address + w in arms and (kind_of_value != VALUE_ADDRESS or word != return_address + w):
                jumped.add(return_address + w)
            if word % w:
                for touched in (word - word % w, word - word % w + w):
                    if touched in arms:
                        drop(touched, 'a wflip across two words')
            elif word in arms and kind_of_value != VALUE_ADDRESS:
                into = numbers if kind_of_value == VALUE_NUMBER else computed
                into[word] = into.get(word, 0) | value
            address += 2 * w
        elif kind == PAD:
            address += next_slot() * 2 * w
        elif kind == op_stream.OBJ:
            obj = stream.refs[next_slot()]
            address = obj.start_address if isinstance(obj, NewSegment) else obj.first_address_after_reserved
        else:
            if kind == FJ_PACKED:
                packed = next_slot()
                flip, jump = packed >> 32, packed & op_stream.PACKED_JUMP_MASK
            else:
                flip = next_slot()
                if flip < 0:
                    flip = _deferred_value(stream, ~flip, next_slot, labels, None)
                jump = next_slot()
                if jump < 0:
                    jump = _deferred_value(stream, ~jump, next_slot, labels, None)
                if kind == FJ_POS:
                    next_slot()  # the position
            if address + w in arms:
                with_op.add(address + w)
                rests[address + w] = jump
            if jump + w in arms:
                jumped.add(jump + w)
            word = flip - flip % w
            if word in arms:
                flipped_bits[word] = flipped_bits.get(word, 0) | 1 << (flip - word)
            address += 2 * w
    low = sorted((pads or {}).items())
    for word, base in list(pinned.items()):
        top = window_top(base, arms[word])
        above = ~((1 << top) - 1)
        if word not in with_op:
            drop(word, 'no op of its own')
        elif flipped_bits.get(word, 0) & above:
            drop(word, 'a bit flip above its window')
        elif numbers.get(word, 0) & above:
            drop(word, 'armed with a number')
        elif computed.get(word, 0) & above:
            drop(word, 'armed with a value computed from labels')
        elif word in jumped and tables_armed_below(
            low, top, rests.get(word, 0), flipped_bits.get(word, 0) | numbers.get(word, 0) | computed.get(word, 0)
        ):
            drop(word, 'a flip below its window could arm a table there')


def tables_armed_below(pads: Sequence[Tuple[int, int]], top: int, rest: int, flips: int) -> bool:
    """could a word resting at `rest`, flipped by the bits of `flips` (not by an arm), hold an address in a table of
    `pads` ([(address, the bits a pad aligned it to)], ascending) below 2^top, with a flip's help? Every value it holds
    has the bits of `rest` that no flip toggles, and no bit outside `rest | flips`."""
    for address, bits in pads:
        if address >> top:
            break
        above = ~((1 << bits) - 1)  # a table's address, above the bits its entries differ in
        if address & flips & above and not address & ~(rest | flips) and not rest & ~flips & above & ~address:
            return True
    return False


def _label_address(stream: OpStream, encoded: int, labels: Dict[str, int]) -> int:
    """the address of the label of a deferred `label + constant` value (its slot is ~encoded)"""
    ref = stream.refs[encoded >> 2]
    if encoded & 3 == op_stream.TAG_EXPR:
        ref = op_stream.linear_sum(ref)[0]  # type: ignore[index]
    return labels[ref]


# what a wflip's value is (see BinaryData.pinned_value): a number, one address (a label, plus a constant), or
# a value computed from labels
VALUE_NUMBER, VALUE_ADDRESS, VALUE_COMPUTED = 0, 1, 2


def value_kind(value: Expr) -> int:
    """the kind of a wflip's value expression (VALUE_*)"""
    if not value.all_unknown_labels():
        return VALUE_NUMBER
    linear = op_stream.linear_sum(value)
    return VALUE_ADDRESS if linear is not None and linear[0] is not None else VALUE_COMPUTED


def _deferred_value_kind(stream: OpStream, encoded: int) -> int:
    """the kind of a deferred wflip value (its slot is ~encoded)"""
    tag = encoded & 3
    if tag != op_stream.TAG_EXPR:
        return VALUE_ADDRESS
    return value_kind(stream.refs[encoded >> 2])


def _deferred_value(
    stream: OpStream,
    encoded: int,
    next_slot: Callable[[], int],
    labels: Dict[str, int],
    guard: Optional[OffsetGuard],
    is_address: bool = True,
) -> int:
    """the value of a deferred operand (its slot is ~encoded; see OpStream). Raises what the Expr would, and
    what a table pool's OffsetGuard does (`is_address` unless it is a wflip's value: see check_expr)."""
    ref = stream.refs[encoded >> 2]
    tag = encoded & 3
    if tag == op_stream.TAG_LABEL:
        value = labels.get(ref)
        if value is None:
            return Expr(ref).exact_eval(labels)  # raises the Expr's own "Can't evaluate label"
        if guard is not None and ref in guard.stranded:
            guard.check(ref, value, 0)
        return value
    if tag == op_stream.TAG_LABEL_PLUS:
        const = next_slot()
        value = labels.get(ref)
        if value is None:
            return Expr(ref).exact_eval(labels) + const  # raises, as above
        if guard is not None:
            guard.check(ref, value, const)
        return value + const
    value = ref.exact_eval(labels)
    if guard is not None:
        guard.check_expr(ref, value, is_address)
    return value  # type: ignore[no-any-return]


def _labels_resolve_stream(
    stream: OpStream,
    binary_data: BinaryData,
    labels: Dict[str, int],
    fjm_writer: Writer,
    on_progress: Optional[Callable[[float], None]],
    guard: Optional[OffsetGuard],
) -> None:
    """labels_resolve over a compact OpStream (the preprocessor's output): the same BinaryData calls,
    in the same order, with the same values, as the op objects it replaces would make."""
    insert_fj_op = binary_data.insert_fj_op
    insert_wflip_ops = binary_data.insert_wflip_ops
    pinned = binary_data.pinned
    kinds = stream.kinds
    refs = stream.refs
    slots = iter(stream.vals)
    next_slot = slots.__next__
    next(slots)  # the first op (the first NewSegment) was taken by the caller
    total_ops = max(1, len(kinds))
    op_index = 1
    FJ, FJ_POS, WFLIP, PAD = op_stream.FJ, op_stream.FJ_POS, op_stream.WFLIP, op_stream.PAD
    FJ_PACKED, PACKED_JUMP_MASK = op_stream.FJ_PACKED, op_stream.PACKED_JUMP_MASK
    while op_index < len(kinds):
        chunk_end = min(op_index + _PROGRESS_CHUNK, len(kinds))
        for kind in kinds[op_index:chunk_end]:
            if kind == FJ_PACKED:
                packed = next_slot()
                insert_fj_op(packed >> 32, packed & PACKED_JUMP_MASK)
            elif kind == FJ:
                insert_fj_op(next_slot(), next_slot())
            elif kind == FJ_POS:
                try:
                    flip = next_slot()
                    if flip < 0:
                        flip = _deferred_value(stream, ~flip, next_slot, labels, guard)
                    jump = next_slot()
                    if jump < 0:
                        jump = _deferred_value(stream, ~jump, next_slot, labels, guard)
                    next_slot()  # the position
                    insert_fj_op(flip, jump)
                except FlipJumpException as e:
                    raise FlipJumpAssemblerException(f"{e} in op {stream.op_at(op_index)}.")
            elif kind == WFLIP:
                try:
                    word_address = next_slot()
                    if word_address < 0:
                        word_address = _deferred_value(stream, ~word_address, next_slot, labels, guard)
                    flip_value = next_slot()
                    kind_of_value = VALUE_NUMBER
                    if flip_value < 0:
                        if pinned:
                            kind_of_value = _deferred_value_kind(stream, ~flip_value)
                        flip_value = _deferred_value(stream, ~flip_value, next_slot, labels, guard, False)
                    return_address = next_slot()
                    if return_address < 0:
                        return_address = _deferred_value(stream, ~return_address, next_slot, labels, guard)
                    next_slot()  # the position
                    if pinned:
                        flip_value = binary_data.pinned_value(word_address, flip_value, kind_of_value)
                    insert_wflip_ops(word_address, flip_value, return_address)
                except FlipJumpException as e:
                    raise FlipJumpAssemblerException(f"{e} in op {stream.op_at(op_index)}.")
            elif kind == PAD:
                binary_data.insert_padding(next_slot())
            else:
                op = refs[next_slot()]
                if isinstance(op, NewSegment):
                    binary_data.insert_new_segment(fjm_writer, op.start_address, op.wflip_start_address)
                elif isinstance(op, ReserveBits):
                    binary_data.insert_reserve_bits(fjm_writer, op.first_address_after_reserved)
                else:
                    raise FlipJumpAssemblerException(f"Can't resolve/assemble the next opcode - {str(op)}")
            op_index += 1
        if on_progress is not None:
            on_progress(1 - (len(kinds) - op_index) / total_ops)


def labels_resolve(
    ops: Union[OpStream, Deque[LastPhaseOp]],
    labels: Dict[str, int],
    memory_width: int,
    fjm_writer: Writer,
    *,
    save_wflip_labels: bool = True,
    pinned: Optional[Dict[int, int]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    pool_start: Optional[int] = None,
    offset_guard: Optional[OffsetGuard] = None,
    wflips_at: Optional[int] = None,
) -> None:
    """
    resolve the labels and expressions to get the list of fj ops, and add all the data and segments into the fjm_writer.
    @param ops:[in]: the list ops returned from the preprocessor stage
    @param labels:[in]: dictionary from label to its resolved value
    @param memory_width: the memory-width
    @param fjm_writer: [out]: the .fjm file writer
    @param save_wflip_labels: record a `:wflips:N` debugging label per generated wflip-chain op
    @param on_progress: if given, called every _PROGRESS_CHUNK ops with the fraction of ops resolved (OpStream only)
    @param pinned: {jump-word address: block base} of the words a BlockPool pinned (see BinaryData.pinned_value)
    @param pool_start: where a table pool starts, if one placed tables: no other segment may reach it
    @param offset_guard: the table pool's, from the preprocessor: it checks the operands that waited for a label
    @param wflips_at: if given, where the wflip areas go (see BinaryData)
    """
    if isinstance(ops, OpStream):
        first_object = ops.first_object()
        if not isinstance(first_object, NewSegment):
            first_op = ops.op_at(0) if ops else None
            raise FlipJumpAssemblerException(f"The first op must be of type NewSegment (and not {first_op}).")
        binary_data = BinaryData(
            memory_width,
            first_object,
            labels,
            save_wflip_labels=save_wflip_labels,
            pinned=pinned,
            pool_start=pool_start,
            wflips_at=wflips_at,
        )
        _guard_where(offset_guard, binary_data)
        _labels_resolve_stream(ops, binary_data, labels, fjm_writer, on_progress, offset_guard)
        binary_data.close_and_add_segment(fjm_writer)
        _refuse_unbaked_pins(binary_data)
        return

    first_segment = ops.popleft()
    if not isinstance(first_segment, NewSegment):
        raise FlipJumpAssemblerException(f"The first op must be of type NewSegment (and not {first_segment}).")

    binary_data = BinaryData(
        memory_width,
        first_segment,
        labels,
        save_wflip_labels=save_wflip_labels,
        pinned=pinned,
        pool_start=pool_start,
        wflips_at=wflips_at,
    )
    _guard_where(offset_guard, binary_data)

    for op in ops:
        if isinstance(op, FlipJump):
            try:
                flip, jump = op.get_flip(labels), op.get_jump(labels)
                if offset_guard is not None:
                    offset_guard.check_expr(op.flip, flip)
                    offset_guard.check_expr(op.jump, jump)
                binary_data.insert_fj_op(flip, jump)
            except FlipJumpException as e:
                raise FlipJumpAssemblerException(f"{e} in op {op}.")

        elif isinstance(op, WordFlip):
            try:
                word_address, flip_value = op.get_word_address(labels), op.get_flip_value(labels)
                if offset_guard is not None:
                    offset_guard.check_expr(op.word_address, word_address)
                    offset_guard.check_expr(op.flip_value, flip_value, False)
                    offset_guard.check_expr(op.return_address, op.get_return_address(labels))
                if pinned:
                    flip_value = binary_data.pinned_value(word_address, flip_value, value_kind(op.flip_value))
                binary_data.insert_wflip_ops(word_address, flip_value, op.get_return_address(labels))
            except FlipJumpException as e:
                raise FlipJumpAssemblerException(f"{e} in op {op}.")

        elif isinstance(op, Padding):
            binary_data.insert_padding(op.ops_count)

        elif isinstance(op, NewSegment):
            binary_data.insert_new_segment(fjm_writer, op.start_address, op.wflip_start_address)

        elif isinstance(op, ReserveBits):
            binary_data.insert_reserve_bits(fjm_writer, op.first_address_after_reserved)

        else:
            raise FlipJumpAssemblerException(f"Can't resolve/assemble the next opcode - {str(op)}")

    binary_data.close_and_add_segment(fjm_writer)
    _refuse_unbaked_pins(binary_data)


def _guard_where(guard: Optional[OffsetGuard], binary_data: BinaryData) -> None:
    """an OffsetGuard refusal in labels_resolve says where the op is: its address, and the label nearest it"""
    if guard is not None:

        def where() -> str:
            address = binary_data.first_address + len(binary_data.fj_words) * binary_data.memory_width
            return f' of the op at {hex(address)} (after the label {guard.label_below(address)})'

        guard.where = where
        guard.emitting = None  # every table moved: an operand that reaches over one is refused


def _refuse_unbaked_pins(binary_data: BinaryData) -> None:
    """a pinned word with no op of its own cannot rest at its base: assemble() un-pins it first (see unpin), so this
    guards the callers that pin without it"""
    unbaked = binary_data.unbaked_pins()
    if unbaked:
        raise FlipJumpAssemblerException(
            f'table-placement: {len(unbaked)} pinned word(s) are no op (a `reserve`d variable?), first at '
            f'{hex(unbaked[0])}: such a word rests at 0, not at its block base, so its arms miss. Un-pin it (unpin).'
        )


def resolve_macros_with_pool(
    memory_width: int, macros: Dict[MacroName, Macro], table_pool: Optional[TablePool], **kwargs: Any
) -> Tuple[OpStream, Dict[str, int]]:
    """resolve_macros(memory_width, macros, table_pool=table_pool, **kwargs) - and again while a pool based right
    above the program replans it (BlockPool.replan: tables it placed past the flat maximum stay inline; and every
    table, where the wflip areas after the pool might end past the maximum or the memory: _wflip_ops_bound), which
    keeps the macro tree (consume_main is then off)"""
    replanning = table_pool if isinstance(table_pool, BlockPool) and table_pool.auto_base else None
    if replanning is not None:
        kwargs['consume_main'] = False
    why_inline = ''
    for attempt in itertools.count():
        ops, labels = resolve_macros(memory_width, macros, table_pool=table_pool, **kwargs)
        if replanning is None:
            return ops, labels
        if replanning.replan(attempt):
            continue
        if why_inline:
            replanning.base_reason = why_inline
            return ops, labels
        wflips_at = replanning.wflips_at()
        try:
            wflip_ops = 0 if wflips_at is None else _wflip_ops_bound(ops, labels, replanning)
        except FlipJumpException:
            return ops, labels  # labels_resolve says what is wrong, and where
        if wflips_at is None or wflips_at + wflip_ops * 2 * memory_width <= min(
            1 << memory_width, replanning.flat_limit or 1 << memory_width
        ):
            return ops, labels
        # the wflip areas, after the pool, might not end below the flat maximum (nor in the memory): no table moves
        why_inline = (
            f'none: the wflip areas after the pool at {hex(replanning.pool_base)} could take {wflip_ops} ops, past '
            f'{hex(min(1 << memory_width, replanning.flat_limit or 1 << memory_width))}: every table stays inline'
        )
        replanning.replan(attempt, every=True)
    raise AssertionError('unreachable')


def _wflip_ops_bound(stream: OpStream, labels: Dict[str, int], table_pool: TablePool) -> int:
    """at most how many ops the wflip areas take: each wflip's chain but its first op, into the padding emitted before
    it while its segment has some left (as BinaryData.get_wflip_spot takes it), no tail shared - a word the pool may
    pin flipped by an address as it is, or xor its block base, whichever has more bits"""
    pins, _ = resolve_pinned(table_pool.pinned_words(), labels, exclude=table_pool.pin_exclude)
    area = padding = 0
    slots = iter(stream.vals)
    next_slot = slots.__next__
    for kind in stream.kinds:
        if kind == op_stream.WFLIP:
            word = next_slot()
            if word < 0:
                word = _deferred_value(stream, ~word, next_slot, labels, None)
            value = next_slot()
            kind_of_value = VALUE_NUMBER
            if value < 0:
                kind_of_value = _deferred_value_kind(stream, ~value)
                value = _deferred_value(stream, ~value, next_slot, labels, None)
            return_address = next_slot()
            if return_address < 0:
                _deferred_value(stream, ~return_address, next_slot, labels, None)
            next_slot()  # the position
            bits = bin(value).count('1')
            if kind_of_value == VALUE_ADDRESS and word in pins:
                bits = max(bits, bin(value ^ pins[word]).count('1'))
            chain = max(bits - 1, 0)
            taken = min(chain, padding)
            padding -= taken
            area += chain - taken
        elif kind in (op_stream.FJ, op_stream.FJ_POS):
            for _ in range(2):
                value = next_slot()
                if value < 0:
                    _deferred_value(stream, ~value, next_slot, labels, None)
            if kind == op_stream.FJ_POS:
                next_slot()  # the position
        elif kind == op_stream.PAD:
            padding += next_slot()
        else:
            next_slot()
            if kind == op_stream.OBJ:
                padding = 0  # a segment or a reserve starts: the padding before it is not taken after it
    return area


def assert_first_op_assembled(fjm_writer: Writer) -> None:
    """
    A FlipJump program starts executing at address 0, so the assembled .fjm must hold its
    first op there: a segment covering bits 0..2w-1 (words 0 and 1). Raise otherwise.
    """
    if not any(start == 0 and length >= 2 for start, length, _, _ in fjm_writer.segments):
        raise FlipJumpAssemblerException(
            "the assembled program has no first op at address 0: no segment holds bits 0..2w-1 "
            "(words 0 and 1). a FlipJump program must start executing at address 0."
        )


def assemble(
    input_files: List[Tuple[str, Path]],
    memory_width: int,
    fjm_writer: Writer,
    *,
    warning_as_errors: bool = True,
    debugging_file_path: Optional[Path] = None,
    show_statistics: bool = False,
    print_time: bool = True,
    max_recursion_depth: int = DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    defines_file: Optional[Path] = None,
    table_pool: Optional[TablePool] = None,
) -> None:
    """
    runs the assembly pipeline. assembles the input files to a .fjm.
    :param input_files:[in]: a list of (short_file_name, fj_file_path). The files will to be parsed in that given order.
    :param memory_width: the memory-width
    :param fjm_writer:[out]: the .fjm file writer
    :param warning_as_errors: treat warnings as errors (stop execution on warnings)
    :param debugging_file_path:[out]: is specified, save debug information in this file
    :param show_statistics: if true shows macro-usage statistics
    :param print_time: if true prints the times of each assemble-stage
    :param max_recursion_depth: The compiler supports macros that recursively uses other macros,
    up to the specified recursion depth.
    :param table_pool: if given, move the lookup tables to addresses that are cheap to arm (see table_pool.py).
    """
    # the pipeline allocates a large acyclic graph: the cyclic gc only re-walks it (restored in finally)
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        with ProgressBar('  parsing:         ', print_time=print_time, total=1.0) as progress:
            macros = parse_macro_tree(
                input_files, memory_width, warning_as_errors, defines_file, progress.update if progress.active else None
            )

        with ProgressBar('  macro resolve:   ', print_time=print_time, total=1.0) as progress:
            # the `...---:start:` macro-start labels are read only by the debugging file
            ops, labels = resolve_macros_with_pool(
                memory_width,
                macros,
                table_pool,
                show_statistics=show_statistics,
                max_recursion_depth=max_recursion_depth,
                save_debug_labels=debugging_file_path is not None,
                on_progress=progress.update if progress.active else None,
                consume_main=True,  # the tree is ours, and dropped right below
            )
        # MEMORY: the macro tree is not read again - free it before labels-resolve builds the binary.
        # (The op stream keeps what it still needs of it: label names, positions, the odd Expr.)
        del macros

        # BlockPool pins each source word to its block base. The base was chosen during macro
        # expansion but the WORD is an expression (variables are usually declared after the code),
        # so it is resolved here, now that every label is known.
        pinned = None
        pool_start = None
        if table_pool is not None:
            pool_start = table_pool.pool_start()
            # a BlockPool records the alias these labels imply, for its census (a dict, not the labels)
            table_pool.labels_resolved(labels)
            pinned, table_pool.pin_conflicts = resolve_pinned(
                table_pool.pinned_words(), labels, exclude=table_pool.pin_exclude
            )
            if pinned:  # each pin that cannot hold is dropped (the pool's unpins say why)
                raw = table_pool.reads_words_raw and table_pool.pin_exclude is None
                guard = table_pool.offset_guard
                assert guard is not None  # a pool's preprocessor made it
                unpin(
                    ops,
                    labels,
                    memory_width,
                    pinned,
                    table_pool.unpins,
                    guard.alignment,
                    raw=raw,
                    pads=guard.tables_inline(),
                )
            if not pinned:
                pinned = None

        with ProgressBar('  labels resolve:  ', print_time=print_time, total=1.0) as progress:
            # the `:wflips:N` labels are read only by the debugging file
            labels_resolve(
                ops,
                labels,
                memory_width,
                fjm_writer,
                save_wflip_labels=debugging_file_path is not None,
                pinned=pinned,
                on_progress=progress.update if progress.active else None,
                pool_start=pool_start,
                offset_guard=None if table_pool is None else table_pool.offset_guard,
                wflips_at=None if table_pool is None else table_pool.wflips_at(),
            )
        # MEMORY: every op is in the writer now; the stream (which labels-resolve reads, not consumes)
        # would otherwise live on through the binary's packing and compression
        del ops

        assert_first_op_assembled(fjm_writer)

        with ProgressBar('  create binary:   ', print_time=print_time, total=1.0) as progress:
            fjm_writer.write_to_file(progress if progress.active else None)
            if debugging_file_path is not None:
                progress.set_total(None)  # a spinner: the labels are written in one go
                progress.update(0, stage='debugging file')
            save_debugging_labels(debugging_file_path, labels)

    except FlipJumpException as fj_exception:
        raise fj_exception
    except Exception as unknown_exception:
        raise FlipJumpAssemblerException(
            "Unknown exception during assembling the .fj files, please report this bug"
        ) from unknown_exception
    finally:
        if gc_was_enabled:
            gc.enable()
