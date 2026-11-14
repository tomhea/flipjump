"""
unit-tests for the assembler: the language rules compile (and, where there's arithmetic,
produce the expected memory-word), and the error/edge cases raise the right exceptions.

the rule-tests assemble a tiny inline no-stl program; MINIMAL_STARTUP is exactly two ops,
so the first user op always lands at word-index 4 (flip-word) / 5 (jump-word).
"""

from pathlib import Path

import pytest

from flipjump import assemble
from flipjump.utils.exceptions import (
    FlipJumpException,
    FlipJumpAssemblerException,
    FlipJumpParsingException,
    FlipJumpPreprocessorException,
)
from tests.unit.unit_utils import MINIMAL_STARTUP, assemble_to_path, compile_and_get_reader

# the word-indices of the first user op (right after the 2-op MINIMAL_STARTUP).
FIRST_OP_FLIP_WORD = 4
FIRST_OP_JUMP_WORD = 5

# --- language rules: each program should assemble into a valid .fjm ---


def _program(body: str) -> str:
    return MINIMAL_STARTUP + '\nstartup\n' + body


def test_flip_jump_op(tmp_path: Path) -> None:
    reader = compile_and_get_reader(_program('a:\n8;a\n'), tmp_path)
    assert reader.get_memory()[FIRST_OP_FLIP_WORD] == 8


def test_jump_only_op(tmp_path: Path) -> None:
    compile_and_get_reader(_program('loop:\n;loop\n'), tmp_path)


def test_flip_only_op(tmp_path: Path) -> None:
    compile_and_get_reader(_program('here:\n8;\nloop:\n;loop\n'), tmp_path)


def test_forward_and_backward_label_reference(tmp_path: Path) -> None:
    compile_and_get_reader(_program(';forward\nback:\n;back\nforward:\n;back\n'), tmp_path)


def test_constant_definition_and_use(tmp_path: Path) -> None:
    # the constant X must be substituted into the jump-word of ";X".
    reader = compile_and_get_reader(_program('X = 5\n;X\n'), tmp_path)
    assert reader.get_memory()[FIRST_OP_JUMP_WORD] == 5


def test_expression_in_address(tmp_path: Path) -> None:
    compile_and_get_reader(_program('here:\n;(here + 2*w)\nl:\n;l\n'), tmp_path)


def test_rep_macro_call(tmp_path: Path) -> None:
    source = MINIMAL_STARTUP + '\ndef m a < IO {\n a+IO;\n}\nstartup\nrep(4, i) m i\nl:\n;l\n'
    compile_and_get_reader(source, tmp_path)


def test_macro_definition_and_call(tmp_path: Path) -> None:
    source = MINIMAL_STARTUP + '\ndef m a {\n a;a\n}\nstartup\nm 8\nl:\n;l\n'
    compile_and_get_reader(source, tmp_path)


def test_nested_macro_call(tmp_path: Path) -> None:
    source = MINIMAL_STARTUP + '\ndef inner a {\n a;a\n}\ndef outer b {\n inner b\n}\nstartup\nouter 8\nl:\n;l\n'
    compile_and_get_reader(source, tmp_path)


def test_wflip_op(tmp_path: Path) -> None:
    # wflip of word 0x100 with bits {0, 2} set emits a first fj-op that flips address 0x100.
    reader = compile_and_get_reader(_program('wflip 0x100, 0x5, l\nl:\n;l\n'), tmp_path)
    assert reader.get_memory()[FIRST_OP_FLIP_WORD] == 0x100


def test_wflip_zero_value(tmp_path: Path) -> None:
    compile_and_get_reader(_program('wflip 0x100, 0, l\nl:\n;l\n'), tmp_path)


def test_pad_directive(tmp_path: Path) -> None:
    compile_and_get_reader(_program('pad 4\nl:\n;l\n'), tmp_path)


def test_reserve_directive(tmp_path: Path) -> None:
    compile_and_get_reader(_program('reserve 2*w\nl:\n;l\n'), tmp_path)


