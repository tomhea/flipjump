"""
unit-tests for hex.idiv (flipjump/stl/hex/div.fj) against python, at every rem_opt: the quotient, the remainder (its
sign by rem_opt), and a, b restored. Exact divisions with mixed signs are the edge: rem_opt 0 and 2 "corrected" a zero
remainder too (0 / -5 gave q = -1, r = -5). q and r are preset dirty; b = 0 jumps to div0 and leaves them.
"""

from pathlib import Path
from typing import List, Tuple

import pytest

from tests.unit.unit_utils import run_source

# (n, nb, a, b): signed values.
CASES: List[Tuple[int, int, int, int]] = [
    (2, 2, 0, -5),
    (2, 2, -6, 3),
    (2, 2, 6, -3),
    (2, 2, -6, -3),
    (2, 2, 6, 3),
    (2, 2, -8, 1),
    (2, 2, -1, -1),
    (2, 2, -128, 1),
    (2, 2, -128, -1),
    (2, 2, -128, 2),
    (2, 2, 127, -127),
    (2, 2, 7, -2),
    (2, 2, -7, 2),
    (2, 2, -7, -2),
    (2, 2, 20, 12),
    (2, 2, -20, 12),
    (4, 2, -0x1000, 0x10),
    (4, 2, 0x1000, -0x10),
    (4, 2, -0x1001, 0x10),
    (2, 2, 5, 0),
    (2, 2, -5, 0),
]


def _signed_div(a: int, b: int, rem_opt: int) -> Tuple[int, int]:
    q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)  # truncated: sign(r) == sign(a)
    r = a - q * b
    if r and rem_opt == 0 and (r < 0) != (b < 0):
        q, r = q - 1, r + b
    if r < 0 and rem_opt == 2:
        q, r = (q - 1, r + b) if b > 0 else (q + 1, r - b)
    return q, r


def _case_code(index: int, rem_opt: int, n: int, nb: int, a: int, b: int) -> str:
    return f'''
    hex.idiv {n}, {nb}, q{index}, r{index}, a{index}, b{index}, z{index}, {rem_opt}
    ;p{index}
  z{index}:
    stl.output 'Z'
  p{index}:
    hex.print {n // 2}, q{index}
    hex.print {nb // 2}, r{index}
    hex.print {n // 2}, a{index}
    hex.print {nb // 2}, b{index}
    ;e{index}
  q{index}: hex.vec {n}, {16**n - 1}
  r{index}: hex.vec {nb}, {16**nb - 1}
  a{index}: hex.vec {n}, {a % 16**n}
  b{index}: hex.vec {nb}, {b % 16**nb}
  e{index}:
'''


def _expected(rem_opt: int, n: int, nb: int, a: int, b: int) -> bytes:
    q, r = (16**n - 1, 16**nb - 1) if b == 0 else _signed_div(a, b, rem_opt)
    values = [(q, n), (r, nb), (a, n), (b, nb)]
    return (b'Z' if b == 0 else b'') + b''.join((v % 16**k).to_bytes(k // 2, 'little') for v, k in values)


@pytest.mark.parametrize('rem_opt', [0, 1, 2])
def test_idiv_against_python(tmp_path: Path, rem_opt: int) -> None:
    program = 'stl.startup\n' + ''.join(_case_code(i, rem_opt, *c) for i, c in enumerate(CASES))
    _, io_device = run_source(program + 'stl.loop\nhex.init\n', tmp_path, use_stl=True)
    assert io_device.get_output() == b''.join(_expected(rem_opt, *c) for c in CASES)
