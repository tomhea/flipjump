"""
unit-tests for the stl's bit-vector macros (flipjump/stl/bit/*.fj and casting.fj), against python.

Every result is preset dirty (all ones) before the macro writes it, so a macro that does not fully set its result is
seen. Each test names the mutant it catches.
"""

from pathlib import Path

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
