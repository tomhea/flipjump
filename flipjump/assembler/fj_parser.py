"""
the flipjump lexer and parser.
built on the sly library, it tokenizes and parses .fj source files into the macro-tree:
macro definitions and the operations (flips, jumps, labels, etc.) that make up each macro.
"""

import re
from os import path
from pathlib import Path
from typing import Set, List, Tuple, Dict, Optional, Union

import sly

# noinspection PyProtectedMember
from sly.lex import Token

# noinspection PyProtectedMember
from sly.yacc import YaccProduction as ParsedRule

from flipjump.utils.exceptions import FlipJumpExprException, FlipJumpParsingException
from flipjump.assembler.inner_classes.expr import Expr, get_minimized_expr
from flipjump.assembler.inner_classes.ops import (
    get_used_labels,
    get_declared_labels,
    CodePosition,
    MacroName,
    Op,
    Macro,
    INITIAL_MACRO_NAME,
    MacroCall,
    RepCall,
    FlipJump,
    WordFlip,
    Label,
    Segment,
    Reserve,
    Pad,
)

curr_file: Path
curr_file_short_name: str
curr_text: str
curr_namespace: List[str]
all_errors: str
error_occurred: bool


def get_position(lineno: int) -> CodePosition:
    return CodePosition(str(curr_file), curr_file_short_name, lineno)


def syntax_error(lineno: int, msg: str = '') -> None:
    syntax_error_at(get_position(lineno), msg)


def syntax_error_at(code_position: CodePosition, msg: str = '') -> None:
    global error_occurred, all_errors
    error_occurred = True

    if msg:
        error_string = f"Syntax Error in {code_position}:\n  {msg}"
    else:
        error_string = f"Syntax Error in {code_position}"
    all_errors += f"{error_string}\n"

    print(error_string)


def syntax_warning(line: int, is_error: bool, msg: str = '') -> None:
    error_string = f"Syntax Warning in file {curr_file}"
    if line is not None:
        error_string += f" line {line}"
    if msg:
        error_string += f":\n  {msg}"

    if is_error:
        global error_occurred, all_errors
        error_occurred = True
        all_errors += f"{error_string}\n"

    print(error_string)


# Regex for the parser

id_re = r'[a-zA-Z_][a-zA-Z_0-9]*'
dot_id_re = fr'(({id_re})|\.*)?(\.({id_re}))+'

bin_num = r'0[bB][01]+'
hex_num = r'0[xX][0-9a-fA-F]+'
dec_num = r'[0-9]+'

char_escape_dict = {
    '0': 0x0,
    'a': 0x7,
    'b': 0x8,
    'e': 0x1B,
    'f': 0xC,
    'n': 0xA,
    'r': 0xD,
    't': 0x9,
    'v': 0xB,
    '\\': 0x5C,
    "'": 0x27,
    '"': 0x22,
    '?': 0x3F,
}
escape_chars = ''.join(k for k in char_escape_dict)
char = fr'[\x20-\x5B\x5D-\x7E]|\\[{re.escape(escape_chars)}]|\\[xX][0-9a-fA-F]{{2}}'

number_re = fr"({bin_num})|({hex_num})|('({char})')|({dec_num})"
string_re = fr'"({char})*"'


def get_char_value_and_length(s: str) -> Tuple[int, int]:
    if s[0] != '\\':
        return ord(s[0]), 1
    if s[1] in char_escape_dict:
        return char_escape_dict[s[1]], 2
    return int(s[2:4], 16), 4


