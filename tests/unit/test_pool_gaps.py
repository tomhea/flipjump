"""
unit-tests for the table pool on crafted programs: every one of them (tests/unit/pool_gaps) either runs with a pool
exactly as without one - through the fallbacks it counts where it needs them (a table kept inline, a word left
unpinned) - or is refused with the reason; and the pool's smaller promises (a pool lays out one assembly, a wflip
area that leaves the memory says so, fj --pool-tables' pool sits right above the program).
"""

import random
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from flipjump import assemble, assemble_run_according_to_cmd_line_args, flipjump_cli, flipjump_quickstart
from flipjump.fjm.fjm_reader import read_fjm_header
from flipjump.interpreter.run_defaults import FLAT_MEMORY_AUTO_MAX_WORDS
from flipjump.assembler import pins
from flipjump.assembler.inner_classes import op_stream
from flipjump.assembler.fj_parser import parse_macro_tree
from tests.unit import relocation_proof
from tests.unit.relocation_proof import prove_relocation
from flipjump.assembler.inner_classes.offset_guard import OffsetGuard
from flipjump.assembler.inner_classes.table_pool import BlockPool
from flipjump.utils.functions import get_file_tuples, load_debugging_labels
from flipjump.utils.exceptions import FlipJumpException
from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from tests.unit.pool_fuzzer import pools, run
from tests.unit.unit_utils import (
    PROGRAMS_DIR,
    build_and_run,
    every_table_moves,
    kept_by_the_guard,
    no_refusals,
    relocatable,
)

GAPS = Path(__file__).parent / 'pool_gaps'
W = 32
OP_BITS = 2 * W
POOL_BASE = 1 << 26


def _pool(kind: str, memory_width: int, source: Path, tmp_path: Path) -> BlockPool:
    """the fuzzer's pool `kind`: 'cli' (fj --pool-tables), 'half' (a BlockPool based at half the memory)"""
    return pools(memory_width, random.Random(0))[kind]()  # nosec B311 - these pools draw nothing


