"""
unit-tests for the table pool's gap hunts: every crafted program (tests/unit/pool_gaps) either runs with a pool
exactly as without one - through the fallbacks it counts where it needs them (a table kept inline, a word left
unpinned) - or is refused with the reason; and the pool's smaller promises (a pool lays out one assembly, a TablePool
slot is aligned and inside the span, a wflip area that leaves the memory says so).
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from flipjump import assemble, assemble_run_according_to_cmd_line_args
from flipjump.assembler import assembler, table_pool as table_pool_module
from flipjump.assembler.fj_parser import parse_macro_tree
from flipjump.assembler.relocation_proof import prove_relocation
from flipjump.assembler.table_pool import BlockPool, OffsetGuard, TablePool
from flipjump.utils.functions import get_file_tuples
from flipjump.utils.exceptions import FlipJumpException
from tests.unit.pool_fuzzer import run
from tests.unit.test_table_pool import build_and_run, relocatable

GAPS = Path(__file__).parent / 'pool_gaps'
W = 32
OP_BITS = 2 * W
POOL_BASE = 1 << 26


def _pool(kind: str, memory_width: int, source: Path, tmp_path: Path) -> TablePool:
    base = 1 << (memory_width - 1)
    if kind == 'cli':  # fj --pool-tables
        return BlockPool(memory_width, base, one_pass=True)
    if kind == 'table':
        return TablePool(memory_width, base, run_ops=16)
    counting = BlockPool(memory_width, base)
    assemble([source], tmp_path / 'count.fjm', memory_width=memory_width, print_time=False, table_pool=counting)
    return BlockPool(memory_width, base, counts=counting.counts, widths=counting.widths)


SAME = None
GUARD_C06 = (
    r'the operand `L` of the op at 0x480 \(in the top level\) names L, declared at 0x400 before the `pad` of the '
    r'table T: it named the table\'s start'
)
GUARD_C07_PLUS = (
    r'the operand `T \+ 64` of the op at 0x80 \(after the label f1:l11:stl.startup---code_start\) leaves the moved '
    r'table T at 0x80000000: an offset from a moved table\'s label must stay inside it'
)
GUARD_C07_COMPOUND = (
    r'the operand `\(\(T \+ 64\) \| 0\)` of the op at 0x80 \(after the label f1:l11:stl.startup---code_start\) '
    r'leaves the moved table T at 0x80000000: an offset from a moved table\'s label must stay inside it'
)
GUARD_C25 = r'the operand `f1:l19:go\(1\)---T \+ 64` .* leaves the moved table f1:l19:go\(1\)---T at 0x80000000'
GUARD_C29 = (
    r'the operand `\(\(\(\(b - a\) >> 6\) & 15\) \* 64\)` of the op at 0x140 \(in the top level\) takes the distance '
    r'across the place of the table f1:l8:hex.xor\(2\)---s16:l12:hex.exact_xor\(5\)---switch, which moved out of 0x100'
)
GUARD_C35 = (
    r'the operand `L \+ 448` of the op at 0x100 \(in the top level\) reaches across the `pad 8` at 0x140, which pads '
    r'to another op once a table before it moved out: it would no longer point where it did'
)
REACHES = 'an operand reaches over its place'
BELOW = 'a flip below its window, with a table there'
GUARDED = [
    'c06_label_before_pad',
    'c07a_reach_in_plus',
    'c07b_reach_in_compound',
    'c07c_reach_in_compound_labelled',
    'c07d_reach_in_plus_labelled',
]
# (program, memory width): {pool: SAME, or the words of its refusal}
CASES: Dict[Tuple[str, int], Dict[str, Optional[str]]] = {
    ('c01_raw_read_bit_ptr', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # B1, N7: un-pinned
    ('c16_runtime_write_upper_half', 32): {'cli': SAME, 'table': SAME},  # B1, N7: un-pinned
    ('c02_pinned_floor', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # B2
    ('c03_pinned_in_reserve', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # B3, N7: un-pinned
    ('c08_retarget_xor', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # B4, N7: un-pinned
    ('c08b_retarget_xor_labelled', 32): {'cli': SAME, 'table': SAME},  # B4, N7: un-pinned
    ('c05_label_offset_out_of_table', 32): {'cli': 'leaves the table through', 'table': 'leaves the table through'},
    ('c05b_trailing_label', 32): {'cli': 'declares a label after its last op', 'table': 'after its last op'},  # B5
    ('c09_pad3', 32): {'cli': 'power-of-two pad', 'table': 'power-of-two pad'},  # B6
    ('c10_wflip_area_crosses_pool', 16): {'cli': 'its wflip area reach', 'table': 'its wflip area reach'},  # M1
    ('c11_segment_above_pool', 16): {'cli': 'inside the table pool'},  # M2
    ('c14_accidental_namespace', 32): {'cli': 'needs a `pad`'},  # M6: a reserved namespace with the pool
    ('c13_label_between_pad_and_call', 32): {'cli': SAME, 'table': SAME},
    ('c15_nested_namespaces', 32): {'cli': SAME, 'table': SAME},
    ('c19_no_label_after_call', 32): {'cli': SAME, 'table': SAME},
    # an operand outside a moved table that names a label and reaches the table's place (OffsetGuard)
    ('c06_label_before_pad', 32): {'cli': GUARD_C06, 'table': GUARD_C06},
    ('c07a_reach_in_plus', 32): {'cli': GUARD_C07_PLUS, 'table': GUARD_C07_PLUS},
    ('c07b_reach_in_compound', 32): {'cli': GUARD_C07_COMPOUND, 'table': GUARD_C07_COMPOUND},
    ('c07c_reach_in_compound_labelled', 32): {'cli': GUARD_C07_COMPOUND, 'table': GUARD_C07_COMPOUND},
    ('c07d_reach_in_plus_labelled', 32): {'cli': GUARD_C07_PLUS, 'table': GUARD_C07_PLUS},
    # the gap hunt's second round
    ('c20_dollar_across_table', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # N1: kept inline
    ('c21_inline_compound_across_table', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # N2: kept inline
    ('c22_label_digit_into_pinned', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # N6: a value, stays pinned
    ('c23_numeric_address_arm', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # N4: un-pinned
    ('c24_constant_value', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},
    ('c25_arg_reach_out', 32): {'cli': GUARD_C25, 'two_pass': GUARD_C25, 'table': GUARD_C25},  # N3
    ('c26_pointer_write_only', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # N6, N7: un-pinned
    ('c27_bitwise_arm', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # N5: un-pinned
    ('c28_segment_back', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # N6
    ('c29_label_difference_value', 32): {'cli': GUARD_C29, 'two_pass': GUARD_C29, 'table': GUARD_C29},  # N2
    ('c30_count_bits_stays_pinned', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # N5, R3-1: un-pinned
    # the gap hunt's third round
    ('c32_numeric_arm_below_window', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # R3-1: un-pinned
    ('c33_bitwise_arm_below_window', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # R3-2: un-pinned
    ('c34_numeric_arm_wide_window', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # R3-3: un-pinned
    ('c35_offset_across_realigned_pad', 32): {'cli': GUARD_C35, 'two_pass': GUARD_C35, 'table': GUARD_C35},  # R3-4
    ('c36_opaque_label_plus_offset', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # R3-5: kept inline
}
# (program, pool): (the tables the pool kept inline, the words it left unpinned), by reason - every other SAME
# program needs neither
POINTERS: Tuple[Dict[str, int], Dict[str, int]] = ({}, {'the pointer runtime': 1})
FALLBACKS: Dict[Tuple[str, str], Tuple[Dict[str, int], Dict[str, int]]] = {
    ('c01_raw_read_bit_ptr', 'cli'): POINTERS,
    ('c01_raw_read_bit_ptr', 'two_pass'): POINTERS,
    ('c16_runtime_write_upper_half', 'cli'): POINTERS,
    ('c03_pinned_in_reserve', 'cli'): ({}, {'no op of its own': 1}),
    ('c03_pinned_in_reserve', 'two_pass'): ({}, {'no op of its own': 1}),
    ('c08_retarget_xor', 'cli'): ({}, {'armed with a value computed from labels': 1}),
    ('c08_retarget_xor', 'two_pass'): ({}, {'armed with a value computed from labels': 1}),
    ('c08b_retarget_xor_labelled', 'cli'): ({}, {'armed with a value computed from labels': 1}),
    ('c20_dollar_across_table', 'cli'): ({REACHES: 1}, {}),
    ('c20_dollar_across_table', 'two_pass'): ({REACHES: 1}, {}),
    ('c20_dollar_across_table', 'table'): ({REACHES: 1}, {}),
    ('c21_inline_compound_across_table', 'cli'): ({REACHES: 1}, {}),
    ('c21_inline_compound_across_table', 'two_pass'): ({REACHES: 1}, {}),
    ('c21_inline_compound_across_table', 'table'): ({REACHES: 1}, {}),
    ('c23_numeric_address_arm', 'cli'): ({}, {'armed with a number': 1}),
    ('c23_numeric_address_arm', 'two_pass'): ({}, {'armed with a number': 1}),
    ('c26_pointer_write_only', 'cli'): POINTERS,
    ('c26_pointer_write_only', 'two_pass'): POINTERS,
    ('c27_bitwise_arm', 'cli'): ({}, {'a bit flip above its window': 1}),
    ('c27_bitwise_arm', 'two_pass'): ({}, {'a bit flip above its window': 1}),
    ('c30_count_bits_stays_pinned', 'cli'): ({}, {BELOW: 1}),
    ('c30_count_bits_stays_pinned', 'two_pass'): ({}, {BELOW: 1}),
    ('c32_numeric_arm_below_window', 'cli'): ({}, {BELOW: 1}),
    ('c32_numeric_arm_below_window', 'two_pass'): ({}, {BELOW: 1}),
    ('c33_bitwise_arm_below_window', 'cli'): ({}, {BELOW: 1}),
    ('c33_bitwise_arm_below_window', 'two_pass'): ({}, {BELOW: 1}),
    ('c34_numeric_arm_wide_window', 'cli'): ({}, {'armed with a number': 1}),
    ('c34_numeric_arm_wide_window', 'two_pass'): ({}, {'armed with a number': 1}),
    ('c36_opaque_label_plus_offset', 'cli'): ({REACHES: 1}, {}),
    ('c36_opaque_label_plus_offset', 'two_pass'): ({REACHES: 1}, {}),
    ('c36_opaque_label_plus_offset', 'table'): ({REACHES: 1}, {}),
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
    assert run(pooled, 30) == run(plain, 30)
    assert (table_pool.unsafe_declines, table_pool.unpins) == FALLBACKS.get((name, pool), ({}, {}))
    assert table_pool.allocated or table_pool.unsafe_declines


@pytest.mark.parametrize('pool', ['cli', 'table'])
@pytest.mark.parametrize('name', GUARDED)
def test_without_the_offset_guard_a_reach_into_a_moved_table_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, pool: str
) -> None:
    # the control: each refusal above prevents a real miscompile, which this run shows
    monkeypatch.setattr(OffsetGuard, 'check', lambda self, label, address, offset, operand=None: None)
    monkeypatch.setattr(OffsetGuard, 'check_expr', lambda self, expr, value, is_address=True: None)
    source = GAPS / f'{name}.fj'
    plain = tmp_path / 'plain.fjm'
    assemble([source], plain, memory_width=W, print_time=False)
    pooled = tmp_path / 'pooled.fjm'
    table_pool = _pool(pool, W, source, tmp_path)
    assemble([source], pooled, memory_width=W, print_time=False, table_pool=table_pool)
    assert table_pool.allocated and run(pooled, 30) != run(plain, 30)


def _differs(source: Path, tmp_path: Path, pool: str) -> bool:
    """does `source` run differently with the pool than without it?"""
    plain = tmp_path / 'plain.fjm'
    assemble([source], plain, memory_width=W, print_time=False)
    pooled = tmp_path / 'pooled.fjm'
    assemble([source], pooled, memory_width=W, print_time=False, table_pool=_pool(pool, W, source, tmp_path))
    return run(pooled, 30) != run(plain, 30)


@pytest.mark.parametrize('pool', ['cli', 'two_pass', 'table'])
@pytest.mark.parametrize('name', ['c20_dollar_across_table', 'c21_inline_compound_across_table'])
def test_without_keeping_its_table_inline_a_reach_over_its_place_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, pool: str
) -> None:
    # the control of N1 and N2: the table moves, and nothing later checks the operand
    monkeypatch.setattr(OffsetGuard, 'may_move', lambda self, anchor: None)
    monkeypatch.setattr(OffsetGuard, 'check_expr', lambda self, expr, value, is_address=True: None)
    assert _differs(GAPS / f'{name}.fj', tmp_path, pool)


@pytest.mark.parametrize('pool', ['cli', 'two_pass', 'table'])
def test_without_the_distance_check_a_label_difference_across_a_moved_table_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: str
) -> None:
    # the control of N2: `b - a` across a moved table's place is a different number
    monkeypatch.setattr(OffsetGuard, '_check_part', lambda self, coefficients, constant, operand: False)
    assert _differs(GAPS / 'c29_label_difference_value.fj', tmp_path, pool)


@pytest.mark.parametrize('pool', ['cli', 'two_pass'])
@pytest.mark.parametrize('name', ['c23_numeric_address_arm', 'c27_bitwise_arm'])
def test_without_unpinning_a_pin_that_cannot_hold_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, pool: str
) -> None:
    # the control of N4, N5: the word stays pinned. (c08's `T1 ^ T2` would hold pinned - the bases cancel - but a
    # value computed from labels is un-pinned whatever it computes)
    monkeypatch.setattr(assembler, 'unpin', lambda *args, **kwargs: None)
    assert _differs(GAPS / f'{name}.fj', tmp_path, pool)


def test_a_program_that_reaches_the_pool_says_so(tmp_path: Path) -> None:
    # N8: a counting pool moves nothing, so an operand past pool_base is the program's own reach
    with pytest.raises(FlipJumpException, match='the program reached 0x80e0, which is inside the table pool at 0x8000'):
        assemble(
            [GAPS / 'c31_program_reaches_the_pool.fj'],
            tmp_path / 'a.fjm',
            memory_width=16,
            print_time=False,
            table_pool=BlockPool(16, 1 << 15),
        )


def test_count_bits_keeps_its_pin_unless_a_table_lies_below_its_window(tmp_path: Path) -> None:
    # N5: hex.count_bits flips dst+dbit+4/5 inside the 64-op table it armed dst with: a move the base does not change.
    # R3-1: but its own 16-op table lies at op 16, below that window, where the same flip of an unarmed dst would
    # point - a flip the pool cannot tell from an arm, so dst is unpinned. With no table below, it stays pinned.
    source = GAPS / 'c30_count_bits_stays_pinned.fj'
    pool = BlockPool(W, 1 << (W - 1), one_pass=True)
    assemble([source], tmp_path / 'pooled.fjm', memory_width=W, print_time=False, table_pool=pool)
    assert pool.pinned_words() and pool.unpins == {BELOW: 1}
    lowest = pool.offset_guard.lowest_pad() if pool.offset_guard else None
    assert lowest == 0x400  # count_switch, behind a `pad 16`
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(OffsetGuard, 'lowest_pad', lambda self: 0x1000)  # no table below the 64-op window (2^12)
        pool = BlockPool(W, 1 << (W - 1), one_pass=True)
        assemble([source], tmp_path / 'pinned.fjm', memory_width=W, print_time=False, table_pool=pool)
        assert pool.pinned_words() and pool.unpins == {}
        assert run(tmp_path / 'pinned.fjm', 30) == run(tmp_path / 'pooled.fjm', 30)


def _old_alignment(self: OffsetGuard, address: int) -> int:
    """OffsetGuard.alignment before R3-3: an inline table's alignment was its address's own"""
    table = self.table_at(address)
    if table >= 0:
        return (self._ends[table] - self._starts[table] - 1).bit_length()
    return (address & -address).bit_length() - 1 if address else 1 << 30


# each third-round finding's check, disabled: {program: [(the owner of a check, its name, its stand-in)]}. C34's
# segment at 0x4000 is a table below the wide window too, so R3-1's check catches it as well: both are disabled.
Check = Tuple[Any, str, Any]
NO_LOW_TABLE: Check = (OffsetGuard, 'lowest_pad', lambda self: None)
DISABLED: Dict[str, List[Check]] = {
    'c32_numeric_arm_below_window': [NO_LOW_TABLE],
    'c33_bitwise_arm_below_window': [NO_LOW_TABLE],
    'c34_numeric_arm_wide_window': [NO_LOW_TABLE, (OffsetGuard, 'alignment', _old_alignment)],
    'c35_offset_across_realigned_pad': [(OffsetGuard, 'pad_realigned', lambda self, address, ops_alignment: None)],
    'c36_opaque_label_plus_offset': [(table_pool_module, '_moves_with', lambda expr, label, address, value: False)],
}


def _disable(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    for owner, attribute, stand_in in DISABLED[name]:
        monkeypatch.setattr(owner, attribute, stand_in)


@pytest.mark.parametrize('pool', ['cli', 'two_pass', 'table'])
@pytest.mark.parametrize('name', sorted(DISABLED))
def test_without_its_check_a_third_round_program_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, pool: str
) -> None:
    # the controls of R3-1 .. R3-5: each check above prevents a real miscompile, which this run shows. (TablePool
    # pins nothing, so R3-1 .. R3-3 cannot reach it: it runs the same either way.)
    _disable(monkeypatch, name)
    pins_only = name < 'c35'
    assert _differs(GAPS / f'{name}.fj', tmp_path, pool) is not (pins_only and pool == 'table')


def test_a_wide_inline_arm_alone_does_not_widen_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # R3-3 alone: with no table below the window (R3-1's check off), C34's numeric arm is still above the moved
    # table's window - unpinned, and the same; widened by T2's address (as before R3-3), it is kept and miscompiles
    monkeypatch.setattr(OffsetGuard, 'lowest_pad', lambda self: None)
    source = GAPS / 'c34_numeric_arm_wide_window.fj'
    pool = _pool('cli', W, source, tmp_path)
    assemble([source], tmp_path / 'pooled.fjm', memory_width=W, print_time=False, table_pool=pool)
    assert pool.unpins == {'armed with a number': 1}
    assert not _differs(source, tmp_path, 'cli')
    monkeypatch.setattr(OffsetGuard, 'alignment', _old_alignment)
    assert _differs(source, tmp_path, 'cli')


def _proof(name: str) -> List[str]:
    files = get_file_tuples([str(GAPS / f'{name}.fj')], no_stl=False)
    return prove_relocation(W, parse_macro_tree(files, W, True), BlockPool(W, 1 << (W - 1), one_pass=True)).mismatches


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
# OffsetGuard sees each in a different place
_TABLE_AND_PRINTS = """
before:
    pad 1
    tbl.__relocatable__.table tbl_start, print_a, print_b
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
    # (a TablePool: the table is dispatched through no word, so a BlockPool would keep it in place)
    source, refusal = OUTSIDE_OFFSET_PROGRAMS[program]
    assert build_and_run(source, tmp_path, None, name='plain')[0] == b'B'
    path = tmp_path / 'p.fj'
    path.write_text(source)
    with pytest.raises(FlipJumpException, match=refusal):
        build_and_run(source, tmp_path, _pool('table', W, path, tmp_path), name='pooled')
    # the control: without the guard it is a miscompile
    monkeypatch.setattr(OffsetGuard, 'check', lambda self, label, address, offset: None)
    table_pool = _pool('table', W, path, tmp_path)
    assert build_and_run(source, tmp_path, table_pool, name='unguarded')[0] != b'B' and table_pool.allocated


