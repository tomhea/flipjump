"""
the relocation proof: a table pool's program does what the program without the pool does, op for op.

It does not trust the pool. It expands the program twice, without and with the pool, with no label folded into a
number, and maps every op of the plain program to the pooled one: a moved op by the label before it in the pool
(labels keep their names), every other op in order. Then it checks what a wrong move or a wrong pin breaks:
    - every moved op is an op of a declared table (preprocessor.relocatable_call), and a declared table moves
      whole: all its ops by one distance, or none;
    - every operand that is an address - one label plus a constant - is the plain operand mapped, so an offset
      from an outside label into a moved table fails; so is a number in an address's place (a jump, a flip, a
      wflip's word or return) that is the address of a plain op - `$ + 15*dw` included. A wflip's value that is a
      number is a value, and is not compared. Of an operand computed from labels, every +/- sum (linear_parts) is
      compared: one label plus a constant as an address, a distance (`b - a`) as a number that must not change.
      An address computed on from one label (`(L | 0) + 15*dw`) is compared whole, as the plain address mapped;
      what the other sums compute beyond that (`(L >> 6) & 15` as a value) is listed as unchecked
      (RelocationProof.unchecked);
    - (BlockPool) the pins the assembler keeps (assembler.unpin) can hold, judged from the plain program and the
      assembled words with none of the assembler's rules or helpers: every pinned word has an op that rests at its
      value xor its base; every wflip of it flips an address it is armed with xor the base, wflip chains included;
      and every other flip of it - a number, a value computed from labels, an `a;b` flip - stays inside its window
      (below its base's alignment and below the widest table it is armed with: a declared table's size, an inline
      one's alignment where a `pad` aligned it, else nothing), and is none at all while the plain program has a
      table such a flip could arm below the window (an op a `pad` aligned, or a segment's start, that did not
      move); and no pointer runtime reads it.
"""

from __future__ import annotations

import bisect
import dataclasses
from array import array
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple, Union

from flipjump.assembler.assembler import labels_resolve, resolve_pinned, unpin
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.ops import FlipJump, Macro, MacroName, NewSegment, Padding, ReserveBits, WordFlip
from flipjump.assembler.inner_classes.op_stream import OpStream, linear_parts, linear_sum
from flipjump.assembler.inner_classes.ops import INITIAL_ARGS, INITIAL_LABELS_PREFIX, INITIAL_MACRO_NAME
from flipjump.assembler.preprocessor import OpsQueue, PreprocessorData, resolve_macro_aux
from flipjump.assembler.table_pool import BlockPool
from flipjump.fjm.fjm_consts import FJMVersion
from flipjump.fjm.fjm_writer import Writer
from flipjump.utils.constants import DEFAULT_MAX_MACRO_RECURSION_DEPTH
from flipjump.utils.exceptions import FlipJumpExprException

_Op = Union[FlipJump, WordFlip]


def resolve_macros(
    memory_width: int,
    macros: Dict[MacroName, Macro],
    *,
    table_pool: Optional[BlockPool] = None,
    relocatable_extents: Optional[List[Tuple[int, int]]] = None,
    pads: Optional[Dict[int, int]] = None,
) -> Tuple[OpsQueue, Dict[str, int]]:
    """preprocessor.resolve_macros with no label folded into a number (every operand keeps its expression), appending
    [start, end) of every inline relocatable table to `relocatable_extents`, and filling `pads` (see PreprocessorData)
    """
    data = PreprocessorData(
        memory_width, macros, DEFAULT_MAX_MACRO_RECURSION_DEPTH, save_debug_labels=False, table_pool=table_pool
    )
    data.result_ops.labels = data.pool_ops.labels = {}
    data.relocatable_extents = relocatable_extents
    if pads is not None:
        data.pads = pads
    resolve_macro_aux(data, INITIAL_MACRO_NAME, INITIAL_ARGS, INITIAL_LABELS_PREFIX)
    data.finish(False)
    return data.get_result_ops_and_labels()