SAME = None
GUARD_C06 = (
    r'the operand `L` of the op at 0x480 \(in the top level\) names L, declared before the place of the table T, '
    r'which moved out of 0x400'
)
GUARD_C07_PLUS = (
    r'the operand `T \+ 64` of the op at 0x80 \(after the label f1:l11:stl.startup---code_start\) leaves the moved '
    r'table T at 0x[0-9a-f]+: an offset from a moved table\'s label must stay inside it'
)
GUARD_C07_COMPOUND = (
    r'the operand `\(\(T \+ 64\) \| 0\)` of the op at 0x80 \(after the label f1:l11:stl.startup---code_start\) '
    r'leaves the moved table T at 0x[0-9a-f]+: an offset from a moved table\'s label must stay inside it'
)
GUARD_C25 = r'the operand `f1:l19:go\(1\)---T \+ 64` .* leaves the moved table f1:l19:go\(1\)---T at 0x[0-9a-f]+'
GUARD_C29 = (
    r'the operand `\(\(\(\(b - a\) >> 6\) & 15\) \* 64\)` of the op at 0x140 \(in the top level\) takes the distance '
    r'across the place of the table f1:l8:hex.xor\(2\)---s16:l12:hex.exact_xor\(5\)---switch, which moved out of 0x100'
)
GUARD_C35 = (
    r'the operand `L \+ 448` of the op at 0x100 \(in the top level\) reaches across the `pad 8` at 0x140, which pads '
    r'to another op once a table before it moved out'
)
GUARD_C37 = (
    r'the operand `\(\(end - start\) / 64\)`, the count of the rep at file .*c37_rep_count_across_table.fj \(line 7\) '
    r'\(in the top level\), takes the distance across the place of the table '
    r'f1:l5:hex.xor\(2\)---s16:l12:hex.exact_xor\(5\)---switch'
)
GUARD_C38 = (
    r'the operand .*\(S - M\).* takes the distance across the start of the segment at 0x4000, which stays where it '
    r'is\.'
)
REACHES = 'an operand reaches over its place'
JUMPED = 'an op jumps through it without arming it'
NO_TABLE = 'armed with an address that is no table'
GUARDED = [
    'c06_label_before_pad',
    'c07a_reach_in_plus',
    'c07b_reach_in_compound',
    'c07c_reach_in_compound_labelled',
    'c07d_reach_in_plus_labelled',
]
# (program, memory width): {pool: SAME, or the words of its refusal}
CASES: Dict[Tuple[str, int], Dict[str, Optional[str]]] = {
    ('c01_raw_read_bit_ptr', 32): {'cli': SAME},  # un-pinned
    ('c16_runtime_write_upper_half', 32): {'cli': SAME},  # un-pinned
    ('c02_pinned_floor', 32): {'cli': SAME},
    ('c03_pinned_in_reserve', 32): {'cli': SAME},  # un-pinned
    ('c08_retarget_xor', 32): {'cli': SAME},  # un-pinned
    ('c08b_retarget_xor_labelled', 32): {'cli': SAME},  # un-pinned
    ('c05_label_offset_out_of_table', 32): {'cli': 'leaves the table through'},
    ('c05b_trailing_label', 32): {'cli': 'declares a label after its last op'},
    ('c09_pad3', 32): {'cli': 'power-of-two pad'},
    # a pool at half the memory is reached; fj --pool-tables's is based above the program
    ('c10_wflip_area_crosses_pool', 16): {'cli': SAME, 'half': 'its wflip area reach'},
    ('c11_segment_above_pool', 16): {'cli': SAME, 'half': 'inside the table pool'},
    ('c14_accidental_namespace', 32): {'cli': 'needs a `pad`'},  # a reserved namespace
    ('c13_label_between_pad_and_call', 32): {'cli': SAME},
    ('c15_nested_namespaces', 32): {'cli': SAME},
    ('c19_no_label_after_call', 32): {'cli': SAME},
    # an operand outside a moved table that names a label and reaches the table's place (OffsetGuard)
    ('c06_label_before_pad', 32): {'cli': GUARD_C06},
    ('c07a_reach_in_plus', 32): {'cli': GUARD_C07_PLUS},
    ('c07b_reach_in_compound', 32): {'cli': GUARD_C07_COMPOUND},
    ('c07c_reach_in_compound_labelled', 32): {'cli': GUARD_C07_COMPOUND},
    ('c07d_reach_in_plus_labelled', 32): {'cli': GUARD_C07_PLUS},
    # operands that reach over a moved table's place, and the words a pool can or cannot pin
    ('c20_dollar_across_table', 32): {'cli': SAME},  # kept inline
    ('c21_inline_compound_across_table', 32): {'cli': SAME},  # kept inline
    ('c22_label_digit_into_pinned', 32): {'cli': SAME},  # a value, stays pinned
    ('c23_numeric_address_arm', 32): {'cli': SAME},  # un-pinned
    ('c24_constant_value', 32): {'cli': SAME},
    ('c25_arg_reach_out', 32): {'cli': GUARD_C25},
    ('c26_pointer_write_only', 32): {'cli': SAME},  # un-pinned
    ('c27_bitwise_arm', 32): {'cli': SAME},  # un-pinned
    ('c28_segment_back', 32): {'cli': SAME},
    ('c29_label_difference_value', 32): {'cli': GUARD_C29},
    ('c30_count_bits_stays_pinned', 32): {'cli': SAME},  # pinned
    # arms below or above a moved table's window, and offsets across a realigned pad
    ('c32_numeric_arm_below_window', 32): {'cli': SAME},  # un-pinned
    ('c33_bitwise_arm_below_window', 32): {'cli': SAME},  # un-pinned
    ('c34_numeric_arm_wide_window', 32): {'cli': SAME},  # un-pinned
    ('c35_offset_across_realigned_pad', 32): {'cli': GUARD_C35},
    ('c36_opaque_label_plus_offset', 32): {'cli': SAME},  # kept inline
    # distances across a moved table's place: a rep's count, and into another segment
    ('c37_rep_count_across_table', 32): {'cli': GUARD_C37},
    ('c38_distance_into_numeric_segment', 32): {'cli': GUARD_C38},
    ('c39_distance_into_earlier_segment', 32): {'cli': GUARD_C38},
    ('c40_unmoved_distance_across_segments', 32): {'cli': SAME},
}
# (program, pool): (the tables the pool kept inline, the words it left unpinned), by reason - every other SAME
# program needs neither
POINTERS: Tuple[Dict[str, int], Dict[str, int]] = ({}, {'the pointer runtime': 1})
FALLBACKS: Dict[Tuple[str, str], Tuple[Dict[str, int], Dict[str, int]]] = {
    ('c01_raw_read_bit_ptr', 'cli'): POINTERS,
    ('c16_runtime_write_upper_half', 'cli'): POINTERS,
    ('c03_pinned_in_reserve', 'cli'): ({}, {'no op of its own': 1}),
    ('c08_retarget_xor', 'cli'): ({}, {'armed with a value computed from labels': 1}),
    ('c08b_retarget_xor_labelled', 'cli'): ({}, {'armed with a value computed from labels': 1}),
    ('c20_dollar_across_table', 'cli'): ({REACHES: 1}, {}),
    ('c21_inline_compound_across_table', 'cli'): ({REACHES: 1}, {}),
    ('c23_numeric_address_arm', 'cli'): ({}, {'armed with a number': 1}),
    ('c26_pointer_write_only', 'cli'): POINTERS,
    ('c27_bitwise_arm', 'cli'): ({}, {'a bit flip above its window': 1}),
    ('c32_numeric_arm_below_window', 'cli'): ({}, {JUMPED: 1}),
    ('c33_bitwise_arm_below_window', 'cli'): ({}, {JUMPED: 1}),
    ('c34_numeric_arm_wide_window', 'cli'): ({}, {NO_TABLE: 1}),
    ('c36_opaque_label_plus_offset', 'cli'): ({REACHES: 1}, {}),
}
PARAMS = [(name, width, pool, refusal) for (name, width), pools in CASES.items() for pool, refusal in pools.items()]


