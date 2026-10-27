"""
unit-tests for the stl's bit-vector macros (flipjump/stl/bit/*.fj and casting.fj), against python.

Every result is preset dirty (all ones) before the macro writes it, so a macro that does not fully set its result is
seen. Each test names the mutant it catches.
"""

from pathlib import Path

import pytest

from tests.unit.unit_utils import run_source

HEX_DIGITS = b'0123456789abcdefABCDEF'


def _output(tmp_path: Path, program: str, memory_width: int = 64) -> bytes:
    _, io_device = run_source(program, tmp_path, memory_width=memory_width, use_stl=True)
    return io_device.get_output()


ASCII2HEX_PROGRAM = '''
stl.startup
rep(256, i) test_ascii2hex i
stl.loop

// prints the ascii (which must be unchanged), then the byte error<<4 | hex.
def test_ascii2hex v @ ascii, error, hex, result, end {
    bit.ascii2hex error, hex, ascii
    bit.print ascii
    bit.zero 8, result
    bit.xor 4, result, hex
    bit.xor result+4*dw, error
    bit.print result
    ;end

  ascii:  bit.vec 8, v
  error:  bit.bit 1
  hex:    bit.vec 4, 0xf
  result: bit.vec 8
  end:
}
'''


def test_ascii2hex_every_byte(tmp_path: Path) -> None:
    """all 256 inputs: hex and error, and the input left unchanged (the old body left 'A'..'G' and 'a'..'g' + 1)."""
    expected = bytearray()
    for value in range(256):
        is_hex = value in HEX_DIGITS
        expected += bytes([value, int(chr(value), 16) if is_hex else 0x10])
    assert _output(tmp_path, ASCII2HEX_PROGRAM) == bytes(expected)


def _bytes(value: int, n_bits: int) -> bytes:
    return (value % 2**n_bits).to_bytes(n_bits // 8, 'little')


def _signed_bits(value: int, n: int) -> int:
    value %= 2**n
    return value - 2**n if value >> (n - 1) else value


@pytest.mark.parametrize('macro', ['div', 'div_loop', 'idiv', 'idiv_loop'])
def test_the_divides_restore_their_inputs_and_skip_b_0(tmp_path: Path, macro: str) -> None:
    """q, r against python (preset dirty), and a, b afterwards: idiv / idiv_loop negate a negative a or b in place and
    must negate it back (a `no b restore` mutant passed every test). b = 0: q and r untouched (no test divided by 0)."""
    n = 8
    cases = [(100, 7), (-100, 7), (100, -7), (-100, -7), (0x9C, 0x13), (5, 0), (-5, 0)]
    signed = macro.startswith('i')
    program = 'stl.startup\n'
    expected = b''
    for i, (a, b) in enumerate(cases):
        if not signed:
            a, b = a % 2**n, b % 2**n
        program += f'''
    bit.{macro} {n}, a{i}, b{i}, q{i}, r{i}
    bit.print {n // 8}, q{i}
    bit.print {n // 8}, r{i}
    bit.print {n // 8}, a{i}
    bit.print {n // 8}, b{i}
    ;e{i}
  a{i}: bit.vec {n}, {a % 2**n}
  b{i}: bit.vec {n}, {b % 2**n}
  q{i}: bit.vec {n}, 0xff
  r{i}: bit.vec {n}, 0xff
  e{i}:
'''
        if b == 0:
            q, r = 0xFF, 0xFF
        elif signed:
            sa, sb = _signed_bits(a, n), _signed_bits(b, n)
            q = abs(sa) // abs(sb) * (1 if (sa < 0) == (sb < 0) else -1)
            r = sa - q * sb
        else:
            q, r = a // b, a % b
        expected += _bytes(q, n) + _bytes(r, n) + _bytes(a, n) + _bytes(b, n)
    assert _output(tmp_path, program + 'stl.loop\n') == expected
