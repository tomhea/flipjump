"""
unit-tests for the assembler's table placement: BlockPool (group the tables dispatched through one jump word into an
aligned block, so the arming wflip writes an index instead of an address), the declared tables (`__relocatable__`
macros) and the pin resolver (assembler.resolve_pinned).

the property pinned throughout: a blocked program produces the SAME output as the inline one, in fewer ops - a table
is reached only by jump, so its address is free to choose.
"""

import re
import tracemalloc
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

import flipjump.assembler.preprocessor as preprocessor
import flipjump.assembler.table_pool as table_pool
from flipjump import assemble
from flipjump.assembler.assembler import resolve_pinned
from flipjump.assembler.fj_parser import parse_macro_tree
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.ops import MacroName, NewSegment
from flipjump.assembler.preprocessor import PreprocessorData, is_relocatable, resolve_macros
from flipjump.assembler.table_pool import RESERVED_BELOW, BlockPool, DeferredTables, HotSite, heat_key
from flipjump.fjm.fjm_reader import Reader
from flipjump.utils.constants import DEFAULT_MAX_MACRO_RECURSION_DEPTH
from flipjump.utils.exceptions import FlipJumpException, FlipJumpPreprocessorException
from flipjump.utils.functions import load_debugging_labels
from flipjump.utils.functions import get_file_tuples
from tests.unit.unit_utils import build_and_run, labels_off_their_ops, relocatable, sum_expr

W = 32
OP_BITS = 2 * W
POOL_BASE = 1 << 26  # above everything the programs below reach

XOR_PROGRAM = """
stl.startup_and_init_all
    hex.set 4, a, 0x1234
    hex.set 4, b, 0x5678
    rep(24, i) hex.xor 4, a, b
    hex.print_uint 4, a, 1, 1
    stl.loop
a: hex.vec 4
b: hex.vec 4
"""

CMP_PROGRAM = """
stl.startup_and_init_all
    hex.set 4, a, 0x00F5
    hex.set 4, b, 0x0031
    rep(6, i) hex.shl_bit 4, a
    rep(2, i) hex.shr_hex 4, a
    hex.mul 4, r, a, b
    hex.cmp 4, r, b, LT, EQ, GT
LT:
    hex.print_uint 4, r, 1, 1
    ;done
EQ:
    hex.print_uint 4, b, 1, 1
    ;done
GT:
    hex.print_uint 4, a, 1, 1
done:
    stl.loop
a: hex.vec 4
b: hex.vec 4
r: hex.vec 4
"""

# hex.input_hex's table has a `wflip` ENTRY (`flip3: wflip stl.IO+w, flip3, end`): the run of plain
# ops before it is not a table, and a pass that moves them away from it miscompiles every input
INPUT_PROGRAM = """
stl.startup_and_init_all
    hex.input b
    hex.print_uint 2, b, 1, 1
    stl.loop
b: hex.vec 2
"""

# `hex.xor e, d` arms d's jump word, so BlockPool pins d; the pointer read of d then jumps
# through that word expecting `value*dw` and lands in the block instead of the decoder
POINTER_PROGRAM = """
stl.startup_and_init_all
    hex.set d, 6
    hex.xor e, d
    hex.set v, 0xD
    hex.xor_hex_from_ptr v, p
    hex.print_as_digit v, 1
    stl.loop
d: hex.hex
e: hex.hex
v: hex.hex
p: hex.vec w/4, d
"""

# bit.exact_xor's table ends in `dst;` - a fall-through into the next op - so it is not declared relocatable
FALL_THROUGH_PROGRAM = """
stl.startup_and_init_all
    rep(16, i) bit.xor a+i*dw, b+i*dw
    bit.print 8, a
    stl.loop
a: bit.vec 16, 0x4A4B
b: bit.vec 16, 0x0102
"""

# every hex digit appears once, so each shift reaches all 16 entries of both of its tables
SHIFT_PROGRAM = """
stl.startup_and_init_all
    hex.shl_bit 16, a
    hex.shr_bit 16, b
    hex.print_as_digit 16, a, 0
    hex.print_as_digit 16, b, 0
    stl.loop
a: hex.vec 16, 0xFEDCBA9876543210
b: hex.vec 16, 0xFEDCBA9876543210
"""

# `b` and `b+0` name one word through two expressions, so its groups come in aliased pairs
ALIAS_PROGRAM = """
stl.startup_and_init_all
    hex.set 4, a, 0x1234
    hex.set 4, b, 0x5678
    hex.xor 4, a, b
    hex.xor 4, a, b+0
    hex.print_uint 4, a, 1, 1
    stl.loop
a: hex.vec 4
b: hex.vec 4
"""

EXT_TABLE = """
def ext_table x @ end < ext_switch {
    wflip x+w, ext_switch, x
    pad 16
    ext_table.__relocatable__.table ext_switch, end
  end:
    wflip x+w, ext_switch
}
""" + relocatable('ext_table', 'switch, end', 'switch', '    ;end\n' * 16)
# an expression the expansion evaluates names a table's label: its address is unknown until the expansion ends
EXPANSION_TIME_TEMPLATE = EXT_TABLE + """
def nothing {
    ;$
}
stl.startup_and_init_all
    hex.set x, 5
    ext_table x
{CODE}
    hex.print_as_digit x, 0
    stl.loop
x: hex.hex
{DATA}
"""
EXPANSION_TIME_CASES = {
    'rep': '    rep(ext_switch - ext_switch + 1, i) nothing',
    'pad': '    ;pad_end\n    pad ext_switch - ext_switch + 4\n  pad_end:',
}
# two words that name ext_switch, an unplaced table's label, when the pool is sized
ALIAS_DEFERRED_PROGRAM = (
    EXT_TABLE
    + """
def disp word @ switch, end {
    wflip word, switch, end
    pad 16
    disp.__relocatable__.table switch, end
  end:
    wflip word, switch
}
"""
    + relocatable('disp', 'switch, end', 'switch', '    ;end\n' * 16)
    + """
stl.startup_and_init_all
    hex.set x, 5
    ext_table x
    hex.print_as_digit x, 0
    stl.loop
    disp ext_switch+0+32
    disp ext_switch+32
x: hex.hex
"""
)


