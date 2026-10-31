"""
the core assembly pipeline.
orchestrates the three stages that turn .fj source into a .fjm binary:
parsing the macro-tree, resolving macros into a flat op-list (the preprocessor),
and resolving labels into addresses while writing the result with the fjm Writer.
"""

import dataclasses
import gc
from array import array
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import AbstractSet, Callable, List, Dict, Mapping, MutableSequence, Sequence, Set, Tuple, Optional, Union

from flipjump.fjm.fjm_writer import Writer, new_word_buffer
from flipjump.utils.constants import WFLIP_LABEL_PREFIX, DEFAULT_MAX_MACRO_RECURSION_DEPTH
from flipjump.utils.functions import save_debugging_labels, write_defines_file
from flipjump.utils.progress import ProgressBar, clear_progress_line
from flipjump.assembler.fj_parser import parse_macro_tree
from flipjump.utils.exceptions import (
    FlipJumpAssemblerException,
    FlipJumpException,
    FlipJumpExprException,
    FlipJumpWriteFjmException,
)
from flipjump.assembler.inner_classes.ops import NewSegment, ReserveBits
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes import op_stream
from flipjump.assembler.inner_classes.op_stream import OpStream
from flipjump.assembler.preprocessor import resolve_macros
from flipjump.assembler.inner_classes.table_pool import BlockPool, OffsetGuard, RESERVED_BELOW


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
    @param take: the caller gives fj_words away (the writer may keep it instead of copying it) and switches to new
    buffers, so neither list is cleared.
    """
    validate_addresses(memory_width, first_address, last_address)
    if first_address == last_address:
        return

    # Two appends instead of `fj_words + wflip_words`, which would copy every word. The data stays contiguous.
    # measured before add_data, since a writer that takes fj_words appends to it
    data_length = len(fj_words) + len(wflip_words)
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
        @param save_wflip_labels: record a `:wflips:N` label for each generated wflip-chain op
        @param pinned: the pinned words, {jump-word address: block base}, or None for no pins
        @param pool_start: where the table pool starts; no segment may reach into it
        @param wflips_at: if given, put the wflip areas here instead (BlockPool.wflips_at)
        """
        self.memory_width = memory_width
        self.pinned = pinned
        self.pool_start = pool_start

        self.first_address = first_segment.start_address
        self.wflips_at = wflips_at
        self.next_wflip_address = first_segment.wflip_start_address if wflips_at is None else wflips_at

        self.labels = labels
        self.wflips_so_far = 0
        self.save_wflip_labels = save_wflip_labels

        # every word of the program, in typed arrays (a plain list if no array type fits the width)
        self.fj_words = new_word_buffer(memory_width)
        self.wflip_words = new_word_buffer(memory_width)

        # Indices into self.fj_words. MEMORY: a typed array (8 bytes per index) instead of a list of ints (40 bytes
        # each), since alignment padding can leave millions of ops waiting for a wflip op.
        self.padding_ops_indices = array('q')

        # return_address -> {chain tail -> start_flip_address}; a tail is keyed by one int (see insert_wflip_ops)
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
        if self.wflips_at is not None:  # write the code here, and its wflip area after the pool
            code_end = self.first_address + len(self.fj_words) * self.memory_width
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
        """Start new word buffers: the writer took the old ones (it keeps the first segment's words instead of copying
        them, see Writer.add_data)."""
        self.fj_words = new_word_buffer(self.memory_width)
        self.wflip_words = new_word_buffer(self.memory_width)

    def _insert_wflip_label(self, address: int) -> None:
        """Add a `:wflips:N` label. Only the debugging file reads these (fj source can't name them)."""
        if self.save_wflip_labels:
            self.labels[f'{WFLIP_LABEL_PREFIX}{self.wflips_so_far}'] = address
            self.wflips_so_far += 1

    def insert_fj_op(self, flip: int, jump: int) -> None:
        if self.pinned:
            # A pinned variable's word starts at `base + value`. Only its own op has a pinned jump word.
            base = self.pinned.get(self.first_address + (len(self.fj_words) + 1) * self.memory_width)
            if base:
                jump ^= base
        # `+=` on an array only accepts another array; extend() takes any iterable of ints.
        self.fj_words.extend((flip, jump))

    def pinned_value(self, word_address: int, flip_value: int, value_kind: int) -> int:
        """Return the value a wflip flips into `word_address`. Into a pinned word, an address V (a label plus a
        constant) is flipped as `V ^ base`; any other value is flipped as is (unpin kept only the pins where that is
        safe)."""
        base = self.pinned.get(word_address) if self.pinned else None
        if not base or value_kind != VALUE_ADDRESS:
            return flip_value
        return flip_value ^ base

    def insert_wflip_ops(self, word_address: int, flip_value: int, return_address: int) -> None:
        if 0 == flip_value:
            self.insert_fj_op(0, return_address)
        else:
            assert_address_in_memory(self.memory_width, flip_value)

            return_dict = self.wflips_dict[return_address]

            # The chain flips the word's set bits from the lowest address up: of the many orders tried, this one shares
            # the most tails through return_dict. A tail is the set of addresses still to flip. A tail {a + i : bit i of
            # m} (a is its lowest address, so bit 0 of m is set) is keyed by one int, `a << w | m`: two tails get the
            # same key exactly when they flip the same addresses.
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
        # Only the ops' buffer was handed over. The wflip ops gathered so far stay: they go in the segment's wflip
        # area, after the reserved region, when the segment closes.
        self.fj_words = new_word_buffer(self.memory_width)

        self.first_address = new_first_address

        del self.padding_ops_indices[:]