# noinspection PyUnboundLocalVariable,PyRedeclaration,PyPep8Naming,PyMethodMayBeStatic
class FJLexer(sly.Lexer):
    # noinspection PyUnresolvedReferences
    tokens = {
        NS,
        DEF,
        REP,
        WFLIP,
        PAD,
        SEGMENT,
        RESERVE,
        ID,
        DOT_ID,
        NUMBER,
        STRING,
        LE,
        GE,
        EQ,
        NEQ,
        SHL,
        SHR,
        POW,
        LAND,
        LOR,
        NL,
        SC,
    }

    literals = {
        '=',
        '+',
        '-',
        '*',
        '/',
        '%',
        '(',
        ')',
        '$',
        '^',
        '|',
        '&',
        '~',
        '?',
        ':',
        '<',
        '>',
        '"',
        '#',
        '{',
        '}',
        "@",
        ",",
    }

    ignore_ending_comment = r'//.*'
    ignore_line_continuation = r'\\[ \t]*\n'

    # Tokens
    DOT_ID = dot_id_re
    ID = id_re
    NUMBER = number_re
    STRING = string_re

    # noinspection PyUnresolvedReferences
    ID[r'def'] = DEF
    # noinspection PyUnresolvedReferences
    ID[r'rep'] = REP
    # noinspection PyUnresolvedReferences
    ID[r'ns'] = NS

    # noinspection PyUnresolvedReferences
    ID[r'wflip'] = WFLIP
    # noinspection PyUnresolvedReferences
    ID[r'pad'] = PAD

    # noinspection PyUnresolvedReferences
    ID[r'segment'] = SEGMENT
    # noinspection PyUnresolvedReferences
    ID[r'reserve'] = RESERVE

    LE = "<="
    GE = ">="

    EQ = "=="
    NEQ = "!="

    SHL = r'<<'
    SHR = r'>>'

    POW = r'\*\*'
    LAND = r'&&'
    LOR = r'\|\|'

    # Punctuations
    NL = r'[\r\n]'
    SC = r';'

    ignore = ' \t'

    # noinspection PyTypeChecker
    def NUMBER(self, t: Token) -> Token:
        n = t.value
        if len(n) >= 2:
            # noinspection PyUnresolvedReferences
            if n[0] == "'":
                t.value = get_char_value_and_length(n[1:-1])[0]
            elif n[1] in 'xX':
                t.value = int(n, 16)
            elif n[1] in 'bB':
                t.value = int(n, 2)
            else:
                t.value = int(n)
        else:
            t.value = int(t.value)
        return t

    def STRING(self, t: Token) -> Token:
        chars = []
        s = t.value[1:-1]
        i = 0
        while i < len(s):
            val, length = get_char_value_and_length(s[i:])
            chars.append(val)
            i += length
        t.value = sum(val << (i * 8) for i, val in enumerate(chars))
        return t

    def NL(self, t: Token) -> Token:
        self.lineno += 1
        return t

    def ignore_line_continuation(self, t: Token) -> Token:
        self.lineno += 1
        return t

    def error(self, t: Token) -> None:
        global error_occurred, all_errors
        error_occurred = True

        error_string = f"Lexing Error in {get_position(self.lineno)}: {t.value[0]}"
        all_errors += f"{error_string}\n"
        print(error_string)

        self.index += 1


def next_address() -> Expr:
    return Expr('$')


def _get_main_macro_code_position(first_file: Tuple[str, Path]) -> CodePosition:
    short_file_name, fj_file_path = first_file
    return CodePosition(str(fj_file_path.absolute()), short_file_name, 1)


