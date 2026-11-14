"""
unit-tests for pure preprocessor/ops helpers.

covers macro parameter-binding (get_params_dictionary), rep-count evaluation, the
used/declared-labels collectors, and macro-local label name substitution - all without
running the full assembly pipeline.
"""

from typing import List

import pytest

from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.ops import (
    CodePosition,
    FlipJump,
    Label,
    Macro,
    Op,
    RepCall,
    get_declared_labels,
    get_used_labels,
)
from flipjump.assembler.preprocessor import get_params_dictionary
from flipjump.utils.constants import MACRO_SEPARATOR_STRING
from flipjump.utils.exceptions import FlipJumpExprException

CODE_POSITION = CodePosition('file.fj', 'f1', 1)


def _macro(params: List[str], local_params: List[str], namespace: str) -> Macro:
    return Macro(params=params, local_params=local_params, ops=[], namespace=namespace, code_position=CODE_POSITION)


def test_get_params_dictionary_binds_positional_and_local_params() -> None:
    macro = _macro(['x', 'y'], ['z'], '')
    params_dict = get_params_dictionary(macro, [Expr(10), Expr(20)], 'PREFIX')

    assert int(params_dict['x']) == 10
    assert int(params_dict['y']) == 20
    assert params_dict['z'].value == f'PREFIX{MACRO_SEPARATOR_STRING}z'


def test_get_params_dictionary_adds_namespaced_duplicates() -> None:
    macro = _macro(['x'], ['z'], 'ns')
    params_dict = get_params_dictionary(macro, [Expr(10)], 'PREFIX')

    assert int(params_dict['ns.x']) == 10
    assert params_dict['ns.z'].value == params_dict['z'].value


def _rep_call(times: Expr) -> RepCall:
    return RepCall(times, 'i', 'some_macro', [], CODE_POSITION)


def test_rep_calculate_times() -> None:
    assert _rep_call(Expr(3)).calculate_times({}) == 3


def test_rep_calculate_times_zero() -> None:
    assert _rep_call(Expr(0)).calculate_times({}) == 0


def test_rep_calculate_times_unresolved_raises() -> None:
    with pytest.raises(FlipJumpExprException):
        _rep_call(Expr('unknown')).calculate_times({})


def test_get_used_and_declared_labels() -> None:
    ops: List[Op] = [Label('x', CODE_POSITION), FlipJump(Expr('x'), Expr('y'), CODE_POSITION)]
    assert get_used_labels(ops) == {'x', 'y'}
    assert get_declared_labels(ops) == {'x'}


def test_label_eval_name_substitutes_from_dict() -> None:
    assert Label('x', CODE_POSITION).eval_name({'x': Expr('renamed_x')}) == 'renamed_x'
    assert Label('q', CODE_POSITION).eval_name({}) == 'q'


def test_label_eval_name_bad_swap_raises() -> None:
    with pytest.raises(FlipJumpExprException):
        Label('x', CODE_POSITION).eval_name({'x': Expr(5)})
