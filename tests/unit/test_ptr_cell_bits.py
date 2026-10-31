"""
unit-tests for hex.pointers.PTR_CELL_BITS / PTR_WIDE_BITS: the widths of a pointed-to memory cell, and so of the
decoder table(s) ptr_init lays down.

these are the only tests of a WIDE pointer table (the .fj test tables have no column for -D). each case assembles a
real pointer program at several width configurations and requires the recorded output back. a configuration is a
pair: every cell PTR_CELL_BITS wide (wide_bits=0, one table), or narrow cells plus a SECOND table for the sites that
ask for it (wide_bits>0, two tables coexisting).

the knob-efficacy test is the control of the output tests: if -D reached nothing, every width would be the same
binary, and "it still works at 16" would be vacuous (a constant is substituted where it is USED, at parse time, so
a defines file read after the stl reaches nothing).
"""

import subprocess  # nosec B404 - the test drives the fj CLI
import sys
from pathlib import Path
from typing import Dict, Union

import pytest

from flipjump import assemble, run_test_output

REPO = Path(__file__).resolve().parents[2]
POINTER_PROGRAMS = [
    (
        'pointer_setters',
        'programs/hexlib_tests/basics2/pointer_setters.fj',
        'tests/inout/hexlib_tests/basics2/pointer_setters.out',
    ),
    ('hex_ptr', 'programs/concept_checks/hex_ptr.fj', 'tests/inout/concept_checks/hex_ptr.out'),
    (
        'nth_pointers',
        'programs/hexlib_tests/basics2/nth_pointers.fj',
        'tests/inout/hexlib_tests/basics2/nth_pointers.out',
    ),
    (
        'read_write_back',
        'programs/hexlib_tests/basics2/read_write_back.fj',
        'tests/inout/hexlib_tests/basics2/read_write_back.out',
    ),
    (
        'seq_pointers',
        'programs/hexlib_tests/basics2/seq_pointers.fj',
        'tests/inout/hexlib_tests/basics2/seq_pointers.out',
    ),
]
# seq_pointers takes ~30 s an assembly: it runs with --slow
POINTER_PARAMS = [
    pytest.param(*program, id=program[0], marks=[pytest.mark.slow] if program[0] == 'seq_pointers' else [])
    for program in POINTER_PROGRAMS
]
# (PTR_CELL_BITS, PTR_WIDE_BITS) pairs. 0 means "no wide table".
WIDTH_CONFIGS = [(8, 0), (12, 0), (16, 0), (8, 12), (8, 16), (12, 16)]
DRIVER = (
    'import sys; from flipjump import assemble_run_according_to_cmd_line_args as go; go(cmd_line_args=sys.argv[1:])'
)


def _assemble_at(tmp_path: Path, program: str, cell_bits: int, wide_bits: int = 0) -> Path:
    """
    assemble in a child process: the parser's stl-prefix cache cannot carry one width into another, and a -D that
    makes the stl unparseable fails here rather than poisoning pytest.
    """
    out = tmp_path / f'cell{cell_bits}w{wide_bits}_{Path(program).stem}.fjm'
    args = [sys.executable, '-c', DRIVER, '--asm', '-o', str(out), '-w', '64']
    if cell_bits != 8:
        args += ['-D', f'hex.pointers.PTR_CELL_BITS = {cell_bits}']
    if wide_bits != 0:
        args += ['-D', f'hex.pointers.PTR_WIDE_BITS = {wide_bits}']
    args.append(str(REPO / program))
    result = subprocess.run(args, cwd=str(REPO), capture_output=True, text=True, timeout=900)  # nosec B603
    assert result.returncode == 0, (
        f'assembling at PTR_CELL_BITS={cell_bits}, PTR_WIDE_BITS={wide_bits} failed:'
        f'\n{result.stdout}{result.stderr}'
    )
    return out