@pytest.mark.parametrize('name, memory_width, pool, refusal', PARAMS, ids=[f'{p[0]}-{p[2]}' for p in PARAMS])
def test_a_crafted_program_runs_the_same_with_the_pool_or_is_refused(
    tmp_path: Path, name: str, memory_width: int, pool: str, refusal: Optional[str]
) -> None:
    source = GAPS / f'{name}.fj'
    plain = tmp_path / 'plain.fjm'
    assemble([source], plain, memory_width=memory_width, print_time=False)
    pooled = tmp_path / 'pooled.fjm'
    if refusal is not None:
        with pytest.raises(FlipJumpException, match=refusal):
            assemble(
                [source],
                pooled,
                memory_width=memory_width,
                print_time=False,
                table_pool=_pool(pool, memory_width, source, tmp_path),
            )
        return
    table_pool = _pool(pool, memory_width, source, tmp_path)
    assemble([source], pooled, memory_width=memory_width, print_time=False, table_pool=table_pool)
    expected = run(plain, 30)
    assert expected[1] != 'TIMEOUT' and run(pooled, 30) == expected  # two timeouts are not the same run
    assert (kept_by_the_guard(table_pool), table_pool.unpins) == FALLBACKS.get((name, pool), ({}, {}))
    assert table_pool.moved or table_pool.kept_inline


@pytest.mark.parametrize('pool', ['cli', 'half'])
@pytest.mark.parametrize('name', GUARDED)
def test_without_the_offset_guard_a_reach_into_a_moved_table_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, pool: str
) -> None:
    # the control: each refusal above prevents a real miscompile, which this run shows
    no_refusals(monkeypatch)
    source = GAPS / f'{name}.fj'
    plain = tmp_path / 'plain.fjm'
    assemble([source], plain, memory_width=W, print_time=False)
    pooled = tmp_path / 'pooled.fjm'
    table_pool = _pool(pool, W, source, tmp_path)
    assemble([source], pooled, memory_width=W, print_time=False, table_pool=table_pool)
    assert table_pool.moved and run(pooled, 30) != run(plain, 30)


def _differs(source: Path, tmp_path: Path, pool: str) -> bool:
    """does `source` run differently with the pool than without it?"""
    plain = tmp_path / 'plain.fjm'
    assemble([source], plain, memory_width=W, print_time=False)
    pooled = tmp_path / 'pooled.fjm'
    assemble([source], pooled, memory_width=W, print_time=False, table_pool=_pool(pool, W, source, tmp_path))
    expected = run(plain, 30)
    assert expected[1] != 'TIMEOUT'  # two timeouts would be no difference, and no sameness either
    return run(pooled, 30) != expected


@pytest.mark.parametrize('pool', ['cli', 'half'])
@pytest.mark.parametrize('name', ['c20_dollar_across_table', 'c21_inline_compound_across_table'])
def test_without_keeping_its_table_inline_a_reach_over_its_place_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, pool: str
) -> None:
    # the control: the table moves, and nothing later refuses the operand
    every_table_moves(monkeypatch)
    no_refusals(monkeypatch)
    assert _differs(GAPS / f'{name}.fj', tmp_path, pool)


@pytest.mark.parametrize('pool', ['cli', 'half'])
def test_without_the_distance_check_a_label_difference_across_a_moved_table_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: str
) -> None:
    # the control: `b - a` across a moved table's place is a different number
    no_refusals(monkeypatch)
    assert _differs(GAPS / 'c29_label_difference_value.fj', tmp_path, pool)


@pytest.mark.parametrize('pool', ['cli', 'half'])
@pytest.mark.parametrize('name', ['c23_numeric_address_arm', 'c27_bitwise_arm'])
def test_without_unpinning_a_pin_that_cannot_hold_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, pool: str
) -> None:
    # the control: the word stays pinned. (c08's `T1 ^ T2` would hold pinned - the bases cancel - but a
    # value computed from labels is un-pinned whatever it computes)
    monkeypatch.setattr(pins, 'unpin', lambda *args, **kwargs: None)
    assert _differs(GAPS / f'{name}.fj', tmp_path, pool)


def test_a_program_that_reaches_the_pool_says_so(tmp_path: Path) -> None:
    # the code passes pool_base even with its tables moved out: the program's own reach, not an operand's
    with pytest.raises(
        FlipJumpException, match='the program reached 0x[0-9a-f]+, which is inside the table pool at 0x1000'
    ):
        assemble(
            [GAPS / 'c31_program_reaches_the_pool.fj'],
            tmp_path / 'a.fjm',
            memory_width=16,
            print_time=False,
            table_pool=BlockPool(16, 1 << 12),
        )