def test_a_table_an_earlier_offset_reaches_over_stays_inline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pool = TablePool(W, 1 << (W - 1), run_ops=16)
    assert build_and_run(PENDING, tmp_path, pool, name='pooled')[0] == b'B'
    assert pool.unsafe_declines == {REACHES: 1} and pool.allocated == 0
    # the control: moved, the program miscompiles
    monkeypatch.setattr(OffsetGuard, 'may_move', lambda self, anchor: None)
    assert build_and_run(PENDING, tmp_path, TablePool(W, 1 << (W - 1), run_ops=16), name='moved')[0] != b'B'


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
    pool: TablePool
    for pool in (TablePool(W, POOL_BASE), BlockPool(W, POOL_BASE), BlockPool(W, POOL_BASE, one_pass=True)):
        assemble([_source(tmp_path)], tmp_path / 'a.fjm', memory_width=W, print_time=False, table_pool=pool)
        with pytest.raises(FlipJumpException, match='already laid out'):  # M4
            assemble([_source(tmp_path)], tmp_path / 'b.fjm', memory_width=W, print_time=False, table_pool=pool)


def test_an_overflow_region_below_the_program_is_refused(tmp_path: Path) -> None:
    pool = BlockPool(W, POOL_BASE, one_pass=True, overflow_base=16 * OP_BITS)  # M3
    with pytest.raises(FlipJumpException, match='inside the table pool'):
        assemble([_source(tmp_path)], tmp_path / 'a.fjm', memory_width=W, print_time=False, table_pool=pool)