# --- helpers ---


def _pool_segments(fjm_path: Path, memory_width: int = W) -> List[int]:
    """the word addresses of the segments that landed in the pool"""
    reader = Reader(fjm_path)
    return [s.segment_start for s in reader.memory_segments if s.segment_start * memory_width >= POOL_BASE]


def _laid_out(width_hist: Dict[str, Dict[int, int]], **kwargs: Any) -> BlockPool:
    """a pool with the blocks of this census laid out, as place_deferred lays them out"""
    pool = BlockPool(W, POOL_BASE, **kwargs)
    pool.lay_out({group: sum(hist.values()) for group, hist in width_hist.items()}, width_hist)
    return pool


def _uniform(counts: Dict[str, int], **kwargs: Any) -> BlockPool:
    """_laid_out with every table 16 ops wide"""
    return _laid_out({group: {16: count} for group, count in counts.items()}, **kwargs)


def _reserve(pool: BlockPool, width: int = 16, group: str = 'g', site: str = '') -> Optional[int]:
    pool.exprs.setdefault(group, Expr(group))  # the group's source word, as the expansion records it
    return pool._reserve(16, width, group, site)


def _place(pool: BlockPool, sites: List[str], width: int = 16) -> Dict[str, int]:
    """{site path: slot index} for tables of `width` ops placed in this order, all in group 'g'"""
    base = pool.groups['g'][0]
    out = {}
    for site in sites:
        address = _reserve(pool, width, site=site)
        assert address is not None
        out[site] = (address - base) // (16 * OP_BITS)
    return out


def expand_labels(source: str, tmp_path: Path, pool: Optional[BlockPool], name: str = 'p') -> Dict[str, int]:
    """the labels of `source` expanded with the stl and `pool` (macro expansion only)"""
    fj_path = tmp_path / f'{name}.fj'
    fj_path.write_text(source)
    macros = parse_macro_tree(get_file_tuples([str(fj_path)]), W, True)
    _, labels = resolve_macros(W, macros, table_pool=pool, save_debug_labels=False)
    return labels


# --- declared tables: one expansion of a `__relocatable__` macro is one table ---

# `go x` arms x's word and dispatches to its declared table; {PAD} is the pad before the call, {CALL} the
# call, {TABLE} the table's declaration
DECLARED_TEMPLATE = """
def go x @ switch, end {
    wflip x+w, switch, x
{PAD}
{CALL}
  end:
    wflip x+w, switch
}
{TABLE}
stl.startup_and_init_all
    hex.set x, 2
    go x
    hex.print_as_digit x, 0
    stl.loop
x: hex.hex
"""
DECLARED_PARTS = {
    '{PAD}': '    pad 4',
    '{CALL}': '    go.__relocatable__.table switch, end',
    '{TABLE}': relocatable('go', 'switch, end', 'switch', '    ;end\n' * 4),
}
# {case: (the part it changes, to what, the error it is refused with when a pool is on)}
DECLARED_ERRORS = {
    'uses $': ('{TABLE}', relocatable('go', 'switch, end', 'switch', '    ;end\n    ;end\n    ;$\n    ;end\n'), '`$`'),
    'falls through': ('{TABLE}', relocatable('go', 'switch, end', 'switch', '    ;end\n' * 3 + '    end;\n'), '`$`'),
    'no pad': ('{PAD}', '', 'needs a `pad`'),
    'late start label': (
        '{TABLE}',
        'ns go {\n    ns __relocatable__ {\n        def table switch, end {\n            ;end\n          switch:\n'
        + '    ;end\n' * 3
        + '        }\n    }\n}\n',
        'start label',
    ),
    'not only ops': (
        '{TABLE}',
        relocatable('go', 'switch, end', 'switch', '    ;end\n' * 3 + '    wflip end, 1, end\n'),
        'only',
    ),
    'called by rep': ('{CALL}', '    rep(1, i) go.__relocatable__.table switch, end', 'rep'),
}


def _declared(**changes: str) -> str:
    source = DECLARED_TEMPLATE
    for part, text in {**DECLARED_PARTS, **changes}.items():
        source = source.replace(part, text)
    return source


def test_a_relocatable_is_a_macro_whose_innermost_namespace_is_relocatable() -> None:
    assert is_relocatable(MacroName('hex.exact_xor.__relocatable__.table', 6))
    assert is_relocatable(MacroName('__relocatable__.table'))
    assert not is_relocatable(MacroName('hex.exact_xor', 5))
    assert not is_relocatable(MacroName('__relocatable__.inner.table'))  # not the innermost namespace


def test_a_declared_table_moves_and_the_program_runs_the_same(tmp_path: Path) -> None:
    reference, _, _ = build_and_run(_declared(), tmp_path, None, name='inline')
    assert reference == b'2'
    output, _, fjm_path = build_and_run(_declared(), tmp_path, BlockPool(W, POOL_BASE))
    assert output == reference and _pool_segments(fjm_path)


