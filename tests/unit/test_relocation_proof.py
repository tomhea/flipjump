"""
unit-tests for the relocation proof (tests/unit/relocation_proof.py): with a table pool, every program
of the test tables is the program without it, moved - and the proof sees a corrupted moved op, a declared table
moved in pieces or with an op that is not its own, an offset from an outside label into a moved table, an
operand it cannot compare, and a pin whose arms were not rewritten; and it passes the correct crafted programs.
"""

import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from flipjump.assembler import assembler
from flipjump.assembler.fj_parser import parse_macro_tree
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.ops import FlipJump, Macro, MacroName, NewSegment
from tests.unit import relocation_proof
from tests.unit.relocation_proof import prove_relocation
from flipjump.assembler.table_pool import BlockPool
from flipjump.utils.exceptions import FlipJumpException, FlipJumpPreprocessorException
from flipjump.utils.functions import get_file_tuples
from tests.unit.unit_utils import PROGRAMS_DIR, build_and_run, every_table_moves, no_refusals, relocatable

W = 32
POOL_BASE = 1 << 26
TABLES = PROGRAMS_DIR.parent / 'tests' / 'tests_tables'
GAPS = Path(__file__).parent / 'pool_gaps'
# {test-table program: the operands its proof lists as unchecked} - a ceiling, so a new unchecked operand is noticed
UNCHECKED_CEILINGS: Dict[str, int] = json.loads((Path(__file__).parent / 'relocation_proof_unchecked.json').read_text())


def _test_table_programs() -> List[Tuple[str, List[str], int, bool]]:
    """(name, .fj paths, memory width, use stl) of the programs the compile tables build"""
    programs = []
    for stem in ('hexlib', 'fast', 'medium', 'slow'):
        with open(TABLES / f'test_compile_{stem}.csv') as table:
            for row in csv.reader(table):
                if row and row[0].strip():
                    files = [str(PROGRAMS_DIR.parent / path.strip()) for path in row[1].split('|')]
                    programs.append((row[0].strip(), files, int(row[3]), row[6].strip() == 'True'))
    return programs


TEST_TABLE_PROGRAMS = _test_table_programs()
# the programs whose proof takes 9 - 165 s (the stl's pointer and fixed-point tests): run with --slow
SLOW_PROOFS = {
    f'hexlib-{name}' for name in ('fixed_point', 'seq_pointers', 'read_entry', 'ptr_index_n', 'read_write_back')
}
PROOF_PARAMS = [
    pytest.param(program, id=program[0], marks=[pytest.mark.slow] if program[0] in SLOW_PROOFS else [])
    for program in TEST_TABLE_PROGRAMS
]


def _macros(files: List[str], memory_width: int, use_stl: bool = True) -> Dict[MacroName, Macro]:
    return parse_macro_tree(get_file_tuples(files, no_stl=not use_stl), memory_width, True)


def _cli_pool(memory_width: int) -> BlockPool:
    return BlockPool(memory_width)  # the pool of fj --pool-tables


@pytest.mark.parametrize('program', PROOF_PARAMS)
def test_every_test_program_is_the_same_program_with_the_pool(program: Tuple[str, List[str], int, bool]) -> None:
    _, files, memory_width, use_stl = program
    pool = _cli_pool(memory_width)
    try:
        proof = prove_relocation(memory_width, _macros(files, memory_width, use_stl), pool)
    except FlipJumpPreprocessorException as refused:
        assert 'Raise pool_end' in str(refused)  # prime_sieve's reserve fills the memory above its tables
        pytest.skip('the program leaves its tables no room')
    assert proof.mismatches == []
    assert len(proof.unchecked) <= UNCHECKED_CEILINGS.get(program[0], 0), proof.unchecked
    if not pool.allocated:
        pytest.skip('the program uses no relocatable table')


def _gap(name: str) -> List[str]:
    return [str(GAPS / f'{name}.fj')]