def test_segment_directive(tmp_path: Path) -> None:
    compile_and_get_reader(_program('segment 0x400\nl:\n;l\n'), tmp_path)


def test_namespace(tmp_path: Path) -> None:
    source = 'ns A {\n def m {\n 5;6\n }\n}\n' + MINIMAL_STARTUP + '\nstartup\nA.m\nl:\n;l\n'
    reader = compile_and_get_reader(source, tmp_path)
    assert reader.get_memory()[FIRST_OP_FLIP_WORD] == 5


def test_multi_file_macro_sharing(tmp_path: Path) -> None:
    # file B calls a macro defined in file A; the macro's "5;6" op lands at the first word.
    file_a = MINIMAL_STARTUP + '\ndef shared {\n 5;6\n}\n'
    file_b = 'startup\nshared\nl:\n;l\n'
    reader = compile_and_get_reader([file_a, file_b], tmp_path)
    assert reader.get_memory()[FIRST_OP_FLIP_WORD] == 5
    assert reader.get_memory()[FIRST_OP_JUMP_WORD] == 6


# --- error handling / edge cases ---


def test_syntax_error_raises(tmp_path: Path) -> None:
    with pytest.raises(FlipJumpParsingException):
        assemble_to_path('this is not valid fj $$$ {{{', tmp_path)


def test_undefined_label_raises(tmp_path: Path) -> None:
    with pytest.raises(FlipJumpAssemblerException):
        assemble_to_path(_program(';undefined_label\n'), tmp_path)


def test_wrong_macro_argument_count_raises(tmp_path: Path) -> None:
    source = MINIMAL_STARTUP + '\ndef m a {\n a;a\n}\nstartup\nm 1, 2\nl:\n;l\n'
    with pytest.raises(FlipJumpPreprocessorException):
        assemble_to_path(source, tmp_path)


def test_duplicate_label_raises(tmp_path: Path) -> None:
    with pytest.raises(FlipJumpPreprocessorException):
        assemble_to_path(_program('dup:\ndup:\n;dup\n'), tmp_path)


def test_macro_recursion_depth_exceeded_raises(tmp_path: Path) -> None:
    source = 'def loop {\n    loop\n}\nloop\n'
    with pytest.raises(FlipJumpPreprocessorException):
        assemble_to_path(source, tmp_path, max_recursion_depth=50)


def test_address_beyond_memory_width_raises(tmp_path: Path) -> None:
    source = _program('segment 0x1000\n;0\n')
    with pytest.raises(FlipJumpAssemblerException):
        assemble_to_path(source, tmp_path, memory_width=8)


def test_assert_first_op_assembled_requires_a_segment_at_address_zero(tmp_path: Path) -> None:
    # the assembler must reject a program whose first op is not at address 0
    from flipjump.assembler.assembler import assert_first_op_assembled
    from flipjump.fjm.fjm_consts import FJMVersion
    from flipjump.fjm.fjm_writer import Writer

    not_at_zero = Writer(tmp_path / 'a.fjm', 16, FJMVersion.NormalVersion)
    not_at_zero.add_simple_segment_with_data(2, [10, 20])  # first op at word 2, not 0
    with pytest.raises(FlipJumpAssemblerException):
        assert_first_op_assembled(not_at_zero)

    at_zero = Writer(tmp_path / 'b.fjm', 16, FJMVersion.NormalVersion)
    at_zero.add_simple_segment_with_data(0, [10, 20])  # a segment holds the first op
    assert_first_op_assembled(at_zero)  # no raise


def test_assemble_rejects_program_not_starting_at_address_zero(tmp_path: Path) -> None:
    # a program with real ops but whose first segment is at a non-zero address has no first
    # op at 0 - the assembler must reject it (memory starts at another segment)
    source = 'segment 0x100\n;0\n'
    with pytest.raises(FlipJumpAssemblerException, match='no first op at address 0'):
        assemble_to_path(source, tmp_path, use_stl=False)