@pytest.mark.parametrize('name, program, expected', POINTER_PARAMS)
@pytest.mark.parametrize('cell_bits', [8, 12, 16])
def test_pointer_program_is_correct_at_every_cell_width(
    tmp_path: Path, name: str, program: str, expected: str, cell_bits: int
) -> None:
    # a wider cell changes how many bits one dereference carries, not what a program prints:
    # read_byte / read_hex keep taking the low 2 / 1 hexes of a wider read_byte register
    fjm = _assemble_at(tmp_path, program, cell_bits)
    assert run_test_output(fjm, b'', (REPO / expected).read_bytes(), print_time=False, print_termination=False)


@pytest.mark.parametrize('name, program, expected', POINTER_PARAMS)
def test_pointer_program_is_unperturbed_by_a_wide_table(tmp_path: Path, name: str, program: str, expected: str) -> None:
    # a program that never uses read_cell runs the same with a wide table laid: a narrow dereference arms the slot
    # at dbit+PTR_CELL_BITS and never lands in the table at op 2^PTR_WIDE_BITS
    fjm = _assemble_at(tmp_path, program, 8, wide_bits=16)
    assert run_test_output(fjm, b'', (REPO / expected).read_bytes(), print_time=False, print_termination=False)


def test_ptr_cell_bits_actually_reaches_the_decoder_table(tmp_path: Path) -> None:
    # the control of the tests above: PTR_CELL_BITS sizes a `pad` and a `rep` inside ptr_init, and PTR_WIDE_BITS
    # lays a SECOND table, so each knob must produce a bigger, different binary
    program = POINTER_PROGRAMS[0][1]
    binaries = {}
    for cell_bits, wide_bits in [(8, 0), (12, 0), (16, 0), (8, 16)]:
        binaries[(cell_bits, wide_bits)] = _assemble_at(tmp_path, program, cell_bits, wide_bits).read_bytes()
    assert binaries[(8, 0)] != binaries[(12, 0)], '-D hex.pointers.PTR_CELL_BITS did not reach the table'
    assert binaries[(12, 0)] != binaries[(16, 0)], '-D hex.pointers.PTR_CELL_BITS did not reach the table'
    assert binaries[(8, 16)] != binaries[(8, 0)], '-D hex.pointers.PTR_WIDE_BITS laid no wide table'
    assert binaries[(8, 16)] != binaries[(16, 0)], 'the wide table aliased PTR_CELL_BITS instead of joining it'


def test_ptr_cell_bits_is_overridable_because_the_defines_file_precedes_the_stl() -> None:
    # PTR_CELL_BITS is declared in runlib.fj and used in basic_pointers.fj, both stl files: an override read after
    # the stl would arrive too late
    from flipjump.utils.functions import get_stl_paths

    stl_names = [p.name for p in get_stl_paths()]
    assert stl_names.index('runlib.fj') < stl_names.index('basic_pointers.fj')


def _expected_wide_cells(cell_bits: int, wide_bits: int) -> bytes:
    """
    what wide_cells.fj prints at this width configuration, derived from the program rather than recorded: hex i of
    the cell pattern holds i+1 and print_as_digit prints most-significant first, the all-ones case is the highest
    table entry the cell can reach, the byte API always sees the low byte, the pair section walks two adjacent cells,
    and the zeroptr section checks the whole narrow cell when it can (one table) and a narrow byte round-trip when it
    cannot.
    """
    n = (wide_bits if wide_bits else cell_bits) // 4
    pattern = ''.join(f'{i + 1:x}' for i in range(n - 1, -1, -1))
    pair_first = ''.join(f'{i + 2:x}' for i in range(n - 1, -1, -1))
    pair_second = ''.join(f'{0xd - i:x}' for i in range(n - 1, -1, -1))
    zeroptr = '0' * n if wide_bits == 0 else '00'
    return (
        f"cell:{pattern}\nones:{'f' * n}\nzeroed:{'0' * n}\nlowbyte:21\n"
        f'pair:{pair_first}.{pair_second}\nzeroptr:{zeroptr}\n'
    ).encode()


