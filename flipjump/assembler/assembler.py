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
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Iterator, List, Dict, Mapping, MutableSequence, Tuple, Optional, Union

from flipjump.fjm.fjm_writer import Writer, new_word_buffer
from flipjump.utils.constants import WFLIP_LABEL_PREFIX, DEFAULT_MAX_MACRO_RECURSION_DEPTH
from flipjump.utils.functions import save_debugging_labels, write_defines_file
from flipjump.utils.progress import ProgressBar
from flipjump.assembler.fj_parser import parse_macro_tree
from flipjump.utils.exceptions import (
    FlipJumpAssemblerException,
    FlipJumpException,
    FlipJumpWriteFjmException,
)
from flipjump.assembler.inner_classes.ops import NewSegment, ReserveBits
from flipjump.assembler.inner_classes import op_stream
from flipjump.assembler.inner_classes.op_stream import OpStream
from flipjump.assembler.preprocessor import resolve_macros
from flipjump.assembler.inner_classes.offset_guard import OffsetGuard
from flipjump.assembler.inner_classes.table_pool import BlockPool
from flipjump.assembler.pins import pins_that_hold


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


def validate_words_fit(memory_width: int, first_address: int, words: MutableSequence[int]) -> None:
    """Check that each word of the segment at `first_address` fits the memory width. Only a list of python ints
    can hold one that does not: a typed array (the widths up to 64) refuses such a word as it is inserted (see
    _resolve_ops)."""
    if isinstance(words, list) and words and not 0 <= min(words) <= max(words) < 1 << memory_width:
        raise FlipJumpAssemblerException(
            f'Not enough space with the {memory_width}-bits memory-width: a word of the segment at '
            f'{hex(first_address)} does not fit in {memory_width} bits.'
        )


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
    if first_address == last_address:
        return
    validate_addresses(memory_width, first_address, last_address)
    validate_words_fit(memory_width, first_address, fj_words)
    validate_words_fit(memory_width, first_address, wflip_words)

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
            f"failed to add the segment "
            f"{fjm_writer.get_segment_addresses_repr(segment_start_address, segment_length)}: {e}"
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
    """The words of the program being resolved: the segment gathered so far, and its wflip area."""

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
            after_the_pool = '' if self.wflips_at is None else ' The wflip areas come after the table pool.'
            raise FlipJumpAssemblerException(
                f'Not enough space with the {self.memory_width}-bits memory-width: the wflip area of the segment at '
                f'{hex(self.first_address)} reaches {hex(self.next_wflip_address + 2 * self.memory_width)}.'
                f'{after_the_pool}'
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
        if not base or value_kind != op_stream.VALUE_ADDRESS:
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


# labels_resolve reports its progress once per this many ops
_PROGRESS_CHUNK = 1 << 16


def labels_resolve(
    ops: OpStream,
    labels: Dict[str, int],
    memory_width: int,
    fjm_writer: Writer,
    *,
    save_wflip_labels: bool = True,
    on_progress: Optional[Callable[[float], None]] = None,
    table_pool: Optional[BlockPool] = None,
    warning_as_errors: bool = True,
) -> None:
    """
    resolve the labels and expressions to get the list of fj ops, and add all the data and segments into the fjm_writer.
    @param ops:[in]: the op stream from the preprocessor
    @param labels:[in]: dictionary from label to its resolved value
    @param memory_width: the memory-width
    @param fjm_writer: [out]: the .fjm file writer
    @param save_wflip_labels: record a `:wflips:N` debugging label for each generated wflip-chain op
    @param on_progress: if given, called every _PROGRESS_CHUNK ops with the fraction resolved so far
    @param table_pool: the pool the preprocessor laid out, if any. Now that every label is known, the words it pins
    are resolved to addresses (pins.pins_that_hold).
    @param warning_as_errors: treat warnings as errors (a pin_all_but label the program does not define)
    """
    pinned = None if table_pool is None else pins_that_hold(ops, labels, table_pool, warning_as_errors)
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
    binary_data = BinaryData(
        memory_width,
        _first_segment(ops),
        labels,
        save_wflip_labels=save_wflip_labels,
        pinned=pinned,
        pool_start=pool_start,
        wflips_at=wflips_at,
    )
    if offset_guard is not None:
        offset_guard.resolving(lambda: _resolved_op_place(binary_data, offset_guard))
    _resolve_ops(ops, labels, binary_data, fjm_writer, offset_guard, on_progress)
    binary_data.close_and_add_segment(fjm_writer)


def _first_segment(ops: OpStream) -> NewSegment:
    first_segment = ops.first_object()
    if not isinstance(first_segment, NewSegment):
        raise FlipJumpAssemblerException(
            f"The first op must be of type NewSegment (and not {ops.op_at(0) if ops else None})."
        )
    return first_segment


def _resolved_op_place(binary_data: BinaryData, guard: OffsetGuard) -> str:
    """Return where the op being resolved is, for the guard's refusals: its address, and the label nearest it."""
    address = binary_data.first_address + len(binary_data.fj_words) * binary_data.memory_width
    return f' of the op at {hex(address)} (after the label {guard.label_below(address)})'


def _resolve_ops(
    ops: OpStream,
    labels: Dict[str, int],
    binary_data: BinaryData,
    fjm_writer: Writer,
    guard: Optional[OffsetGuard],
    on_progress: Optional[Callable[[float], None]],
) -> None:
    """Resolve every op after the first NewSegment into `binary_data`, in stream order. The literal ops (most of
    them) are inserted right here, with no `_resolve_*` call."""
    insert_fj_op = binary_data.insert_fj_op
    kinds = ops.kinds
    refs = ops.refs
    next_slot = iter(ops.vals).__next__
    next_slot()  # the first op is the first NewSegment
    op_index = 1
    FJ, FJ_POS, WFLIP, PAD = op_stream.FJ, op_stream.FJ_POS, op_stream.WFLIP, op_stream.PAD
    FJ_PACKED, PACKED_JUMP_BITS, PACKED_JUMP_MASK = (
        op_stream.FJ_PACKED,
        op_stream.PACKED_JUMP_BITS,
        op_stream.PACKED_JUMP_MASK,
    )
    try:
        while op_index < len(kinds):
            chunk_end = min(op_index + _PROGRESS_CHUNK, len(kinds))
            for kind in kinds[op_index:chunk_end]:
                if kind == FJ_PACKED:
                    packed = next_slot()
                    insert_fj_op(packed >> PACKED_JUMP_BITS, packed & PACKED_JUMP_MASK)
                elif kind == FJ:
                    insert_fj_op(next_slot(), next_slot())
                elif kind == FJ_POS or kind == WFLIP:
                    try:
                        if kind == FJ_POS:
                            _resolve_flip_jump(ops, next_slot, labels, binary_data, guard)
                        else:
                            _resolve_word_flip(ops, next_slot, labels, binary_data, guard)
                    except FlipJumpException as e:
                        raise FlipJumpAssemblerException(f"{e} in op {ops.op_at(op_index)}.")
                elif kind == PAD:
                    binary_data.insert_padding(next_slot())
                else:
                    _resolve_object(refs[next_slot()], binary_data, fjm_writer)
                op_index += 1
            if on_progress is not None:
                on_progress(op_index / len(kinds))
    except OverflowError:  # a typed word array took a word that does not fit its width
        width = binary_data.memory_width
        raise FlipJumpAssemblerException(
            f'Not enough space with the {width}-bits memory-width: a word of the op {ops.op_at(op_index)} does not '
            f'fit in {width} bits.'
        ) from None


def _resolve_flip_jump(
    ops: OpStream,
    next_slot: Callable[[], int],
    labels: Dict[str, int],
    binary_data: BinaryData,
    guard: Optional[OffsetGuard],
) -> None:
    """Resolve an `a;b` op that has a deferred operand: read its slots, and insert it. (Each operand is read in
    place, here and in _resolve_word_flip, not through OpStream.read_value: these run for a big share of the
    ops.)"""
    flip = next_slot()
    if flip < 0:
        flip = ops.deferred_value(~flip, next_slot, labels, guard)
    jump = next_slot()
    if jump < 0:
        jump = ops.deferred_value(~jump, next_slot, labels, guard)
    next_slot()  # the position
    binary_data.insert_fj_op(flip, jump)


def _resolve_word_flip(
    ops: OpStream,
    next_slot: Callable[[], int],
    labels: Dict[str, int],
    binary_data: BinaryData,
    guard: Optional[OffsetGuard],
) -> None:
    """Resolve a wflip op: read its slots, and insert its chain of ops. Into a pinned word, an address is flipped xor
    the word's base (BinaryData.pinned_value)."""
    pinned = binary_data.pinned
    word_address = next_slot()
    if word_address < 0:
        word_address = ops.deferred_value(~word_address, next_slot, labels, guard)
    flip_value = next_slot()
    value_kind = op_stream.VALUE_NUMBER
    if flip_value < 0:
        if pinned:
            value_kind = ops.value_kind(~flip_value)
        flip_value = ops.deferred_value(~flip_value, next_slot, labels, guard, False)
    return_address = next_slot()
    if return_address < 0:
        return_address = ops.deferred_value(~return_address, next_slot, labels, guard)
    next_slot()  # the position
    if pinned:
        flip_value = binary_data.pinned_value(word_address, flip_value, value_kind)
    binary_data.insert_wflip_ops(word_address, flip_value, return_address)


def _resolve_object(op: Union[NewSegment, ReserveBits], binary_data: BinaryData, fjm_writer: Writer) -> None:
    """Resolve a NewSegment or a ReserveBits: close what was gathered so far into a segment, and start anew."""
    if isinstance(op, NewSegment):
        binary_data.insert_new_segment(fjm_writer, op.start_address, op.wflip_start_address)
    elif isinstance(op, ReserveBits):
        binary_data.insert_reserve_bits(fjm_writer, op.first_address_after_reserved)
    else:
        raise FlipJumpAssemblerException(f"Can't resolve/assemble the next opcode - {str(op)}")


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


def _writer_of(fjm_writer: Optional[Writer], memory_width: Optional[int]) -> Writer:
    """Return the writer assemble() got, checked: it is required, and `memory_width`, if given, must be its width."""
    if fjm_writer is None:
        raise TypeError('assemble() needs fjm_writer, the .fjm writer to assemble into')
    if memory_width is not None and memory_width != fjm_writer.word_size:
        raise FlipJumpAssemblerException(
            f"memory_width {memory_width} differs from the writer's memory-width {fjm_writer.word_size}: "
            "give one of them (the writer's is enough)"
        )
    return fjm_writer


@contextmanager
def _defines_as_a_file(defines: Optional[Mapping[str, Union[int, str]]]) -> Iterator[Optional[Path]]:
    """For the block, write the defines as a temporary .fj file of constants, and yield its path (None if there are
    no defines)."""
    if not defines:
        yield None
        return
    with TemporaryDirectory(suffix='__defines') as directory:
        defines_file = Path(directory) / '_defines.fj'
        write_defines_file(defines, defines_file)
        yield defines_file


@contextmanager
def _no_cyclic_gc() -> Iterator[None]:
    """For the block, switch python's cyclic garbage collector off (and back on after, if it was on).
    The collector only finds objects that refer to each other in a cycle, by walking every live object again and
    again. The pipeline builds millions of long-lived objects with no cycles, so it would cost time and free nothing.
    It costs no memory: every object is still freed the moment nothing refers to it (reference counting), so an
    assembly peaks at the same memory with the collector on or off."""
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def _expand(
    input_files: List[Tuple[str, Path]],
    memory_width: int,
    defines_file: Optional[Path],
    table_pool: Optional[BlockPool],
    *,
    warning_as_errors: bool,
    save_debug_labels: bool,
    show_statistics: bool,
    show_progress: bool,
    max_recursion_depth: int,
) -> Tuple[OpStream, Dict[str, int]]:
    """The first two stages: parse the files into the macro tree, and expand it into the op stream and the labels.
    The macro tree is this function's own, so it is freed when the function returns (the op stream keeps what it
    still needs of it: label names, positions, the odd Expr)."""
    with ProgressBar('  parsing:         ', show_progress=show_progress) as progress:
        macros = parse_macro_tree(
            input_files, memory_width, warning_as_errors, defines_file, progress.get_active_update_function()
        )
    with ProgressBar('  macro resolve:   ', show_progress=show_progress) as progress:
        return resolve_macros(
            memory_width,
            macros,
            table_pool=table_pool,
            show_statistics=show_statistics,
            max_recursion_depth=max_recursion_depth,
            save_debug_labels=save_debug_labels,
            on_progress=progress.get_active_update_function(),
            consume_main=True,  # the macro tree is not read again
        )


def _assemble_into(
    fjm_writer: Writer,
    input_files: List[Tuple[str, Path]],
    defines_file: Optional[Path],
    table_pool: Optional[BlockPool],
    *,
    warning_as_errors: bool,
    debugging: bool,
    show_statistics: bool,
    show_progress: bool,
    max_recursion_depth: int,
) -> Dict[str, int]:
    """The first three stages - parsing, macro resolve, labels resolve - which fill `fjm_writer` with the program's
    segments. Return the labels. `debugging` asks for the labels only the debugging file reads (each macro's start,
    each wflip-chain op).
    Each stage's input is freed when the stage ends: the macro tree is _expand's own, and the op stream is this
    function's, so neither is alive while the binary is packed and compressed."""
    ops, labels = _expand(
        input_files,
        fjm_writer.word_size,
        defines_file,
        table_pool,
        warning_as_errors=warning_as_errors,
        save_debug_labels=debugging,
        show_statistics=show_statistics,
        show_progress=show_progress,
        max_recursion_depth=max_recursion_depth,
    )
    with ProgressBar('  labels resolve:  ', show_progress=show_progress) as progress:
        labels_resolve(
            ops,
            labels,
            fjm_writer.word_size,
            fjm_writer,
            save_wflip_labels=debugging,
            on_progress=progress.get_active_update_function(),
            table_pool=table_pool,
            warning_as_errors=warning_as_errors,
        )
    assert_first_op_assembled(fjm_writer)
    return labels


def _write(
    fjm_writer: Writer, labels: Dict[str, int], debugging_file_path: Optional[Path], show_progress: bool
) -> None:
    """The last stage: write the .fjm, and the debugging file if asked."""
    with ProgressBar('  create binary:   ', show_progress=show_progress) as progress:
        fjm_writer.write_to_file(progress.get_active_update_function())
        save_debugging_labels(debugging_file_path, labels)


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
    fjm_writer = _writer_of(fjm_writer, memory_width)
    with _defines_as_a_file(defines) as defines_file, _no_cyclic_gc():
        if defines_file is not None:  # parsed before the stl, so the defines override the stl's constants too
            input_files = [('d1', defines_file), *input_files]
        try:
            labels = _assemble_into(
                fjm_writer,
                input_files,
                defines_file,
                table_pool,
                warning_as_errors=warning_as_errors,
                debugging=debugging_file_path is not None,
                show_statistics=show_statistics,
                show_progress=show_progress,
                max_recursion_depth=max_recursion_depth,
            )
            _write(fjm_writer, labels, debugging_file_path, show_progress)
            return labels
        except FlipJumpException as fj_exception:
            raise fj_exception
        except Exception as unknown_exception:
            raise FlipJumpAssemblerException(
                "Unknown exception during assembling the .fj files, please report this bug"
            ) from unknown_exception
