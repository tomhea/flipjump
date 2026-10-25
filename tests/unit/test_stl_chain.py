"""
unit-tests for the layout of the stl's chained table dance (hex.tables.chained_jump): the chain's return labels
are a fixed 128 ops apart (pad 16 before each), so moving hex.tables.ret from one to the next flips 1-2 bits.
Breaking the stride costs speed, not correctness, so no program test would see it.

The negative control: the same probe on the unchained dances (rep'd single-hex ops) does not find that stride.
"""

from pathlib import Path
from typing import List

import pytest

from flipjump import assemble
from flipjump.utils.functions import load_debugging_labels

WIDTH = 64
OP = 2 * WIDTH  # bits per op
STRIDE = 128  # ops between two chain steps' return labels


def return_label_ops(tmp_path: Path, call: str, macro: str) -> List[int]:
    """the op indexes of every `return` label of `macro`'s expansions in a program that runs `call`"""
    program = tmp_path / 'program.fj'
    program.write_text(f'stl.startup\n{call}\nstl.loop\nhex.init\nx: hex.vec 8\ny: hex.vec 8\nb: hex.hex\n')
    debug = tmp_path / 'program.fjd'
    assemble([program], tmp_path / 'program.fjm', memory_width=WIDTH, print_time=False, debugging_file_path=debug)
    labels = load_debugging_labels(debug)
    return sorted(address // OP for name, address in labels.items() if f'{macro}(' in name and name.endswith('return'))


def strides(ops: List[int]) -> List[int]:
    return [b - a for a, b in zip(ops, ops[1:])]


@pytest.mark.parametrize('call', ['hex.or 8, x, y', 'hex.and 4, x, y'])
def test_the_chain_steps_are_128_ops_apart(tmp_path: Path, call: str) -> None:
    ops = return_label_ops(tmp_path, call, 'chained_jump')
    assert len(ops) == int(call.split()[1].rstrip(','))
    assert strides(ops) == [STRIDE] * (len(ops) - 1)


@pytest.mark.parametrize('call', ['hex.add 8, x, y', 'hex.sub 4, x, y', 'hex.add_shifted 8, 6, x, y, 1'])
def test_the_bracketed_chain_steps_are_128_ops_apart(tmp_path: Path, call: str) -> None:
    """add / sub chain their two clear_carry brackets too: those two strides are the brackets' own."""
    ops = return_label_ops(tmp_path, call, 'chained_jump')
    assert strides(ops)[1:-1] == [STRIDE] * (len(ops) - 3)


def test_the_add_mul_chain_steps_are_128_ops_apart(tmp_path: Path) -> None:
    ops = return_label_ops(tmp_path, 'hex.add_mul 8, x, y, b', 'chained_add_mul')
    assert strides(ops) == [STRIDE] * 7


def test_the_stride_probe_negative_control(tmp_path: Path) -> None:
    """unchained, the single-hex dances' return labels are not 128 ops apart."""
    ops = return_label_ops(tmp_path, 'rep(8, i) hex.or x+i*dw, y+i*dw', 'jump_to_table_entry')
    assert len(ops) == 8
    assert strides(ops) != [STRIDE] * 7