def resolve_pinned(
    pinned_exprs: Dict[Expr, int], labels: Dict[str, int], unpinned_words: AbstractSet[int] = frozenset()
) -> Tuple[Dict[int, int], int]:
    """Resolve the pinned words' expressions to addresses. Skip a word that two groups pin with different bases
    (like `(x + 32)` and `(x + w)`), and a word of `unpinned_words` (the program reads it raw).
    Returns: ({jump-word address: block base}, how many addresses were skipped for having two bases)."""
    pinned: Dict[int, int] = {}
    conflicts = set()
    for expr, base in pinned_exprs.items():
        try:
            address = expr.exact_eval(labels)
        except FlipJumpExprException:
            continue  # an unresolvable word simply is not pinned
        if address < RESERVED_BELOW:
            continue  # the runtime's word (stl.IO), not the program's
        if address in unpinned_words:
            continue  # the program reads this word raw
        if address in pinned and pinned[address] != base:
            conflicts.add(address)
        pinned[address] = base
    for address in conflicts:
        del pinned[address]
    return pinned, len(conflicts)


# labels_resolve reports its progress once per this many ops
_PROGRESS_CHUNK = 1 << 16


def pins(
    ops: OpStream, labels: Dict[str, int], memory_width: int, pool: BlockPool, warning_as_errors: bool
) -> Dict[int, int]:
    """Return the words to pin, {jump-word address: block base}: the pool's candidates resolved by `labels`, minus
    pin_all_but's words, words two groups pin with different bases, and pins that can't hold (see unpin)."""
    unpinned_words = pool.unpinned_words(labels)
    if pool.pin_all_but_missing:
        pin_all_but_warning(pool.pin_all_but_missing, warning_as_errors)
    pinned, _ = resolve_pinned(pool.pinned_words(), labels, unpinned_words)
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

    def drop(word: int, reason: str) -> None:
        if pinned.pop(word, None) is not None:
            pool.unpins[reason] = pool.unpins.get(reason, 0) + 1

    if pool.reads_words_raw and pool.pin_all_but is None:
        for word in list(pinned):
            drop(word, 'the pointer runtime')
        return
    w = memory_width
    # word -> log2 of the alignment (in bits) of each table it is armed with
    arms: Dict[int, List[int]] = {word: [] for word in pinned}
    flipped_bits: Dict[int, int] = {}  # word -> the bits that `a;b` ops flip in it
    numbers: Dict[int, int] = {}  # word -> the bits that wflips of plain numbers flip in it
    computed: Dict[int, int] = {}  # word -> the bits that wflips of computed values (like `T1 ^ T2`) flip in it
    rests: Dict[int, int] = {}  # word -> its initial value (the jump of its op)
    jumped: Set[int] = set()  # words that an op jumps through without being one of their arms
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
                    arms[word].append(guard.alignment(_label_address(stream, ~value, labels)))
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
            obj = stream.refs[next(slots)]
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
                rests[address + w] = jump
            if jump + w in arms:
                jumped.add(jump + w)
            word = flip - flip % w
            if word in arms:
                flipped_bits[word] = flipped_bits.get(word, 0) | 1 << (flip - word)
            address += 2 * w
    low = sorted(guard.pads.items())
    for word, base in list(pinned.items()):
        # The word's window: its low bits, below its base's alignment and below its widest armed table. A flip inside
        # the window changes a value or moves within the armed table, the same with or without the pool. Only an arm
        # may flip a bit above it.
        top = (base & -base).bit_length() - 1 if base else 1 << 30
        if arms[word]:
            top = min(top, max(arms[word]))
        above = ~((1 << top) - 1)
        if word not in rests:
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
    """Could a word that starts at `rest`, flipped by some of the bits of `flips`, point into a table of `pads` below
    2^top? (`pads` is [(table address, log2 of its alignment in bits)], ascending.)"""
    for address, bits in pads:
        if address >> top:
            break
        above = ~((1 << bits) - 1)  # the bits above the table's alignment (all its entries share them)
        if address & flips & above and not address & ~(rest | flips) and not rest & ~flips & above & ~address:
            return True
    return False


