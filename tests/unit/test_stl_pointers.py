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
    cases = [('ptr_add', -1), ('ptr_add', -3), ('ptr_sub', -2), ('ptr_add', 0), ('ptr_sub', 0), ('ptr_add', 1),
             ('ptr_sub', 1), ('ptr_add', big), ('ptr_sub', big), ('ptr_add', big + 2), ('ptr_sub', 8)]
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
