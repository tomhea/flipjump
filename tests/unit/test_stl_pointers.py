"""
unit-tests for the stl's hex pointers and stack (flipjump/stl/hex/pointers/*.fj, ptrlib.fj), against python.

- a return on an empty stack reaches stack_error_handler (stack[-1] jumped to op 15 before: the run stopped silently);
- ptr_add / ptr_sub move the pointer modulo 2^w (a negative value moved it the wrong way, a value*2w >= 2^w wrote past
  it), and the constant-0 forms assemble.
"""

from pathlib import Path

import pytest

from tests.unit.unit_utils import run_source

EMPTY_STACK_MESSAGE = b'\n\nERROR: You returned on an empty stack.\n'


def _output(tmp_path: Path, program: str, memory_width: int = 64) -> bytes:
    _, io_device = run_source(program, tmp_path, memory_width=memory_width, use_stl=True)
    return io_device.get_output()


@pytest.mark.parametrize('memory_width', [32, 64])
def test_return_on_an_empty_stack_prints_the_error(tmp_path: Path, memory_width: int) -> None:
    program = 'stl.startup_and_init_all\nstl.output "before"\nstl.return\nstl.output "after"\nstl.loop\n'
    assert _output(tmp_path, program, memory_width) == b'before' + EMPTY_STACK_MESSAGE


def _ptr_case(index: int, macro: str, value: int, base: int) -> str:
    return f'''
    hex.{macro} p{index}, {value}
    hex.print w/8, p{index}
    hex.print 1, g{index}
    ;e{index}
  p{index}: hex.vec w/4, {base}
  g{index}: hex.vec 2, 0x5a
  e{index}:
'''