@pytest.mark.parametrize('body', ['loop', 'rep(1, i) loop'], ids=['a call', 'a rep'])
def test_endless_macro_recursion_is_refused_at_the_default_depth(tmp_path: Path, body: str) -> None:
    """the default depth is far deeper than python's own recursion limit allows by default: the preprocessor's
    limit must fire first, for a call and for a rep (each is more than one python call deep)"""
    source = f'def loop {{\n    {body}\n}}\n    loop\n'
    fj_path = tmp_path / 'loop.fj'
    fj_path.write_text(source)
    with pytest.raises(FlipJumpPreprocessorException, match='maximal macro-expansion recursive depth'):
        assemble([fj_path], tmp_path / 'loop.fjm', memory_width=64, print_time=False, use_stl=False)


@pytest.mark.parametrize(
    'memory_width, message',
    [(64, 'Not enough space with the 64-bits memory-width'), (128, 'too many to assemble')],
)
def test_a_pad_of_too_many_ops_is_refused_by_name(tmp_path: Path, memory_width: int, message: str) -> None:
    fj_path = tmp_path / 'pad.fj'
    fj_path.write_text(';\n;\n;\npad 1 << 70\n;0\n')
    with pytest.raises(FlipJumpPreprocessorException, match=message):
        assemble([fj_path], tmp_path / 'pad.fjm', memory_width=memory_width, print_time=False, use_stl=False)


# a program whose constant N a define may override
DEFINABLE_PROGRAM = MINIMAL_STARTUP + '\nN = 3\nstartup\nloop:\n    ;loop + N - N\n'


@pytest.mark.parametrize(
    'defines, error',
    [
        ({'N': 'undefined_name'}, 'Error in the define of "N" (-D N=...):\n  can\'t evaluate its value'),
        ({'N': '1 +'}, 'Syntax Error in the -D defines, at "N = 1 +"'),
        ({'N': '5 $ 6'}, 'the -D defines, at "N = 5 $ 6"'),
        ({'M': 7}, 'Error in the define of "M" (-D M=...):\n  override of non-defined constant "M"'),
    ],
    ids=['a value with no value', 'a syntax error', 'a bad token', 'an unused define'],
)
def test_an_error_in_a_define_names_the_define_and_never_the_defines_file(
    tmp_path: Path, defines: dict, error: str  # type: ignore[type-arg]
) -> None:
    fj_path = tmp_path / 'p.fj'
    fj_path.write_text(DEFINABLE_PROGRAM)
    with pytest.raises(FlipJumpException) as raised:
        assemble([fj_path], tmp_path / 'p.fjm', memory_width=64, print_time=False, use_stl=False, defines=defines)
    message = str(raised.value)
    assert error in message and 'Errors found in the -D defines.' in message
    assert '_defines.fj' not in message and str(tmp_path) not in message


def test_a_pooled_assembly_leaves_none_of_its_data_to_the_cyclic_collector(tmp_path: Path) -> None:
    """assemble() runs with the cyclic collector off, so what it builds must be freed without it. (The lexer library
    leaves a few closures per parse; nothing that grows with the program.)"""
    import gc

    from flipjump.assembler.inner_classes.table_pool import BlockPool

    fj_path = tmp_path / 'p.fj'
    fj_path.write_text('stl.startup\n    hex.xor x, y\n    stl.loop\nx: hex.hex 3\ny: hex.hex 5\n')
    gc.collect()
    gc.set_debug(gc.DEBUG_SAVEALL)  # keep what a collection finds, in gc.garbage
    try:
        assemble([fj_path], tmp_path / 'p.fjm', memory_width=64, print_time=False, table_pool=BlockPool(64))
        gc.collect()
        left = {type(found).__name__ for found in gc.garbage}
    finally:
        gc.set_debug(0)
        gc.garbage.clear()
    assert not left & {'PreprocessorData', 'TableMover', 'OpStream', 'OffsetGuard', 'Macro'}, left
