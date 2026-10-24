"""
unit-tests for the table pool's gap hunts: every crafted program (tests/unit/pool_gaps) either runs with a pool
exactly as without one - through the fallbacks it counts where it needs them (a table kept inline, a word left
unpinned) - or is refused with the reason; and the pool's smaller promises (a pool lays out one assembly, a TablePool
slot is aligned and inside the span, a wflip area that leaves the memory says so).
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from flipjump import assemble, assemble_run_according_to_cmd_line_args, flipjump_quickstart
from flipjump.fjm.fjm_reader import read_fjm_header
from flipjump.interpreter.run_defaults import FLAT_MEMORY_AUTO_MAX_WORDS
from flipjump.assembler import assembler, table_pool as table_pool_module
from flipjump.assembler.fj_parser import parse_macro_tree
from flipjump.assembler.relocation_proof import prove_relocation
from flipjump.assembler.preprocessor import PreprocessorData
from flipjump.assembler.table_pool import BlockPool, OffsetGuard, TablePool
from flipjump.utils.functions import get_file_tuples
from flipjump.utils.exceptions import FlipJumpException
from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from tests.unit.pool_fuzzer import run
from tests.unit.test_table_pool import build_and_run, relocatable
from tests.unit.unit_utils import PROGRAMS_DIR

GAPS = Path(__file__).parent / 'pool_gaps'
W = 32
OP_BITS = 2 * W
POOL_BASE = 1 << 26


def _pool(kind: str, memory_width: int, source: Path, tmp_path: Path) -> TablePool:
    base = 1 << (memory_width - 1)
    if kind == 'cli':  # fj --pool-tables
        return BlockPool.for_flat(memory_width)
    if kind == 'table':
        return TablePool(memory_width, base, run_ops=16)
    if kind == 'half':  # fj --pool-tables's pool, based at half the memory
        return BlockPool(memory_width, base, one_pass=True)
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
    r'to another op once a table before it moved out: it would no longer point where it did'
)
GUARD_C37 = (
    r'the operand `\(\(end - start\) / 64\)` .* takes the distance across the place of the table '
    r'f1:l5:hex.xor\(2\)---s16:l12:hex.exact_xor\(5\)---switch'
)
GUARD_C38 = (
    r'the operand .*\(S - M\).* takes the distance across the start of the segment at 0x4000, which stays where it '
    r'is while code a table moved out of moves'
)
REACHES = 'an operand reaches over its place'
BELOW = 'a flip below its window could arm a table there'
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
    # M1, M2: a pool at half the memory is reached; fj --pool-tables's is based above the program
    ('c10_wflip_area_crosses_pool', 16): {'cli': SAME, 'table': 'its wflip area reach'},
    ('c11_segment_above_pool', 16): {'cli': SAME, 'half': 'inside the table pool'},
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
    ('c30_count_bits_stays_pinned', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # N5, R3-1: pinned
    # the gap hunt's third round
    ('c32_numeric_arm_below_window', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # R3-1: un-pinned
    ('c33_bitwise_arm_below_window', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # R3-2: un-pinned
    ('c34_numeric_arm_wide_window', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # R3-3: un-pinned
    ('c35_offset_across_realigned_pad', 32): {'cli': GUARD_C35, 'two_pass': GUARD_C35, 'table': GUARD_C35},  # R3-4
    ('c36_opaque_label_plus_offset', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},  # R3-5: kept inline
    # the final review
    ('c37_rep_count_across_table', 32): {'cli': GUARD_C37, 'two_pass': GUARD_C37, 'table': GUARD_C37},
    ('c38_distance_into_numeric_segment', 32): {'cli': GUARD_C38, 'two_pass': GUARD_C38, 'table': GUARD_C38},
    ('c39_distance_into_earlier_segment', 32): {'cli': GUARD_C38, 'two_pass': GUARD_C38, 'table': GUARD_C38},
    ('c40_unmoved_distance_across_segments', 32): {'cli': SAME, 'two_pass': SAME, 'table': SAME},
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


def test_count_bits_keeps_its_pin(tmp_path: Path) -> None:
    # N5: hex.count_bits flips dst+dbit+4/5 inside the 64-op table it armed dst with: a move the base does not change.
    # R3-1: its own 16-op table lies at op 16, below that window, where the same flip of an UNARMED dst would point -
    # but nothing jumps through dst except its arms, so no flip of it is one: dst stays pinned. With an op that jumps
    # to dst's op (as C32's arm by a number does), it would not
    source = GAPS / 'c30_count_bits_stays_pinned.fj'
    pool = BlockPool.for_flat(W)
    assemble([source], tmp_path / 'pooled.fjm', memory_width=W, print_time=False, table_pool=pool)
    assert pool.pinned_words() and pool.unpins == {}
    tables = pool.offset_guard.tables_inline() if pool.offset_guard else {}
    assert tables[0x400] == 10  # count_switch, behind a `pad 16`, a value dst's flips can make


def _old_alignment(self: OffsetGuard, address: int) -> int:
    """OffsetGuard.alignment before R3-3: an inline table's alignment was its address's own"""
    table = self.table_at(address)
    if table >= 0:
        return (self._ends[table] - self._starts[table] - 1).bit_length()
    return (address & -address).bit_length() - 1 if address else 1 << 30


# each third-round finding's check, disabled: {program: [(the owner of a check, its name, its stand-in)]}. C34's
# segment at 0x4000 is a table below the wide window too, so R3-1's check catches it as well: both are disabled.
Check = Tuple[Any, str, Any]
NO_LOW_TABLE: Check = (OffsetGuard, 'tables_inline', lambda self: {})
DISABLED: Dict[str, List[Check]] = {
    'c32_numeric_arm_below_window': [NO_LOW_TABLE],
    'c33_bitwise_arm_below_window': [NO_LOW_TABLE],
    'c34_numeric_arm_wide_window': [NO_LOW_TABLE, (OffsetGuard, 'alignment', _old_alignment)],
    'c35_offset_across_realigned_pad': [(OffsetGuard, 'pad_realigned', lambda self, address, ops_alignment: None)],
    'c36_opaque_label_plus_offset': [(table_pool_module, '_moves_with', lambda expr, label, address, value: False)],
    # the final review's: a rep's count the expansion evaluates, a distance across a segment's end
    'c37_rep_count_across_table': [(PreprocessorData, 'check_evaluated', lambda self, expr, value, is_address=0: None)],
    'c38_distance_into_numeric_segment': [(OffsetGuard, 'segment_started', lambda self, address: None)],
    'c39_distance_into_earlier_segment': [(OffsetGuard, 'segment_started', lambda self, address: None)],
}


def _disable(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    for owner, attribute, stand_in in DISABLED[name]:
        monkeypatch.setattr(owner, attribute, stand_in)


@pytest.mark.parametrize('pool', ['cli', 'two_pass', 'table'])
@pytest.mark.parametrize('name', sorted(DISABLED))
def test_without_its_check_a_third_round_program_miscompiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, pool: str
) -> None:
    # the controls of R3-1 .. R3-5, C37 and C38: each check above prevents a real miscompile, which this run shows.
    # (TablePool pins nothing, so R3-1 .. R3-3 cannot reach it: it runs the same either way.)
    _disable(monkeypatch, name)
    pins_only = name < 'c35'
    assert _differs(GAPS / f'{name}.fj', tmp_path, pool) is not (pins_only and pool == 'table')


def test_a_wide_inline_arm_alone_does_not_widen_the_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # R3-3 alone: with no table below the window (R3-1's check off), C34's numeric arm is still above the moved
    # table's window - unpinned, and the same; widened by T2's address (as before R3-3), it is kept and miscompiles
    monkeypatch.setattr(OffsetGuard, 'tables_inline', lambda self: {})
    source = GAPS / 'c34_numeric_arm_wide_window.fj'
    pool = _pool('cli', W, source, tmp_path)
    assemble([source], tmp_path / 'pooled.fjm', memory_width=W, print_time=False, table_pool=pool)
    assert pool.unpins == {'armed with a number': 1}
    assert not _differs(source, tmp_path, 'cli')
    monkeypatch.setattr(OffsetGuard, 'alignment', _old_alignment)
    assert _differs(source, tmp_path, 'cli')


def _proof(name: str) -> List[str]:
    files = get_file_tuples([str(GAPS / f'{name}.fj')], no_stl=False)
    return prove_relocation(W, parse_macro_tree(files, W, True), BlockPool.for_flat(W)).mismatches


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


def test_a_w64_pooled_program_runs_flat(tmp_path: Path) -> None:
    # M5, the flat spec: fj --pool-tables bases the pool right above the program, so a flat-memory run (the run flag,
    # compiled in: the window is required) of a w=64 program maps a small window, and prints what the plain one does
    for name, extra in (('plain', []), ('pooled', ['--pool-tables'])):
        args = ['--asm', '-s', '-o', str(tmp_path / f'{name}.fjm'), '-w', '64', '--run-flags', '1', *extra]
        assemble_run_according_to_cmd_line_args(cmd_line_args=[*args, str(_source(tmp_path))])
    plain, pooled = read_fjm_header(tmp_path / 'plain.fjm'), read_fjm_header(tmp_path / 'pooled.fjm')
    assert pooled.span_words <= 2 * plain.span_words <= FLAT_MEMORY_AUTO_MAX_WORDS
    outputs = []
    for name in ('plain', 'pooled'):
        io_device = FixedIO(b'')
        flipjump_quickstart.run(tmp_path / f'{name}.fjm', io_device=io_device, print_time=False, flat_required=True)
        outputs.append(io_device.get_output())
    assert outputs[0] == outputs[1] == b'6'


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


def _ops(fjm: Path) -> Tuple[bytes, int]:
    """(the output, the ops run) of a program that halts"""
    io_device = FixedIO(b'')
    statistics = fjm_run.run(fjm, io_device=io_device, print_time=False)
    return io_device.get_output(), statistics.op_counter


def test_a_program_that_reads_words_raw_runs_fewer_ops_with_the_pool(tmp_path: Path) -> None:
    # R3-6: the pointer runtime unpins every word, so fj --pool-tables places the tables cheapest-first, as a
    # TablePool does: no block, no pinned word - and fewer ops than without the pool
    source = [PROGRAMS_DIR / 'hexlib_tests' / 'basics2' / 'pointer_setters.fj']
    assemble(source, tmp_path / 'plain.fjm', memory_width=64, print_time=False)
    plain = _ops(tmp_path / 'plain.fjm')
    pool = BlockPool.for_flat(64)
    assemble(source, tmp_path / 'pooled.fjm', memory_width=64, print_time=False, table_pool=pool)
    pooled = _ops(tmp_path / 'pooled.fjm')
    assert pool.reads_words_raw and pool.allocated and pool.pinned_words() == {}
    assert pool.unpins == {'the pointer runtime': 76}
    assert pooled[0] == plain[0] and pooled[1] < plain[1]
    # the control: in blocks (a pin_exclude that vetoes every pin keeps them), the same tables run MORE ops
    blocked_pool = BlockPool.for_flat(64)
    blocked_pool.pin_exclude = lambda address, labels: True
    assemble(source, tmp_path / 'blocked.fjm', memory_width=64, print_time=False, table_pool=blocked_pool)
    blocked = _ops(tmp_path / 'blocked.fjm')
    assert blocked_pool.pinned_words() and blocked[0] == plain[0] and blocked[1] > plain[1]


# the flat spec: fj --pool-tables's pool (BlockPool.for_flat) sits right above the program and ends below the flat
# window. _INIT_FIRST's tables come after hex.init's bulk, so a tight window leaves them no room.
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


def _flat(tmp_path: Path, text: str, memory_width: int, **pool: Any) -> Tuple[BlockPool, Tuple[str, str], int]:
    """(the pool, the run, the .fjm's span in words) of `text` assembled with BlockPool.for_flat(**pool)"""
    source = _source(tmp_path, text)
    table_pool = BlockPool.for_flat(memory_width, **pool)
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
    assert pool.allocated and pooled == plain and pool.past_window == 0
    assert base & (base - 1) == 0 and 'the first power of two above the program' in pool.base_reason
    assert base < 2 * plain_span * memory_width and span < 2 * plain_span


def test_a_program_ending_just_past_a_power_of_two_takes_the_compact_base(tmp_path: Path) -> None:
    # the program ends at 0x10040, just past 2^16: at 2^17 the pool would not end below a 0x14000-bit window, so it
    # is based at the program's end rounded up to its largest block
    text = _XOR + 'segment 0x10000\n    ;0\n'
    plain, _ = _plain(tmp_path, text, W)
    pool, pooled, _ = _flat(tmp_path, text, W)
    assert pool.pool_start() == 0x20000 and pooled == plain
    pool, pooled, _ = _flat(tmp_path, text, W, flat_max_words=0x14000 // W)
    assert 0x10040 <= pool.pool_start() < 0x20000 and pool.pool_start() & (pool.pool_start() - 1)
    assert "the program's end rounded up to its largest block" in pool.base_reason
    assert pool.allocated and pool.end <= 0x14000 and pooled == plain and pool.past_window == 0


# tables before and after hex.init's bulk: in a tight window the ones after it find no room below the window
_TABLES_AROUND_THE_BULK = """
stl.startup
    hex.xor x, y
    hex.or x, y
    ;code
hex.init
code:
    hex.xor y, x
    hex.and y, x
    hex.print_as_digit x, 0
    hex.print_as_digit y, 0
    stl.loop
x: hex.hex 3
y: hex.hex 5
"""


def test_a_tight_flat_window_moves_some_tables_and_keeps_the_rest_inline(tmp_path: Path) -> None:
    plain, plain_span = _plain(tmp_path, _TABLES_AROUND_THE_BULK, W)
    pool, pooled, _ = _flat(tmp_path, _TABLES_AROUND_THE_BULK, W)
    every = pool.allocated + pool.overflowed
    partial = []
    for words in range(plain_span, plain_span + 200, 16):
        pool, pooled, span = _flat(tmp_path, _TABLES_AROUND_THE_BULK, W, flat_max_words=words)
        moved = pool.allocated + pool.overflowed
        assert pooled == plain and moved + pool.past_window >= every and pool.end <= words * W
        assert span <= words or not moved  # the whole program, wflip areas included, inside the window
        if 0 < moved < every:
            partial.append(words)
    assert partial


def test_a_tight_flat_window_moves_fewer_tables_safely(tmp_path: Path) -> None:
    plain, plain_span = _plain(tmp_path, _INIT_FIRST, W)
    pool, pooled, _ = _flat(tmp_path, _INIT_FIRST, W)
    moved = pool.allocated + pool.overflowed
    assert moved and pool.past_window == 0 and pooled == plain
    # a window the pool fits in only compactly: every table moves, fewer get a block
    pool, pooled, span = _flat(tmp_path, _INIT_FIRST, W, flat_max_words=plain_span)
    assert pool.allocated + pool.overflowed == moved and pool.past_window == 0 and pooled == plain
    assert span <= plain_span and pool.end <= plain_span * W
    # a window the program's code alone overfills: no table has room - each stays inline, counted, and the binary is
    # the plain one
    pool, pooled, span = _flat(tmp_path, _INIT_FIRST, W, flat_max_words=plain_span // 2)
    assert pool.allocated + pool.overflowed == 0 and pool.past_window >= moved and pooled == plain
    assert (tmp_path / 'flat.fjm').read_bytes() == (tmp_path / 'plain.fjm').read_bytes()


def test_a_table_pool_for_a_flat_window_takes_its_upper_half() -> None:
    pool = TablePool.for_flat(W, 1 << 20)
    assert pool.pool_start() == (1 << 20) * W // 2 and pool.span_bits == (1 << 20) * W // 2
    assert TablePool.for_flat(64).flat_limit == FLAT_MEMORY_AUTO_MAX_WORDS * 64


def test_a_combined_helper_hands_its_flat_window_to_a_pool_request(tmp_path: Path) -> None:
    # assemble_and_run_test_output (and assemble_and_debug, which assemble_and_run calls) call a request with their
    # own memory width and flat_max_words, so assembling and running cannot disagree; a pool is used as it is
    asked: List[Tuple[int, Optional[int]]] = []

    def request(memory_width: int, flat_max_words: Optional[int]) -> TablePool:
        asked.append((memory_width, flat_max_words))
        return BlockPool.for_flat(memory_width, flat_max_words)

    for helper_pool in (request, BlockPool.for_flat(64, 1 << 20)):
        assert flipjump_quickstart.assemble_and_run_test_output(
            [_source(tmp_path)], b'', b'6', print_time=False, table_pool=helper_pool, flat_max_words=1 << 20
        )
    assert asked == [(64, 1 << 20)]


def test_the_cli_pool_gets_only_its_intended_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # G4: fj --pool-tables passes the width and --flat-max-words, and no tuning knob
    made: List[Tuple[Tuple[Any, ...], Dict[str, Any], BlockPool]] = []
    real = BlockPool.for_flat.__func__  # type: ignore[attr-defined]

    def for_flat(cls: Any, *args: Any, **kwargs: Any) -> BlockPool:
        pool: BlockPool = real(cls, *args, **kwargs)
        made.append((args, kwargs, pool))
        return pool

    monkeypatch.setattr(BlockPool, 'for_flat', classmethod(for_flat))
    args = ['--asm', '-s', '-o', str(tmp_path / 'a.fjm'), '-w', '32', '--pool-tables', '--flat-max-words', '4096']
    assemble_run_according_to_cmd_line_args(cmd_line_args=[*args, str(_source(tmp_path))])
    ((given, kwargs, pool),) = made
    assert given == (32, 4096) and kwargs == {} and pool.flat_limit == 4096 * 32
    assert (pool.max_slot_ops, pool.spread, pool.spread_min_count, pool.run_bits) == (32, 1, 256, 16 * 2 * 32)
    assert not (pool.pin_broken or pool.width_buckets or pool.evict_by_value or pool.merge_aliases or pool._heat)
    assert pool.one_pass and pool.overflow_base is None and pool.counts and not pool.alias


def test_a_flat_pool_with_only_overflow_tables_lays_the_wflip_areas_after_them(tmp_path: Path) -> None:
    # counts that name no group give no block: every table goes to the overflow region, right above the program -
    # and the wflip areas after it (else the main one would run into the pool)
    plain, _ = _plain(tmp_path, _XOR, W)
    pool, pooled, _ = _flat(tmp_path, _XOR, W, counts={})
    assert pool.allocated == 0 and pool.overflowed and pool.wflips_at() == pool.end and pooled == plain


def test_a_flat_pool_that_places_no_table_is_no_pool(tmp_path: Path) -> None:
    # hello_world_with_str moves no table: its base above the code would sit inside its wflip area, so a pool with
    # no table has no start - the binary is the plain one, byte for byte
    source = [PROGRAMS_DIR / 'print_tests' / 'hello_world_with_str.fj']
    assemble(source, tmp_path / 'plain.fjm', memory_width=64, print_time=False)
    pool = BlockPool.for_flat(64)
    assemble(source, tmp_path / 'pooled.fjm', memory_width=64, print_time=False, table_pool=pool)
    assert pool.end == 0 and pool.pool_start() == 1 << 64
    assert (tmp_path / 'pooled.fjm').read_bytes() == (tmp_path / 'plain.fjm').read_bytes()


def test_without_the_wflip_bound_the_wflip_areas_end_past_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the control of the bound: the wflip areas go after the pool, and only their bound keeps them inside the window
    # (resolve_macros_with_pool keeps every table inline where they might not fit)
    monkeypatch.setattr(assembler, '_wflip_ops_bound', lambda stream, labels, table_pool: 0)
    _, plain_span = _plain(tmp_path, _TABLES_AROUND_THE_BULK, W)
    past = []
    for words in range(plain_span, plain_span + 200, 16):
        pool, _, span = _flat(tmp_path, _TABLES_AROUND_THE_BULK, W, flat_max_words=words)
        if pool.allocated + pool.overflowed and span > words:
            past.append(words)
    assert past