@dataclasses.dataclass
class RelocationProof:
    """the proof's findings: every mismatch fails it; `unchecked` lists the operands it could not compare in full
    (what an operand computes beyond its +/- sums, from an address: `(L >> 6) & 15`)"""

    mismatches: List[str]
    unchecked: List[str]


def _ops(stream: OpStream, memory_width: int) -> Iterator[Tuple[int, Optional[_Op]]]:
    """(address, op) of every op in stream order, and (its start, None) where a segment starts"""
    op_bits = 2 * memory_width
    address = 0
    for op in stream.iter_ops():
        if isinstance(op, NewSegment):
            address = op.start_address
            yield address, None
        elif isinstance(op, ReserveBits):
            address = op.first_address_after_reserved
        elif isinstance(op, Padding):
            address += op.ops_count * op_bits
        else:
            yield address, op
            address += op_bits


def _operands(op: _Op) -> Tuple[Expr, ...]:
    if isinstance(op, FlipJump):
        return op.flip, op.jump
    return op.word_address, op.flip_value, op.return_address


def _is_one_address(operand: Expr) -> bool:
    """one label plus a constant: an address the pool may move"""
    linear = linear_sum(operand)
    return linear is not None and linear[0] is not None


class _Shifts:
    """runs of plain op addresses [start, end), each moved by one delta"""

    def __init__(self) -> None:
        self.starts = array('Q')
        self.ends = array('Q')
        self.deltas: List[int] = []

    def add(self, address: int, delta: int, op_bits: int) -> None:
        if self.starts and self.ends[-1] == address and self.deltas[-1] == delta:
            self.ends[-1] = address + op_bits
        else:
            self.starts.append(address)
            self.ends.append(address + op_bits)
            self.deltas.append(delta)

    def delta(self, address: int) -> Optional[int]:
        index = bisect.bisect_right(self.starts, address) - 1
        if index >= 0 and address < self.ends[index]:
            return self.deltas[index]
        return None


class _Image:
    """the words a Writer holds, read by bit address"""

    def __init__(self, writer: Writer, memory_width: int):
        self.memory_width = memory_width
        self.segments = sorted(writer.segments)
        self.starts = [segment[0] for segment in self.segments]
        self.data = writer.data

    def word(self, address: int) -> Optional[int]:
        index = address // self.memory_width
        position = bisect.bisect_right(self.starts, index) - 1
        if position < 0:
            return None
        start, length, data_start, data_length = self.segments[position]
        if index >= start + length:
            return None
        return self.data[data_start + index - start] if index - start < data_length else 0


