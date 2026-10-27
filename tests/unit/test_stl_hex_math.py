"""
unit-tests for the stl's hex constant adders (flipjump/stl/hex/math.fj): hex.add_constant / hex.sub_constant add
const modulo 16^n, so a constant that does not fit (16^n and over, or negative) touches nothing past dst, and a zero
constant assembles (sub_constant had no guard: `negative shift count`). Every dst sits between two guard hexes.
"""

from pathlib import Path
from typing import List, Tuple

import pytest

from tests.unit.unit_utils import run_source

GUARD_LOW, GUARD_HIGH = 0xA, 0x5

# (macro, n, dst value, const): n is even, so dst and its two guards print as whole bytes.
CASES: List[Tuple[str, int, int, int]] = [
    ('add_constant', 4, 0x1234, 1),
    ('add_constant', 4, 0xFFFF, 1),
    ('add_constant', 2, 0x34, 0x100),
    ('add_constant', 2, 0x34, 0x1FF),
    ('add_constant', 2, 0x34, 0x1000),
    ('add_constant', 2, 0x34, -1),
    ('add_constant', 4, 0x1234, -0x235),
    ('add_constant', 4, 0x1234, 0),
    ('sub_constant', 4, 0x1234, 0),
    ('sub_constant', 4, 0x1234, 1),
    ('sub_constant', 4, 0x0000, 1),
    ('sub_constant', 2, 0x34, 0x100),
    ('sub_constant', 4, 0x1234, 0x10000),
    ('sub_constant', 2, 0x34, 0x1FF),
    ('sub_constant', 2, 0x34, -3),
]


def _case_code(index: int, macro: str, n: int, value: int, const: int) -> str:
    vector = GUARD_LOW | value << 4 | GUARD_HIGH << (4 * (n + 1))
    return f'''
    hex.{macro} {n}, v{index}+dw, {const}
    hex.print {(n + 2) // 2}, v{index}
    ;e{index}
  v{index}: hex.vec {n + 2}, {vector}
  e{index}:
'''


def _expected(macro: str, n: int, value: int, const: int) -> bytes:
    result = (value + const if macro == 'add_constant' else value - const) % (1 << (4 * n))
    return (GUARD_LOW | result << 4 | GUARD_HIGH << (4 * (n + 1))).to_bytes((n + 2) // 2, 'little')


@pytest.mark.parametrize('memory_width', [32, 64])
def test_add_and_sub_constant_modulo_16_pow_n(tmp_path: Path, memory_width: int) -> None:
    program = 'stl.startup\n' + ''.join(_case_code(i, *case) for i, case in enumerate(CASES)) + 'stl.loop\nhex.init\n'
    _, io_device = run_source(program, tmp_path, memory_width=memory_width, use_stl=True)
    assert io_device.get_output() == b''.join(_expected(*case) for case in CASES)