def test_a_moved_table_holds_the_ops_its_declaration_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """a declared table holds only labels and `a;b` ops, so its expansion is the ops relocatable_call counts
    (end_relocation asserts it; it was a refusal no program could reach). The control: a miscount is caught."""
    labels = expand_labels(_declared(), tmp_path, BlockPool(W, POOL_BASE))
    assert any(address >= POOL_BASE for address in labels.values())
    counted = preprocessor.relocatable_call

    def miscounted(*args: Any) -> Any:
        table = counted(*args)
        return None if table is None else (table[0], table[1] + 1, *table[2:])

    monkeypatch.setattr(preprocessor, 'relocatable_call', miscounted)
    with pytest.raises(AssertionError):
        expand_labels(_declared(), tmp_path, BlockPool(W, POOL_BASE))


@pytest.mark.parametrize('case', sorted(DECLARED_ERRORS))
def test_a_declared_table_that_breaks_a_rule_is_refused_with_a_pool_on(tmp_path: Path, case: str) -> None:
    part, text, message = DECLARED_ERRORS[case]
    fj_path = tmp_path / 'p.fj'
    fj_path.write_text(_declared(**{part: text}))
    with pytest.raises(FlipJumpPreprocessorException, match=re.escape(message)):
        assemble(
            [fj_path], tmp_path / 'pooled.fjm', memory_width=W, print_time=False, table_pool=BlockPool(W, POOL_BASE)
        )
    # with the pool off nothing is checked and nothing changes
    assemble([fj_path], tmp_path / 'plain.fjm', memory_width=W, print_time=False)


# --- resolve_pinned ---


def test_resolve_pinned_resolves_each_word_once_labels_are_known() -> None:
    pinned, conflicts = resolve_pinned({sum_expr('x', 32): 0x1000, Expr('y'): 0x2000}, {'x': 4096, 'y': 8192})
    assert pinned == {4128: 0x1000, 8192: 0x2000} and conflicts == 0


def test_resolve_pinned_drops_both_pins_of_an_aliased_word() -> None:
    # `(x + 32)` and `(x + w)` are different expressions naming one word; either base is wrong
    # for the other block, so the word is left un-pinned (the full address is then written)
    pinned, conflicts = resolve_pinned({sum_expr('x', 32): 0x1000, sum_expr('x', 'w'): 0x2000}, {'x': 4096, 'w': 32})
    assert pinned == {} and conflicts == 1


def test_resolve_pinned_keeps_an_alias_with_one_base() -> None:
    pinned, conflicts = resolve_pinned({sum_expr('x', 32): 0x1000, sum_expr('x', 'w'): 0x1000}, {'x': 4096, 'w': 32})
    assert pinned == {4128: 0x1000} and conflicts == 0


def test_resolve_pinned_skips_the_runtime_words_and_unresolvable_ones() -> None:
    pinned, _ = resolve_pinned({Expr(64): 0x1000, Expr('missing'): 0x2000, Expr('z'): 0x3000}, {'z': 1 << 20})
    assert pinned == {1 << 20: 0x3000}


@pytest.mark.parametrize('memory_width', [8, 16, 32, 64])
def test_the_runtimes_words_are_reserved_at_every_width(memory_width: int) -> None:
    # G3: stl.IO is op 1, bits [2w, 4w) - the output flips at 2w and 2w+1, the input bit at 3w+#w (fjm_run's
    # _handle_output and _handle_input) - and RESERVED_BELOW covers it at every width
    w = memory_width
    assert 2 * w <= 2 * w + 1 < 4 * w and 2 * w <= 3 * w + w.bit_length() < 4 * w
    assert 4 * w <= RESERVED_BELOW


def test_resolve_pinned_leaves_a_raw_word_unpinned() -> None:
    pinned, _ = resolve_pinned({Expr('a'): 1, Expr('b'): 2}, {'a': 4096, 'b': 8192}, unpinned_words={8192})
    assert pinned == {4096: 1}


def test_unpinned_words_are_every_word_of_each_cell() -> None:
    # a label alone runs to the next label (the last one: its own op); (label, ops) runs `ops` ops; a label the
    # program does not define is skipped and listed
    labels = {'a': 4096, 'b': 4096 + 3 * W, 'c': 8192, 'z': 1 << 20}
    pool = BlockPool(W, POOL_BASE, pin_all_but=['a', ('c', 2), 'z', 'missing'])
    assert pool.unpinned_words(labels) == {
        *range(4096, 4096 + 3 * W, W),
        *range(8192, 8192 + 4 * W, W),
        *range(1 << 20, (1 << 20) + 2 * W, W),
    }
    assert pool.pin_all_but_missing == ['missing']
    assert (
        BlockPool(W, POOL_BASE).unpinned_words(labels)
        == set()
        == BlockPool(W, POOL_BASE, pin_all_but=[]).unpinned_words(labels)
    )


# --- BlockPool, on synthetic tables ---


def test_defer_counts_the_census_and_keeps_a_too_wide_table_inline() -> None:
    pool = BlockPool(W, POOL_BASE)
    assert [pool._defer(16, width, Expr('g')) for width in (16, 16, 31)] == [table_pool.DEFER_OUT] * 3
    assert pool._defer(16, 40, Expr('g')) == table_pool.DEFER_TOO_WIDE  # wider than max_slot_ops (32)
    assert pool._defer(16, 16, None) == table_pool.DEFER_INLINE and pool.ungrouped == 1
    assert pool.counts == {'g': 4} and pool.width_hist == {'g': {16: 2, 31: 1, 40: 1}} and pool.deferred == 3
    assert pool.census().widths == {'g': 40}


