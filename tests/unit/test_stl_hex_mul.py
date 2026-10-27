"""
unit-tests for hex.mul (flipjump/stl/hex/mul.fj) against python. It loops over the operand with the smaller value
(the loop runs once per hex up to that operand's top non-zero hex, and adds a row per non-zero hex), so the operand
choice is a speed property: every test here checks the product, which must not depend on it.

- every (a, b) at n = 1 and n = 2, and a dense sample at n = 3, in run-time loops: res preset dirty, between two guard
  hexes, and every call runs after the previous ones, so a call that leaves the shared state (hex.mul.dst / .ret and
  the tables) dirty breaks the products after it;
- random, lopsided, 0, the maximum and a single high hex at n = 4 / 8 / 16, at w = 32 and w = 64 (hex.init does not
  fit w = 16), with a == b (squaring, which works), then a canary multiply that reads the shared state.
"""

import random
from pathlib import Path
from typing import List, Tuple

import pytest

from tests.unit.unit_utils import run_source

GUARD_LOW, GUARD_HIGH = 0xA, 0x5


def _output(tmp_path: Path, program: str, memory_width: int = 64) -> bytes:
    _, io_device = run_source(program, tmp_path, memory_width=memory_width, use_stl=True)
    return io_device.get_output()


def _guarded(value: int, n: int) -> int:
    """res[:n] between its two guard hexes, as the hex.vec n+2 the programs print"""
    return GUARD_LOW | (value % 16**n) << 4 | GUARD_HIGH << (4 * (n + 1))


def _loop_program(n: int, a_step: int, b_step: int) -> str:
    """multiplies every a = 0, a_step, ... by every b = 0, b_step, ... (both below 16^n); prints res with its guards"""
    width = n + 2 + n % 2  # whole bytes
    return f'''
stl.startup
  outer:
    hex.zero {n}, b
  inner:
    hex.mul {n}, res+dw, a, b
    hex.print {width // 2}, res
    hex.set {n}, res+dw, {16**n - 1}
    hex.cmp {n}, b, b_end, next_b, next_a, next_a
  next_b:
    hex.add_constant {n}, b, {b_step}
    ;inner
  next_a:
    hex.cmp {n}, a, a_end, inc_a, done, done
  inc_a:
    hex.add_constant {n}, a, {a_step}
    ;outer
  done:
    stl.loop
  a: hex.vec {n}
  b: hex.vec {n}
  a_end: hex.vec {n}, {(16**n - 1) // a_step * a_step}
  b_end: hex.vec {n}, {(16**n - 1) // b_step * b_step}
  res: hex.vec {width}, {_guarded(16**n - 1, n)}
hex.init
'''


def _loop_expected(n: int, a_step: int, b_step: int) -> bytes:
    width = n + 2 + n % 2
    return b''.join(
        _guarded(a * b, n).to_bytes(width // 2, 'little')
        for a in range(0, 16**n, a_step)
        for b in range(0, 16**n, b_step)
    )


@pytest.mark.parametrize('n, a_step, b_step', [(1, 1, 1), (2, 1, 1), (3, 7, 11)])
def test_mul_every_pair(tmp_path: Path, n: int, a_step: int, b_step: int) -> None:
    assert _output(tmp_path, _loop_program(n, a_step, b_step)) == _loop_expected(n, a_step, b_step)


def _pairs(n: int) -> List[Tuple[int, int]]:
    rand = random.Random(n)
    top, full = 1 << (4 * n - 1), 16**n - 1
    pairs = [(rand.randrange(16**n), rand.randrange(16**n)) for _ in range(12)]
    pairs += [(0, full), (full, 0), (full, full), (1, full), (top, 3), (3, top), (top | 1, 7), (7, top | 1)]
    pairs += [(0xFF, top), (top, 0xFF), (0x10, 0xF), (0xF, 0x10), (0x8001 % 16**n, 0xFFF), (0xFFF, 0x8001 % 16**n)]
    return pairs


@pytest.mark.parametrize('memory_width', [32, 64])
@pytest.mark.parametrize('n', [4, 8, 16])
def test_mul_random_lopsided_and_squaring(tmp_path: Path, n: int, memory_width: int) -> None:
    pairs = _pairs(n)
    program = 'stl.startup\n'
    expected = b''
    for i, (a, b) in enumerate(pairs):
        program += f'''
    hex.mul {n}, r{i}+dw, a{i}, b{i}
    hex.print {n // 2 + 1}, r{i}
    hex.print {n // 2}, a{i}
    hex.print {n // 2}, b{i}
    ;e{i}
  r{i}: hex.vec {n + 2}, {_guarded(16**n - 1, n)}
  a{i}: hex.vec {n}, {a}
  b{i}: hex.vec {n}, {b}
  e{i}:
'''
        expected += _guarded(a * b, n).to_bytes(n // 2 + 1, 'little') + a.to_bytes(n // 2, 'little')
        expected += b.to_bytes(n // 2, 'little')
    square = pairs[0][0]
    program += f'''
    hex.mul {n}, sq, x, x
    hex.print {n // 2}, sq
    hex.add_mul 2, canary, three, five
    hex.print 1, canary
    stl.loop
  sq: hex.vec {n}, {16**n - 1}
  x: hex.vec {n}, {square}
  canary: hex.vec 2, 0x01
  three: hex.vec 2, 0x03
  five: hex.hex 5
hex.init
'''
    expected += (square * square % 16**n).to_bytes(n // 2, 'little') + bytes([0x01 + 3 * 5])
    assert _output(tmp_path, program, memory_width) == expected


def _mul_ops(tmp_path: Path, a: int, b: int, n: int = 16) -> int:
    program = f'stl.startup\nhex.mul {n}, r, a, b\nstl.loop\nr: hex.vec {n}\n'
    program += f'a: hex.vec {n}, {a}\nb: hex.vec {n}, {b}\nhex.init\n'
    statistics, _ = run_source(program, tmp_path, use_stl=True)
    return statistics.op_counter


def test_mul_loops_over_the_smaller_operand(tmp_path: Path) -> None:
    """the operand choice (a speed property no product shows): 7 * 0x8000000000000001 loops once over 7, either way
    round, so it costs about what 7 * 7 does; looping over the big one (the comparison reversed, or 287f28b's compare
    of the low hexes only) runs 16 rows, ~4x the ops."""
    big = 0x8000000000000001
    small = _mul_ops(tmp_path, 7, 7)
    assert _mul_ops(tmp_path, 7, big) < 1.5 * small
    assert _mul_ops(tmp_path, big, 7) < 1.5 * small