def test_count_bits_keeps_its_pin(tmp_path: Path) -> None:
    # hex.count_bits flips dst+dbit+4/5 inside the 64-op table it armed dst with: a move the base does not change.
    # its own 16-op table lies at op 16, below that window, where the same flip of an UNARMED dst would point -
    # but nothing jumps through dst except its arms, so no flip of it is one: dst stays pinned. With an op that jumps
    # to dst's op (as C32's arm by a number does), it would not
    source = GAPS / 'c30_count_bits_stays_pinned.fj'
    pool = BlockPool(W)
    assemble([source], tmp_path / 'pooled.fjm', memory_width=W, print_time=False, table_pool=pool)
    assert pool.pinned_words() and pool.unpins == {}
    # count_switch, behind a `pad 16`, a value dst's flips can make
    assert pool.offset_guard is not None and pool.offset_guard.pads[0x400] == 10


_real_alignment = vars(OffsetGuard)['alignment']  # the original, called by the patch


def _address_alignment(self: OffsetGuard, address: int) -> int:
    """OffsetGuard.alignment without its rule: an inline table's alignment taken as its address's own"""
    if self.table_at(address) is not None:
        alignment: int = _real_alignment(self, address)
        return alignment
    return (address & -address).bit_length() - 1 if address else 1 << 30


def _keep_every_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """the control: the assembler drops no pin (and neither does the relocation proof's copy of it)"""
    monkeypatch.setattr(pins, 'unpin', lambda *args, **kwargs: None)
    monkeypatch.setattr(relocation_proof, 'unpin', lambda *args, **kwargs: None)


def _moved_unrefused(monkeypatch: pytest.MonkeyPatch) -> None:
    """the control: C36's table moves, and labels_resolve lets the operand that reaches over its place through"""
    every_table_moves(monkeypatch)
    no_refusals(monkeypatch)


# each program's control: C32 .. C34's pins kept, C36's table moved, the operands of C35 and C37 .. C39 let through
# (a distance across a realigned pad, a rep's count the expansion evaluates, a distance across a segment's start)
DISABLED: Dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    'c32_numeric_arm_below_window': _keep_every_pin,
    'c33_bitwise_arm_below_window': _keep_every_pin,
    'c34_numeric_arm_wide_window': _keep_every_pin,
    'c35_offset_across_realigned_pad': no_refusals,
    'c36_opaque_label_plus_offset': _moved_unrefused,
    'c37_rep_count_across_table': no_refusals,
    'c38_distance_into_numeric_segment': no_refusals,
    'c39_distance_into_earlier_segment': no_refusals,
}


def _disable(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    DISABLED[name](monkeypatch)


# C32's and C33's miscompiles never halt: their pin-keeping pools run into the 30 s timeout (with --slow)
HIDDEN_REACH_PARAMS = [
    pytest.param(name, pool, marks=[pytest.mark.slow] if name < 'c34' else [])
    for name in sorted(DISABLED)
    for pool in ('cli', 'half')
]


@pytest.mark.parametrize('name, pool', HIDDEN_REACH_PARAMS)
def test_without_its_check_a_program_with_a_hidden_reach_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, pool: str
) -> None:
    # the controls of C32 .. C39: each check above prevents a real miscompile, which this run shows
    _disable(monkeypatch, name)
    assert _differs(GAPS / f'{name}.fj', tmp_path, pool)


def test_a_wide_inline_arm_alone_does_not_widen_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # C34 arms its word with T2 too, a label at a segment's start (0x8000): that address's own alignment (15 bits) is
    # no table's, so the arm is no arm and the word is unpinned. Taken for a table's, it would widen the word's window
    # over the numeric arm (0x4000), and - with the jump through the unarmed word let through too - miscompile.
    source = GAPS / 'c34_numeric_arm_wide_window.fj'
    pool = _pool('cli', W, source, tmp_path)
    assemble([source], tmp_path / 'pooled.fjm', memory_width=W, print_time=False, table_pool=pool)
    assert pool.unpins == {NO_TABLE: 1}
    assert not _differs(source, tmp_path, 'cli')
    monkeypatch.setattr(OffsetGuard, 'alignment', _address_alignment)
    monkeypatch.setattr(pins.WordUses, '_wflip', _wflip_never_jumps)
    assert _differs(source, tmp_path, 'cli')


def _wflip_never_jumps(self: pins.WordUses, word: int, value: int, value_kind: int, return_address: int) -> None:
    """the control: WordUses._wflip, but no wflip counts as a jump through a word without arming it"""
    if word in self.arms and value_kind != op_stream.VALUE_ADDRESS:
        into = self.numbers if value_kind == op_stream.VALUE_NUMBER else self.computed
        into[word] = into.get(word, 0) | value


