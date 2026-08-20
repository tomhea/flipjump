"""
the core assembly pipeline.
orchestrates the three stages that turn .fj source into a .fjm binary:
parsing the macro-tree, resolving macros into a flat op-list (the preprocessor),
and resolving labels into addresses while writing the result with the fjm Writer.
"""

import gc
from collections import defaultdict
from pathlib import Path
from typing import Deque, List, Dict, Tuple, Optional, NamedTuple

from flipjump.fjm.fjm_writer import Writer, new_word_buffer
from flipjump.utils.constants import WFLIP_LABEL_PREFIX, DEFAULT_MAX_MACRO_RECURSION_DEPTH
from flipjump.utils.functions import save_debugging_labels
from flipjump.utils.classes import PrintTimer
from flipjump.assembler.fj_parser import parse_macro_tree
from flipjump.utils.exceptions import FlipJumpAssemblerException, FlipJumpException, FlipJumpWriteFjmException
from flipjump.assembler.inner_classes.ops import FlipJump, WordFlip, LastPhaseOp, NewSegment, ReserveBits, Padding
from flipjump.assembler.preprocessor import resolve_macros


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
    fj_words: List[int],
    wflip_words: List[int],
) -> None:
    """
    The new segment will be placed in [first_address, last_address),
    And will include the next data: fj_words + wflip_words.
    """
    validate_addresses(memory_width, first_address, last_address)
    if first_address == last_address:
        return

    # PERF (doom-flipjump, 2026-08-20): appended in two pieces instead of `fj_words + wflip_words`.
    # That concatenation built a THIRD list holding every word of the segment -- 78.6M of them on
    # the doom-flipjump program, 22.9 s of pure copying -- purely to pass them as one argument.
    # add_data appends, so two calls land contiguously and `data_start` is still the first one's.
    data_start = fjm_writer.add_data(fj_words)
    fjm_writer.add_data(wflip_words)
    data_length = len(fj_words) + len(wflip_words)

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

    # `del x[:]` not `.clear()`: array.array has no clear() (verified -- it is not registered as
    # a MutableSequence), while `del x[:]` empties BOTH an array and a list.
    del fj_words[:]
    del wflip_words[:]


class WFlipSpot(NamedTuple):
    """Where the next wflip-chain op goes: (the word-list, the index in it, its address).

    PERF (doom-flipjump, 2026-08-20): this was a plain @dataclass, and it is allocated once per
    wflip-chain link -- 16.1M times on the doom-flipjump program, which is the single most
    frequently constructed object in `labels_resolve`. A NamedTuple keeps the exact same
    `.list/.index/.address` attribute access with none of the per-instance __dict__, and tuple
    allocation comes off CPython's freelist. Purely a representation change: no field is added,
    removed, renamed or reordered.
    """

    list: List[int]
    index: int
    address: int