def test_a_block_hands_out_slots_and_pins_the_word() -> None:
    pool = _uniform({'g': 3})
    assert [_reserve(pool) for _ in range(3)] == [POOL_BASE + i * 16 * OP_BITS for i in range(3)]
    assert pool.allocated == 3 and pool.declined == 0
    assert pool.pinned_words() == {Expr('g'): POOL_BASE} or list(pool.pinned_words().values()) == [POOL_BASE]


def test_a_table_too_wide_for_any_slot_declines_and_breaks_the_group() -> None:
    pool = _laid_out({'g': {16: 2, 40: 1}})  # the 40-op table is counted in the widest (32-op) bucket, laid out first
    assert _reserve(pool) == POOL_BASE + 32 * OP_BITS
    assert _reserve(pool, 40) is None and pool.declined == 1 and pool.broken_groups == {'g'}
    assert pool.pinned_words() == {}  # a broken group is never pinned...
    pool.pin_incomplete_groups = True
    assert list(pool.pinned_words().values()) == [POOL_BASE]  # ...unless the caller opts in


def test_where_the_blocks_do_not_fit_the_last_groups_get_none() -> None:
    # big (4 slots) and mid (3 tables, 4 slots) are laid out before small; in 8 slots of span only big keeps its
    # block: mid's 3 tables and small's 1 take 4 slots of the overflow region after it
    slot = 16 * OP_BITS
    assert set(_uniform({'big': 4, 'mid': 3, 'small': 1}, pool_end=POOL_BASE + 9 * slot).groups) == {
        'big',
        'mid',
        'small',
    }
    pool = _uniform({'big': 4, 'mid': 3, 'small': 1}, pool_end=POOL_BASE + 8 * slot)
    assert set(pool.groups) == {'big'} and pool._used == 4 * slot
    assert _reserve(pool, group='mid') is None and _reserve(pool, group='big') == POOL_BASE  # mid: the overflow
    # a hot group always keeps its block (small: 2 slots, with its rank), laid out first: the others give theirs up
    hot = _uniform({'big': 4, 'mid': 3, 'small': 1}, pool_end=POOL_BASE + 9 * slot, heat={'small': [('h', 0, 16)]})
    assert set(hot.groups) == {'small'}


def test_a_pool_too_small_for_its_tables_is_refused() -> None:
    for heat in (None, {'g': [('h', 0, 16)]}):
        with pytest.raises(FlipJumpPreprocessorException, match='has no room for its tables'):
            _uniform({'a': 1, 'g': 2}, pool_end=POOL_BASE + 16 * OP_BITS, heat=heat)


def test_the_runtime_words_are_never_relocated() -> None:
    # `stl.IO` is a LABEL, declared by stl.startup before any code dispatches through it, so the
    # guard sees it resolved; a program variable, declared after the code, is not resolvable yet
    pool = BlockPool(W, POOL_BASE)
    data = PreprocessorData(W, {}, DEFAULT_MAX_MACRO_RECURSION_DEPTH, table_pool=pool)
    data.labels['stl.IO'] = 64
    assert not data.begin_relocation(16, '', 16, sum_expr('stl.IO', 32), 'T')
    assert pool.reserved_words == 1 and pool.deferred == 0
    assert data.begin_relocation(16, '', 16, sum_expr('x', 32), 'T')
    assert pool.reserved_words == 1 and pool.deferred == 1


def test_sparse_indices_use_the_cheapest_slots_of_a_spread_block() -> None:
    pool = _uniform({'g': 300}, spread_factor=2, spread_min_tables=256)
    assert [_reserve(pool) for _ in range(4)] == [POOL_BASE + i * 16 * OP_BITS for i in (0, 1, 2, 4)]


def test_width_buckets_give_each_width_its_own_sub_block() -> None:
    pool = _laid_out({'g': {16: 3, 64: 1}}, max_slot_ops=512)
    layout, block_bits = pool._bucket_layout('g')
    assert layout == {64: (0, 1, 1), 16: (64 * OP_BITS, 4, 3)}
    assert block_bits == 2 * 64 * OP_BITS
    assert _reserve(pool, 40) == POOL_BASE
    assert _reserve(pool, 16) == POOL_BASE + 64 * OP_BITS
    assert _reserve(pool, 600) is None and pool.declined == 1


def test_blocks_are_laid_out_biggest_first() -> None:
    pool = _uniform({'small': 1, 'big': 8, 'mid': 2})
    assert [pool.groups[g][0] - POOL_BASE for g in ('big', 'mid', 'small')] == [0, 8 * 16 * OP_BITS, 10 * 16 * OP_BITS]


# --- pin protection: BlockPool(heat=...) ---


def test_heat_key_strips_the_call_site_coordinates() -> None:
    assert heat_key('f13:l2210:sim.pass(3)---s2:l40:rep3:hex.exact_xor(5)') == 'sim.pass(3)---rep3:hex.exact_xor(5)'
    assert heat_key('((f13:l2210:sim.pass(3)---hp + 128) + 32)') == '((sim.pass(3)---hp + 128) + 32)'
    assert heat_key('(hex.tables.res + 32)') == '(hex.tables.res + 32)'


