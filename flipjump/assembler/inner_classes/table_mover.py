"""
The table mover: the preprocessor's side of a table pool (table_pool.py).

As the expansion reaches a declared table, the mover takes it out of the op stream (begin, end). When the expansion
ends, it has the pool place every table it took, and appends them, placed, to the stream (place_tables). Along the
way it tells the OffsetGuard what the pool moves, so no operand changes meaning.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from flipjump.utils.constants import MACRO_SEPARATOR_STRING
from flipjump.utils.exceptions import FlipJumpExprException, FlipJumpPreprocessorException
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.offset_guard import OffsetGuard, reached_the_pool_exception
from flipjump.assembler.inner_classes.op_stream import OpStream
from flipjump.assembler.inner_classes.ops import CodePosition, FlipJump, Macro, NewSegment, WordFlip, get_used_labels
from flipjump.assembler.inner_classes.table_pool import (
    BlockPool,
    DeferredTables,
    KEPT_RUNTIME_WORD,
    KEPT_TOO_WIDE,
    POINTER_RUNTIME_LABELS,
    heat_key,
    reserved_below,
)

if TYPE_CHECKING:
    from flipjump.assembler.preprocessor import PreprocessorData  # for types only: it imports us


def _dollar_ops(macro: Macro) -> Dict[int, bool]:
    """Return the macro's ops that name `$`, as {op index: True if `$` is in a sum, False if the op just falls
    through}."""
    found: Dict[int, bool] = {}
    for index, op in enumerate(macro.ops):
        if isinstance(op, FlipJump):
            operands: Tuple[Expr, ...] = (op.flip, op.jump)
        elif isinstance(op, WordFlip):
            operands = (op.word_address, op.flip_value, op.return_address)
        else:
            continue
        in_sum = any(operand.value != '$' and '$' in operand.all_unknown_labels() for operand in operands)
        if in_sum or any(operand.value == '$' for operand in operands):
            found[index] = in_sum
    return found


class TableMover:
    """The part of a preprocessor that moves the declared tables to a pool (see the module docstring)."""

    def __init__(self, data: PreprocessorData, pool: BlockPool):
        if pool.offset_guard is not None:
            raise FlipJumpPreprocessorException(
                'table-placement: this pool already laid out an assembly; construct a fresh one for every assemble()'
            )
        if pool.memory_width != data.memory_width:
            raise FlipJumpPreprocessorException(
                f'table-placement: the pool is for w={pool.memory_width}, and the program is assembled at '
                f'w={data.memory_width}'
            )
        self.data = data  # the preprocessor it is part of
        self.pool = pool
        # checks that moving a table changes no operand
        self.guard = pool.offset_guard = OffsetGuard(
            pool.pool_start(), data.labels, where=self._where, emitting=self._emitting
        )
        self.tables = DeferredTables()  # the tables met, queued for the pool
        # the moved tables' ops: each table is a NewSegment and its ops. place_tables appends them to the result.
        self.table_ops = OpStream(data.op_refs, data.labels, self.guard)
        self.table_starts: List[Tuple[int, int]] = []  # where each moved table starts in table_ops: (op, slot) index
        # Where each label of a moved table was declared. Its labels reach the labels dict only when it is placed.
        self.label_positions: Dict[str, CodePosition] = {}
        # while a moved table is emitted: the inline address and stream to return to
        self.resume: Optional[Tuple[int, OpStream]] = None
        self.shift = 0  # how many ops ahead the same program without the pool is, in this segment
        self.code_end = 0  # the highest end of the code of the segments closed so far
        # id(macro) -> (its ops that name `$` (_dollar_ops), does it name POINTER_RUNTIME_LABELS)
        self._macro_facts: Dict[int, Tuple[Dict[int, bool], bool]] = {}
        self._evaluating = ''  # while a number the expansion evaluates is checked: what it is, and where

    # ---- what a refusal needs to know ----

    def _where(self) -> str:
        """Return where the op being expanded is, for the guard's refusals."""
        data = self.data
        path = ' > '.join(call.trace_str() for call in data.curr_tree) or 'the top level'
        if self._evaluating:
            return f', {self._evaluating} (in {path}),'
        if self.resume is not None:
            return f' of an op of a moved table (in {path})'
        return f' of the op at {hex(data.curr_address - 2 * data.memory_width)} (in {path})'

    def _emitting(self) -> int:
        """Return the next inline address the expansion emits at."""
        return self.data.curr_address if self.resume is None else self.resume[0]

    def validate_not_reaching_the_pool(self, address: int) -> None:
        """Refuse a program whose code reaches `address`, if that is inside the pool (labels-resolve checks the wflip
        areas)."""
        if address > self.pool.pool_start():
            raise reached_the_pool_exception(address, self.pool.pool_start())

    # ---- as the expansion goes ----

    def entering(self, macro: Macro) -> Dict[int, bool]:
        """Note that `macro` starts to expand: tell the pool if it names the pointer runtime's words. Return its ops
        that name `$` (_dollar_ops)."""
        facts = self._macro_facts.get(id(macro))
        if facts is None:
            uses_pointers = bool(get_used_labels(macro.ops) & POINTER_RUNTIME_LABELS)
            facts = self._macro_facts[id(macro)] = (_dollar_ops(macro), uses_pointers)
        if facts[1]:
            self.pool.reads_words_raw = True
        return facts[0]

    def dollar(
        self, in_sum: bool, labels_prefix: str, op_index: int, code_position: CodePosition, params_dict: Dict[str, Expr]
    ) -> None:
        """Handle an op that names `$`: in a sum, `$` becomes a label at the next op; alone, the op falls through."""
        data = self.data
        if in_sum:
            name = f'{labels_prefix}{MACRO_SEPARATOR_STRING}$:{op_index}' if labels_prefix else f'$:{op_index}'
            data.insert_label(name, code_position)
            self.guard.watched.add(name)
            params_dict['$'] = Expr(name)
        else:
            self.guard.falls_to = data.curr_address

    def check_number(self, expr: Expr, value: int, what: str) -> None:
        """Check a number the expansion evaluated from labels: `what` says what it is and where (`the count of the rep
        at ...`). A table pool must not change it (OffsetGuard.check_number)."""
        self._evaluating = what
        try:
            self.guard.check_number(expr, value)
        finally:
            self._evaluating = ''

    def padding(self, ops_alignment: int, ops_to_pad: int) -> None:
        """Note a `pad` at the current address that pads `ops_to_pad` ops. Without the pool it would start `shift`
        ops later, and may pad another number of ops: the guard then lets no operand reach across it."""
        data = self.data
        op_bits = 2 * data.memory_width
        plain_pad = (-(data.curr_address // op_bits + self.shift)) % ops_alignment
        if plain_pad != ops_to_pad:
            self.shift += plain_pad - ops_to_pad
            pad_end = data.curr_address + ops_to_pad * op_bits
            self.guard.pad_realigned(data.curr_address, ops_alignment, self.shift * op_bits, pad_end)

    def segment_started(self, previous_end: int) -> None:
        """Note that the segment that was being emitted ended at `previous_end`, and a new one starts at the current
        address: the same address with or without the pool."""
        start = self.data.curr_address
        self.validate_not_reaching_the_pool(previous_end)
        if start >= self.pool.pool_start():
            raise reached_the_pool_exception(start, self.pool.pool_start())
        self.code_end = max(self.code_end, previous_end)
        self.shift = 0
        self.guard.segment_started(start, previous_end)

    # ---- taking a table out of the stream ----

    def begin(self, ops_alignment: int, labels_prefix: str, table_ops: int, word: Optional[Expr], table: str) -> bool:
        """Take the declared table out of the op stream: `table` is its start label, `word` its group's jump word.
        While it is emitted, curr_address counts from 0, so its labels are offsets from its start. Return False if it
        stays inline (the pool's kept_inline counts why)."""
        group = None if word is None else str(word)
        site = sys.intern(heat_key(labels_prefix))
        kept = self._why_kept_inline(group, word, ops_alignment, table_ops)
        if kept is not None:
            self.pool.kept_inline[kept] = self.pool.kept_inline.get(kept, 0) + 1
            if kept == KEPT_TOO_WIDE:  # the pool still counts it in its group
                self.tables.add(group or '', ops_alignment, table_ops, site, None)
            return False
        # (a raw program's table may have no group: BlockPool._place_cheapest)
        self.tables.add(group or '', ops_alignment, table_ops, site, NewSegment(0))  # placed once all are queued
        self._leave_the_inline_stream(ops_alignment, table_ops, table)
        return True

    def _why_kept_inline(
        self, group: Optional[str], word: Optional[Expr], ops_alignment: int, table_ops: int
    ) -> Optional[str]:
        """Return why the table the expansion reached stays inline, or None if it moves."""
        unsafe = self.guard.may_move(self.data.curr_address)
        if unsafe is not None:
            return unsafe
        if word is not None and self._is_runtime_word(word):
            return KEPT_RUNTIME_WORD
        return self.pool.defer(group, word, ops_alignment, table_ops)

    def _is_runtime_word(self, word: Expr) -> bool:
        """Is `word` one of the runtime's words? Those are declared before the code, so they resolve already (see
        reserved_below)."""
        try:
            return word.exact_eval(self.data.labels) < reserved_below(self.data.memory_width)
        except FlipJumpExprException:
            return False

    def _leave_the_inline_stream(self, ops_alignment: int, table_ops: int, table: str) -> None:
        """Start emitting the table just queued into table_ops, after its NewSegment, with addresses counted from
        0."""
        data = self.data
        op_bits = 2 * data.memory_width
        segment = self.tables.segments[-1]
        assert segment is not None
        # where the same program without the pool would be now, in ops, and where its `pad` would put the table
        plain = data.curr_address // op_bits + self.shift
        plain_start = plain + (-plain) % ops_alignment
        self.shift = plain_start + table_ops - data.curr_address // op_bits
        self.guard.table_moved(data.curr_address, table, self.shift * op_bits, plain_start * op_bits)
        self.table_starts.append((len(self.table_ops), len(self.table_ops.vals)))
        self.table_ops.append(segment)
        self.resume = data.curr_address, data.emit_target
        data.emit_target = self.table_ops
        data.curr_address = 0

    def add_label(self, label: str, code_position: CodePosition, offset: int) -> None:
        """Declare a label of the table being emitted, `offset` bits from its start: an offset now, an address once
        the table is placed."""
        self.label_positions[label] = code_position
        self.tables.add_label(label, offset // (2 * self.data.memory_width))

    def end(self) -> None:
        """Close the moved table (it must hold exactly the ops relocatable_call counted) and resume the inline
        stream."""
        data = self.data
        assert self.resume is not None and data.curr_address == self.tables.sizes[-1] * 2 * data.memory_width
        data.curr_address, data.emit_target = self.resume
        self.resume = None

    # ---- once the expansion is done ----

    def place_tables(self) -> None:
        """Have the pool place every queued table. Give each moved table its address and its labels, and append the
        moved tables to the result stream, by address."""
        data = self.data
        # the program without the moved tables ends where its code ends, or past the highest address an operand reaches
        program_end = max(self.code_end, data.curr_address, self.guard.reach + 1)
        addresses = self.pool.place_deferred(self.tables, data.labels, program_end)
        moved = [index for index, address in enumerate(addresses) if address]  # (the others stayed inline)
        op_bits = 2 * data.memory_width
        extents = [(addresses[index], addresses[index] + self.tables.sizes[index] * op_bits) for index in moved]
        self.guard.tables_placed(extents, self.pool.pool_start())
        for index, (start, end) in zip(moved, extents):
            self._settle(index, start, end)
        self._append_by_address(moved, addresses)
        self.tables = DeferredTables()  # release them

    def _settle(self, index: int, start: int, end: int) -> None:
        """Give the moved table `index` its place: its segment's address, and its labels' addresses."""
        data = self.data
        op_bits = 2 * data.memory_width
        segment = self.tables.segments[index]
        assert segment is not None
        segment.start_address, segment.wflip_start_address = start, end  # a table has no wflips
        for label, offset_ops in self.tables.labels_of(index):
            data.declare_label(label, start + offset_ops * op_bits, self.label_positions.get(label))

    def _append_by_address(self, moved: Sequence[int], addresses: Sequence[int]) -> None:
        """Append the moved tables (each a NewSegment and its ops) to the result stream, lowest address first."""
        data = self.data
        starts = self.table_starts + [(len(self.table_ops), len(self.table_ops.vals))]
        for order in sorted(range(len(moved)), key=lambda order: (addresses[moved[order]], order)):
            segment = self.tables.segments[moved[order]]
            assert segment is not None
            data.last_new_segment = segment
            data.result_ops.extend_slice(self.table_ops, *starts[order], *starts[order + 1])
        self.table_ops = OpStream(data.op_refs, data.labels, self.guard)  # release them
