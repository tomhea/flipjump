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
    return (value % (1 << n_bits)).to_bytes(n_bits // 8, 'little')


def test_to_ascii_with_a_dirty_ascii(tmp_path: Path) -> None:
    """bin2ascii / dec2ascii / hex2ascii zero all 8 bits of ascii first (a `.zero 7` mutant left the top one).
    dec2ascii of 10-15 gives ':'-'?' (documented); hex2ascii gives digits and capital letters."""
    program = 'stl.startup\n'
    for i in range(2):
        program += (
            f'bit.bin2ascii b{i}, bv{i}\nbit.print b{i}\n;be{i}\nb{i}: bit.vec 8, 0xff\nbv{i}: bit.bit {i}\nbe{i}:\n'
        )
    for i in range(16):
        program += f'bit.dec2ascii d{i}, dv{i}\nbit.print d{i}\nbit.hex2ascii h{i}, dv{i}\nbit.print h{i}\n;de{i}\n'
        program += f'd{i}: bit.vec 8, 0xff\nh{i}: bit.vec 8, 0xff\ndv{i}: bit.vec 4, {i}\nde{i}:\n'
    expected = b'01' + b''.join(bytes([0x30 + i]) + b'%X' % i for i in range(16))
    assert _output(tmp_path, program + 'stl.loop\n') == expected


def test_bit2hex_with_a_dirty_hex(tmp_path: Path) -> None:
    """stl.bit2hex (1 bit) and stl.bit2hex n zero their (n+3)/4 hexes first (mutants: no zero; n/4 hexes)."""
    program = '''
stl.startup
    stl.bit2hex h1, b1
    stl.bit2hex 5, h5, b5
    hex.print h1
    hex.print h5
    stl.loop
h1: hex.vec 2, 0xff
h5: hex.vec 2, 0xff
b1: bit.bit 1
b5: bit.vec 5, 0b10110
'''
    assert _output(tmp_path, program) == bytes([0xF1, 0x16])


def test_mov_and_swap_on_themselves(tmp_path: Path) -> None:
    """bit.mov n, x, x keeps x (without its dst==src check it would not); bit.swap of all 4 bit pairs, and
    bit.swap n, x, x (a == b is allowed)."""
    program = '''
stl.startup
    bit.mov 8, x, x
    bit.print x
    rep(4, i) swap_pair i&1, i>>1
    bit.swap 8, x, x
    bit.print x
    stl.loop
x: bit.vec 8, 0xa5

// prints the byte b<<1 | a after the swap
def swap_pair a0, b0 @ a, b, out, end {
    bit.swap a, b
    bit.zero 8, out
    bit.xor out, a
    bit.xor out+dw, b
    bit.print out
    ;end
  a: bit.bit a0
  b: bit.bit b0
  out: bit.vec 8
  end:
}
'''
    expected = b'\xa5' + bytes([b | a << 1 for a, b in ((0, 0), (1, 0), (0, 1), (1, 1))]) + b'\xa5'
    assert _output(tmp_path, program) == expected


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


@pytest.mark.parametrize('macro', ['mul', 'mul_loop'])
def test_mul_twice_at_one_site(tmp_path: Path, macro: str) -> None:
    """the same multiply run twice (a loop): its res buffer must be cleared each time (a `no res zero` mutant
    passed every test, which ran each multiply once)."""
    program = f'''
stl.startup
  again:
    bit.{macro} 8, x, y
    bit.print x
    bit.if1 done, finish
    bit.not done
    bit.zero 8, x
    bit.xor 4, x, five
    ;again
  finish:
    stl.loop
x: bit.vec 8, 7
y: bit.vec 8, 11
five: bit.vec 4, 5
done: bit.bit 0
'''
    assert _output(tmp_path, program) == bytes([77, 55])


def test_mul_squares_safely(tmp_path: Path) -> None:
    """bit.mul's documented aliasing: dst == src squares (bit.mul n, x, x)."""
    program = 'stl.startup\nbit.mul 8, x, x\nbit.print x\nstl.loop\nx: bit.vec 8, 13\n'
    assert _output(tmp_path, program) == bytes([169])


def test_print_dec_uint_at_n_4(tmp_path: Path) -> None:
    """n = 4 is div10's smallest windowed size (a mutant skipping it at n = 4 passed: no test printed at n = 4)."""
    program = 'stl.startup\n' + ''.join(
        f'bit.print_dec_uint 4, x{v}\nstl.output_char 32\n;e{v}\nx{v}: bit.vec 4, {v}\ne{v}:\n' for v in range(16)
    )
    assert _output(tmp_path, program + 'stl.loop\n') == b''.join(b'%d ' % v for v in range(16))


def test_print_str_terminates_only_on_a_zero_byte(tmp_path: Path) -> None:
    """a byte with only bit 7 set (0x80) is not the '\\0' terminator (a mutant testing bits 0..6 stopped there)."""
    program = 'stl.startup\nbit.print_str 6, s\nstl.loop\ns: bit.vec 48, 0x628061\n'
    assert _output(tmp_path, program) == b'a\x80b'