def _label_address(stream: OpStream, encoded: int, labels: Dict[str, int]) -> int:
    ref = stream.refs[encoded >> 2]
    if encoded & 3 != op_stream.TAG_EXPR:
        return labels[ref]
    linear = op_stream.linear_sum(ref)
    label = None if linear is None else linear[0]
    assert label is not None  # a VALUE_ADDRESS expression is one label plus a constant
    return labels[label]


# The kinds of wflip value (see BinaryData.pinned_value): a number, an address (a label plus a constant), or a
# value computed from labels.
VALUE_NUMBER, VALUE_ADDRESS, VALUE_COMPUTED = 0, 1, 2


def _deferred_value_kind(stream: OpStream, encoded: int) -> int:
    if encoded & 3 != op_stream.TAG_EXPR:
        return VALUE_ADDRESS
    value: Expr = stream.refs[encoded >> 2]
    if not value.all_unknown_labels():
        return VALUE_NUMBER
    linear = op_stream.linear_sum(value)
    return VALUE_ADDRESS if linear is not None and linear[0] is not None else VALUE_COMPUTED


def _deferred_value(
    stream: OpStream,
    encoded: int,
    next_slot: Callable[[], int],
    labels: Dict[str, int],
    guard: Optional[OffsetGuard],
    is_address: bool = True,
) -> int:
    """Return a deferred operand's value (its slot holds ~ref), checked by the pool's guard if there is one (see
    check_expr)."""
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


def labels_resolve(
    ops: OpStream,
    labels: Dict[str, int],
    memory_width: int,
    fjm_writer: Writer,
    *,
    save_wflip_labels: bool = True,
    on_progress: Optional[Callable[[float], None]] = None,
    table_pool: Optional[BlockPool] = None,
    pinned: Optional[Dict[int, int]] = None,
) -> None:
    """
    resolve the labels and expressions to get the list of fj ops, and add all the data and segments into the fjm_writer.
    @param ops:[in]: the op stream from the preprocessor
    @param labels:[in]: dictionary from label to its resolved value
    @param memory_width: the memory-width
    @param fjm_writer: [out]: the .fjm file writer
    @param save_wflip_labels: record a `:wflips:N` debugging label for each generated wflip-chain op
    @param on_progress: if given, called every _PROGRESS_CHUNK ops with the fraction resolved so far
    @param table_pool: the pool the preprocessor laid out, if any
    @param pinned: the words it pins (assembler.pins)
    """
    resolve_into(
        ops,
        labels,
        memory_width,
        fjm_writer,
        save_wflip_labels=save_wflip_labels,
        on_progress=on_progress,
        pinned=pinned or None,
        pool_start=None if table_pool is None else table_pool.pool_start(),
        offset_guard=None if table_pool is None else table_pool.offset_guard,
        wflips_at=None if table_pool is None else table_pool.wflips_at(),
    )