def test_a_flat_memory_run_of_a_w64_pooled_program_is_refused(tmp_path: Path) -> None:
    # M5: the tables sit at 2^63, so a flat window would map 2^57 words
    args = ['--asm', '-s', '-o', str(tmp_path / 'a.fjm'), '-w', '64', '--pool-tables', '--run-flags', '1']
    with pytest.raises(SystemExit):
        assemble_run_according_to_cmd_line_args(cmd_line_args=[*args, str(_source(tmp_path))])


def test_a_tablepool_slot_is_aligned_and_a_passed_slot_is_used_later() -> None:
    run_bits = 16 * OP_BITS
    pool = TablePool(W, POOL_BASE + run_bits, run_ops=16)  # M8: offset 0 is not 32-op aligned
    reserved = pool.reserve(32, 32)
    assert reserved is not None and reserved[0] % (32 * OP_BITS) == 0
    pool.commit(reserved[0] + 32 * OP_BITS)
    assert pool.reserve(16, 16) == (POOL_BASE + run_bits, True, 0)  # the offset passed over above


def test_a_tablepool_table_past_the_span_is_declined() -> None:
    pool = TablePool(W, POOL_BASE, run_ops=16, span_bits=16 * OP_BITS)  # M8: one run of span
    assert pool.reserve(16, 32) is None and pool.declined == 1


def test_a_wflip_area_past_the_memory_says_so(tmp_path: Path) -> None:
    # M9: at w=16 this program's wflip chains need more than the 2^16 bits of memory
    wflips = ''.join(f'    wflip z+w, {(i * 4999 + 0x0F0F) & 0xFFFE}\n' for i in range(600))
    source = _source(tmp_path, f'stl.startup\n{wflips}    stl.loop\nz: hex.hex\n')
    with pytest.raises(FlipJumpException, match='Not enough space with the 16-bits memory-width: the wflip area'):
        assemble([source], tmp_path / 'a.fjm', memory_width=16, print_time=False)