def _proof(name: str) -> List[str]:
    files = get_file_tuples([str(GAPS / f'{name}.fj')], no_stl=False)
    return prove_relocation(W, parse_macro_tree(files, W, True), BlockPool(W)).mismatches


@pytest.mark.parametrize(
    'name', ['c32_numeric_arm_below_window', 'c33_bitwise_arm_below_window', 'c34_numeric_arm_wide_window']
)
def test_the_proof_judges_the_pins_on_its_own(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    # the relocation proof judges a kept pin from the plain program and the image, with none of the assembler's
    # rules: with the assembler's check disabled, the pin it keeps fails the proof
    assert _proof(name) == []
    _disable(monkeypatch, name)
    mismatches = _proof(name)
    assert any('the pinned word at' in mismatch for mismatch in mismatches), mismatches


# `before + 2*dw` names the table's third entry ('B') only while the table is where it was declared; the
# OffsetGuard sees each in a different place. (The wflip after the call, never run, gives the table a group.)
_TABLE_AND_PRINTS = """
before:
    pad 1
    tbl.__relocatable__.table tbl_start, print_a, print_b
    wflip x+w, tbl_start
x:  ;0
print_a:
    stl.output 'A'
    stl.loop
print_b:
    stl.output 'B'
    stl.loop
""" + relocatable('tbl', 'start, a, b', 'start', '    ;a\n    ;a\n    ;b\n    ;a\n')
OUTSIDE_OFFSET_PROGRAMS = {
    # the label is declared after the operand: labels_resolve checks it
    'deferred': ('stl.startup\n    ;before + 2*dw\n' + _TABLE_AND_PRINTS, 'reaches across the place of the table'),
    # the label is known when the operand is emitted, after the table moved: the preprocessor checks it
    'folded': ('stl.startup\n    ;go\n' + _TABLE_AND_PRINTS + 'go:\n    ;before + 2*dw\n', 'reaches across'),
}
# the label is known, and the table moves after the operand: it stays inline
PENDING = 'stl.startup\nhere:\n    ;here + 3*dw\n' + _TABLE_AND_PRINTS


@pytest.mark.parametrize('program', sorted(OUTSIDE_OFFSET_PROGRAMS))
def test_an_offset_from_outside_into_a_moved_table_is_refused_wherever_it_is_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str
) -> None:
    source, refusal = OUTSIDE_OFFSET_PROGRAMS[program]
    assert build_and_run(source, tmp_path, None, name='plain')[0] == b'B'
    path = tmp_path / 'p.fj'
    path.write_text(source)
    with pytest.raises(FlipJumpException, match=refusal):
        build_and_run(source, tmp_path, _pool('half', W, path, tmp_path), name='pooled')
    # the control: without the guard's refusal it is a miscompile
    no_refusals(monkeypatch)
    table_pool = _pool('half', W, path, tmp_path)
    assert build_and_run(source, tmp_path, table_pool, name='unguarded')[0] != b'B' and table_pool.moved


def test_a_table_an_earlier_offset_reaches_over_stays_inline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pool = BlockPool(W, 1 << (W - 1))
    assert build_and_run(PENDING, tmp_path, pool, name='pooled')[0] == b'B'
    assert kept_by_the_guard(pool) == {REACHES: 1} and pool.moved == 0
    # the control: moved, the program miscompiles
    every_table_moves(monkeypatch)
    assert build_and_run(PENDING, tmp_path, BlockPool(W, 1 << (W - 1)), name='moved')[0] != b'B'


_XOR = """
stl.startup
    hex.xor x, y
    hex.print_as_digit x, 0
    stl.loop
x: hex.hex 3
y: hex.hex 5
"""


def _source(tmp_path: Path, text: str = _XOR) -> Path:
    path = tmp_path / 'p.fj'
    path.write_text(text)
    return path


def test_a_pool_lays_out_one_assembly(tmp_path: Path) -> None:
    for pool in (BlockPool(W, POOL_BASE), BlockPool(W)):
        assemble([_source(tmp_path)], tmp_path / 'a.fjm', memory_width=W, print_time=False, table_pool=pool)
        with pytest.raises(FlipJumpException, match='already laid out'):
            assemble([_source(tmp_path)], tmp_path / 'b.fjm', memory_width=W, print_time=False, table_pool=pool)