@pytest.mark.parametrize('memory_width', [32, 64])
def test_ptr_add_and_ptr_sub_modulo_2_pow_w(tmp_path: Path, memory_width: int) -> None:
    dw = 2 * memory_width
    big = (1 << memory_width) // dw  # value * 2w == 2^w: no move at all
    base = 7 * dw
    cases = [
        ('ptr_add', -1),
        ('ptr_add', -3),
        ('ptr_sub', -2),
        ('ptr_add', 0),
        ('ptr_sub', 0),
        ('ptr_add', 1),
        ('ptr_sub', 1),
        ('ptr_add', big),
        ('ptr_sub', big),
        ('ptr_add', big + 2),
        ('ptr_sub', 8),
    ]
    program = 'stl.startup\n' + ''.join(_ptr_case(i, *c, base) for i, c in enumerate(cases)) + 'stl.loop\nhex.init\n'
    expected = b''
    for macro, value in cases:
        moved = (base + (value if macro == 'ptr_add' else -value) * dw) % (1 << memory_width)
        expected += moved.to_bytes(memory_width // 8, 'little') + b'\x5a'
    assert _output(tmp_path, program, memory_width) == expected


def test_sp_sub_0_and_call_with_0_parameters(tmp_path: Path) -> None:
    """`hex.sp_sub 0` and `stl.call f, 0` assemble (sub_constant 0 did not) and leave sp where it was."""
    program = '''
stl.startup_and_init_all
    stl.get_sp before
    hex.sp_sub 0
    stl.call f, 0
    stl.get_sp after
    hex.print w/8, before
    hex.print w/8, after
    stl.loop
f:
    stl.output 'f'
    stl.return
before: hex.vec w/4
after: hex.vec w/4, (0-1)
'''
    output = _output(tmp_path, program)
    assert output[:1] == b'f' and output[1:9] == output[9:17] != bytes(8)


CELLS = '  cells: ;0x12*dw\n  ;0x34*dw\n  ;0x56*dw\n  ;0x78*dw\n'


def test_the_and_inc_forms_no_program_reached(tmp_path: Path) -> None:
    """read_hex_and_inc, write_hex_and_inc, hex.pointers.xor_hex_to_ptr_and_inc / xor_byte_to_ptr_and_inc: the value
    and the pointer advanced by one cell (no program called them: mutants without the inc passed)."""
    program = '''
stl.startup_and_init_all
    hex.read_hex_and_inc h, p
    hex.write_hex_and_inc p, five
    hex.pointers.xor_hex_to_ptr_and_inc p, five
    hex.pointers.xor_byte_to_ptr_and_inc p, ff
    hex.sub w/4, p, base
    hex.print w/8, p
    hex.print 1, h
    hex.set w/4, p, cells
    rep(4, i) hex.read_byte_and_inc b+2*i*dw, p
    hex.print 4, b
    stl.loop
  p: hex.vec w/4, cells
  base: hex.vec w/4, cells
  h: hex.vec 2, 0xa0
  five: hex.hex 5
  ff: hex.vec 2, 0xff
  b: hex.vec 8
''' + CELLS
    # p moved 4 cells; h = cell 0's low hex (0x2); cell 1 = its high hex kept, low hex 5; cell 2 ^= 5; cell 3 ^= 0xff
    assert _output(tmp_path, program) == (4 * 128).to_bytes(8, 'little') + b'\xa2' + bytes([0x12, 0x35, 0x53, 0x87])


@pytest.mark.parametrize('memory_width', [32, 64])
def test_an_n_cell_read_across_the_middle_of_memory(tmp_path: Path, memory_width: int) -> None:
    """read_byte 4 over cells straddling bit address 2^(w-1): the pointer advance carries into its top bit (a mutant
    that never carries there passed: no test crossed it)."""
    program = '''
stl.startup_and_init_all
    hex.read_byte 4, d, p
    hex.print 4, d
    stl.loop
  p: hex.vec w/4, cells
  d: hex.vec 8
segment (1<<(w-1)) - 2*dw
''' + CELLS
    assert _output(tmp_path, program, memory_width) == bytes([0x12, 0x34, 0x56, 0x78])


def test_one_ptr_index_n_and_read_entry_site_run_twice(tmp_path: Path) -> None:
    """a narrow ptr_index n site and a read_entry site, each run twice with different indexes (a loop): their
    scaled-index scratch must be zeroed each time (mutants without the zero passed: every test site ran once)."""
    program = '''
stl.startup_and_init_all
  again:
    hex.ptr_index 2, d, base, i
    hex.sub w/4, d, base
    hex.print w/8, d
    hex.read_entry 2, e, table, 2, i
    hex.print 1, e
    hex.read_entry_byte i, table, 2, i
    hex.print 1, i
    bit.if1 done, finish
    bit.not done
    hex.set 2, i, 3
    ;again
  finish:
    stl.loop
  base: hex.vec w/4, 0x40000
  d: hex.vec w/4
  e: hex.vec 2
  i: hex.vec 2, 1
  done: bit.bit 0
  table: rep(4, k) hex.vec 2, 0x11*(k+1)
'''
    # entry k of the hex[:2] table is 0x11*(k+1); as bytes (read_entry_byte, dst == idx), cell 2k is k+1's low hex...
    expected = b''
    for index in (1, 3):
        expected += (index * 128).to_bytes(8, 'little') + bytes([0x11 * (index + 1)])
        expected += bytes([(0x11 * (index // 2 + 1) >> (4 * (index % 2))) & 0xF])
    assert _output(tmp_path, program) == expected


def test_get_sp_with_a_dirty_dst(tmp_path: Path) -> None:
    """get_sp copies all w/4 hexes (a mutant copying w/4 - 1 left dst's top hex)."""
    program = '''
stl.startup_and_init_all
    stl.get_sp d
    hex.xor w/4, d, hex.pointers.sp
    hex.print w/8, d
    stl.loop
  d: hex.vec w/4, (0-1)
'''
    assert _output(tmp_path, program) == bytes(8)


def test_read_entry_packed_into_its_own_index(tmp_path: Path) -> None:
    """read_entry_packed's documented alias: dst == idx (the entry overwrites the index it was read by)."""
    program = '''
stl.startup_and_init_all
    hex.read_entry_packed 2, j, table, 2, j
    hex.print 2, j
    stl.loop
  j: hex.vec 4, 2
  table: rep(8, k) stl.fj 0, (0x30+k)*dw
'''
    assert _output(tmp_path, program) == b'45'
