"""
the Expr class.
an arithmetic expression-tree over numbers and label names. it supports the flipjump math
operators, and evaluates (minimizes) to a number once all the labels it references are
known/resolved.
"""

from __future__ import annotations

from operator import mul, add, sub, floordiv, lshift, rshift, mod, xor, or_, and_
from typing import Union, Tuple, Set, Dict, Callable

from flipjump.utils.exceptions import FlipJumpExprException

# dictionary from a math-op string, to its pythonic function.
# @note: if changed, update Expr.__str__().
UNARY_TYPE = Callable[[int], int]
BINARY_TYPE = Callable[[int, int], int]
TRINARY_TYPE = Callable[[int, int, int], int]


def _pow(base: int, exp: int) -> int:
    """integer power. rejects negative exponents (they'd yield a non-integer)."""
    if exp < 0:
        raise FlipJumpExprException(f'** got a negative exponent: {base} ** {exp}')
    return int(base**exp)  # int() since (int ** non-negative-int) is always an int


op_string_to_function: Dict[str, Union[UNARY_TYPE, BINARY_TYPE, TRINARY_TYPE]] = {
    '+': add,
    '-': sub,
    '*': mul,
    '/': floordiv,
    '%': mod,
    '**': _pow,
    '<<': lshift,
    '>>': rshift,
    '^': xor,
    '|': or_,
    '&': and_,
    '&&': lambda a, b: 1 if (a and b) else 0,
    '||': lambda a, b: 1 if (a or b) else 0,
    '#': lambda x: x.bit_length(),
    '~': lambda a: ~a,
    '?:': lambda a, b, c: b if a else c,
    '<': lambda a, b: 1 if a < b else 0,
    '>': lambda a, b: 1 if a > b else 0,
    '<=': lambda a, b: 1 if a <= b else 0,
    '>=': lambda a, b: 1 if a >= b else 0,
    '==': lambda a, b: 1 if a == b else 0,
    '!=': lambda a, b: 1 if a != b else 0,
}


class Expr:
    """
    The python representation of a .fj expression (from labels, consts and math-ops)
    """

    # PERF (doom-flipjump, 2026-08-20): __slots__. MEASURED: assembling a 129 MB program
    # peaks at ~13.6 GB on a 16.8 GB machine and PAGES -- every CPU model says the build
    # should take ~800 s and it takes 6,332-8,054 s. This class is instantiated millions of
    # times (the real program has 24.4M labels and ~42M ops), and without __slots__ each
    # instance carries a per-object __dict__. This is a pure memory-layout change: no
    # attribute is added, removed or renamed, so emission is unaffected -- asserted by
    # sha256 of the .fjm (scratchpad/asm_bench.py, which ships a negative control).
    __slots__ = ('value',)

    def __init__(self, expr: Union[int, str, Tuple[str, Tuple[Expr, ...]]]):
        self.value = expr

    def is_int(self) -> bool:
        return isinstance(self.value, int)

    def __int__(self) -> int:
        if self.is_int():
            return self.value  # type: ignore[return-value]
        raise FlipJumpExprException(f"Can't resolve labels:  {', '.join(self.all_unknown_labels())}")

    def all_unknown_labels(self) -> Set[str]:
        """
        @return: all labels used (recursively) in this expression.
        """
        if isinstance(self.value, int):
            return set()
        if isinstance(self.value, str):
            return {self.value}
        return set(label for expr in self.value[1] for label in expr.all_unknown_labels())

    def eval_new(self, params_dict: Dict[str, Expr]) -> Expr:
        """
        returns a minimal Expr: replaces every string it can with its dictionary value, and
        evaluates any op it can.
        Expr objects are immutable, so unchanged (sub)expressions are returned as-is and may
        be shared between trees - the dominant assemble-time cost used to be cloning them.
        @param params_dict: the label->ExprValue dictionary to be used
        @raise FlipJumpExprException if math op failed
        @return: this Expr if nothing was substituted, else a new minimal Expr
        """
        value = self.value
        if isinstance(value, int):
            return self

        if isinstance(value, str):
            replacement = params_dict.get(value)
            # params_dict values are already minimal - substituting is a node swap, not a clone
            return replacement if replacement is not None else self

        op, args = value
        # one pass: evaluate the args while tracking "all ints" (foldable) and "unchanged"
        all_ints = True
        unchanged = True
        evaluated_args = []
        for arg in args:
            evaluated_arg = arg.eval_new(params_dict)
            evaluated_args.append(evaluated_arg)
            if evaluated_arg is not arg:
                unchanged = False
            if not isinstance(evaluated_arg.value, int):
                all_ints = False
        if all_ints:
            try:
                return Expr(op_string_to_function[op](*(arg.value for arg in evaluated_args)))  # type: ignore[arg-type]
            except Exception as e:
                raise FlipJumpExprException(f'{repr(e)}. bad math operation ({op}): {str(self)}.')
        if unchanged:
            return self  # nothing was substituted
        return Expr((op, tuple(evaluated_args)))

    def exact_eval(self, labels: Dict[str, int]) -> int:
        """
        evaluates the expression's value with the labels
        @param labels: the label->value dictionary to be used
        @raise FlipJumpExprException if it can't evaluate
        @return: the integer-value of the expression
        """
        value = self.value
        if isinstance(value, int):
            return value

        if isinstance(value, str):
            label_value = labels.get(value)
            if label_value is None:
                raise FlipJumpExprException(f"Can't evaluate label {value} in expression {self}")
            return label_value

        op, args = value
        try:
            # PERF (doom-flipjump, 2026-08-20): the binary case is spelled out. Nearly every fj
            # operator is binary, and `f(*(e.exact_eval(labels) for e in args))` paid for a whole
            # generator frame per expression NODE to deliver two values -- 17.0 s of a 321.7 s
            # labels-resolve on the doom-flipjump program. Unary/ternary keep the general path.
            if len(args) == 2:
                first, second = args
                return op_string_to_function[op](first.exact_eval(labels), second.exact_eval(labels))
            return op_string_to_function[op](*[e.exact_eval(labels) for e in args])
        except FlipJumpExprException:
            raise
        except Exception as e:
            raise FlipJumpExprException(f'{repr(e)}. bad math operation ({op}): {str(self)}.')

    def __str__(self) -> str:
        if isinstance(self.value, tuple):
            op, expressions = self.value
            if len(expressions) == 1:
                e1 = expressions[0]
                return f'({op}{str(e1)})'
            elif len(expressions) == 2:
                e1, e2 = expressions
                return f'({str(e1)} {op} {str(e2)})'
            else:
                e1, e2, e3 = expressions
                return f'({str(e1)} ? {str(e2)} : {str(e3)})'
        if isinstance(self.value, str):
            return self.value
        if isinstance(self.value, int):
            return str(self.value)
        raise FlipJumpExprException(f'bad expression: {self.value} (of type {type(self.value)})')

    def __repr__(self) -> str:
        return str(self)


def get_minimized_expr(op: str, params: Tuple[Expr, ...]) -> Expr:
    """
    tries to calculate the op on the params, if possible. returns the resulting Expr.
    @param op: the math-op string
    @param params: the op parameters
    @return: the expression
    """
    if all(param.is_int() for param in params):
        return Expr(op_string_to_function[op](*map(int, params)))
    else:
        return Expr((op, params))