def test_hot_sites_get_the_cheapest_indices_whatever_the_encounter_order() -> None:
    sites = [f'f1:l{line}:cold' for line in range(5)] + ['f1:l9:hot']  # the hot table comes LAST
    assert _place(_uniform({'g': 6}), sites)['f1:l9:hot'] == 5  # encounter order: popcount 2
    hot = _uniform({'g': 6}, heat={'g': [('hot', 0, 16)]})
    placed = _place(hot, sites)
    assert placed['f1:l9:hot'] == 0  # its rank's index: popcount 0
    assert sorted(placed.values()) == [0, 1, 2, 3, 4, 5]  # the others take the next cheapest, once each
    assert hot.hot_sites_matched == 1


def test_hot_sites_rank_within_their_width_bucket() -> None:
    pool = _laid_out({'g': {16: 2, 64: 2}}, max_slot_ops=512, heat={'g': [('b', 0, 16), ('a', 0, 64)]})
    wide, narrow = 64 * OP_BITS, 16 * OP_BITS
    # each width holds its 2 tables plus its 1 reserved rank: 4 slots; the 64-op bucket comes first
    narrow_bucket = 4 * wide
    got = [_reserve(pool, width, site=site) for site, width in (('x', 64), ('a', 64), ('y', 16), ('b', 16))]
    assert [address - POOL_BASE for address in got] == [  # type: ignore[operator]
        wide,  # x: the rank after `a`'s
        0,  # a: the cheapest 64-op slot
        narrow_bucket + narrow,  # y: the rank after `b`'s
        narrow_bucket,  # b: the cheapest 16-op slot
    ]


def test_three_listed_sites_take_three_distinct_cheapest_indices() -> None:
    pool = _uniform({'g': 4}, heat={'g': [('a', 0, 16), ('b', 0, 16), ('c', 0, 16)]})
    # 4 tables + 3 reserved ranks -> 8 slots, cheapest first 0, 1, 2, 4, 3, ...
    assert _place(pool, ['f1:l1:c', 'f1:l2:x', 'f1:l3:b', 'f1:l4:a']) == {
        'f1:l1:c': 2,
        'f1:l2:x': 4,
        'f1:l3:b': 1,
        'f1:l4:a': 0,
    }


def test_a_listed_site_whose_width_changed_is_placed_as_unlisted() -> None:
    # `a` was profiled as a 16-op table and is now 64 ops wide; `c` is listed at 64 ops. Taking `a`'s
    # old rank in the 64-op width would put it in `c`'s slot.
    pool = _laid_out({'g': {64: 2, 16: 1}}, max_slot_ops=512, heat={'g': [('a', 0, 16), ('c', 0, 64)]})
    got = {
        site: _reserve(pool, width, site=f'f1:l{line}:{site}')
        for line, (site, width) in enumerate((('a', 64), ('c', 64), ('b', 16)))
    }
    assert got['a'] == POOL_BASE + 64 * OP_BITS  # unlisted in its new width: after `c`'s rank
    assert got['c'] == POOL_BASE
    assert pool.hot_sites_width_changed == 1 and pool.hot_sites_matched == 1


def test_occurrences_count_the_tables_that_decline() -> None:
    # the census saw both tables, so the heat list numbers the 16-op one as occurrence 1
    pool = _laid_out({'g': {40: 1, 16: 1}}, heat={'g': [('site', 1, 16)]})
    assert _reserve(pool, 40, site='f1:l1:site') is None  # too wide
    assert _reserve(pool, 16, site='f1:l2:site') == POOL_BASE + 32 * OP_BITS  # after the 32-op bucket
    assert pool.hot_sites_matched == 1


def test_a_listed_site_the_program_lost_pushes_no_table_out() -> None:
    sites = [f'f1:l{line}:t' for line in range(4)]
    for heat in (None, {'g': [('gone', 0, 16)]}):
        pool = _uniform({'g': 4}, heat=heat)
        assert sorted(_place(pool, sites).values()) == ([0, 1, 2, 3] if heat is None else [1, 2, 3, 4])
        assert pool.declined == 0 and not pool.broken_groups


def test_a_hot_width_grows_only_when_its_ranks_do_not_fit() -> None:
    # 300 tables with spread 2 own 1,024 slots, so 300 reserved ranks (600 in all) fit and the block
    # keeps its size; sizing the next power of two of 600 before the spread would double it
    heat = {'g': [(f's{i}', 0, 16) for i in range(300)]}
    plain = _uniform({'g': 300}, spread_factor=2, spread_min_tables=256)
    hot = _uniform({'g': 300}, spread_factor=2, spread_min_tables=256, heat=heat)
    assert hot._block_bits('g') == plain._block_bits('g')
    # 4 tables own 4 slots: 3 more ranks do not fit, so the block doubles
    grown = _uniform({'g': 4}, heat={'g': [(s, 0, 16) for s in 'abc']})
    assert grown._block_bits('g') == 2 * _uniform({'g': 4})._block_bits('g')


def test_hot_groups_are_placed_first_from_the_pool_base() -> None:
    assert _uniform({'big': 8, 'small': 1}).groups['big'][0] == POOL_BASE  # biggest first
    hot = _uniform({'big': 8, 'small': 1}, heat={'small': [('h', 0, 16)]})
    assert hot.groups['small'][0] == POOL_BASE and hot.groups['big'][0] > POOL_BASE


def test_sites_that_share_a_key_are_told_apart_by_occurrence() -> None:
    # two calls of one macro on different lines strip to one key; the heat list names the second
    pool = _uniform({'g': 2}, heat={'g': [('site', 1, 16)]})
    assert _place(pool, ['f1:l5:site', 'f1:l8:site']) == {'f1:l5:site': 1, 'f1:l8:site': 0}