# noinspection PyUnusedLocal,PyUnresolvedReferences,PyPep8Naming
class FJParser(sly.Parser):
    tokens = FJLexer.tokens
    # 'UMINUS', 'UNOT' and 'LEADING_ID' are fictitious precedence tokens (not real
    #  terminals). 'UMINUS'/'UNOT' give the unary "-"/"~" rules a higher precedence
    #  than the binary ones via "%prec" (this is what lets unary minus coexist with
    #  binary minus). 'LEADING_ID' is put on the (expr_ -> id) rule so that a statement
    #  starting with "id -" reduces (binary minus, e.g. a flipjump "x-1 ; y") instead
    #  of shifting into a unary-minus macro-argument - this keeps the pre-existing
    #  flipjump behavior and matches how a leading "id +" already behaves.
    precedence = (
        ('right', '?', ':'),
        ('left', LOR),
        ('left', LAND),
        ('left', '|'),
        ('left', '^'),
        ('nonassoc', '<', '>', LE, GE),
        ('left', EQ, NEQ),
        ('left', '&'),
        ('left', SHL, SHR),
        ('left', '+', '-'),
        ('left', '*', '/', '%'),
        ('right', '#', 'UMINUS', 'UNOT'),
        ('right', POW),
        ('nonassoc', 'LEADING_ID'),
    )
    # debugfile = 'src/parser.out'

    def __init__(
        self,
        memory_width: int,
        warning_as_errors: bool,
        first_file: Tuple[str, Path],
        defines_file: Optional[Path] = None,
    ):
        self.consts: Dict[str, Expr] = {'w': Expr(memory_width)}
        self.warning_as_errors: bool = warning_as_errors
        self.macros: Dict[MacroName, Macro] = {
            INITIAL_MACRO_NAME: Macro([], [], [], '', _get_main_macro_code_position(first_file))
        }
        # -D overrides. The defines file is a real .fj file, so a define keeps the whole expression
        # language (`w + 33`, an earlier define, an stl constant); what makes it an OVERRIDE rather
        # than a declaration is that its constants land here instead of in self.consts.
        self.defines_file: Optional[Path] = defines_file.resolve() if defines_file else None
        self.defines: Dict[str, int] = {}
        self.defines_lineno: Dict[str, int] = {}
        self.used_defines: Set[str] = set()
        # `w` and friends are parser builtins, not declarations a program made, so an
        # override of one must NOT count as satisfied -- otherwise `-D w=64` would quietly
        # rewrite the width in the const table while the assembler kept using -w.
        self.builtin_consts: Set[str] = set(self.consts)

    def _parsing_the_defines_file(self) -> bool:
        try:
            return self.defines_file is not None and curr_file.resolve() == self.defines_file
        except OSError:
            return False

    def validate_free_macro_name(self, name: MacroName, lineno: int) -> None:
        if name in self.macros:
            syntax_error(
                lineno, f'macro {name} is declared twice! ' f'also declared in {self.macros[name].code_position}.'
            )

    def validate_params(self, ids: List[str], macro_name: MacroName, lineno: int) -> None:
        for param_id in ids:
            if param_id in self.consts:
                syntax_error(
                    lineno,
                    f'parameter {param_id} in macro {macro_name}) '
                    f'is also defined as a constant variable (with value {self.consts[param_id]})',
                )
        seen_ids = set()
        for _id in ids:
            if _id in seen_ids:
                syntax_error(lineno, f'parameter {_id} in macro {macro_name}) is declared twice!')
            else:
                seen_ids.add(_id)

    def validate_label_usage(
        self,
        labels_used: Set[str],
        labels_declared: Set[str],
        regular_labels: Set[str],
        extern_labels: Set[str],
        global_labels: Set[str],
        lineno: int,
        macro_name: MacroName,
    ) -> None:
        self.validate_labels_groups(extern_labels, global_labels, regular_labels, lineno, macro_name)

        self.validate_no_unused_labels(regular_labels, global_labels, labels_declared, labels_used, lineno, macro_name)
        self.validate_no_unknown_label_uses(
            regular_labels, global_labels, labels_declared, labels_used, lineno, macro_name
        )
        self.validate_no_bad_label_declarations(regular_labels, extern_labels, labels_declared, lineno, macro_name)
        self.validate_all_extern_labels_are_declared(extern_labels, labels_declared, lineno, macro_name)

    @staticmethod
    def validate_labels_groups(
        extern_labels: Set[str], global_labels: Set[str], regular_labels: Set[str], lineno: int, macro_name: MacroName
    ) -> None:
        if global_labels & extern_labels:
            syntax_error(
                lineno,
                f"In macro {macro_name}:  "
                f"extern labels can't be global labels: " + ', '.join(global_labels & extern_labels),
            )
        if global_labels & regular_labels:
            syntax_error(
                lineno,
                f"In macro {macro_name}:  "
                f"global labels can't be regular labels: " + ', '.join(global_labels & regular_labels),
            )
        if extern_labels & regular_labels:
            syntax_error(
                lineno,
                f"In macro {macro_name}:  "
                f"extern labels can't be regular labels: " + ', '.join(extern_labels & regular_labels),
            )

    def validate_no_unused_labels(
        self,
        regular_labels: Set[str],
        global_labels: Set[str],
        labels_declared: Set[str],
        labels_used: Set[str],
        lineno: int,
        macro_name: MacroName,
    ) -> None:
        unused_labels = regular_labels.union(global_labels) - labels_used.union(
            self.to_base_name(label) for label in labels_declared
        )
        if unused_labels:
            syntax_warning(
                lineno,
                self.warning_as_errors,
                f"In macro {macro_name}:  " f"unused labels: {', '.join(unused_labels)}.",
            )

    def validate_all_extern_labels_are_declared(
        self, extern_labels: Set[str], labels_declared: Set[str], lineno: int, macro_name: MacroName
    ) -> None:
        unused_labels = extern_labels - {self.to_base_name(label) for label in labels_declared}
        if unused_labels:
            syntax_warning(
                lineno,
                self.warning_as_errors,
                f"In macro {macro_name}:  " f"undeclared extern label: {', '.join(unused_labels)}.",
            )

    def validate_no_bad_label_declarations(
        self,
        regular_labels: Set[str],
        extern_labels: Set[str],
        labels_declared: Set[str],
        lineno: int,
        macro_name: MacroName,
    ) -> None:
        bad_declarations = labels_declared - set(
            self.ns_full_name(label) for label in extern_labels.union(regular_labels)
        )
        if bad_declarations:
            syntax_warning(
                lineno,
                self.warning_as_errors,
                f"In macro {macro_name}:  " f"Declared a not extern/parameter label: {', '.join(bad_declarations)}.",
            )

    def validate_no_unknown_label_uses(
        self,
        regular_labels: Set[str],
        global_labels: Set[str],
        labels_declared: Set[str],
        labels_used: Set[str],
        lineno: int,
        macro_name: MacroName,
    ) -> None:
        bad_uses = labels_used - global_labels - regular_labels - set(labels_declared) - {'$'}
        if bad_uses:
            syntax_warning(
                lineno,
                self.warning_as_errors,
                f"In macro {macro_name}:  "
                f"Used a not global/parameter/declared-extern label: {', '.join(bad_uses)}.",
            )

    @staticmethod
    def validate_no_segment_or_reserve(ops: List[Op], macro_name: MacroName) -> None:
        for op in ops:
            if isinstance(op, Segment):
                syntax_error(op.code_position.line, f"segment can't be declared inside a macro ({macro_name}).")
            if isinstance(op, Reserve):
                syntax_error(op.code_position.line, f"reserve can't be declared inside a macro ({macro_name}).")

    def validate_no_label_const_collisions(self) -> None:
        for op in self.macros[INITIAL_MACRO_NAME].ops:
            if isinstance(op, Label) and op.name in self.consts:
                syntax_error_at(
                    op.code_position,
                    f'label "{op.name}" can\'t be used: it is also defined as a constant '
                    f'(with value {self.consts[op.name]}).',
                )

    def validate_macro_declaration(
        self,
        name: MacroName,
        ops: List[Op],
        lineno: int,
        params: List[str],
        local_params: List[str],
        global_params: Set[str],
        extern_params: Set[str],
    ) -> None:
        self.validate_free_macro_name(name, lineno)

        regular_params = params + local_params
        self.validate_params(regular_params, name, lineno)
        self.validate_label_usage(
            get_used_labels(ops),
            get_declared_labels(ops),
            set(regular_params),
            set(extern_params),
            set(global_params),
            lineno,
            name,
        )

        self.validate_no_segment_or_reserve(ops, name)

    @staticmethod
    def ns_name() -> str:
        return '.'.join(curr_namespace)

    @staticmethod
    def ns_full_name(base_name: str) -> str:
        return '.'.join(curr_namespace + [base_name])

    @staticmethod
    def base_name_to_ns_full_name(base_name: str, lineno: int) -> str:
        without_dots = base_name.lstrip('.')
        if len(without_dots) == len(base_name):
            return base_name

        num_of_dots = len(base_name) - len(without_dots)
        if num_of_dots - 1 > len(curr_namespace):
            syntax_error(
                lineno,
                f'Used more leading dots than current namespace depth ' f'({num_of_dots}-1 > {len(curr_namespace)})',
            )

        return '.'.join(curr_namespace[: len(curr_namespace) - (num_of_dots - 1)] + [without_dots])

    @staticmethod
    def to_base_name(name: str) -> str:
        return name.split('.')[-1]

    def error(self, token: Token) -> None:
        global error_occurred, all_errors
        error_occurred = True

        if token is None:
            error_string = (
                f'Syntax Error in {get_position(self.line_position(None))}. '
                f'Maybe missing }} or {{ before this line?'
            )
        else:
            error_string = f'Syntax Error in {get_position(token.lineno)}, token=("{token.type}", {token.value})'

        all_errors += f"{error_string}\n"
        print(error_string)

    # Parsing Rules:

    @_('definable_line_statements')
    def program(self, p: ParsedRule) -> None:
        ops = p.definable_line_statements
        self.macros[INITIAL_MACRO_NAME].ops += ops

    # noinspection PyUnresolvedReferences
    @_('definable_line_statements NL definable_line_statement')
    def definable_line_statements(self, p: ParsedRule) -> List[Op]:
        if p.definable_line_statement:
            p.definable_line_statements.extend(p.definable_line_statement)
        return p.definable_line_statements

    @_('definable_line_statement')
    def definable_line_statements(self, p: ParsedRule) -> List[Op]:
        if p.definable_line_statement:
            return p.definable_line_statement
        return []

    @_('')
    def empty(self, p: ParsedRule) -> None:
        return None

    @_('line_statement')
    def definable_line_statement(self, p: ParsedRule) -> List[Op]:
        return p.line_statement

    @_('macro_def')
    def definable_line_statement(self, p: ParsedRule) -> List[Op]:
        return []

    @_('NS ID')
    def namespace(self, p: ParsedRule) -> None:
        curr_namespace.append(p.ID)

    @_('namespace "{" NL definable_line_statements NL "}"')
    def definable_line_statement(self, p: ParsedRule) -> List[Op]:
        curr_namespace.pop()
        return p.definable_line_statements

    @_('DEF ID macro_params "{" NL line_statements NL "}"')
    def macro_def(self, p: ParsedRule) -> None:
        params, local_params, global_params, extern_params = p.macro_params
        name = MacroName(self.ns_full_name(p.ID), len(params))
        ops = p.line_statements

        self.validate_macro_declaration(name, ops, p.lineno, params, local_params, global_params, extern_params)

        self.macros[name] = Macro(params, local_params, ops, self.ns_name(), get_position(p.lineno))
        return None

    @_('empty')
    def maybe_ids(self, p: ParsedRule) -> List[str]:
        return []

    @_('IDs')
    def maybe_ids(self, p: ParsedRule) -> List[str]:
        return p.IDs

    @_('empty')
    def maybe_local_ids(self, p: ParsedRule) -> List[str]:
        return []

    @_('"@" IDs')
    def maybe_local_ids(self, p: ParsedRule) -> List[str]:
        return p.IDs

    @_('empty')
    def maybe_global_ids(self, p: ParsedRule) -> List[str]:
        return []

    @_('"<" ids')
    def maybe_global_ids(self, p: ParsedRule) -> List[str]:
        return p.ids

    @_('empty')
    def maybe_extern_ids(self, p: ParsedRule) -> List[str]:
        return []

    @_('">" IDs')
    def maybe_extern_ids(self, p: ParsedRule) -> List[str]:
        return p.IDs

    @_('maybe_ids maybe_local_ids maybe_global_ids maybe_extern_ids')
    def macro_params(self, p: ParsedRule) -> Tuple[List[str], List[str], List[str], List[str]]:
        return p.maybe_ids, p.maybe_local_ids, p.maybe_global_ids, p.maybe_extern_ids

    @_('IDs "," ID')
    def IDs(self, p: ParsedRule) -> List[str]:
        return p.IDs + [p.ID]

    @_('ID')
    def IDs(self, p: ParsedRule) -> List[str]:
        return [p.ID]

    @_('line_statements NL line_statement')
    def line_statements(self, p: ParsedRule) -> List[Op]:
        p.line_statements.extend(p.line_statement)
        return p.line_statements

    @_('line_statement')
    def line_statements(self, p: ParsedRule) -> List[Op]:
        return p.line_statement

    # @_('empty')
    # def line_statements(self, p: ParsedRule) -> List[Op]:
    #     return []

    @_('empty')
    def line_statement(self, p: ParsedRule) -> List[Op]:
        return []

    @_('statement')
    def line_statement(self, p: ParsedRule) -> List[Op]:
        if p.statement:
            return [p.statement]
        return []

    @_('label statement')
    def line_statement(self, p: ParsedRule) -> List[Op]:
        if p.statement:
            return [p.label, p.statement]
        return [p.label]

    @_('label')
    def line_statement(self, p: ParsedRule) -> List[Op]:
        return [p.label]

    @_('ID ":"')
    def label(self, p: ParsedRule) -> Label:
        return Label(self.ns_full_name(p.ID), get_position(p.lineno))

    @_('expr SC')
    def statement(self, p: ParsedRule) -> FlipJump:
        return FlipJump(p.expr, next_address(), get_position(p.lineno))

    @_('expr SC expr')
    def statement(self, p: ParsedRule) -> FlipJump:
        return FlipJump(p.expr0, p.expr1, get_position(p.lineno))

    @_('SC expr')
    def statement(self, p: ParsedRule) -> FlipJump:
        return FlipJump(Expr(0), p.expr, get_position(p.lineno))

    @_('SC')
    def statement(self, p: ParsedRule) -> FlipJump:
        return FlipJump(Expr(0), next_address(), get_position(p.lineno))

    @_('ID')
    def id(self, p: ParsedRule) -> Tuple[str, int]:
        return p.ID, p.lineno

    @_('DOT_ID')
    def id(self, p: ParsedRule) -> Tuple[str, int]:
        return self.base_name_to_ns_full_name(p.DOT_ID, p.lineno), p.lineno

    @_('ids "," id')
    def ids(self, p: ParsedRule) -> List[str]:
        return p.ids + [p.id[0]]

    @_('id')
    def ids(self, p: ParsedRule) -> List[str]:
        return [p.id[0]]

    @_('id')
    def statement(self, p: ParsedRule) -> MacroCall:
        macro_name, lineno = p.id
        return MacroCall(macro_name, [], get_position(lineno))

    @_('id expressions')
    def statement(self, p: ParsedRule) -> MacroCall:
        macro_name, lineno = p.id
        return MacroCall(macro_name, p.expressions, get_position(lineno))

    @_('WFLIP expr "," expr')
    def statement(self, p: ParsedRule) -> WordFlip:
        return WordFlip(p.expr0, p.expr1, next_address(), get_position(p.lineno))

    @_('WFLIP expr "," expr "," expr')
    def statement(self, p: ParsedRule) -> WordFlip:
        return WordFlip(p.expr0, p.expr1, p.expr2, get_position(p.lineno))

    @_('PAD expr')
    def statement(self, p: ParsedRule) -> Pad:
        return Pad(p.expr, get_position(p.lineno))

    @_('ID "=" expr')
    def statement(self, p: ParsedRule) -> None:
        name = self.ns_full_name(p.ID)

        if self._parsing_the_defines_file():
            # a -D line. It does not DECLARE the constant, it overrides whatever declares it.
            evaluated = p.expr.eval_new(self.consts)
            try:
                self.defines[name] = int(evaluated)
            except FlipJumpExprException:
                syntax_error(p.lineno, f'Can\'t evaluate expression:  {str(evaluated)}.')
                return
            self.defines_lineno[name] = p.lineno
            if name in self.consts and name not in self.builtin_consts:
                # a file before the defines file (i.e. the stl) declared it: satisfied already.
                self.used_defines.add(name)
            # visible from here on, so a later define may use an earlier one and the
            # overridden program still sees the value at every use site.
            self.consts[name] = Expr(self.defines[name])
            return

        if name in self.defines:
            # -D wins. Behave exactly as if this line had never been written. The value is
            # already in self.consts - the defines file put it there when it was read.
            self.used_defines.add(name)
            return

        if name in self.consts:
            syntax_error(p.lineno, f'Can\'t redeclare the variable "{name}".')

        evaluated = p.expr.eval_new(self.consts)
        try:
            self.consts[name] = Expr(int(evaluated))
        except FlipJumpExprException:
            syntax_error(p.lineno, f'Can\'t evaluate expression:  {str(evaluated)}.')

    @_('REP "(" expr "," ID ")" id')
    def statement(self, p: ParsedRule) -> RepCall:
        macro_name, lineno = p.id
        code_position = get_position(lineno)
        return RepCall(p.expr, p.ID, macro_name, [], code_position)

    @_('REP "(" expr "," ID ")" id expressions')
    def statement(self, p: ParsedRule) -> RepCall:
        macro_name, lineno = p.id
        code_position = get_position(lineno)
        return RepCall(p.expr, p.ID, macro_name, p.expressions, code_position)

    @_('SEGMENT expr')
    def statement(self, p: ParsedRule) -> Segment:
        return Segment(p.expr, get_position(p.lineno))

    @_('RESERVE expr')
    def statement(self, p: ParsedRule) -> Reserve:
        return Reserve(p.expr, get_position(p.lineno))

    @_('expressions "," expr')
    def expressions(self, p: ParsedRule) -> List[Expr]:
        return p.expressions + [p.expr]

    @_('expr')
    def expressions(self, p: ParsedRule) -> List[Expr]:
        return [p.expr]

    @_('expr_')
    def expr(self, p: ParsedRule) -> Expr:
        return p.expr_[0]

    @_('expr_ "+" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('+', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ "-" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('-', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('"-" expr_ %prec UMINUS')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        # unary minus: reuse subtraction as (0 - expr).
        return get_minimized_expr('-', (Expr(0), p.expr_[0])), p.lineno

    @_('"~" expr_ %prec UNOT')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('~', (p.expr_[0],)), p.lineno

    @_('expr_ "*" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('*', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ POW expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('**', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('"#" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('#', (p.expr_[0],)), p.lineno

    @_('expr_ "/" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('/', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ "%" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('%', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ SHL expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('<<', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ SHR expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('>>', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ "^" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('^', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ "|" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('|', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ "&" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('&', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ LAND expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('&&', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ LOR expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('||', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ "?" expr_ ":" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('?:', (p.expr_0[0], p.expr_1[0], p.expr_2[0])), p.lineno

    @_('expr_ "<" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('<', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ ">" expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('>', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ LE expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('<=', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ GE expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('>=', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ EQ expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('==', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('expr_ NEQ expr_')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return get_minimized_expr('!=', (p.expr_0[0], p.expr_1[0])), p.lineno

    @_('"(" expr_ ")"')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return p.expr_

    @_('NUMBER')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return Expr(p.NUMBER), p.lineno

    @_('STRING')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return Expr(p.STRING), p.lineno

    @_('"$"')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        return next_address(), p.lineno

    @_('id %prec LEADING_ID')
    def expr_(self, p: ParsedRule) -> Tuple[Expr, int]:
        id_str, lineno = p.id
        if id_str in self.consts:
            return self.consts[id_str], lineno
        return Expr(id_str), lineno


def exit_if_errors() -> None:
    if error_occurred:
        raise FlipJumpParsingException(
            f'Errors found in file {curr_file}. ' f'Assembly stopped.\n\nThe Errors:\n{all_errors}'
        )


def validate_current_file(files_seen: Set[Union[str, Path]]) -> None:
    if not path.isfile(curr_file):
        raise FlipJumpParsingException(f"No such file {curr_file}.")

    if curr_file_short_name in files_seen:
        raise FlipJumpParsingException(f"Short file name is repeated: '{curr_file_short_name}'.")

    abs_path = curr_file.absolute()
    if abs_path in files_seen:
        raise FlipJumpParsingException(f".fj file path is repeated: '{abs_path}'.")

    files_seen.add(curr_file_short_name)
    files_seen.add(abs_path)


def lex_parse_curr_file(lexer: FJLexer, parser: FJParser) -> None:
    global curr_text, curr_namespace
    curr_text = curr_file.open('r', encoding='utf-8').read()
    curr_namespace = []

    lex_res = lexer.tokenize(curr_text)
    exit_if_errors()

    parser.parse(lex_res)
    exit_if_errors()


# ---- the stl-prefix parse cache ----
# parsing the standard library (the leading stl files of almost every assemble) is a fixed
# cost (~0.1s); the test catalog assembles ~1,000 programs in one process and used to pay
# it every time. the parser state after the stl prefix is tiny (the consts dict + the
# macros dict + the main macro's accumulated top-level ops), and the parsed Macro/op
# objects are immutable from then on (the preprocessor clones-or-shares, never mutates) -
# so it is snapshotted once per (stl files, memory-width, warnings-mode) and reused.
# user files are never cached.
_StlCacheKey = Tuple[int, bool, Tuple[Tuple[str, str, int, int], ...]]
_StlCacheValue = Tuple[Dict[str, Expr], Dict[MacroName, Macro], List[Op]]
_stl_prefix_cache: Dict[_StlCacheKey, _StlCacheValue] = {}


# the directory whose files are considered cacheable (module-level so tests can redirect it)
_STL_DIR = (Path(__file__).parent.parent / 'stl').resolve()


def _stl_prefix_length(input_files: List[Tuple[str, Path]]) -> int:
    """the length of the leading run of input files that live inside the packaged stl."""
    stl_dir = _STL_DIR
    prefix_length = 0
    for _, file_path in input_files:
        try:
            if not file_path.resolve().is_relative_to(stl_dir):
                break
        except OSError:
            break
        prefix_length += 1
    return prefix_length


def _stl_cache_key(
    input_files: List[Tuple[str, Path]], prefix_length: int, memory_width: int, warning_as_errors: bool
) -> Optional[_StlCacheKey]:
    """the cache key of the prefix files, or None when it can't be built (e.g. a missing
    file - the regular parse flow will report it clearly)."""
    files_key = []
    for short_name, file_path in input_files[:prefix_length]:
        try:
            file_stat = file_path.stat()
        except OSError:
            return None
        files_key.append((short_name, str(file_path.resolve()), file_stat.st_mtime_ns, file_stat.st_size))
    return memory_width, warning_as_errors, tuple(files_key)


def _restore_parser_from_cache(parser: FJParser, cached: _StlCacheValue) -> None:
    cached_consts, cached_macros, cached_main_ops = cached
    parser.consts = dict(cached_consts)
    parser.macros = dict(cached_macros)
    cached_main_macro = cached_macros[INITIAL_MACRO_NAME]
    # the main macro accumulates top-level ops while parsing - give this parse its own copy
    parser.macros[INITIAL_MACRO_NAME] = Macro(
        list(cached_main_macro.params),
        list(cached_main_macro.local_params),
        list(cached_main_ops),
        cached_main_macro.namespace,
        cached_main_macro.code_position,
    )


def _snapshot_parser_to_cache(parser: FJParser, cache_key: _StlCacheKey) -> None:
    _stl_prefix_cache[cache_key] = (
        dict(parser.consts),
        dict(parser.macros),
        list(parser.macros[INITIAL_MACRO_NAME].ops),
    )


def _parse_files_into_parser(
    parser: FJParser, lexer: FJLexer, input_files: List[Tuple[str, Path]], memory_width: int, warning_as_errors: bool
) -> None:
    """
    lex+parse every input file into the parser, transparently using the stl-prefix cache:
    restore the cached parser state for the leading stl files when available (skipping their
    re-parse), and snapshot it after the first time the whole prefix is parsed. see the
    module comment above for why the snapshot is safe.
    """
    global curr_file, curr_file_short_name
    files_seen: Set[Union[str, Path]] = set()

    prefix_length = _stl_prefix_length(input_files)
    cache_key = _stl_cache_key(input_files, prefix_length, memory_width, warning_as_errors) if prefix_length else None

    first_uncached_index = 0
    if cache_key is not None and cache_key in _stl_prefix_cache:
        _restore_parser_from_cache(parser, _stl_prefix_cache[cache_key])
        # the skipped prefix files still need their name/path-uniqueness validated
        for curr_file_short_name, curr_file in input_files[:prefix_length]:
            validate_current_file(files_seen)
        first_uncached_index = prefix_length

    for file_index, (curr_file_short_name, curr_file) in enumerate(input_files):
        if file_index < first_uncached_index:
            continue
        validate_current_file(files_seen)
        lex_parse_curr_file(lexer, parser)
        if cache_key is not None and file_index == prefix_length - 1:
            _snapshot_parser_to_cache(parser, cache_key)


def parse_macro_tree(
    input_files: List[Tuple[str, Path]],
    memory_width: int,
    warning_as_errors: bool,
    defines_file: Optional[Path] = None,
) -> Dict[MacroName, Macro]:
    """
    parse the .fj files and create a macro-dictionary.
    The files will be parsed as if they were concatenated.
    @param input_files:[in]: a list of (short_file_name, fj_file_path). The files will to be parsed in that given order.
    @param memory_width:[in]: the memory-width
    @param warning_as_errors:[in]: treat warnings as errors (stop execution on warnings)
    @param defines_file:[in]: the -D file among input_files, if any. Constants declared in it
    OVERRIDE the declaration elsewhere instead of adding one - see FJParser.statement.
    @return: the macro-dictionary.
    """
    global error_occurred, all_errors
    error_occurred = False
    all_errors = ''

    if not input_files:
        raise FlipJumpParsingException("The FlipJump parser got an empty files list.")

    lexer = FJLexer()
    parser = FJParser(memory_width, warning_as_errors, input_files[0], defines_file)

    _parse_files_into_parser(parser, lexer, input_files, memory_width, warning_as_errors)

    # -D may only OVERRIDE: a define that never met a declaration is a typo, and silently
    # accepting it is how a misspelled -D looks exactly like a working one.
    for name in sorted(set(parser.defines) - parser.used_defines):
        syntax_error(parser.defines_lineno[name], f'override of non-defined constant "{name}".')

    parser.validate_no_label_const_collisions()
    exit_if_errors()

    return parser.macros