class BinaryData:
    # PERF (doom-flipjump, 2026-08-20): __slots__. One instance per assembly, so this is not about
    # instance size -- it is about attribute ACCESS: every slot read in the hot loop below becomes an
    # index instead of a dict lookup, and those run tens of millions of times.
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
    )

    def __init__(
        self,
        memory_width: int,
        first_segment: NewSegment,
        labels: Dict[str, int],
        *,
        save_wflip_labels: bool = True,
    ):
        """
        @param save_wflip_labels: whether to record a `:wflips:N` label per generated wflip-chain op.
        These labels exist ONLY for the debugging-file; see the note on _insert_wflip_label.
        """
        self.memory_width = memory_width

        self.first_address = first_segment.start_address
        self.next_wflip_address = first_segment.wflip_start_address

        self.labels = labels
        self.wflips_so_far = 0
        self.save_wflip_labels = save_wflip_labels

        # PERF (doom-flipjump, 2026-08-20): typed word arrays, not lists of python ints -- these
        # hold every word of the program (78.6M on the doom-flipjump build) and labels-resolve is
        # the phase that pages. Falls back to a plain list where the typecode does not fit exactly.
        self.fj_words = new_word_buffer(memory_width)
        self.wflip_words = new_word_buffer(memory_width)

        self.padding_ops_indices: List[int] = []  # indices in self.fj_words

        # return_address -> { (f3, f2, f1, f0) -> start_flip_address }
        # PERF: defaultdict(dict) not defaultdict(lambda: {}) -- the C constructor, not a python
        # call, on every previously-unseen return address.
        self.wflips_dict: Dict[int, Dict[Tuple[int, ...], int]] = defaultdict(dict)

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
            self.memory_width, fjm_writer, self.first_address, self.next_wflip_address, self.fj_words, self.wflip_words
        )

    def _insert_wflip_label(self, address: int) -> None:
        """Record the debugging label for a generated wflip-chain op.

        ⚠ THESE LABELS ARE UNREACHABLE FROM FJ SOURCE. `WFLIP_LABEL_PREFIX` is ':wflips:' and the
        lexer's identifier rule is `[a-zA-Z_][a-zA-Z_0-9]*` -- a ':' can never appear in a user
        label, so no Expr can ever name one. Their only consumer is save_debugging_labels(), i.e.
        the optional debugging-file. MEASURED (doom-flipjump, 2026-08-20): on that program they are
        16,137,538 of the 24,386,265 entries in the labels dict -- 66% of it -- built out of 16.1M
        f-string formats and 16.1M dict insertions that nothing then reads. When no debugging file
        was asked for, skipping them is not an approximation: it is removing dead work. Emission is
        untouched, asserted by sha256 of the .fjm.
        """
        if self.save_wflip_labels:
            self.labels[f'{WFLIP_LABEL_PREFIX}{self.wflips_so_far}'] = address
            self.wflips_so_far += 1

    def insert_fj_op(self, flip: int, jump: int) -> None:
        # `+=` on an array only accepts another array; extend() takes any iterable of ints.
        self.fj_words.extend((flip, jump))

    def insert_wflip_ops(self, word_address: int, flip_value: int, return_address: int) -> None:
        if 0 == flip_value:
            self.insert_fj_op(0, return_address)
        else:
            assert_address_in_memory(self.memory_width, flip_value)

            return_dict = self.wflips_dict[return_address]

            # this is the order of flip_addresses (tested with many other orders) that produces the best
            #  found-statistic for searching flip_bit[:i] with different i's in return_dict.
            # PERF (doom-flipjump, 2026-08-20): counted DOWN rather than built-then-`[::-1]`-copied.
            # Same list, one allocation instead of two, on every wflip in the program.
            flip_addresses = [word_address + i for i in range(self.memory_width - 1, -1, -1) if flip_value >> i & 1]

            # insert the first op
            self.insert_fj_op(flip_addresses.pop(), 0)
            last_return_address_index = self.fj_words, len(self.fj_words) - 1

            while flip_addresses:
                flips_key = tuple(flip_addresses)
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

                    wflip_spot.list[wflip_spot.index] = flip_addresses.pop()
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

        self.padding_ops_indices.clear()

    def insert_reserve_bits(self, fjm_writer: Writer, new_first_address: int) -> None:
        add_segment_to_fjm(self.memory_width, fjm_writer, self.first_address, new_first_address, self.fj_words, [])

        self.first_address = new_first_address

        self.padding_ops_indices.clear()