def prove_relocation(
    memory_width: int, macros: Dict[MacroName, Macro], table_pool: BlockPool, *, max_reported: int = 20
) -> RelocationProof:
    """
    expand `macros` without a pool and with `table_pool` (a fresh one, as for any assembly), and compare the two
    programs op by op (see the module docstring). A refusal of the pool's own (a pin it cannot hold) is raised.
    @return: the proof: up to max_reported mismatches, each naming the plain op's address and the label before
    it, and the operands it could not compare in full
    """
    op_bits = 2 * memory_width
    extents: List[Tuple[int, int]] = []
    plain_pads: Dict[int, int] = {}
    plain_stream, plain_labels = resolve_macros(memory_width, macros, relocatable_extents=extents, pads=plain_pads)
    pooled_stream, pooled_labels = resolve_macros(memory_width, macros, table_pool=table_pool)
    pool_start = table_pool.pool_start()
    mismatches: List[str] = []

    def mismatch(text: str) -> None:
        if len(mismatches) < max_reported:
            mismatches.append(text)

    plain_by_address = sorted((address, name) for name, address in plain_labels.items())
    label_addresses = [address for address, _ in plain_by_address]

    def where(address: int) -> str:
        index = bisect.bisect_right(label_addresses, address) - 1
        if index < 0:
            return hex(address)
        label_address, name = plain_by_address[index]
        return f'{hex(address)} ({name} + {(address - label_address) // op_bits} ops)'

    extents.sort()
    extent_starts = [start for start, _ in extents]

    def extent_of(address: int) -> int:
        """the index of the declared table holding the plain op at `address`, or -1"""
        index = bisect.bisect_right(extent_starts, address) - 1
        return index if index >= 0 and address < extents[index][1] else -1

    # the moved ops, each at the plain address the pool label before it gives
    moved_labels = {name for name, address in pooled_labels.items() if address >= pool_start}
    pool_labels = sorted((pooled_labels[name], name) for name in moved_labels)
    pool_label_addresses = [address for address, _ in pool_labels]
    moved: Dict[int, Tuple[int, _Op]] = {}  # plain address -> (pooled address, op)
    for pooled_address, op_in_pool in _ops(pooled_stream, memory_width):
        if op_in_pool is None or pooled_address < pool_start:
            continue
        index = bisect.bisect_right(pool_label_addresses, pooled_address) - 1
        if index < 0 or pool_labels[index][1] not in plain_labels:
            mismatch(f'the pooled op at {hex(pooled_address)} follows no label of the plain program')
            continue
        label = pool_labels[index][1]
        plain_address = plain_labels[label] + pooled_address - pooled_labels[label]
        if extent_of(plain_address) < 0:
            mismatch(f'the plain op at {where(plain_address)} moved, and it is no op of a declared table')
        moved[plain_address] = (pooled_address, op_in_pool)

    def pooled_inline() -> Iterator[Tuple[int, _Op]]:
        for address, op in _ops(pooled_stream, memory_width):
            if op is not None and address < pool_start:
                yield address, op

    # the other ops pair up in order; a declared table moves whole
    shifts = _Shifts()
    inline = pooled_inline()
    extent_deltas: Dict[int, int] = {}
    moved_seen = 0
    for plain_address, op in _ops(plain_stream, memory_width):
        if op is None:
            continue
        if plain_address in moved:
            moved_seen += 1
            delta = moved[plain_address][0] - plain_address
        else:
            pooled = next(inline, None)
            if pooled is None:
                mismatch(f'the plain op at {where(plain_address)} has no op in the pooled program')
                break
            delta = pooled[0] - plain_address
        shifts.add(plain_address, delta, op_bits)
        extent = extent_of(plain_address)
        if extent >= 0 and extent_deltas.setdefault(extent, delta) != delta:
            mismatch(f'the declared table at {where(extents[extent][0])} moved in pieces')
    if moved_seen != len(moved):
        mismatch(f'{len(moved) - moved_seen} moved ops are no op of the plain program')
    extra = sum(1 for _ in inline)
    if extra:
        mismatch(f'the pooled program has {extra} inline ops more than the plain one')

    deltas_at: Dict[int, Set[int]] = {}
    for name, address in plain_labels.items():
        deltas_at.setdefault(address, set()).add(pooled_labels.get(name, address) - address)

    def mapped(value: int) -> Set[int]:
        """where a plain address is with the pool: its op's place, else the labels' at the address before it
        (a `reserve`d region, a label at a moved table's place - the table's or the code after it), else unmoved"""
        delta = shifts.delta(value - value % op_bits)
        if delta is not None:
            return {value + delta}
        index = bisect.bisect_right(label_addresses, value) - 1
        if index < 0:
            return {value}
        return {value + delta for delta in deltas_at[label_addresses[index]]}

    # the pins the assembler keeps: the subject, judged below with none of its rules
    pinned, _ = resolve_pinned(table_pool.pinned_words(), pooled_labels, exclude=table_pool.pin_exclude)
    raw = table_pool.reads_words_raw and table_pool.pin_exclude is None
    guard = table_pool.offset_guard
    if pinned and guard is not None:
        unpin(
            pooled_stream,
            pooled_labels,
            memory_width,
            pinned,
            {},
            guard.alignment,
            raw=raw,
            pads=guard.pads,
        )

    def arm_alignment(plain_address: int) -> int:
        """the bits the table a word is armed with (at `plain_address` without the pool) is aligned to: a declared
        table's size, an inline one's `pad`, else none"""
        extent = extent_of(plain_address)
        if extent >= 0:
            return (extents[extent][1] - extents[extent][0] - 1).bit_length()
        return plain_pads.get(plain_address, 0)

    def compare(plain_address: int, op: _Op, operand: Expr, actual: int, expected: Set[int]) -> None:
        if actual not in expected:
            mismatch(
                f'the plain op at {where(plain_address)} ({op}): {operand} should be '
                f'{" or ".join(hex(v) for v in sorted(expected))} with the pool, and is {hex(actual)}'
            )

    # every operand that is an address maps
    unchecked: List[str] = []
    inline = pooled_inline()
    pairs: List[Tuple[_Op, int, _Op]] = []  # (plain op, pooled address, pooled op) of the pin check
    jumped: Set[int] = set()  # the pinned words an op jumps through (see _check_pins)
    for plain_address, op in _ops(plain_stream, memory_width):
        if op is None:
            continue
        if plain_address in moved:
            pooled_address, pooled_op = moved[plain_address]
        else:
            next_inline = next(inline, None)
            if next_inline is None:
                break
            pooled_address, pooled_op = next_inline
        if type(pooled_op) is not type(op):
            mismatch(f'the plain op at {where(plain_address)} is {op}, the pooled one {pooled_op}')
            continue
        if pinned and _touches_a_pin(pooled_address, pooled_op, pooled_labels, pinned, memory_width):
            pairs.append((op, pooled_address, pooled_op))
        if pinned:  # an op that jumps to a pinned word's op, and is no arm of it: it jumps through the word
            target = _operands(pooled_op)[-1].exact_eval(pooled_labels) + memory_width
            if target in pinned and not (
                isinstance(op, WordFlip)
                and _is_one_address(op.flip_value)
                and pooled_op.word_address.exact_eval(pooled_labels) == target  # type: ignore[union-attr]
            ):
                jumped.add(target)
        for index, (plain_operand, pooled_operand) in enumerate(zip(_operands(op), _operands(pooled_op))):
            plain_operand, pooled_operand = _selected(plain_operand), _selected(pooled_operand)
            is_value = isinstance(op, WordFlip) and index == 1
            actual = pooled_operand.exact_eval(pooled_labels)
            if not plain_operand.all_unknown_labels():
                # a number: a value, or - where an address goes - the address of a plain op, mapped (`$ + 15*dw`)
                number = plain_operand.exact_eval(plain_labels)
                if not is_value and shifts.delta(number - number % op_bits) is not None:
                    compare(plain_address, op, plain_operand, actual, mapped(number))
                continue
            if _is_one_address(plain_operand):
                compare(plain_address, op, plain_operand, actual, mapped(plain_operand.exact_eval(plain_labels)))
                continue
            derived = False  # does it compute beyond its sums from an address (not only from distances)?
            for (coefficients, constant), (pooled_coefficients, pooled_constant) in zip(
                linear_parts(plain_operand), linear_parts(pooled_operand)
            ):
                part = constant + sum(c * plain_labels[label] for label, c in coefficients.items())
                pooled_part = pooled_constant + sum(
                    c * pooled_labels[label] for label, c in pooled_coefficients.items()
                )
                total = sum(coefficients.values())
                if total == 0:
                    compare(plain_address, op, plain_operand, pooled_part, {part})  # a distance: the same
                    continue
                if total == 1:
                    compare(plain_address, op, plain_operand, pooled_part, mapped(part))  # an address: mapped
                derived = True
            if derived and not _is_linear(plain_operand):
                if not is_value and _moves_with_its_label(plain_operand, plain_labels):
                    # an address computed on from one label, that moves with it: the plain address, mapped
                    compare(plain_address, op, plain_operand, actual, mapped(plain_operand.exact_eval(plain_labels)))
                else:
                    unchecked.append(f'{where(plain_address)} ({op}): {plain_operand}')

    if pinned:
        _check_pins(
            memory_width,
            pooled_stream,
            pooled_labels,
            plain_labels,
            pinned,
            raw,
            pool_start,
            pairs,
            mismatch,
            arm_alignment,
            sorted((address, bits) for address, bits in plain_pads.items() if address not in moved),  # they stay
            jumped,
            table_pool.wflips_at(),
        )
    return RelocationProof(mismatches, unchecked)