# `before + 2*dw` names the table's third entry ('B') only while the table is where it was declared (the wflip
# after the call, never run, gives the table a group: the word `x + w`)
OUTSIDE_OFFSET_PROGRAM = """
stl.startup
    ;before + 2*dw
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


def _write(tmp_path: Path, source: str) -> List[str]:
    path = tmp_path / 'p.fj'
    path.write_text(source)
    return [str(path)]


def test_an_offset_from_outside_into_a_moved_table_fails_the_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = prove_relocation(W, _macros(_write(tmp_path, OUTSIDE_OFFSET_PROGRAM), W), BlockPool(W, POOL_BASE))
    assert any('(before + 128)' in mismatch for mismatch in proof.mismatches), proof.mismatches
    # assemble() refuses it (OffsetGuard); without that, the moved program prints nothing where the plain one prints B
    assert build_and_run(OUTSIDE_OFFSET_PROGRAM, tmp_path, None, name='plain')[0] == b'B'
    with pytest.raises(FlipJumpException, match='reaches across the place of the table tbl_start'):
        build_and_run(OUTSIDE_OFFSET_PROGRAM, tmp_path, BlockPool(W, POOL_BASE), name='pooled')
    no_refusals(monkeypatch)
    assert build_and_run(OUTSIDE_OFFSET_PROGRAM, tmp_path, BlockPool(W, POOL_BASE), name='unguarded')[0] != b'B'


@pytest.mark.parametrize('name', ['c07a_reach_in_plus', 'c07d_reach_in_plus_labelled'])
def test_an_outside_label_reaching_a_moved_tables_place_fails_the_proof(name: str) -> None:
    assert prove_relocation(W, _macros(_gap(name), W), _cli_pool(W)).mismatches


def test_a_label_before_a_moved_tables_pad_fails_the_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    # c06: its pooled expansion is refused (OffsetGuard), and without that the proof fails it
    with pytest.raises(FlipJumpException, match='names L, declared before the place of the table T'):
        prove_relocation(W, _macros(_gap('c06_label_before_pad'), W), _cli_pool(W))
    no_refusals(monkeypatch)
    assert prove_relocation(W, _macros(_gap('c06_label_before_pad'), W), _cli_pool(W)).mismatches


@pytest.mark.parametrize('name', ['c07b_reach_in_compound', 'c07c_reach_in_compound_labelled'])
def test_an_operand_computed_from_a_moved_label_fails_the_proof(name: str) -> None:
    # the `T + dw` of `(T + dw) | 0` is an address: mapped, it is the op after the table, which the pool moved apart
    mismatches = prove_relocation(W, _macros(_gap(name), W), _cli_pool(W)).mismatches
    assert any('((T + 64) | 0) should be' in mismatch for mismatch in mismatches), mismatches


def test_an_operand_computed_from_unmoved_labels_is_counted_unchecked(tmp_path: Path) -> None:
    # `(print_a | 0)` moves with print_a: an address, compared; `(print_a >> 6) & 15` is print_a's address
    # used as a number: unchecked
    base = OUTSIDE_OFFSET_PROGRAM.replace('    ;before + 2*dw\n', '    ;tbl_start + 2*dw\n')
    unchecked = prove_relocation(W, _macros(_write(tmp_path, base), W), BlockPool(W, POOL_BASE)).unchecked
    source = base.replace('    ;tbl_start + 2*dw\n', '    ;tbl_start + 2*dw\n    ;(print_a | 0)\n')
    proof = prove_relocation(W, _macros(_write(tmp_path, source), W), BlockPool(W, POOL_BASE))
    assert proof.mismatches == [] and len(proof.unchecked) == len(unchecked)
    source = base.replace('    ;tbl_start + 2*dw\n', '    ;tbl_start + 2*dw\n    ;(print_a >> 6) & 15\n')
    proof = prove_relocation(W, _macros(_write(tmp_path, source), W), BlockPool(W, POOL_BASE))
    assert proof.mismatches == [] and len(proof.unchecked) == len(unchecked) + 1
    assert '((print_a >> 6) & 15)' in proof.unchecked[-1]


def test_an_address_computed_on_from_one_label_is_compared(monkeypatch: pytest.MonkeyPatch) -> None:
    # `(L | 0) + 15*dw` is L plus what it adds - compared whole, so with the table it reaches over moved
    # (the guard keeps it inline) it fails the proof
    assert prove_relocation(W, _macros(_gap('c36_opaque_label_plus_offset'), W), _cli_pool(W)).mismatches == []
    every_table_moves(monkeypatch)
    mismatches = prove_relocation(W, _macros(_gap('c36_opaque_label_plus_offset'), W), _cli_pool(W)).mismatches
    assert any('((L | 0) + 960) should be' in mismatch for mismatch in mismatches), mismatches


CORRECT_CRAFTED = [
    'c20_dollar_across_table',
    'c21_inline_compound_across_table',
    'c22_label_digit_into_pinned',
    'c23_numeric_address_arm',
    'c24_constant_value',
    'c26_pointer_write_only',
    'c27_bitwise_arm',
    'c28_segment_back',
    'c30_count_bits_stays_pinned',
    'c32_numeric_arm_below_window',
    'c33_bitwise_arm_below_window',
    'c34_numeric_arm_wide_window',
    'c36_opaque_label_plus_offset',
]


@pytest.mark.parametrize('pool', ['cli', 'half'])
@pytest.mark.parametrize('name', CORRECT_CRAFTED)
def test_the_correct_crafted_programs_pass_the_proof(name: str, pool: str) -> None:
    table_pool = _cli_pool(W) if pool == 'cli' else BlockPool(W, 1 << (W - 1))
    assert prove_relocation(W, _macros(_gap(name), W), table_pool).mismatches == []


@pytest.mark.parametrize(
    'name, mismatch',
    [
        ('c01_raw_read_bit_ptr', 'pointer runtime'),
        ('c08_retarget_xor', 'outside its window'),
        ('c23_numeric_address_arm', 'outside its window'),
        ('c26_pointer_write_only', 'pointer runtime'),
        ('c27_bitwise_arm', 'flips bit 10 of the pinned word'),
        ('c32_numeric_arm_below_window', 'which could arm the table at 0x200'),
        ('c33_bitwise_arm_below_window', 'which could arm the table at 0x200'),
        ('c34_numeric_arm_wide_window', 'outside its window'),
    ],
)
def test_the_proof_fails_a_pin_that_cannot_hold(monkeypatch: pytest.MonkeyPatch, name: str, mismatch: str) -> None:
    # the control: the assembler keeps every pin (assembler.unpin does nothing); the proof judges the pins from the
    # plain program, not by the assembler's rules
    monkeypatch.setattr(relocation_proof, 'unpin', lambda *args, **kwargs: None)
    pool = _cli_pool(W)
    if mismatch == 'pointer runtime':  # a raw program's pool places its tables in no block - forced into blocks,
        monkeypatch.setattr(BlockPool, '_raw', lambda self: False)  # its words are pinned
    mismatches = prove_relocation(W, _macros(_gap(name), W), pool).mismatches
    assert any(mismatch in found for found in mismatches), mismatches


def test_the_proof_compares_a_number_where_an_address_goes(monkeypatch: pytest.MonkeyPatch) -> None:
    # the control: the table moves under `;$ + 15*dw`
    every_table_moves(monkeypatch)
    mismatches = prove_relocation(W, _macros(_gap('c20_dollar_across_table'), W), _cli_pool(W)).mismatches
    assert any('1152 should be' in mismatch for mismatch in mismatches), mismatches


def test_the_proof_compares_the_sums_of_a_computed_operand(monkeypatch: pytest.MonkeyPatch) -> None:
    # the control: the table moves under `(L + 15*dw) | 0`, and `b - a` spans a moved table
    every_table_moves(monkeypatch)
    no_refusals(monkeypatch)
    for name, operand in [
        ('c21_inline_compound_across_table', '((L + 960) | 0) should be'),
        ('c29_label_difference_value', '((((b - a) >> 6) & 15) * 64) should be'),
    ]:
        mismatches = prove_relocation(W, _macros(_gap(name), W), _cli_pool(W)).mismatches
        assert any(operand in mismatch for mismatch in mismatches), mismatches


@pytest.mark.parametrize(
    'name, memory_width, pool',
    [
        ('c19_no_label_after_call', W, 'cli'),  # an op after a moved table with no label of its own
        ('c10_wflip_area_crosses_pool', W, 'cli'),  # wflip values that are numbers equal to code addresses
        ('c03_pinned_in_reserve', W, 'half'),  # a label + offset into a `reserve` after a moved table
    ],
)
def test_correct_programs_pass_the_proof(name: str, memory_width: int, pool: str) -> None:
    table_pool = _cli_pool(memory_width) if pool == 'cli' else BlockPool(memory_width, 1 << (memory_width - 1))
    assert prove_relocation(memory_width, _macros(_gap(name), memory_width), table_pool).mismatches == []


def _with_extents_changed(monkeypatch: pytest.MonkeyPatch, start: int, end: int) -> None:
    """the plain expansion reports every declared table `start` ops later and `end` ops longer (the proof's
    control: what it reads as declared, the pool did not move)"""
    real_resolve = getattr(relocation_proof, 'resolve_macros')

    def changing(*args: Any, **kwargs: Any) -> Any:
        result = real_resolve(*args, **kwargs)
        extents = kwargs.get('relocatable_extents')
        if extents is not None:
            op_bits = 2 * args[0]
            extents[:] = [(s + start * op_bits, e + end * op_bits) for s, e in extents]
        return result

    monkeypatch.setattr('tests.unit.relocation_proof.resolve_macros', changing)


def test_a_table_moved_short_of_its_declaration_fails_the_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [str(PROGRAMS_DIR / 'hexlib_tests' / '2params' / 'and.fj')]
    _with_extents_changed(monkeypatch, 0, 1)  # the op after each table read as its last
    mismatches = prove_relocation(64, _macros(files, 64), _cli_pool(64)).mismatches
    assert any('moved in pieces' in mismatch for mismatch in mismatches), mismatches


def test_a_moved_op_that_no_declaration_holds_fails_the_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [str(PROGRAMS_DIR / 'hexlib_tests' / '2params' / 'and.fj')]
    _with_extents_changed(monkeypatch, 1, 0)  # each table's first op read as outside it
    mismatches = prove_relocation(64, _macros(files, 64), _cli_pool(64)).mismatches
    assert any('no op of a declared table' in mismatch for mismatch in mismatches), mismatches


def test_a_corrupted_moved_op_fails_the_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [str(PROGRAMS_DIR / 'hexlib_tests' / '2params' / 'and.fj')]
    real_resolve = getattr(relocation_proof, 'resolve_macros')

    def corrupting(*args: Any, **kwargs: Any) -> Any:
        stream, labels = real_resolve(*args, **kwargs)
        if kwargs.get('table_pool') is None:
            return stream, labels  # the plain program
        pool_start = kwargs['table_pool'].pool_start()

        class Corrupted:
            """the pooled stream, with the jump of its first moved op one op further"""

            def iter_ops(self) -> Iterator[Any]:
                in_pool, done = False, False
                for op in stream.iter_ops():
                    if isinstance(op, NewSegment):
                        in_pool = op.start_address >= pool_start
                    elif in_pool and not done and isinstance(op, FlipJump):
                        op = FlipJump(op.flip, Expr(('+', (op.jump, Expr(128)))), op.code_position)
                        done = True
                    yield op

        return Corrupted(), labels

    assert prove_relocation(64, _macros(files, 64), _cli_pool(64)).mismatches == []
    monkeypatch.setattr('tests.unit.relocation_proof.resolve_macros', corrupting)
    monkeypatch.setattr('tests.unit.relocation_proof.labels_resolve', lambda *args, **kwargs: None)
    monkeypatch.setattr('tests.unit.relocation_proof.unpin', lambda *args, **kwargs: None)
    mismatches = prove_relocation(64, _macros(files, 64), _cli_pool(64)).mismatches
    assert any('with the pool' in mismatch for mismatch in mismatches), mismatches


def test_a_pin_whose_arms_are_not_rewritten_fails_the_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    # the fuzzer's pin_floor control: the pinned words rest at value ^ base, but no arm or disarm is rewritten
    files = [str(PROGRAMS_DIR / 'hexlib_tests' / '2params' / 'and.fj')]
    assert prove_relocation(64, _macros(files, 64), _cli_pool(64)).mismatches == []
    monkeypatch.setattr(assembler.BinaryData, 'pinned_value', lambda self, word, value, kind: value)
    mismatches = prove_relocation(64, _macros(files, 64), _cli_pool(64)).mismatches
    assert any('of the pinned word' in mismatch for mismatch in mismatches), mismatches


def test_a_pin_that_does_not_rest_at_its_base_fails_the_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [str(PROGRAMS_DIR / 'hexlib_tests' / '2params' / 'and.fj')]
    real_insert = assembler.BinaryData.insert_fj_op

    def unbased(self: Any, flip: int, jump: int) -> None:
        pinned, self.pinned = self.pinned, None
        real_insert(self, flip, jump)
        self.pinned = pinned

    monkeypatch.setattr(assembler.BinaryData, 'insert_fj_op', unbased)
    mismatches = prove_relocation(64, _macros(files, 64), _cli_pool(64)).mismatches
    assert any('does not rest at its value xor its base' in mismatch for mismatch in mismatches), mismatches


def test_a_ternary_with_a_number_for_its_condition_is_compared_as_its_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    # hex.if_flags' entries (`(flags >> i) & 1 ? return + dbit : 0`) and hex.tables' `x ? label : other_label`: each
    # is one of its branches, so the proof compares it whole. The control: without that, each is listed unchecked
    files = [str(PROGRAMS_DIR / 'hexlib_tests' / 'basics1' / 'switch.fj')]
    proof = prove_relocation(64, _macros(files, 64), _cli_pool(64))
    assert proof.mismatches == [] and proof.unchecked == []
    monkeypatch.setattr(relocation_proof, '_selected', lambda operand: operand)
    unchecked = prove_relocation(64, _macros(files, 64), _cli_pool(64)).unchecked
    assert len(unchecked) > 2000 and all('?' in entry for entry in unchecked), unchecked[:3]