@pytest.mark.parametrize('cell_bits, wide_bits', WIDTH_CONFIGS)
def test_a_whole_cell_round_trips_at_every_width(tmp_path: Path, cell_bits: int, wide_bits: int) -> None:
    # every other pointer program stores values that fit in a byte, so it never lands on a table entry above 255,
    # never sets a high hex of read_byte, and cannot tell a wide table from a narrow one. the wide_bits>0 rows run
    # narrow and wide dereferences against BOTH tables in one program, and walk adjacent cells with the _and_inc
    # pair; zero_byte covering less than the whole narrow cell fails the (16, 0) row
    fjm = _assemble_at(tmp_path, 'programs/hexlib_tests/basics2/wide_cells.fj', cell_bits, wide_bits)
    assert run_test_output(
        fjm, b'', _expected_wide_cells(cell_bits, wide_bits), print_time=False, print_termination=False
    )


@pytest.mark.parametrize('bad_bits', [4, 7, 10, 60])
def test_a_bad_ptr_cell_bits_is_refused_at_assemble_time(tmp_path: Path, bad_bits: int) -> None:
    # none of these faults loudly on its own: 7 reads 0x41 back as 0x01, and 4 is a multiple of 4 with dbit+4 < dw,
    # yet wild-jumps, because the byte API writes two hexes into a register sized PTR_CELL_BITS/4
    # (60: a multiple of 4 and >= 8, so only the dw bound refuses it: dbit + 60 >= dw at w=64)
    with pytest.raises(AssertionError, match='PTR_CELL_BITS__must'):
        _assemble_at(tmp_path, 'programs/hexlib_tests/basics2/pointer_setters.fj', bad_bits)


@pytest.mark.parametrize('cell_bits, bad_wide_bits', [(8, 8), (8, 10), (16, 12), (8, 60)])
def test_a_bad_ptr_wide_bits_is_refused_at_assemble_time(tmp_path: Path, cell_bits: int, bad_wide_bits: int) -> None:
    # a wide table not wider than the narrow one has no reason to exist and (8, 8) would lay two tables at the same
    # op; 10 is not a hex boundary; and a "wide" table narrower than the cells (16, 12) inverts which arming bit
    # lands where
    # (8, 60): only the dw bound refuses it
    with pytest.raises(AssertionError, match='PTR_WIDE_BITS__must'):
        _assemble_at(tmp_path, 'programs/hexlib_tests/basics2/wide_cells.fj', cell_bits, bad_wide_bits)


def test_the_api_defines_actually_reach_the_parse(tmp_path: Path) -> None:
    # flipjump.assemble() takes defines too, not only the CLI: a parameter that was accepted and ignored would
    # build fine, just not overridden
    prog = tmp_path / 'prog.fj'
    prog.write_text('ns a {\n  GREET = 0x5\n}\nstl.startup\nhex.print_as_digit g, 0\nstl.loop\n  g: hex.hex a.GREET\n')

    plain, overridden = tmp_path / 'plain.fjm', tmp_path / 'ov.fjm'
    assemble([prog], plain, memory_width=32, print_time=False)
    assemble([prog], overridden, memory_width=32, print_time=False, defines={'a.GREET': 0x8})
    assert plain.read_bytes() != overridden.read_bytes(), 'defines was ignored'

    prog.write_text(prog.read_text().replace('0x5', '0x8'))
    written = tmp_path / 'written.fjm'
    assemble([prog], written, memory_width=32, print_time=False)
    assert overridden.read_bytes() == written.read_bytes(), 'the override is not the written value'
    expression = tmp_path / 'expression.fjm'
    assemble([prog], expression, memory_width=32, print_time=False, defines={'a.GREET': 'w / 4'})
    assert expression.read_bytes() == written.read_bytes(), 'a string value is not an fj expression'


@pytest.mark.parametrize('defines', [{'2GREET': 1}, {'a..b': 1}, {'GREET GREET': 1}, {'GREET': ''}, {'GREET': '1\n2'}])
def test_the_api_refuses_a_malformed_define(tmp_path: Path, defines: Dict[str, Union[int, str]]) -> None:
    prog = tmp_path / 'prog.fj'
    prog.write_text('GREET = 0x5\nstl.startup\nstl.loop\n')
    with pytest.raises(ValueError, match='define'):
        assemble([prog], tmp_path / 'o.fjm', memory_width=32, print_time=False, defines=defines)
    assemble([prog], tmp_path / 'o.fjm', memory_width=32, print_time=False, defines={'GREET': 1})  # the control