def test_a_w64_pooled_program_runs_flat(tmp_path: Path) -> None:
    # fj --pool-tables bases the pool right above the program, so a flat-memory run (--flat-memory,
    # compiled in: the window is required) of a w=64 program maps a small window, and prints what the plain one does
    for name, extra in (('plain', []), ('pooled', ['--pool-tables'])):
        args = ['--asm', '-s', '-o', str(tmp_path / f'{name}.fjm'), '-w', '64', '--flat-memory', *extra]
        assemble_run_according_to_cmd_line_args(cmd_line_args=[*args, str(_source(tmp_path))])
    plain, pooled = read_fjm_header(tmp_path / 'plain.fjm'), read_fjm_header(tmp_path / 'pooled.fjm')
    assert pooled.span_words <= 2 * plain.span_words <= FLAT_MEMORY_AUTO_MAX_WORDS
    outputs = []
    for name in ('plain', 'pooled'):
        io_device = FixedIO(b'')
        flipjump_quickstart.run(tmp_path / f'{name}.fjm', io_device=io_device, print_time=False, flat_memory=True)
        outputs.append(io_device.get_output())
    assert outputs[0] == outputs[1] == b'6'


def test_a_wflip_area_past_the_memory_says_so(tmp_path: Path) -> None:
    # at w=16 this program's wflip chains need more than the 2^16 bits of memory
    wflips = ''.join(f'    wflip z+w, {(i * 4999 + 0x0F0F) & 0xFFFE}\n' for i in range(600))
    source = _source(tmp_path, f'stl.startup\n{wflips}    stl.loop\nz: hex.hex\n')
    with pytest.raises(FlipJumpException, match='Not enough space with the 16-bits memory-width: the wflip area'):
        assemble([source], tmp_path / 'a.fjm', memory_width=16, print_time=False)


def _ops(fjm: Path, stdin: bytes = b'') -> Tuple[bytes, int]:
    """(the output, the ops run) of a program that halts"""
    io_device = FixedIO(stdin)
    statistics = fjm_run.run(fjm, io_device=io_device, print_time=False)
    return io_device.get_output(), statistics.op_counter


def test_a_program_that_reads_words_raw_runs_fewer_ops_with_the_pool(tmp_path: Path) -> None:
    # the pointer runtime, with no pin_all_but, unpins every word: fj --pool-tables places each table at a cheap
    # address instead (BlockPool._place_cheapest) - no block, no pinned word, and fewer ops than without the pool
    # (pointer_setters: 15137 plain, 11431 cheapest-first)
    source = [PROGRAMS_DIR / 'hexlib_tests' / 'basics2' / 'pointer_setters.fj']
    plain_fjd = tmp_path / 'plain.fjd'
    assemble(source, tmp_path / 'plain.fjm', memory_width=64, print_time=False, debugging_file_path=plain_fjd)
    plain = _ops(tmp_path / 'plain.fjm')
    pool = BlockPool(64)
    assemble(source, tmp_path / 'pooled.fjm', memory_width=64, print_time=False, table_pool=pool)
    pooled = _ops(tmp_path / 'pooled.fjm')
    assert pool.reads_words_raw and pool.moved and not pool.blocks and pool.pinned_words() == {}
    assert list(pool.unpins) == ['the pointer runtime'] and pooled[0] == plain[0] and pooled[1] < plain[1]
    # the control: in blocks with no word pinned (every cell named raw), the same tables run more ops. (The debugging
    # file's macro-start labels exist only in a build that writes one: here they are missing labels, a warning.)
    blocked_pool = BlockPool(64, pin_all_but=list(load_debugging_labels(plain_fjd)))
    assemble(
        source,
        tmp_path / 'blocked.fjm',
        memory_width=64,
        print_time=False,
        table_pool=blocked_pool,
        warning_as_errors=False,
    )
    blocked = _ops(tmp_path / 'blocked.fjm')
    assert blocked_pool.blocks and blocked[0] == plain[0] and blocked[1] > pooled[1]


# the flat spec: fj --pool-tables's pool (BlockPool(w): AFTER_PROGRAM) sits at the first power of two above the
# program
_INIT_FIRST = """
stl.startup
    ;code
hex.init
code:
    hex.xor x, y
    hex.xor y, x
    hex.or x, y
    hex.and y, x
    hex.print_as_digit x, 0
    hex.print_as_digit y, 0
    stl.loop
x: hex.hex 3
y: hex.hex 5
"""


def _flat(tmp_path: Path, text: str, memory_width: int) -> Tuple[BlockPool, Tuple[str, str], int]:
    """(the pool, the run, the .fjm's span in words) of `text` assembled with BlockPool(memory_width)"""
    source = _source(tmp_path, text)
    table_pool = BlockPool(memory_width)
    assemble([source], tmp_path / 'flat.fjm', memory_width=memory_width, print_time=False, table_pool=table_pool)
    return table_pool, run(tmp_path / 'flat.fjm', 30), read_fjm_header(tmp_path / 'flat.fjm').span_words


def _plain(tmp_path: Path, text: str, memory_width: int) -> Tuple[Tuple[str, str], int]:
    """(the run, the span in words) of `text` without a pool"""
    assemble([_source(tmp_path, text)], tmp_path / 'plain.fjm', memory_width=memory_width, print_time=False)
    return run(tmp_path / 'plain.fjm', 30), read_fjm_header(tmp_path / 'plain.fjm').span_words


