"""
unit-tests for the assembler's memory-saving representations: the compact op stream (OpStream) that the
preprocessor emits and labels-resolve reads, the label-position list behind the "declared twice" error,
the parser's bounded sly position records, consume_main, and the fjm Writer taking a buffer over.

Each representation must be invisible: the same values, the same binary, the same errors.
"""

from array import array
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flipjump.assembler import fj_parser
from flipjump.assembler.assembler import labels_resolve
from flipjump.assembler.fj_parser import parse_macro_tree
from flipjump.assembler.inner_classes import op_stream
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.op_stream import OpStream
from flipjump.assembler.inner_classes.ops import INITIAL_MACRO_NAME, CodePosition, FlipJump, NewSegment, WordFlip
from flipjump.assembler.preprocessor import resolve_macros
from flipjump.fjm.fjm_consts import FJMVersion
from flipjump.fjm.fjm_writer import Writer
from flipjump.utils.exceptions import FlipJumpAssemblerException, FlipJumpPreprocessorException
from tests.unit.unit_utils import MINIMAL_STARTUP, assemble_to_path, sum_expr

POSITION = CodePosition('a.fj', 'f1', 7)


# operands as a macro body holds them, and the params they expand with
PARAMS: Dict[str, Expr] = {
    'dst': sum_expr('va', 64),  # an argument `va + 2*dw`
    'src': Expr('vb'),
    'n': Expr(3),
    'big': Expr(1 << 40),
    'neg': Expr(-5),
    '$': Expr(4096),
}
OPERANDS: List[Expr] = [
    Expr(0),
    Expr(12345),
    Expr(1 << 62),  # a literal too wide to pack
    Expr(-1),  # not a literal: kept as an Expr
    Expr('$'),
    Expr('n'),
    Expr('neg'),
    Expr('big'),
    Expr('known'),  # declared before the op
    Expr('later'),  # declared after it
    Expr('dst'),
    Expr('src'),
    sum_expr('dst', 33, 3),  # `dst + dbit + 3`
    Expr(('-', (sum_expr('dst', 33), Expr(1)))),
    sum_expr('known', 'n'),
    sum_expr(5, 'later'),
    Expr(('-', (Expr(100), Expr('later')))),  # coefficient -1: kept as an Expr
    Expr(('*', (Expr('n'), Expr('src')))),  # not a sum: kept as an Expr
    Expr(('-', (Expr('later'), Expr('known')))),  # two labels: kept as an Expr
    Expr(('+', (Expr('later'), Expr(-(1 << 63) - 1)))),  # a constant beyond int64: kept as an Expr
]


def _stream_values(stream: OpStream, labels: Dict[str, int]) -> List[List[int]]:
    """every op's operand values, read back the way labels-resolve reads them"""
    values = []
    for op in stream:
        if isinstance(op, FlipJump):
            values.append([op.flip.exact_eval(labels), op.jump.exact_eval(labels)])
        elif isinstance(op, WordFlip):
            values.append([e.exact_eval(labels) for e in (op.word_address, op.flip_value, op.return_address)])
    return values


def test_every_operand_shape_keeps_its_value() -> None:
    labels: Dict[str, int] = {'known': 1 << 20}
    stream = OpStream([], labels)
    expected = []
    for flip in OPERANDS:
        for jump in OPERANDS:
            stream.add_flip_jump_in(flip, jump, PARAMS, POSITION)
            expected.append((flip.eval_new(PARAMS), jump.eval_new(PARAMS)))
        stream.add_word_flip_in(flip, Expr('src'), Expr('$'), PARAMS, POSITION)
    labels.update({'later': 1 << 30, 'va': 1 << 12, 'vb': 1 << 13})  # declared after the ops

    values = _stream_values(stream, labels)
    want = []
    position = 0
    for flip in OPERANDS:
        for _ in OPERANDS:
            evaluated_flip, evaluated_jump = expected[position]
            position += 1
            want.append([evaluated_flip.exact_eval(labels), evaluated_jump.exact_eval(labels)])
        want.append([flip.eval_new(PARAMS).exact_eval(labels), labels['vb'], 4096])
    assert values == want


def test_literal_ops_pack_into_one_slot_when_they_fit() -> None:
    stream = OpStream([], {})
    stream.add_flip_jump(Expr(5), Expr(6), POSITION)
    stream.add_flip_jump(Expr(1 << 31), Expr(6), POSITION)  # flip too wide to pack
    stream.add_flip_jump(Expr(5), Expr(1 << 32), POSITION)  # jump too wide to pack
    assert list(stream.kinds) == [op_stream.FJ_PACKED, op_stream.FJ, op_stream.FJ]
    assert len(stream.vals) == 1 + 2 + 2
    assert _stream_values(stream, {}) == [[5, 6], [1 << 31, 6], [5, 1 << 32]]