def _check_pins(
    memory_width: int,
    stream: OpStream,
    labels: Dict[str, int],
    plain_labels: Dict[str, int],
    pinned: Dict[int, int],
    raw: bool,
    pool_start: int,
    pairs: List[Tuple[_Op, int, _Op]],
    mismatch: Callable[[str], None],
    arm_alignment: Callable[[int], int],
    tables: List[Tuple[int, int]],
    jumped: Set[int],
    wflips_at: Optional[int],
) -> None:
    """the assembled words of the pinned program (see the module docstring): each pinned word rests at its value
    xor its base; each wflip of it flips an address it is armed with xor the base, or a value inside its window as
    it is; each `a;b` flip of it stays inside its window; none of these while a table it could arm (the plain
    program's `tables`: [(address, the bits a pad aligned it to)], of the ops that did not move) lies below its window
    whose address those flips and its rest value can make, while an op that is no arm of it jumps through it
    (`jumped`); and no pointer runtime reaches it"""
    if raw and pinned:
        mismatch(
            'the program uses the pointer runtime and no pin_exclude says which words it reaches, yet words are pinned'
        )
    writer = Writer(Path('relocation_proof.fjm'), memory_width, FJMVersion.NormalVersion)
    labels_resolve(
        stream,
        labels,
        memory_width,
        writer,
        save_wflip_labels=False,
        pinned=pinned,
        pool_start=pool_start,
        wflips_at=wflips_at,
    )
    image = _Image(writer, memory_width)
    w = memory_width
    # each word's window: below its base's alignment and the widest table it is armed with (none: no window)
    arms: Dict[int, List[int]] = {word: [] for word in pinned}
    for plain_op, _, pooled_op in pairs:
        if isinstance(pooled_op, WordFlip) and isinstance(plain_op, WordFlip) and _is_one_address(plain_op.flip_value):
            word = pooled_op.word_address.exact_eval(labels)
            if word in arms:
                plain_label = linear_sum(plain_op.flip_value)
                arms[word].append(arm_alignment(plain_labels[plain_label[0]]))  # type: ignore[index]
    windows = {
        word: (1 << min((base & -base).bit_length() - 1, max(arms[word], default=0))) - 1
        for word, base in pinned.items()
    }
    flipped: Dict[int, int] = {}  # word -> the bits its non-arm flips flip
    rests: Dict[int, int] = {}  # word -> the value it rests at, without the base
    rested = set()
    for plain_op, address, pooled_op in pairs:
        if isinstance(pooled_op, FlipJump):
            flip = image.word(address)
            if flip is not None and flip - flip % w in windows:
                flipped[flip - flip % w] = flipped.get(flip - flip % w, 0) | 1 << (flip % w)
                if not windows[flip - flip % w] >> (flip % w) & 1:
                    mismatch(
                        f'the op at {hex(address)} flips bit {flip % w} of the pinned word at {hex(flip - flip % w)}, '
                        f'outside its window'
                    )
            base = pinned.get(address + w)
            if base is None:
                continue
            rested.add(address + w)
            rests[address + w] = pooled_op.jump.exact_eval(labels)
            expected = rests[address + w] ^ base
            if image.word(address + w) != expected:
                mismatch(f'the pinned word at {hex(address + w)} does not rest at its value xor its base')
            continue
        assert isinstance(pooled_op, WordFlip) and isinstance(plain_op, WordFlip)
        word = pooled_op.word_address.exact_eval(labels)
        base = pinned.get(word)
        if base is None:
            continue
        value = pooled_op.flip_value.exact_eval(labels)
        if _is_one_address(plain_op.flip_value):
            value ^= base  # an address it is armed with (the operand compare mapped it)
        elif value & ~windows[word]:
            mismatch(
                f'the pinned word at {hex(word)} is wflipped by {plain_op.flip_value} = {hex(value)}, outside '
                f'its window and no address it is armed with'
            )
            continue
        else:
            flipped[word] = flipped.get(word, 0) | value
        chain = _flipped(image, address, pooled_op.return_address.exact_eval(labels), w)
        if chain is None or chain != _bits(value, word, w):
            mismatch(f'the wflip at {hex(address)} of the pinned word at {hex(word)} does not flip {hex(value)}')
    for word in sorted(set(pinned) - rested)[:3]:
        mismatch(f'the pinned word at {hex(word)} is no op: it cannot rest at its base')
    for word, bits in sorted(flipped.items()):
        if word not in jumped:
            continue  # nothing jumps through it but its arms: no flip of it is one
        rest = rests.get(word, 0)
        for table, aligned in tables:
            if table > windows[word]:
                break
            # the values the word can hold keep rest's bits that no flip toggles, and have no bit outside rest | bits
            high = table >> aligned << aligned
            fixed = (rest & ~bits) >> aligned << aligned
            if high & bits and not high & ~(rest | bits) and not fixed & ~high:
                mismatch(
                    f'the pinned word at {hex(word)} (resting at {hex(rest)}) is flipped by {hex(bits)} inside its '
                    f'window, which could arm the table at {hex(table)} of the plain program, below it'
                )
                break


