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
from typing import Callable, Deque, List, Dict, MutableSequence, Tuple, Optional, Union

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
from flipjump.assembler.inner_classes.ops import FlipJump, WordFlip, LastPhaseOp, NewSegment, ReserveBits, Padding
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes import op_stream
from flipjump.assembler.inner_classes.op_stream import OpStream
from flipjump.assembler.preprocessor import resolve_macros
from flipjump.assembler.table_pool import TablePool, RESERVED_BELOW


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
        'pinned_floor',
    )

    def __init__(
        self,
        memory_width: int,
        first_segment: NewSegment,
        labels: Dict[str, int],
        *,
        save_wflip_labels: bool = True,
        pinned: Optional[Dict[int, int]] = None,
    ):
        """
        @param save_wflip_labels: whether to record a `:wflips:N` label per generated wflip-chain op
        """
        self.memory_width = memory_width

        # {jump-word address: block base} for BlockPool; None for every other build, so the two use
        # sites below cost one truthiness test per op when off
        self.pinned = pinned
        # a flip value below a `pad 16` table address is a VALUE flip (hex.set, xor_by), not a jump target
        self.pinned_floor = 16 * 2 * memory_width

        self.first_address = first_segment.start_address
        self.next_wflip_address = first_segment.wflip_start_address

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

        wflip_spot = WFlipSpot(self.wflip_words, len(self.wflip_words), self.next_wflip_address)
        self.wflip_words.extend((0, 0))
        self.next_wflip_address += 2 * self.memory_width
        return wflip_spot

    def close_and_add_segment(self, fjm_writer: Writer) -> None:
        if self.next_wflip_address == self.first_address:
            return

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
            base = self.pinned.get(self.first_address + (len(self.fj_words) + 1) * self.memory_width)
            if base:
                jump ^= base
        # `+=` on an array only accepts another array; extend() takes any iterable of ints.
        self.fj_words.extend((flip, jump))

    def insert_wflip_ops(self, word_address: int, flip_value: int, return_address: int) -> None:
        if self.pinned:
            # the word rests at `base + digit`, so a writer installing a jump target V flips
            # `V ^ base` - for the table's own arm just its index in the block, and the same rule
            # for every other dispatcher through the word (stl.comp_if1, hex.shifts.*, hex.tables.*)
            base = self.pinned.get(word_address)
            if base and flip_value >= self.pinned_floor:
                flip_value ^= base
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
        self.next_wflip_address = wflip_first_address

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


def _deferred_value(stream: OpStream, encoded: int, next_slot: Callable[[], int], labels: Dict[str, int]) -> int:
    """the value of a deferred operand (its slot is ~encoded; see OpStream). Raises what the Expr would."""
    ref = stream.refs[encoded >> 2]
    tag = encoded & 3
    if tag == op_stream.TAG_LABEL:
        value = labels.get(ref)
        if value is None:
            return Expr(ref).exact_eval(labels)  # raises the Expr's own "Can't evaluate label"
        return value
    if tag == op_stream.TAG_LABEL_PLUS:
        const = next_slot()
        value = labels.get(ref)
        if value is None:
            return Expr(ref).exact_eval(labels) + const  # raises, as above
        return value + const
    return ref.exact_eval(labels)  # type: ignore[no-any-return]


def _labels_resolve_stream(
    stream: OpStream,
    binary_data: BinaryData,
    labels: Dict[str, int],
    fjm_writer: Writer,
    on_progress: Optional[Callable[[float], None]],
) -> None:
    """labels_resolve over a compact OpStream (the preprocessor's output): the same BinaryData calls,
    in the same order, with the same values, as the op objects it replaces would make."""
    insert_fj_op = binary_data.insert_fj_op
    insert_wflip_ops = binary_data.insert_wflip_ops
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
                        flip = _deferred_value(stream, ~flip, next_slot, labels)
                    jump = next_slot()
                    if jump < 0:
                        jump = _deferred_value(stream, ~jump, next_slot, labels)
                    next_slot()  # the position
                    insert_fj_op(flip, jump)
                except FlipJumpException as e:
                    raise FlipJumpAssemblerException(f"{e} in op {stream.op_at(op_index)}.")
            elif kind == WFLIP:
                try:
                    word_address = next_slot()
                    if word_address < 0:
                        word_address = _deferred_value(stream, ~word_address, next_slot, labels)
                    flip_value = next_slot()
                    if flip_value < 0:
                        flip_value = _deferred_value(stream, ~flip_value, next_slot, labels)
                    return_address = next_slot()
                    if return_address < 0:
                        return_address = _deferred_value(stream, ~return_address, next_slot, labels)
                    next_slot()  # the position
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
) -> None:
    """
    resolve the labels and expressions to get the list of fj ops, and add all the data and segments into the fjm_writer.
    @param ops:[in]: the list ops returned from the preprocessor stage
    @param labels:[in]: dictionary from label to its resolved value
    @param memory_width: the memory-width
    @param fjm_writer: [out]: the .fjm file writer
    @param save_wflip_labels: record a `:wflips:N` debugging label per generated wflip-chain op
    @param on_progress: if given, called every _PROGRESS_CHUNK ops with the fraction of ops resolved (OpStream only)
    """
    if isinstance(ops, OpStream):
        first_object = ops.first_object()
        if not isinstance(first_object, NewSegment):
            first_op = ops.op_at(0) if ops else None
            raise FlipJumpAssemblerException(f"The first op must be of type NewSegment (and not {first_op}).")
        binary_data = BinaryData(memory_width, first_object, labels, save_wflip_labels=save_wflip_labels, pinned=pinned)
        _labels_resolve_stream(ops, binary_data, labels, fjm_writer, on_progress)
        binary_data.close_and_add_segment(fjm_writer)
        return

    first_segment = ops.popleft()
    if not isinstance(first_segment, NewSegment):
        raise FlipJumpAssemblerException(f"The first op must be of type NewSegment (and not {first_segment}).")

    binary_data = BinaryData(memory_width, first_segment, labels, save_wflip_labels=save_wflip_labels, pinned=pinned)

    for op in ops:
        if isinstance(op, FlipJump):
            try:
                binary_data.insert_fj_op(op.get_flip(labels), op.get_jump(labels))
            except FlipJumpException as e:
                raise FlipJumpAssemblerException(f"{e} in op {op}.")

        elif isinstance(op, WordFlip):
            try:
                binary_data.insert_wflip_ops(
                    op.get_word_address(labels), op.get_flip_value(labels), op.get_return_address(labels)
                )
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
            ops, labels = resolve_macros(
                memory_width,
                macros,
                table_pool=table_pool,
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
        if table_pool is not None:
            # a BlockPool records the alias these labels imply, for its census (a dict, not the labels)
            table_pool.labels_resolved(labels)
            pinned, table_pool.pin_conflicts = resolve_pinned(
                table_pool.pinned_words(), labels, exclude=table_pool.pin_exclude
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