@pytest.mark.parametrize('memory_width', [16, 32, 64])
def test_the_default_flat_layout_is_compact(tmp_path: Path, memory_width: int) -> None:
    # the pool's base is the first power of two above the program's end (one set bit), the wflip areas after it
    text = _XOR if memory_width == 16 else _INIT_FIRST
    plain, plain_span = _plain(tmp_path, text, memory_width)
    pool, pooled, span = _flat(tmp_path, text, memory_width)
    base = pool.pool_start()
    assert pool.moved and pooled == plain and base & (base - 1) == 0 and pool.wflips_at() == pool.end
    assert base < 2 * plain_span * memory_width and span < 2 * plain_span


def _cli_pools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *flags: str) -> List[BlockPool]:
    """the pools fj --pool-tables <flags> makes"""
    made: List[BlockPool] = []

    class Recorded(BlockPool):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(flipjump_cli, 'BlockPool', Recorded)
    args = ['--asm', '-s', '-o', str(tmp_path / 'a.fjm'), '-w', '32', '--pool-tables', '--flat-max-words', '4096']
    assemble_run_according_to_cmd_line_args(cmd_line_args=[*args, *flags, str(_source(tmp_path))])
    return made


def test_the_cli_pool_gets_only_its_intended_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # G4: fj --pool-tables alone: the pool goes after the program, with no tuning knob
    (pool,) = _cli_pools(tmp_path, monkeypatch)
    assert pool.after_program and pool.pool_end is None and pool.pin_all_but is None
    assert (pool.max_slot_ops, pool.spread_factor, pool.spread_min_tables) == (32, 1, 256)
    assert not (pool.pin_incomplete_groups or pool.heat) and pool.census and not pool.alias


def test_the_cli_pool_flags_reach_the_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flags = ['--pool-base', '0x40000000', '--pool-end', '0x80000000', '--pool-max-slot-ops', '64']
    flags += ['--pool-spread-factor', '2', '--pool-spread-min-tables', '4', '--pool-pin-incomplete-groups']
    (pool,) = _cli_pools(tmp_path, monkeypatch, *flags)
    assert not pool.after_program and (pool.pool_base, pool.pool_end) == (0x40000000, 0x80000000)
    assert (pool.max_slot_ops, pool.spread_factor, pool.spread_min_tables, pool.pin_incomplete_groups) == (
        64,
        2,
        4,
        True,
    )


def test_a_flat_pool_that_places_no_table_is_no_pool(tmp_path: Path) -> None:
    # hello_world_with_str moves no table: its base above the code would sit inside its wflip area, so a pool with
    # no table has no start - the binary is the plain one, byte for byte
    source = [PROGRAMS_DIR / 'print_tests' / 'hello_world_with_str.fj']
    assemble(source, tmp_path / 'plain.fjm', memory_width=64, print_time=False)
    pool = BlockPool(64)
    assemble(source, tmp_path / 'pooled.fjm', memory_width=64, print_time=False, table_pool=pool)
    assert pool.end == 0 and pool.pool_start() == 1 << 64
    assert (tmp_path / 'pooled.fjm').read_bytes() == (tmp_path / 'plain.fjm').read_bytes()


def test_a_program_that_leaves_its_tables_no_room_is_refused(tmp_path: Path) -> None:
    # prime_sieve's code comes first, its tables leave the stream, and then a reserve fills the upper half of the
    # memory: no room for them above the program
    source = [PROGRAMS_DIR / 'prime_sieve.fj']
    with pytest.raises(FlipJumpException, match='has no room for its tables'):
        assemble(source, tmp_path / 'pooled.fjm', memory_width=64, print_time=False, table_pool=BlockPool(64))


# --- the D cases: programs that reviews of the pool crafted, most of them without the stl ---