def _selected(operand: Expr) -> Expr:
    """`c ? a : b` whose condition is a number is a or b, compared as that (hex.if_flags' entries, `(flags >> i) & 1 ?
    return + dbit : 0`, and the tables' `x ? label : other_label`)"""
    value = operand.value
    while isinstance(value, tuple) and value[0] == '?:' and isinstance(value[1][0].value, int):
        operand = value[1][1] if value[1][0].value else value[1][2]
        value = operand.value
    return operand


def _is_linear(operand: Expr) -> bool:
    """is `operand` one +/- sum (linear_parts) - of labels and numbers, with labels times numbers?"""
    value = operand.value
    if not isinstance(value, tuple):
        return True
    op, args = value
    if len(args) != 2 or op not in ('+', '-', '*') or not all(_is_linear(arg) for arg in args):
        return False
    return op != '*' or not args[0].all_unknown_labels() or not args[1].all_unknown_labels()


def _moves_with_its_label(operand: Expr, labels: Dict[str, int]) -> bool:
    """does `operand` name one label, and move as far as it does, when it moves far (by a power of two above both)?"""
    named = operand.all_unknown_labels()
    if len(named) != 1:
        return False
    label = next(iter(named))
    value = operand.exact_eval(labels)
    far = 1 << (max(labels[label], value).bit_length() + 1)
    try:
        return operand.exact_eval({label: labels[label] + far}) == value + far
    except FlipJumpExprException:
        return False


def _touches_a_pin(address: int, op: _Op, labels: Dict[str, int], pinned: Dict[int, int], memory_width: int) -> bool:
    """is the op a pinned word's own op, a wflip of a pinned word, or a flip of a pinned word's bit?"""
    if isinstance(op, FlipJump):
        flip = op.flip.exact_eval(labels)
        return address + memory_width in pinned or flip - flip % memory_width in pinned
    return op.word_address.exact_eval(labels) in pinned


def _bits(value: int, word: int, memory_width: int) -> Set[int]:
    return {word + bit for bit in range(memory_width) if value >> bit & 1}


def _flipped(image: _Image, address: int, return_address: int, memory_width: int) -> Optional[Set[int]]:
    """the bits the wflip chain at `address` flips, followed until it jumps to `return_address` (None: it does not)"""
    flipped: Set[int] = set()
    for _ in range(memory_width + 1):
        flip, jump = image.word(address), image.word(address + memory_width)
        if flip is None or jump is None:
            return None
        if flip:
            flipped ^= {flip}
        if jump == return_address:
            return flipped
        address = jump
    return None