def test_a_hot_group_is_pinned_even_when_it_breaks() -> None:
    for heat, pinned in ((None, False), ({'g': [('h', 0, 16)]}, True)):
        pool = _laid_out({'g': {16: 2, 40: 1}}, heat=heat)
        _reserve(pool, 16, site='f1:l1:t')
        _reserve(pool, 40, site='f1:l2:t')  # too wide: the group breaks
        assert pool.broken_groups == {'g'} and bool(pool.pinned_words()) == pinned


def test_heat_names_are_matched_without_coordinates_and_reported() -> None:
    heat = {'((x + 64) + 32)': [('h', 0, 16)], '(gone + 32)': [('h', 0, 16)], '(twice + 32)': [('h', 0, 16)]}
    pool = _uniform({'((f2:l7:x + 64) + 32)': 1, '(f1:l1:twice + 32)': 1, '(f1:l2:twice + 32)': 1}, heat=heat)
    assert set(pool.hot_sites) == {'((f2:l7:x + 64) + 32)'}
    assert pool.hot_missing == ['(gone + 32)'] and pool.hot_ambiguous == ['(twice + 32)']
    assert pool.heat_report() == {
        'hot_groups': 1,
        'hot_groups_missing': 1,
        'hot_groups_ambiguous': 1,
        'hot_sites_listed': 1,
        'hot_sites_matched': 0,
        'hot_sites_width_changed': 0,
    }


def test_the_heat_list_may_not_name_a_site_twice() -> None:
    with pytest.raises(FlipJumpPreprocessorException, match='twice'):
        _uniform({'g': 2}, heat={'g': [('h', 0, 16), ('h', 0, 16)]})


def test_no_heat_and_an_empty_heat_list_change_nothing() -> None:
    sites = [(group, f'f1:l{i}:t') for i in range(40) for group in ('g', 'k') if group == 'g' or i < 5]
    results = []
    for heat in (None, {}, {'(absent + 32)': [('h', 0, 16)]}):
        pool = _uniform({'g': 300, 'k': 5}, spread_factor=2, spread_min_tables=256, heat=heat)
        results.append([_reserve(pool, group=g, site=site) for g, site in sites])
    assert results[0] == results[1] == results[2]


def test_a_heat_ordered_program_is_the_same_program_in_fewer_ops(tmp_path: Path) -> None:
    recorder = BlockPool(W, POOL_BASE)
    reference, plain_ops, _ = build_and_run(XOR_PROGRAM, tmp_path, recorder, name='plain')
    assert recorder.report is not None
    # the busiest group's LAST table, which encounter order hands its dearest index
    group = max(recorder.counts, key=lambda g: (recorder.counts[g], g))
    last = [table for table in recorder.report.table_sites if table.group == group][-1]
    assert last.site.endswith('hex.exact_xor(5)') and last.address  # a real table's path, and where it went
    placing = BlockPool(W, POOL_BASE, heat={group: [HotSite(last.site, last.occurrence, 16)]})
    output, ops, _ = build_and_run(XOR_PROGRAM, tmp_path, placing, name='heat')
    assert output == reference
    assert placing.hot_sites_matched == 1 and placing.pinned_words()
    assert ops < plain_ops


# --- end to end: a blocked program is the same program, in fewer ops ---


@pytest.mark.parametrize('memory_width', [32, 64])
@pytest.mark.parametrize('source', [XOR_PROGRAM, CMP_PROGRAM, SHIFT_PROGRAM], ids=['xor', 'cmp/shift/mul', 'shifts'])
def test_blocked_tables_leave_the_output_unchanged_and_cost_fewer_ops(
    source: str, tmp_path: Path, memory_width: int
) -> None:
    reference, reference_ops, inline_fjm = build_and_run(
        source, tmp_path, None, name='inline', memory_width=memory_width
    )
    assert reference and not _pool_segments(inline_fjm, memory_width)
    pool = BlockPool(memory_width, POOL_BASE)
    output, ops, fjm_path = build_and_run(source, tmp_path, pool, memory_width=memory_width)
    assert output == reference
    assert pool.allocated > 0 and pool.allocated == pool.deferred and pool.pinned_words()
    assert _pool_segments(fjm_path, memory_width) and ops < reference_ops


def test_the_shift_tables_are_tables_a_pool_can_see(tmp_path: Path) -> None:
    # shl_bit_once / shr_bit_once dispatch like exact_xor, and declare their tables relocatable
    pool = BlockPool(W, POOL_BASE)
    output, _, _ = build_and_run(SHIFT_PROGRAM, tmp_path, pool)
    assert output == b'fdb97530eca864207f6e5d4c3b2a1908'
    assert sum(pool.counts.values()) == pool.allocated == 32  # shl_bit 16 + shr_bit 16, one table per call


@pytest.mark.parametrize('memory_width', [32, 64])
def test_an_input_table_is_never_split(tmp_path: Path, memory_width: int) -> None:
    reference, _, _ = build_and_run(INPUT_PROGRAM, tmp_path, None, name='inline', fixed_input=b'A')
    assert reference == b'0x41'
    pool = BlockPool(memory_width, POOL_BASE)
    output, _, _ = build_and_run(INPUT_PROGRAM, tmp_path, pool, fixed_input=b'A', memory_width=memory_width)
    assert output == reference and pool.deferred > 0