# program: (memory width, with the stl, SAME or the words of its refusal)
# what five of the programs share, assembled before each of them
D_SCAFFOLD = 'd00_scaffold.fj'
D_SCAFFOLD_USERS = {
    'd01_segment_gap',
    'd02_segment_back',
    'd03_segment_far_between',
    'd05_value_moves_with_label',
    'd06_distance_in_one_piece',
}
D_CASES: Dict[str, Tuple[int, bool, Optional[str]]] = {
    # operands across segments that are not emitted in rising address order
    'd01_segment_gap': (
        32,
        False,
        r'the operand `L \+ 4096` of the op at 0x4000 \(in the top level\) reaches across the start of the segment at '
        r'0x1000, which stays where it is',
    ),
    'd02_segment_back': (32, False, SAME),  # kept inline
    'd03_segment_far_between': (32, False, SAME),  # kept inline
    'd04_pad_slot_back': (32, False, r'the operand `M - 64` .* lands in the padding of the `pad 4` at 0x[0-9a-f]+'),
    'd05_value_moves_with_label': (32, False, SAME),  # kept inline
    # operands that move nothing: no refusal
    'd06_distance_in_one_piece': (32, False, SAME),
    'd07_ternary_unselected': (32, False, SAME),
    # what a refusal says
    'd08_refusal_names_its_op': (32, False, r'the operand `L \+ 4096` of the op at 0x140 \(in macro jumper\(1\)'),
    'd09_segment_address_from_moved_code': (
        32,
        False,
        r'the address of the segment at file .* \(line \d+\) \(in the top level\), is 17024 without the pool and is '
        r'16704 with it',
    ),
    'd10_huge_pad': (64, False, SAME),
    # numbers the expansion evaluates from the addresses of code that moved
    'd12_rep_count_from_address': (
        32,
        True,
        r'the count of the rep at file .*, is 1 without the pool and is 4 with it',
    ),
    'd13_opaque_distance_count': (
        32,
        True,
        r'the count of the rep at file .*, is 31 without the pool and is 2 with it',
    ),
    'd14_reserve_to_absolute': (
        32,
        True,
        r'the size of the reserve at file .*, is 6016 without the pool and is 7872 with it',
    ),
    # pins that cannot hold
    'd15_jump_through_unarmed_word': (32, True, SAME),
    'd16_number_arm_of_unpadded_table': (32, True, SAME),
    'd17_label_value_then_dispatch': (32, True, SAME),
}
D_UNPINS = {
    'd15_jump_through_unarmed_word': {JUMPED: 1},
    'd16_number_arm_of_unpadded_table': {JUMPED: 1},
    'd17_label_value_then_dispatch': {NO_TABLE: 1},
}
# each program's control: the check that keeps it the same program, switched off
D_CONTROLS: Dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    'd01_segment_gap': no_refusals,
    'd02_segment_back': _moved_unrefused,
    'd03_segment_far_between': _moved_unrefused,
    'd04_pad_slot_back': no_refusals,
    'd05_value_moves_with_label': _moved_unrefused,
    'd12_rep_count_from_address': no_refusals,
    'd13_opaque_distance_count': no_refusals,
    'd14_reserve_to_absolute': no_refusals,
    'd15_jump_through_unarmed_word': _keep_every_pin,
    'd16_number_arm_of_unpadded_table': _keep_every_pin,
    'd17_label_value_then_dispatch': _keep_every_pin,
}
# d15's and d16's miscompiles never halt: they run into the 30 s timeout (with --slow)
D_CONTROL_PARAMS = [
    pytest.param(name, marks=[pytest.mark.slow] if name[:3] in ('d15', 'd16') else []) for name in sorted(D_CONTROLS)
]


def _build_both(tmp_path: Path, name: str, pool: BlockPool) -> Tuple[Path, Path]:
    """assemble the D case `name` without a pool and with `pool`; return the two .fjm paths"""
    memory_width, use_stl, _ = D_CASES[name]
    plain, pooled = tmp_path / 'plain.fjm', tmp_path / 'pooled.fjm'
    sources = ([GAPS / D_SCAFFOLD] if name in D_SCAFFOLD_USERS else []) + [GAPS / f'{name}.fj']
    assemble(sources, plain, memory_width=memory_width, print_time=False, use_stl=use_stl)
    assemble(sources, pooled, memory_width=memory_width, print_time=False, use_stl=use_stl, table_pool=pool)
    return plain, pooled


@pytest.mark.parametrize('name', sorted(D_CASES))
def test_a_reviewed_program_runs_the_same_with_the_pool_or_is_refused(tmp_path: Path, name: str) -> None:
    memory_width, _, refusal = D_CASES[name]
    pool = BlockPool(memory_width)
    if refusal is not None:
        with pytest.raises(FlipJumpException, match=refusal):
            _build_both(tmp_path, name, pool)
        return
    plain, pooled = _build_both(tmp_path, name, pool)
    expected = run(plain, 30)
    assert expected[1] != 'TIMEOUT' and run(pooled, 30) == expected  # two timeouts are not the same run
    assert pool.unpins == D_UNPINS.get(name, {})


@pytest.mark.parametrize('name', D_CONTROL_PARAMS)
def test_without_its_check_a_reviewed_program_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # the controls of the D cases: each check above prevents a real miscompile, which this run shows
    D_CONTROLS[name](monkeypatch)
    plain, pooled = _build_both(tmp_path, name, BlockPool(D_CASES[name][0]))
    expected = run(plain, 30)
    assert expected[1] != 'TIMEOUT' and run(pooled, 30) != expected


def test_an_undefined_relocatable_macro_says_so_with_a_pool_too(tmp_path: Path) -> None:
    source = GAPS / 'd11_undefined_relocatable.fj'
    for pool in (None, BlockPool(W)):
        with pytest.raises(FlipJumpException, match=r"macro my.__relocatable__.table\(3\) is used but isn't defined"):
            assemble([source], tmp_path / 'p.fjm', memory_width=W, print_time=False, use_stl=False, table_pool=pool)