def resolve_into(
    ops: OpStream,
    labels: Dict[str, int],
    memory_width: int,
    fjm_writer: Writer,
    *,
    save_wflip_labels: bool,
    on_progress: Optional[Callable[[float], None]],
    pinned: Optional[Dict[int, int]],
    pool_start: Optional[int],
    offset_guard: Optional[OffsetGuard],
    wflips_at: Optional[int],
) -> None:
    """labels_resolve, with the pool's parts passed one by one: `pinned`, `pool_start` and `wflips_at` (see
    BinaryData), and `offset_guard`, which checks the operands that had to wait for a label."""
    first_segment = ops.first_object()
    if not isinstance(first_segment, NewSegment):
        raise FlipJumpAssemblerException(
            f"The first op must be of type NewSegment (and not {ops.op_at(0) if ops else None})."
        )
    binary_data = BinaryData(
        memory_width,
        first_segment,
        labels,
        save_wflip_labels=save_wflip_labels,
        pinned=pinned,
        pool_start=pool_start,
        wflips_at=wflips_at,
    )
    guard = offset_guard
    if guard is not None:  # a refusal says where the op is: its address, and the label nearest it
        label_below = guard.label_below

        def where() -> str:
            address = binary_data.first_address + len(binary_data.fj_words) * memory_width
            return f' of the op at {hex(address)} (after the label {label_below(address)})'

        guard.resolving(where)

    insert_fj_op = binary_data.insert_fj_op
    insert_wflip_ops = binary_data.insert_wflip_ops
    kinds = ops.kinds
    refs = ops.refs
    slots = iter(ops.vals)
    next_slot = slots.__next__
    next(slots)  # the first op is the first NewSegment
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
                        flip = _deferred_value(ops, ~flip, next_slot, labels, guard)
                    jump = next_slot()
                    if jump < 0:
                        jump = _deferred_value(ops, ~jump, next_slot, labels, guard)
                    next_slot()  # the position
                    insert_fj_op(flip, jump)
                except FlipJumpException as e:
                    raise FlipJumpAssemblerException(f"{e} in op {ops.op_at(op_index)}.")
            elif kind == WFLIP:
                try:
                    word_address = next_slot()
                    if word_address < 0:
                        word_address = _deferred_value(ops, ~word_address, next_slot, labels, guard)
                    flip_value = next_slot()
                    kind_of_value = VALUE_NUMBER
                    if flip_value < 0:
                        if pinned:
                            kind_of_value = _deferred_value_kind(ops, ~flip_value)
                        flip_value = _deferred_value(ops, ~flip_value, next_slot, labels, guard, False)
                    return_address = next_slot()
                    if return_address < 0:
                        return_address = _deferred_value(ops, ~return_address, next_slot, labels, guard)
                    next_slot()  # the position
                    if pinned:
                        flip_value = binary_data.pinned_value(word_address, flip_value, kind_of_value)
                    insert_wflip_ops(word_address, flip_value, return_address)
                except FlipJumpException as e:
                    raise FlipJumpAssemblerException(f"{e} in op {ops.op_at(op_index)}.")
            elif kind == PAD:
                binary_data.insert_padding(next_slot())
            else:
                op = refs[next(slots)]
                if isinstance(op, NewSegment):
                    binary_data.insert_new_segment(fjm_writer, op.start_address, op.wflip_start_address)
                elif isinstance(op, ReserveBits):
                    binary_data.insert_reserve_bits(fjm_writer, op.first_address_after_reserved)
                else:
                    raise FlipJumpAssemblerException(f"Can't resolve/assemble the next opcode - {str(op)}")
            op_index += 1
        if on_progress is not None:
            on_progress(op_index / len(kinds))
    binary_data.close_and_add_segment(fjm_writer)


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
    memory_width: Optional[int] = None,
    fjm_writer: Optional[Writer] = None,
    *,
    warning_as_errors: bool = True,
    debugging_file_path: Optional[Path] = None,
    show_statistics: bool = False,
    show_progress: bool = True,
    max_recursion_depth: int = DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    defines: Optional[Mapping[str, Union[int, str]]] = None,
    table_pool: Optional[BlockPool] = None,
    print_time: Optional[bool] = None,
) -> Dict[str, int]:
    """
    runs the assembly pipeline. assembles the input files to a .fjm.
    :param input_files:[in]: a list of (short_file_name, fj_file_path). The files will to be parsed in that given order.
    :param memory_width: must equal the writer's memory width (None: use the writer's)
    :param fjm_writer:[out]: the .fjm file writer (required)
    :param warning_as_errors: treat warnings as errors (stop execution on warnings)
    :param debugging_file_path:[out]: is specified, save debug information in this file
    :param show_statistics: if true shows macro-usage statistics
    :param show_progress: if true, show each stage's time (and, on a terminal, a progress bar)
    :param print_time: flipjump 1.5's name for show_progress. If given, it is used as show_progress.
    :param max_recursion_depth: The compiler supports macros that recursively uses other macros,
    up to the specified recursion depth.
    :param defines: constants that override the program's own declarations (`fj -D NAME=VALUE`), as {full name:
    value}. A full name includes its namespaces (`a.b.NAME`); a value is a number or an fj expression, which may
    use `w` and earlier defines. Overriding a constant that nothing declares is an error.
    :param table_pool: if given, move the lookup tables to addresses that are cheap to arm (see table_pool.py).
    :return: the labels, {full name: address in bits}. With debugging_file_path, also the labels only the debugging
    file needs (`...---:start:` for each macro use, `:wflips:N` for each wflip-chain op).
    """
    show_progress = show_progress if print_time is None else print_time
    if fjm_writer is None:
        raise TypeError('assemble() needs fjm_writer, the .fjm writer to assemble into')
    if memory_width is not None and memory_width != fjm_writer.word_size:
        raise FlipJumpAssemblerException(
            f"memory_width {memory_width} differs from the writer's memory-width {fjm_writer.word_size}: "
            "give one of them (the writer's is enough)"
        )
    memory_width = fjm_writer.word_size
    defines_dir: Optional['TemporaryDirectory[str]'] = None
    defines_file: Optional[Path] = None
    if defines:
        # the defines, as a .fj file parsed before the stl (so they override the stl's constants too); deleted when done
        directory = TemporaryDirectory(suffix='__defines')
        file = Path(directory.name) / '_defines.fj'
        try:
            write_defines_file(defines, file)
        except ValueError:
            directory.cleanup()
            raise
        input_files = [('d1', file), *input_files]
        defines_dir, defines_file = directory, file
    # Disable the cyclic gc: the pipeline builds a huge graph with no cycles, which gc would only re-walk.
    # (Re-enabled in finally.)
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        with ProgressBar('  parsing:         ', show_progress=show_progress) as progress:
            macros = parse_macro_tree(
                input_files, memory_width, warning_as_errors, defines_file, progress.update if progress.active else None
            )

        with ProgressBar('  macro resolve:   ', show_progress=show_progress) as progress:
            ops, labels = resolve_macros(
                memory_width,
                macros,
                table_pool=table_pool,
                show_statistics=show_statistics,
                max_recursion_depth=max_recursion_depth,
                # only the debugging file needs the macro-start labels
                save_debug_labels=debugging_file_path is not None,
                on_progress=progress.update if progress.active else None,
                consume_main=True,  # we own the macro tree, and free it right below
            )
        # MEMORY: the macro tree is not read again - free it before labels-resolve builds the binary.
        # (The op stream keeps what it still needs of it: label names, positions, the odd Expr.)
        del macros

        # now that every label is known, resolve the pool's pinned words to addresses
        pinned = {} if table_pool is None else pins(ops, labels, memory_width, table_pool, warning_as_errors)

        with ProgressBar('  labels resolve:  ', show_progress=show_progress) as progress:
            # the `:wflips:N` labels are read only by the debugging file
            labels_resolve(
                ops,
                labels,
                memory_width,
                fjm_writer,
                save_wflip_labels=debugging_file_path is not None,
                on_progress=progress.update if progress.active else None,
                table_pool=table_pool,
                pinned=pinned,
            )
        # MEMORY: every op is in the writer now. Drop the stream (labels-resolve only read it), or it would stay alive
        # through the binary's packing and compression.
        del ops

        assert_first_op_assembled(fjm_writer)

        with ProgressBar('  create binary:   ', show_progress=show_progress) as progress:
            fjm_writer.write_to_file(progress.update if progress.active else None)
            save_debugging_labels(debugging_file_path, labels)
        return labels

    except FlipJumpException as fj_exception:
        raise fj_exception
    except Exception as unknown_exception:
        raise FlipJumpAssemblerException(
            "Unknown exception during assembling the .fj files, please report this bug"
        ) from unknown_exception
    finally:
        if gc_was_enabled:
            gc.enable()
        if defines_dir is not None:
            defines_dir.cleanup()