def test_a_pinned_word_read_through_a_pointer_needs_the_callers_exclusion(tmp_path: Path) -> None:
    reference, _, _ = build_and_run(POINTER_PROGRAM, tmp_path, None, name='inline')
    assert reference == b'B'
    # without a declaration of the cells pointers reach, no word is pinned: the same program, slower
    unpinned_pool = BlockPool(W, POOL_BASE)
    output, _, _ = build_and_run(POINTER_PROGRAM, tmp_path, unpinned_pool, name='unpinned')
    assert unpinned_pool.reads_words_raw
    assert output == reference and list(unpinned_pool.unpins) == ['the pointer runtime']
    # with the pointed cell named raw (one op at `d`), the blocked program is the same program
    blocked_pool = BlockPool(W, POOL_BASE, pin_all_but=[('d', 1)])
    output, _, _ = build_and_run(POINTER_PROGRAM, tmp_path, blocked_pool, name='excluded')
    assert output == reference and blocked_pool.unpins == {}
    assert blocked_pool.pinned_words()  # the other armed words are still pinned


def test_a_pin_all_but_label_the_program_does_not_define_is_an_error_or_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / 'p.fj'
    source.write_text(POINTER_PROGRAM)
    with pytest.raises(FlipJumpException, match='does not define: dd'):  # warnings are errors: the default
        assemble([source], tmp_path / 'e.fjm', memory_width=W, print_time=False, table_pool=_pin_all_but('dd'))
    pool = _pin_all_but('dd')
    assemble([source], tmp_path / 'w.fjm', memory_width=W, print_time=False, table_pool=pool, warning_as_errors=False)
    assert pool.pin_all_but_missing == ['dd'] and 'Warning: pin_all_but names labels' in capsys.readouterr().out
    # the control: a label the program defines is no warning
    pool = _pin_all_but('d')
    assemble([source], tmp_path / 'd.fjm', memory_width=W, print_time=False, table_pool=pool)
    assert pool.pin_all_but_missing == []


def test_the_report_holds_the_words_the_pool_pinned(tmp_path: Path) -> None:
    # pool.report.pinned is the final pin map: with `d` left out its jump word is not in it, with [] it is
    source = tmp_path / 'p.fj'
    source.write_text(POINTER_PROGRAM)
    reports = {}
    for name, cells in (('all_but_d', [('d', 1)]), ('all', [])):
        pool = BlockPool(W, POOL_BASE, pin_all_but=cells)
        debug = tmp_path / f'{name}.fjd'
        assemble(
            [source],
            tmp_path / f'{name}.fjm',
            memory_width=W,
            print_time=False,
            table_pool=pool,
            debugging_file_path=debug,
        )
        assert pool.report is not None and pool.report.moved == pool.deferred > 0
        reports[name] = (pool.report, load_debugging_labels(debug)['d'] + W)
    report, d_jump = reports['all_but_d']
    assert report.pinned and d_jump not in report.pinned
    report, d_jump = reports['all']
    assert d_jump in report.pinned


def _pin_all_but(*cells: str) -> BlockPool:
    return BlockPool(W, POOL_BASE, pin_all_but=cells)


def test_an_undeclared_table_stays_inline(tmp_path: Path) -> None:
    reference, _, _ = build_and_run(FALL_THROUGH_PROGRAM, tmp_path, None, name='inline')
    pool = BlockPool(W, POOL_BASE)
    output, _, _ = build_and_run(FALL_THROUGH_PROGRAM, tmp_path, pool)
    assert output == reference and pool.deferred == 0


def test_a_program_that_grows_into_the_pool_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FlipJumpException):
        build_and_run(XOR_PROGRAM, tmp_path, BlockPool(W, 1 << 12))


@pytest.mark.parametrize('pin_incomplete', [False, True], ids=['unpin-incomplete', 'pin-incomplete'])
def test_a_table_too_wide_for_any_slot_stays_inline_and_the_program_runs_the_same(
    tmp_path: Path, pin_incomplete: bool
) -> None:
    # no slot of max_slot_ops=16 holds hex.cmp's 31-op table: it is declined at its pad, and its group breaks
    reference, _, _ = build_and_run(CMP_PROGRAM, tmp_path, None, name='inline')
    pool = BlockPool(W, POOL_BASE, max_slot_ops=16, pin_incomplete_groups=pin_incomplete)
    output, _, _ = build_and_run(CMP_PROGRAM, tmp_path, pool)
    assert output == reference and pool.declined > 0 and pool.broken_groups
    pinned = set(pool.pinned_words())
    for group, (_, expr) in pool.groups.items():
        assert (expr in pinned) == (expr is not None and (pin_incomplete or group not in pool.broken_groups))


# a span too small for hex.cmp's blocks: the groups at the end of the layout order go to the overflow region
NO_ROOM_SPAN = 0x30000


def _overflow_starts(pool: BlockPool, fjm_path: Path) -> List[int]:
    blocks_end = POOL_BASE + pool._used
    return [s.segment_start * W for s in Reader(fjm_path).memory_segments if s.segment_start * W >= blocks_end]


@pytest.mark.parametrize('pin_incomplete', [False, True], ids=['unpin-incomplete', 'pin-incomplete'])
def test_what_gets_no_block_goes_to_the_overflow_region(tmp_path: Path, pin_incomplete: bool) -> None:
    reference, _, _ = build_and_run(CMP_PROGRAM, tmp_path, None, name='inline')
    pool = BlockPool(W, POOL_BASE, pool_end=POOL_BASE + NO_ROOM_SPAN, pin_incomplete_groups=pin_incomplete)
    output, _, fjm_path = build_and_run(CMP_PROGRAM, tmp_path, pool)
    assert output == reference
    assert pool.overflowed > 0 and pool.allocated > 0 and pool.deferred == pool.allocated + pool.overflowed
    starts = _overflow_starts(pool, fjm_path)
    assert len(starts) == pool.overflowed and max(starts) < POOL_BASE + NO_ROOM_SPAN  # after the blocks, in the pool
    unblocked = [group for group in pool.exprs if pool.alias.get(group, group) not in pool.groups]
    assert (
        unblocked and pool.pinned_words() and not {pool.exprs[group] for group in unblocked} & set(pool.pinned_words())
    )