def labels_resolve(
    ops: Deque[LastPhaseOp],
    labels: Dict[str, int],
    memory_width: int,
    fjm_writer: Writer,
    *,
    save_wflip_labels: bool = True,
) -> None:
    """
    resolve the labels and expressions to get the list of fj ops, and add all the data and segments into the fjm_writer.
    @param ops:[in]: the list ops returned from the preprocessor stage
    @param labels:[in]: dictionary from label to its resolved value
    @param memory_width: the memory-width
    @param fjm_writer: [out]: the .fjm file writer
    @param save_wflip_labels: record a `:wflips:N` debugging label per generated wflip-chain op.
    Only the debugging-file reads them (see BinaryData._insert_wflip_label), so a caller that is not
    writing one can pass False and skip the work. It cannot change the emitted .fjm.
    """
    first_segment = ops.popleft()
    if not isinstance(first_segment, NewSegment):
        raise FlipJumpAssemblerException(f"The first op must be of type NewSegment (and not {first_segment}).")

    binary_data = BinaryData(memory_width, first_segment, labels, save_wflip_labels=save_wflip_labels)

    # PERF (doom-flipjump, 2026-08-20): this loop body runs once per emitted op -- ~42M times on the
    # doom-flipjump program, where this phase is 46% of a 29-minute assembly. Three changes, all
    # behaviour-preserving:
    #   * exact-type dispatch (`op.__class__ is FlipJump`) instead of isinstance(). LastPhaseOp is a
    #     Union, not a base class, so nothing here is ever subclassed -- but the isinstance chain is
    #     KEPT as the fallback below, so a subclass would still be handled correctly rather than
    #     falling into the error branch.
    #   * the Expr integer fast-path is INLINED. exact_eval()'s own first two lines are
    #     `value = self.value; if isinstance(value, int): return value`, and after macro-resolve
    #     essentially every operand is already an int -- so the call, the frame and the isinstance
    #     were pure overhead ~84M times (two operands per fj op).
    #   * the bound methods are hoisted into locals.
    # The try/except blocks stay exactly where they were: python 3.11 has zero-cost exceptions, so
    # hoisting them out of the loop would buy nothing, and it would change which errors get the
    # "... in op {op}." suffix.
    insert_fj_op = binary_data.insert_fj_op
    insert_wflip_ops = binary_data.insert_wflip_ops

    # PERF (doom-flipjump, 2026-08-20): the deque is CONSUMED, not iterated. `for op in ops` held
    # all ~42M op objects (and the Expr trees under them) alive until the phase ended, which is
    # most of the live set in the phase that pages hardest; popleft() lets each one be freed the
    # moment it has been emitted. This mutates the caller's deque -- which labels_resolve already
    # did, via the popleft() of the first segment above -- and `assemble()` discards it right after.
    popleft = ops.popleft
    while ops:
        op = popleft()
        op_class = op.__class__

        if op_class is FlipJump:
            try:
                flip = op.flip.value
                jump = op.jump.value
                insert_fj_op(
                    flip if flip.__class__ is int else op.flip.exact_eval(labels),
                    jump if jump.__class__ is int else op.jump.exact_eval(labels),
                )
            except FlipJumpException as e:
                raise FlipJumpAssemblerException(f"{e} in op {op}.")

        elif op_class is WordFlip:
            try:
                word_address = op.word_address.value
                flip_value = op.flip_value.value
                return_address = op.return_address.value
                insert_wflip_ops(
                    word_address if word_address.__class__ is int else op.word_address.exact_eval(labels),
                    flip_value if flip_value.__class__ is int else op.flip_value.exact_eval(labels),
                    return_address if return_address.__class__ is int else op.return_address.exact_eval(labels),
                )
            except FlipJumpException as e:
                raise FlipJumpAssemblerException(f"{e} in op {op}.")

        elif op_class is Padding:
            binary_data.insert_padding(op.ops_count)

        elif op_class is NewSegment:
            binary_data.insert_new_segment(fjm_writer, op.start_address, op.wflip_start_address)

        elif op_class is ReserveBits:
            binary_data.insert_reserve_bits(fjm_writer, op.first_address_after_reserved)

        # the exact-type tests above are the fast path; these keep any subclass working.
        elif isinstance(op, FlipJump):
            try:
                insert_fj_op(op.get_flip(labels), op.get_jump(labels))
            except FlipJumpException as e:
                raise FlipJumpAssemblerException(f"{e} in op {op}.")

        elif isinstance(op, WordFlip):
            try:
                insert_wflip_ops(op.get_word_address(labels), op.get_flip_value(labels), op.get_return_address(labels))
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
    """
    # PERF (doom-flipjump, 2026-08-20): the cyclic garbage collector is off for the pipeline.
    # Assembly is one enormous monotonic allocation: the doom-flipjump program builds ~42M op objects
    # and ~24M labels and frees almost none of them until it is done. CPython triggers a generation-2
    # sweep on allocation COUNT, and every sweep has to walk each live container -- so the cost grows
    # with the size of a graph that is, by construction, still fully reachable. Nothing built here is
    # cyclic (Expr trees are acyclic tuples of Exprs; ops reference Exprs and never back), so plain
    # reference counting still reclaims temporaries immediately and the peak does not move.
    # RESTORED IN `finally`: leaving the interpreter gc off after assemble() returns would be a
    # global side effect on the caller, and this library is used as an in-process API.
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        with PrintTimer('  parsing:         ', print_time=print_time):
            macros = parse_macro_tree(input_files, memory_width, warning_as_errors)

        with PrintTimer('  macro resolve:   ', print_time=print_time):
            # the `...---:start:` macro-start labels are debugging-file-only and unreachable from
            # fj source, same as `:wflips:N` below -- don't build 1.7M of them for nobody.
            ops, labels = resolve_macros(
                memory_width,
                macros,
                show_statistics=show_statistics,
                max_recursion_depth=max_recursion_depth,
                save_debug_labels=debugging_file_path is not None,
            )

        with PrintTimer('  labels resolve:  ', print_time=print_time):
            # the `:wflips:N` labels are debugging-file-only and unreachable from fj source; don't
            # build 16M of them for a caller that is not writing a debugging file.
            labels_resolve(
                ops, labels, memory_width, fjm_writer, save_wflip_labels=debugging_file_path is not None
            )

        assert_first_op_assembled(fjm_writer)

        with PrintTimer('  create binary:   ', print_time=print_time):
            fjm_writer.write_to_file()
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
