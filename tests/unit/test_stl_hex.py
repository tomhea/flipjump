"""
unit-tests for the stl's hex macros (flipjump/stl/hex/*.fj) that no program test reached, against python: the
quadrupled_exact_xor and sparse_* families, the allowed self-aliases (mov / swap x, x), and the input / print edges
(a '\\0' terminator, a leading '+', a 0-byte inside a line, x after printing a negative). Each test names the mutant it
catches.
"""

from pathlib import Path

import pytest

from tests.unit.unit_utils import run_source


def _output(tmp_path: Path, program: str, fixed_input: bytes = b'') -> bytes:
    _, io_device = run_source(program, tmp_path, fixed_input, use_stl=True)
    return io_device.get_output()


def test_quadrupled_exact_xor_every_source(tmp_path: Path) -> None:
    """every source value 0..15 walks entry V of each of the 4 chained tables (a wrong bit in the fourth table passed
    every test: nothing reached the macro)."""
    program = '''
stl.startup
    rep(16, v) xor_four v
    stl.loop
  a: hex.hex 0x0
  b: hex.hex 0x5
  c: hex.hex 0xa
  d: hex.hex 0xf
  consts: rep(16, j) hex.hex j

def xor_four v < a, b, c, d, consts {
    hex.quadrupled_exact_xor a+dbit+3, a+dbit+2, a+dbit+1, a+dbit+0, b+dbit+3, b+dbit+2, b+dbit+1, b+dbit+0, \\
                             c+dbit+3, c+dbit+2, c+dbit+1, c+dbit+0, d+dbit+3, d+dbit+2, d+dbit+1, d+dbit+0, consts+v*dw
    hex.print 2, a
    hex.print 2, c
}
'''
    # a, b, c, d are adjacent hexes: print 2 bytes from a (a, b, c, d) and 2 from c (c, d, consts[0], consts[1])
    expected = b''
    values = [0x0, 0x5, 0xA, 0xF]
    for v in range(16):
        values = [x ^ v for x in values]
        expected += bytes([values[0] | values[1] << 4, values[2] | values[3] << 4])
        expected += bytes([values[2] | values[3] << 4, 0x10])
    assert _output(tmp_path, program) == expected


@pytest.mark.parametrize('pad', [16, 64])
def test_the_sparse_forms(tmp_path: Path, pad: int) -> None:
    """sparse_mov (n) over a dirty dst (a mutant without its zero xored), sparse_zero n, and sparse_xor over every
    source value (a wrong table entry passed every program test)."""
    program = f'''
stl.startup
    hex.sparse_mov {pad}, m1, s1
    hex.sparse_mov {pad}, 4, m4, s4
    hex.sparse_zero {pad}, 4, z4
    hex.print 1, m1
    hex.print 2, m4
    hex.print 2, z4
    hex.print 2, y4
    rep(16, v) double v
    stl.loop
  m1: hex.vec 2, 0xff
  s1: hex.vec 2, 0x07
  m4: hex.vec 4, 0xffff
  s4: hex.vec 4, 0x1234
  z4: hex.vec 4, 0x0f0f
  y4: hex.vec 4, 0xabcd
  t: hex.vec 2, 0x05
  consts: rep(16, j) hex.hex j

def double v < t, consts {{
    hex.sparse_xor {pad}, t, consts+v*dw
    hex.print 1, t
}}
'''
    expected = bytes([0xF7]) + (0x1234).to_bytes(2, 'little') + bytes(2) + (0xABCD).to_bytes(2, 'little')
    t = 0x5
    for v in range(16):
        t ^= v
        expected += bytes([t])
    assert _output(tmp_path, program) == expected


def test_mov_and_swap_on_themselves(tmp_path: Path) -> None:
    """the documented self-aliases: hex.mov / hex.swap x, x (single and n) keep x (a mov without its dst==src check
    zeroes x: no test moved a variable onto itself)."""
    program = '''
stl.startup
    hex.mov x, x
    hex.mov 4, y, y
    hex.swap x, x
    hex.swap 4, y, y
    hex.print 1, x
    hex.print 2, y
    stl.loop
  x: hex.vec 2, 0x9a
  y: hex.vec 4, 0x5e7c
'''
    assert _output(tmp_path, program) == bytes([0x9A]) + (0x5E7C).to_bytes(2, 'little')


def test_print_dec_int_restores_x(tmp_path: Path) -> None:
    """print_dec_int negates a negative x to print it, and must negate it back (a mutant leaving it passed)."""
    program = '''
stl.startup
    hex.print_dec_int 2, x
    hex.print 1, x
    stl.loop
  x: hex.vec 2, 0xd6
hex.init
'''
    assert _output(tmp_path, program) == b'-42\xd6'


def test_input_dec_terminators_and_a_leading_plus(tmp_path: Path) -> None:
    """input_dec_uint accepts a '\\0' terminator as it does '\\n' (a '\\n'-only mutant passed: no test ended a number
    with a 0-byte); input_dec_int_until stops at a leading '+' with 0 (a mutant negating on '+' passed)."""
    program = '''
stl.startup
    hex.input_dec_uint 4, u, error
    hex.print 2, u
    hex.input_dec_int_until 4, s, stop
    hex.print 2, s
    hex.print 1, stop
    stl.loop
  error:
    stl.output 'E'
    stl.loop
  u: hex.vec 4, 0xffff
  s: hex.vec 4, 0xffff
  stop: hex.vec 2
hex.init
'''
    assert _output(tmp_path, program, b'123\x00+5\n') == (123).to_bytes(2, 'little') + bytes(2) + b'+'


def test_input_ptr_line_stops_at_a_zero_byte(tmp_path: Path) -> None:
    """input_ptr_line ends the line at a 0-byte too (a mutant reading past it passed: no input line held one)."""
    program = '''
stl.startup_and_init_all
    hex.input_ptr_line p, len
    hex.print w/8, len
    hex.print_ptr_text p, len
    stl.loop
  p: hex.vec w/4, buf
  len: hex.vec w/4, (0-1)
  buf: rep(8, i) stl.fj 0, 0
'''
    output = _output(tmp_path, program, b'ab\x00cd\n')
    assert output == (2).to_bytes(8, 'little') + b'ab'