def test_an_overflow_table_moved_off_its_labels_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """the control: the first overflow table's labels one slot (16 ops) above its ops - not the same program"""
    reference, _, _ = build_and_run(CMP_PROGRAM, tmp_path, None, name='inline')

    def first_overflow(pool: BlockPool, tables: DeferredTables, addresses: Sequence[int]) -> int:
        unblocked = [i for i, group in enumerate(tables.groups) if pool.alias.get(group, group) not in pool.groups]
        return min((addresses[i], i) for i in unblocked if addresses[i])[1]

    labels_off_their_ops(monkeypatch, 16 * OP_BITS, first_overflow)
    try:
        output = build_and_run(CMP_PROGRAM, tmp_path, BlockPool(W, POOL_BASE, pool_end=POOL_BASE + NO_ROOM_SPAN))[0]
    except FlipJumpException:
        output = None  # (refused: its segment overlaps another)
    assert output != reference


def test_a_program_too_big_for_its_pool_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FlipJumpException, match='has no room for its tables'):
        build_and_run(CMP_PROGRAM, tmp_path, BlockPool(W, POOL_BASE, pool_end=POOL_BASE + (1 << 14)), name='tiny')


def test_the_op_stream_does_not_depend_on_what_the_pool_decides(tmp_path: Path) -> None:
    """every label outside the tables lands at the same address whatever the pool does with them"""

    def outside_tables(labels: Dict[str, int]) -> Dict[str, int]:
        return {label: address for label, address in labels.items() if address < POOL_BASE}

    configs: List[Dict[str, Any]] = [{}, {'spread_factor': 2, 'spread_min_tables': 4}, {'max_slot_ops': 512}]
    laid_out = [outside_tables(expand_labels(CMP_PROGRAM, tmp_path, BlockPool(W, POOL_BASE, **c))) for c in configs]
    assert all(labels == laid_out[0] for labels in laid_out[1:])
    assert outside_tables(expand_labels(CMP_PROGRAM, tmp_path, None)) != laid_out[0]  # the comparison can see a move


def test_the_pool_merges_the_groups_of_one_word(tmp_path: Path) -> None:
    reference, _, _ = build_and_run(ALIAS_PROGRAM, tmp_path, None, name='inline')
    pool = BlockPool(W, POOL_BASE)
    output, _, _ = build_and_run(ALIAS_PROGRAM, tmp_path, pool)
    assert output == reference and pool.alias and pool.pinned_words()
    census = pool.census()
    assert set(census.counts) == set(pool.groups) and not set(pool.alias) & set(census.counts)


def test_the_aliases_a_pool_cannot_resolve_are_counted(tmp_path: Path) -> None:
    reference, _, _ = build_and_run(ALIAS_DEFERRED_PROGRAM, tmp_path, None, name='inline')
    pool = BlockPool(W, POOL_BASE)
    output, _, _ = build_and_run(ALIAS_DEFERRED_PROGRAM, tmp_path, pool)
    assert output == reference
    assert pool.alias_unresolved == 2 and pool.alias == {}  # both words name ext_switch, unplaced then


@pytest.mark.parametrize('case', list(EXPANSION_TIME_CASES))
def test_a_table_label_evaluated_during_the_expansion_is_refused(tmp_path: Path, case: str) -> None:
    source = EXPANSION_TIME_TEMPLATE.replace('{CODE}', EXPANSION_TIME_CASES[case]).replace('{DATA}', '')
    assert build_and_run(source, tmp_path, None, name='inline')[0] == b'5'
    with pytest.raises(FlipJumpException, match="Can't evaluate"):
        build_and_run(source, tmp_path, BlockPool(W, POOL_BASE), name='pooled')


def test_a_deferred_table_costs_tens_of_bytes() -> None:
    """what the pool keeps per queued table, beyond what an inline table holds too (the op pointers, the
    NewSegment, the label strings, the site path string): see DeferredTables"""
    count = 20000
    groups = [f'((f13:l2210:sim.pass(3)---hp + {128 * (i % 50)}) + 32)' for i in range(count)]
    segments = [NewSegment(0) for _ in range(count)]
    names = [f'f13:l2210:sim.pass(3)---s2:l40:rep{i}:hex.exact_xor(5)---switch' for i in range(count)]
    sites = [name.rpartition('---')[0] for name in names]

    def per_table(labels_per_table: int) -> float:
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        tables = DeferredTables()
        for i in range(count):
            tables.add(groups[i], 16, 16, sites[i], segments[i])
            for label in range(labels_per_table):
                tables.add_label(names[i], label * 448)
        cost = (tracemalloc.get_traced_memory()[0] - before) / count
        tracemalloc.stop()
        assert len(tables.sizes) == count
        return cost

    one_label, two_labels = per_table(1), per_table(2)
    extra_label = two_labels - one_label
    print(f'deferred table: {one_label:.1f} B, a 2nd label +{extra_label:.1f} B')
    assert one_label < 80 and extra_label < 24


def test_the_pool_is_importable_from_the_preprocessor_too() -> None:
    for name in ('BlockPool', 'heat_key'):
        assert getattr(preprocessor, name) is getattr(table_pool, name), name
