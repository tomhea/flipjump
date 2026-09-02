"""hex.pointers.PTR_CELL_BITS: the width of one pointed-to memory cell, and so of the decoder table.

These are the only tests that exercise a WIDE pointer table, because the .fj test tables in
tests/tests_tables have no column for -D. Each case assembles a real pointer program at 8, 12 and
16 bits and requires the recorded output back.

The knob-efficacy assertion is what makes the output assertions mean something: if -D reached
nothing, all three widths would be the same binary and "it still works at 16" would be vacuous.
That is not hypothetical -- the first version of this reached nothing at all, because a constant
is substituted where it is USED, at parse time, and the defines file was being read after the stl.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from flipjump import assemble, run_test_output

REPO = Path(__file__).resolve().parents[2]
POINTER_PROGRAMS = [
    ("pointer_setters", "programs/hexlib_tests/basics2/pointer_setters.fj",
     "tests/inout/hexlib_tests/basics2/pointer_setters.out"),
    ("hex_ptr", "programs/concept_checks/hex_ptr.fj", "tests/inout/concept_checks/hex_ptr.out"),
    ("nth_pointers", "programs/hexlib_tests/basics2/nth_pointers.fj",
     "tests/inout/hexlib_tests/basics2/nth_pointers.out"),
]
DRIVER = (
    "import sys; from flipjump import assemble_run_according_to_cmd_line_args as go; "
    "go(cmd_line_args=sys.argv[1:])"
)


def _assemble_at(tmp_path: Path, program: str, cell_bits: int) -> Path:
    """assemble in a CHILD process, so the parser's stl-prefix cache cannot carry one width into
    another, and so a -D that makes the stl unparseable fails here rather than poisoning pytest."""
    out = tmp_path / f"cell{cell_bits}_{Path(program).stem}.fjm"
    args = [sys.executable, "-c", DRIVER, "--asm", "-o", str(out), "-w", "64"]
    if cell_bits != 8:
        args += ["-D", f"hex.pointers.PTR_CELL_BITS = {cell_bits}"]
    args.append(str(REPO / program))
    result = subprocess.run(args, cwd=str(REPO), capture_output=True, text=True, timeout=900)
    assert result.returncode == 0, f"assembling at PTR_CELL_BITS={cell_bits} failed:\n{result.stdout}{result.stderr}"
    return out


@pytest.mark.parametrize("name,program,expected", POINTER_PROGRAMS, ids=[p[0] for p in POINTER_PROGRAMS])
@pytest.mark.parametrize("cell_bits", [8, 12, 16])
def test_pointer_program_is_correct_at_every_cell_width(
    tmp_path: Path, name: str, program: str, expected: str, cell_bits: int
) -> None:
    """a wider cell must not change what a program prints -- only how many bits one dereference
    can carry. read_byte/read_hex keep taking the low 2/1 hexes of a wider read_byte register."""
    fjm = _assemble_at(tmp_path, program, cell_bits)
    assert run_test_output(fjm, b"", (REPO / expected).read_bytes(), print_time=False, print_termination=False)


def test_ptr_cell_bits_actually_reaches_the_decoder_table(tmp_path: Path) -> None:
    """the control for the test above. PTR_CELL_BITS sizes a `pad` and a `rep` inside ptr_init, so a
    wider setting must produce a bigger, different binary. Without this, "still correct at 16"
    would be satisfied by a -D that reached nothing at all."""
    program = POINTER_PROGRAMS[0][1]
    sizes = {}
    for cell_bits in (8, 12, 16):
        sizes[cell_bits] = _assemble_at(tmp_path, program, cell_bits).read_bytes()
    assert sizes[8] != sizes[12], "-D hex.pointers.PTR_CELL_BITS did not reach the table"
    assert sizes[12] != sizes[16], "-D hex.pointers.PTR_CELL_BITS did not reach the table"


def test_ptr_cell_bits_is_overridable_because_the_defines_file_precedes_the_stl(tmp_path: Path) -> None:
    """PTR_CELL_BITS is declared in runlib.fj and consumed in basic_pointers.fj, both stl files. A
    constant is substituted where it is USED, at parse time, so an override read after the stl
    arrives too late. This asserts the ordering that makes the override possible at all."""
    from flipjump.utils.functions import get_stl_paths

    stl_names = [p.name for p in get_stl_paths()]
    assert stl_names.index("runlib.fj") < stl_names.index("basic_pointers.fj")


def _expected_wide_cells(cell_bits: int) -> bytes:
    """what wide_cells.fj must print at this width, derived from the program rather than recorded:
    hex i holds i+1 and print_as_digit prints most-significant first, the all-ones case is the
    highest table entry the cell can reach, and the byte API always sees the low byte."""
    n = cell_bits // 4
    pattern = "".join(str(i) for i in range(n, 0, -1))
    return ("cell:%s" % pattern + chr(10) + "ones:%s" % ("f" * n) + chr(10)
            + "zeroed:%s" % ("0" * n) + chr(10) + "lowbyte:21" + chr(10)).encode()


@pytest.mark.parametrize("cell_bits", [8, 12, 16])
def test_a_whole_cell_round_trips_at_every_width(tmp_path: Path, cell_bits: int) -> None:
    """the test the rest of the pointer suite cannot be: every other program stores values that
    fit in a byte, so it never lands on a table entry above 255, never sets a high hex of
    read_byte, and cannot tell a wide table from a narrow one. Three mutations of the table
    survived the whole suite until this existed."""
    fjm = _assemble_at(tmp_path, "programs/hexlib_tests/basics2/wide_cells.fj", cell_bits)
    assert run_test_output(fjm, b"", _expected_wide_cells(cell_bits),
                           print_time=False, print_termination=False)