def test_a_declared_label_is_folded_and_an_undeclared_one_is_deferred() -> None:
    labels = {'known': 100}
    stream = OpStream([], labels)
    stream.add_flip_jump_in(sum_expr('known', 8), Expr(0), {}, POSITION)
    stream.add_flip_jump_in(sum_expr('later', 8), Expr(0), {}, POSITION)
    assert list(stream.kinds) == [op_stream.FJ_PACKED, op_stream.FJ_POS]
    assert stream.refs == ['later', POSITION]  # the deferred label and the op's position: no Expr kept


def test_labels_resolve_reads_a_stream_like_the_op_objects(tmp_path: Path) -> None:
    labels: Dict[str, int] = {}
    stream = OpStream([], labels)
    stream.add_object(NewSegment(0))
    stream.add_flip_jump_in(sum_expr('x', 3), Expr('$'), {'$': Expr(128)}, POSITION)
    stream.add_word_flip_in(Expr('x'), Expr(64), Expr('y'), {}, POSITION)
    stream.add_padding(2)
    stream.add_flip_jump(Expr(0), Expr(0), POSITION)
    labels.update({'x': 1024, 'y': 2048})
    stream.refs[0].wflip_start_address = 5 * 128

    def build(ops: Any, name: str) -> List[int]:
        writer = Writer(tmp_path / name, 64, FJMVersion.NormalVersion)
        labels_resolve(ops, labels, 64, writer, save_wflip_labels=False)
        return list(writer.data)

    from collections import deque

    objects = deque(stream)  # the same ops, as objects
    objects[0].wflip_start_address = 5 * 128
    assert build(stream, 'stream.fjm') == build(objects, 'objects.fjm')


def test_an_unresolvable_label_still_names_the_op_and_its_line(tmp_path: Path) -> None:
    source = MINIMAL_STARTUP + '\nstartup\nhere:\n;here\n;undefined_label + 8\n'
    with pytest.raises(FlipJumpAssemblerException) as raised:
        assemble_to_path(source, tmp_path)
    message = str(raised.value)
    assert "Can't evaluate label undefined_label" in message
    assert '(line 12)' in message


def test_a_duplicate_label_error_names_both_declarations(tmp_path: Path) -> None:
    source = MINIMAL_STARTUP + '\nstartup\nother:\ndup:\n;dup\ndup:\n;other\n'
    with pytest.raises(FlipJumpPreprocessorException) as raised:
        assemble_to_path(source, tmp_path)
    message = str(raised.value)
    assert 'declared twice - "dup"' in message
    assert '(line 11)' in message and '(line 13)' in message


def _tree(tmp_path: Path) -> Any:
    source = MINIMAL_STARTUP + '\nstartup\ndef m a {\n a;a+w\n}\nm 5\nx:\npad 4\n;x\n;x\ny:\n;y\n'
    path = tmp_path / 'p.fj'
    path.write_text(source)
    return parse_macro_tree([('f1', path)], 64, True)


def test_resolve_macros_keeps_the_tree_unless_told_to_consume_it(tmp_path: Path) -> None:
    macros = _tree(tmp_path)
    first, labels1 = resolve_macros(64, macros, save_debug_labels=False)
    second, labels2 = resolve_macros(64, macros, save_debug_labels=False, consume_main=True)
    assert labels1 == labels2
    assert first.kinds == second.kinds and first.vals == second.vals

    def ref_key(ref: Any) -> Any:
        return (ref.start_address, ref.wflip_start_address) if isinstance(ref, NewSegment) else ref

    assert [ref_key(ref) for ref in first.refs] == [ref_key(ref) for ref in second.refs]
    main_ops = macros[INITIAL_MACRO_NAME].ops
    assert main_ops[0] is None and main_ops[-1] is not None  # consumed as it was expanded


def test_sly_keeps_one_position_not_one_per_reduction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parsers = []
    real_init = fj_parser.FJParser.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        parsers.append(self)

    monkeypatch.setattr(fj_parser.FJParser, '__init__', init)
    _tree(tmp_path)
    assert parsers and len(parsers[0]._line_positions) <= 1 and len(parsers[0]._index_positions) <= 1


def test_the_writer_takes_a_word_array_instead_of_copying_it(tmp_path: Path) -> None:
    writer = Writer(tmp_path / 'a.fjm', 32, FJMVersion.NormalVersion)
    words = array(writer.data.typecode, [1, 2, 3, 4])  # type: ignore[union-attr]
    assert writer.add_data(words, take=True) == 0
    assert writer.data is words
    more = array(writer.data.typecode, [5, 6])
    assert writer.add_data(more, take=True) == 4  # only the first data can be taken
    assert list(writer.data) == [1, 2, 3, 4, 5, 6] and writer.data is words
