"""
the stl's n-cell pointer ops (read_hex / read_byte / write_hex / write_byte / xor_hex_to_ptr / xor_byte_to_ptr n)
refuse n < 1 at assemble time, by name, as ptr_init refuses a bad PTR_CELL_BITS: an n = 0 call would otherwise still
run its first and last cell's code. The negative control: the same calls at n = 1 assemble.
"""

from pathlib import Path

import pytest

from flipjump import assemble
from flipjump.utils.exceptions import FlipJumpException

CALLS = [
    'hex.read_hex {n}, d, p',
    'hex.read_byte {n}, d, p',
    'hex.write_hex {n}, p, d',
    'hex.write_byte {n}, p, d',
    'hex.xor_hex_to_ptr {n}, p, d',
    'hex.xor_byte_to_ptr {n}, p, d',
]


def assemble_call(tmp_path: Path, call: str, n: int) -> None:
    program = tmp_path / 'program.fj'
    program.write_text(f'stl.startup_and_init_all\n{call.format(n=n)}\nstl.loop\np: hex.vec w/4\nd: hex.vec 4\n')
    assemble([program], tmp_path / 'program.fjm', memory_width=64, print_time=False)


@pytest.mark.parametrize('call', CALLS)
def test_n_0_is_refused_at_assemble_time(tmp_path: Path, call: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(FlipJumpException) as refused:
        assemble_call(tmp_path, call, 0)
    assert 'n_cell_ops__n_must_be_at_least_1' in str(refused.value) + capsys.readouterr().out


@pytest.mark.parametrize('call', CALLS)
def test_n_1_assembles(tmp_path: Path, call: str) -> None:
    assemble_call(tmp_path, call, 1)
