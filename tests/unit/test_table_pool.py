"""
unit-tests for the assembler's table placement: TablePool (relocate lookup tables to
low-popcount addresses), BlockPool (group the tables dispatched through one jump word into an
aligned block, so the arming wflip writes an index instead of an address), the declared tables
(`__relocatable__` macros) and the pin resolver (assembler.resolve_pinned).

the property pinned throughout: a relocated or blocked program produces the SAME output as the
inline one, in fewer ops - a table is reached only by jump, so its address is free to choose.

a placing BlockPool's census (what it met, in the shape the next placing pool takes) must equal a
counting pass's report on the same program, and stale counts may cost ops but never correctness.
"""

import json
import random
import re
import tracemalloc
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

import pytest

import flipjump.assembler.preprocessor as preprocessor
import flipjump.assembler.table_pool as table_pool
from flipjump import assemble
from flipjump.assembler.assembler import resolve_pinned
from flipjump.assembler.fj_parser import parse_macro_tree
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.inner_classes.ops import CodePosition, MacroName, NewSegment
from flipjump.assembler.preprocessor import PreprocessorData, is_relocatable, resolve_macros
from flipjump.assembler.table_pool import (
    DECLINE_OTHER,
    DECLINE_STALE,
    DEFER_OUT,
    RESERVED_BELOW,
    BlockCensus,
    BlockPool,
    DeferredTables,
    TablePool,
    heat_key,
    merge_aliased_groups,
)
from flipjump.fjm.fjm_reader import Reader
from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.utils.constants import DEFAULT_MAX_MACRO_RECURSION_DEPTH
from flipjump.utils.exceptions import FlipJumpException, FlipJumpPreprocessorException
from flipjump.utils.functions import get_file_tuples

W = 32
OP_BITS = 2 * W
POOL_BASE = 1 << 26  # above everything the programs below reach
CODE_POSITION = CodePosition('file.fj', 'f1', 1)

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


def relocatable(name: str, params: str, start: str, entries: str) -> str:
    """the declaration of the relocatable table `name.__relocatable__.table params`: `start:` then the entries"""
    return (
        f'ns {name} {{\n    ns __relocatable__ {{\n        def table {params} {{\n          {start}:\n'
        + entries
        + '        }\n    }\n}\n'
    )


# --- helpers ---


def build_and_run(
    source: str,
    tmp_path: Path,
    pool: Optional[TablePool],
    name: str = 'p',
    fixed_input: bytes = b'',
    memory_width: int = W,
) -> Tuple[bytes, int, Path]:
    """assemble `source` with the stl (and the pool, if given), run it on `fixed_input`, and
    return (output, op count, fjm path)."""
    fj_path = tmp_path / f'{name}.fj'
    fj_path.write_text(source)
    fjm_path = tmp_path / f'{name}.fjm'
    assemble([fj_path], fjm_path, memory_width=memory_width, print_time=False, table_pool=pool)
    io_device = FixedIO(fixed_input)
    statistics = fjm_run.run(fjm_path, io_device=io_device, print_time=False)
    return io_device.get_output(allow_incomplete_output=True), statistics.op_counter, fjm_path


def pool_segments(fjm_path: Path, memory_width: int = W) -> List[int]:
    """the word addresses of the segments that landed in the pool"""
    reader = Reader(fjm_path)
    return [s.segment_start for s in reader.memory_segments if s.segment_start * memory_width >= POOL_BASE]


def blocked(source: str, tmp_path: Path, span_bits: Optional[int] = None) -> Tuple[bytes, int, BlockPool]:
    """the two-pass blocking build: count, then place with the frozen counts"""
    counting = BlockPool(W, POOL_BASE, span_bits=span_bits)
    build_and_run(source, tmp_path, counting, name='counting')
    placing = BlockPool(W, POOL_BASE, counts=counting.counts, widths=counting.widths, span_bits=span_bits)
    output, ops, _ = build_and_run(source, tmp_path, placing, name='placing')
    return output, ops, placing


# --- TablePool ---


def test_offsets_are_handed_out_cheapest_popcount_first() -> None:
    pool = TablePool(W, POOL_BASE, run_ops=16, span_bits=16 * OP_BITS * 64)
    offsets = list(islice(pool._cheapest_offsets(), 100))
    assert offsets[:8] == [0, 1, 2, 4, 8, 16, 32, 3]
    assert sorted(offsets) == list(range(64))
    assert offsets == sorted(range(64), key=lambda v: (bin(v).count('1'), v))


def test_offsets_are_generated_lazily_so_w64_works() -> None:
    # an unbounded pool at w=64 has 2^49 run offsets; they are never materialised
    pool = TablePool(64, 1 << 40)
    assert pool.reserve(16, 16) == (1 << 40, True, 0)
    pool.commit((1 << 40) + 16 * 128)
    blocked_pool = BlockPool(64, 1 << 40, counts={'g': 2}, widths={'g': 16})
    assert blocked_pool.reserve(16, 16, group='g', group_expr=Expr('g')) == (1 << 40, True, 0)


def test_run_ops_must_make_a_power_of_two_run() -> None:
    with pytest.raises(ValueError):
        TablePool(W, POOL_BASE, run_ops=24)


def test_reserve_opens_a_run_then_aligns_inside_it() -> None:
    pool = TablePool(W, POOL_BASE, run_ops=256)
    assert pool.reserve(16, 16) == (POOL_BASE, True, 0)
    pool.commit(POOL_BASE + 16 * OP_BITS)
    assert pool.reserve(4, 4) == (POOL_BASE + 16 * OP_BITS, False, 0)
    pool.commit(POOL_BASE + 20 * OP_BITS)
    # the next 16-aligned spot is op 32 of the run: 12 ops of gap the caller must emit as padding
    assert pool.reserve(16, 16) == (POOL_BASE + 32 * OP_BITS, False, 12)
    assert pool.allocated == 3 and pool.declined == 0


def test_a_table_wider_than_a_run_claims_consecutive_slots() -> None:
    pool = TablePool(W, POOL_BASE, run_ops=256)
    assert pool.reserve(16, 16) == (POOL_BASE, True, 0)
    pool.commit(POOL_BASE + 16 * OP_BITS)
    run_bits = 256 * OP_BITS
    # 300 ops do not fit the 240 left in run 0, nor a single run: slots 1 and 2 are taken together
    assert pool.reserve(16, 300) == (POOL_BASE + run_bits, True, 0)
    pool.commit(POOL_BASE + run_bits + 300 * OP_BITS)
    assert pool._consumed == {0, 1, 2}
    # the next new run skips the claimed slots
    assert pool.reserve(16, 256) == (POOL_BASE + 4 * run_bits, True, 0)


def test_reserve_declines_when_the_pool_is_full() -> None:
    pool = TablePool(W, POOL_BASE, run_ops=16, span_bits=16 * OP_BITS)  # room for exactly one run
    assert pool.reserve(16, 16) == (POOL_BASE, True, 0)
    pool.commit(POOL_BASE + 16 * OP_BITS)
    assert pool.reserve(16, 16) is None
    assert pool.declined == 1


def test_commit_past_the_reserved_run_raises() -> None:
    pool = TablePool(W, POOL_BASE, run_ops=16)
    pool.reserve(16, 16)
    with pytest.raises(FlipJumpPreprocessorException):
        pool.commit(POOL_BASE + 32 * OP_BITS)


def test_has_room_honours_the_capacity() -> None:
    assert TablePool(W, POOL_BASE).has_room()
    capped = TablePool(W, POOL_BASE, capacity=1)
    assert capped.has_room()
    capped.reserve(16, 16)
    assert not capped.has_room()


def test_a_plain_pool_pins_nothing() -> None:
    assert TablePool(W, POOL_BASE).pinned_words() == {}


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


def declared(**changes: str) -> str:
    source = DECLARED_TEMPLATE
    for part, text in {**DECLARED_PARTS, **changes}.items():
        source = source.replace(part, text)
    return source


def test_a_relocatable_is_a_macro_whose_innermost_namespace_is_relocatable() -> None:
    assert is_relocatable(MacroName('hex.exact_xor.__relocatable__.table', 6))
    assert is_relocatable(MacroName('__relocatable__.table'))
    assert not is_relocatable(MacroName('hex.exact_xor', 5))
    assert not is_relocatable(MacroName('__relocatable__.inner.table'))  # not the innermost namespace


@pytest.mark.parametrize('blocking', [False, True], ids=['TablePool', 'BlockPool one pass'])
def test_a_declared_table_moves_and_the_program_runs_the_same(tmp_path: Path, blocking: bool) -> None:
    reference, _, plain_fjm = build_and_run(declared(), tmp_path, None, name='inline')
    assert reference == b'2'
    pool = BlockPool(W, POOL_BASE, one_pass=True) if blocking else TablePool(W, POOL_BASE)
    output, _, fjm_path = build_and_run(declared(), tmp_path, pool)
    assert output == reference and pool_segments(fjm_path)


@pytest.mark.parametrize('case', sorted(DECLARED_ERRORS))
def test_a_declared_table_that_breaks_a_rule_is_refused_with_a_pool_on(tmp_path: Path, case: str) -> None:
    part, text, message = DECLARED_ERRORS[case]
    fj_path = tmp_path / 'p.fj'
    fj_path.write_text(declared(**{part: text}))
    with pytest.raises(FlipJumpPreprocessorException, match=re.escape(message)):
        assemble(
            [fj_path], tmp_path / 'pooled.fjm', memory_width=W, print_time=False, table_pool=TablePool(W, POOL_BASE)
        )
    # with the pool off nothing is checked and nothing changes
    assemble([fj_path], tmp_path / 'plain.fjm', memory_width=W, print_time=False)


# --- resolve_pinned ---


def _sum(label: str, value: Union[int, str]) -> Expr:
    return Expr(('+', (Expr(label), Expr(value))))


def test_resolve_pinned_resolves_each_word_once_labels_are_known() -> None:
    pinned, conflicts = resolve_pinned({_sum('x', 32): 0x1000, Expr('y'): 0x2000}, {'x': 4096, 'y': 8192})
    assert pinned == {4128: 0x1000, 8192: 0x2000} and conflicts == 0


def test_resolve_pinned_drops_both_pins_of_an_aliased_word() -> None:
    # `(x + 32)` and `(x + w)` are different expressions naming one word; either base is wrong
    # for the other block, so the word is left un-pinned (the full address is then written)
    pinned, conflicts = resolve_pinned({_sum('x', 32): 0x1000, _sum('x', 'w'): 0x2000}, {'x': 4096, 'w': 32})
    assert pinned == {} and conflicts == 1


def test_resolve_pinned_keeps_an_alias_with_one_base() -> None:
    pinned, conflicts = resolve_pinned({_sum('x', 32): 0x1000, _sum('x', 'w'): 0x1000}, {'x': 4096, 'w': 32})
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


def test_resolve_pinned_lets_the_caller_veto_a_word() -> None:
    pinned, _ = resolve_pinned(
        {Expr('a'): 1, Expr('b'): 2}, {'a': 4096, 'b': 8192}, exclude=lambda a, labels: a == 8192
    )
    assert pinned == {4096: 1}


# --- BlockPool, on synthetic reservations ---


def test_counting_pass_records_counts_widths_and_the_width_histogram() -> None:
    counting = BlockPool(W, POOL_BASE)
    for width in (16, 16, 31):
        assert counting.reserve(16, width, group='g', group_expr=Expr('g')) is None
    assert counting.counts == {'g': 3}
    assert counting.widths == {'g': 31}
    assert counting.width_hist == {'g': {16: 2, 31: 1}}
    assert counting.pinned_words() == {}  # nothing was placed, so nothing is pinned


def test_placing_pass_hands_out_slots_and_pins_the_word() -> None:
    placing = BlockPool(W, POOL_BASE, counts={'g': 3}, widths={'g': 16})
    slot_bits = 16 * OP_BITS
    word = Expr('g')
    assert [placing.reserve(16, 16, group='g', group_expr=word) for _ in range(3)] == [
        (POOL_BASE + i * slot_bits, True, 0) for i in range(3)
    ]
    assert placing.allocated == 3 and placing.declined == 0
    assert placing.pinned_words() == {word: POOL_BASE}


def test_more_tables_than_counted_decline_and_break_the_group() -> None:
    placing = BlockPool(W, POOL_BASE, counts={'g': 3}, widths={'g': 16})
    for _ in range(4):  # 3 counted -> 4 slots; the 5th has nowhere to go
        placing.reserve(16, 16, group='g', group_expr=Expr('g'))
    assert placing.reserve(16, 16, group='g', group_expr=Expr('g')) is None
    assert placing.declined_overflow == 1 and placing.broken_groups == {'g'}
    assert placing.pinned_words() == {}  # a broken group is never pinned...
    placing.pin_broken = True
    assert list(placing.pinned_words().values()) == [POOL_BASE]  # ...unless the caller opts in


def test_a_table_wider_than_its_slot_declines() -> None:
    placing = BlockPool(W, POOL_BASE, counts={'g': 2}, widths={'g': 16})
    assert placing.reserve(16, 40, group='g', group_expr=Expr('g')) is None
    assert placing.declined_too_wide == 1 and 'g' in placing.broken_groups


def test_an_uncounted_group_and_an_ungrouped_table_decline() -> None:
    placing = BlockPool(W, POOL_BASE, counts={'g': 2}, widths={'g': 16})
    assert placing.reserve(16, 16, group='other', group_expr=Expr('other')) is None
    assert placing.declined_no_block == 1
    assert placing.reserve(16, 16) is None
    assert placing.ungrouped == 1


def test_the_runtime_words_are_never_relocated() -> None:
    # `stl.IO` is a LABEL, declared by stl.startup before any code dispatches through it, so the
    # guard sees it resolved; a program variable, declared after the code, is not resolvable yet
    pool = TablePool(W, POOL_BASE)
    data = PreprocessorData(W, {}, DEFAULT_MAX_MACRO_RECURSION_DEPTH, table_pool=pool)
    data.labels['stl.IO'] = 64
    assert not data.begin_relocation(16, 16, '', '(stl.IO + 32)', _sum('stl.IO', 32))
    assert pool.reserved_words == 1 and pool.allocated == 0
    assert data.begin_relocation(16, 16, '', '(x + 32)', _sum('x', 32))
    assert pool.reserved_words == 1 and pool.allocated == 1


def test_sparse_indices_use_the_cheapest_slots_of_a_spread_block() -> None:
    placing = BlockPool(W, POOL_BASE, counts={'g': 300}, widths={'g': 16}, spread=2, spread_min_count=256)
    slot_bits = 16 * OP_BITS
    addresses = [placing.reserve(16, 16, group='g', group_expr=Expr('g'))[0] for _ in range(4)]  # type: ignore[index]
    assert addresses == [POOL_BASE + i * slot_bits for i in (0, 1, 2, 4)]


def test_width_buckets_give_each_width_its_own_sub_block() -> None:
    placing = BlockPool(
        W,
        POOL_BASE,
        counts={'g': 4},
        widths={'g': 64},
        width_hist={'g': {16: 3, 64: 1}},
        width_buckets=True,
        max_slot_ops=512,
    )
    layout, block_bits = placing._bucket_layout('g')
    assert layout == {64: (0, 1, 1), 16: (64 * OP_BITS, 4, 3)}
    assert block_bits == 2 * 64 * OP_BITS
    assert placing.reserve(16, 40, group='g', group_expr=Expr('g')) == (POOL_BASE, True, 0)
    assert placing.reserve(16, 16, group='g', group_expr=Expr('g')) == (POOL_BASE + 64 * OP_BITS, True, 0)
    assert placing.reserve(16, 200, group='g', group_expr=Expr('g')) is None  # a width never counted
    assert placing.declined_too_wide == 1


def test_eviction_drops_the_least_valuable_group_when_the_pool_is_short() -> None:
    kwargs = dict(counts={'a': 1, 'b': 3}, widths={'a': 16, 'b': 16}, span_bits=2 * 16 * OP_BITS)
    greedy = BlockPool(W, POOL_BASE, **kwargs)  # type: ignore[arg-type]
    assert greedy.evicted_low_value == 0 and greedy.broken_groups == {'b'}
    evicting = BlockPool(W, POOL_BASE, evict_by_value=True, **kwargs)  # type: ignore[arg-type]
    assert evicting.evicted_low_value == 1 and evicting.broken_groups == {'b'}
    assert set(evicting.groups) == {'a'}


def test_canonical_alias_merges_groups_that_name_one_word() -> None:
    counting = BlockPool(W, POOL_BASE)
    counting.reserve(16, 16, group='(x + 32)', group_expr=_sum('x', 32))
    counting.reserve(16, 16, group='(x + w)', group_expr=_sum('x', 'w'))
    counting.reserve(16, 16, group='y', group_expr=Expr('y'))
    assert counting.canonical_alias({'x': 4096, 'w': 32, 'y': 8192}) == {'(x + w)': '(x + 32)'}
    merged = BlockPool(W, POOL_BASE, alias={'(x + w)': '(x + 32)'})
    merged.reserve(16, 16, group='(x + w)', group_expr=_sum('x', 'w'))
    assert merged.counts == {'(x + 32)': 1}


# --- pin protection: BlockPool(heat=...) ---


def _place(pool: BlockPool, sites: List[str], width: int = 16) -> Dict[str, int]:
    """{site path: slot index} for tables of `width` ops reserved in this order, all in group 'g'"""
    base = pool.groups['g'][0]
    out = {}
    for site in sites:
        reserved = pool.reserve(16, width, group='g', group_expr=Expr('g'), labels_prefix=site)
        assert reserved is not None
        out[site] = (reserved[0] - base) // (16 * OP_BITS)
    return out


def test_heat_key_strips_the_call_site_coordinates() -> None:
    assert heat_key('f13:l2210:sim.pass(3)---s2:l40:rep3:hex.exact_xor(5)') == 'sim.pass(3)---rep3:hex.exact_xor(5)'
    assert heat_key('((f13:l2210:sim.pass(3)---hp + 128) + 32)') == '((sim.pass(3)---hp + 128) + 32)'
    assert heat_key('(hex.tables.res + 32)') == '(hex.tables.res + 32)'


def test_hot_sites_get_the_cheapest_indices_whatever_the_encounter_order() -> None:
    sites = [f'f1:l{line}:cold' for line in range(5)] + ['f1:l9:hot']  # the hot table comes LAST
    plain = BlockPool(W, POOL_BASE, counts={'g': 6}, widths={'g': 16})
    assert _place(plain, sites)['f1:l9:hot'] == 5  # encounter order: popcount 2
    hot = BlockPool(W, POOL_BASE, counts={'g': 6}, widths={'g': 16}, heat={'g': [('hot', 0, 16)]})
    placed = _place(hot, sites)
    assert placed['f1:l9:hot'] == 0  # its rank's index: popcount 0
    assert sorted(placed.values()) == [0, 1, 2, 3, 4, 5]  # the others take the next cheapest, once each
    assert hot.hot_sites_matched == 1


def test_hot_sites_rank_within_their_width_bucket() -> None:
    kwargs = dict(
        counts={'g': 4}, widths={'g': 64}, width_hist={'g': {16: 2, 64: 2}}, width_buckets=True, max_slot_ops=512
    )
    pool = BlockPool(W, POOL_BASE, heat={'g': [('b', 0, 16), ('a', 0, 64)]}, **kwargs)  # type: ignore[arg-type]
    wide, narrow = 64 * OP_BITS, 16 * OP_BITS
    # each width holds its 2 tables plus its 1 reserved rank: 4 slots; the 64-op bucket comes first
    narrow_bucket = 4 * wide
    got = [
        pool.reserve(16, width, group='g', group_expr=Expr('g'), labels_prefix=site)
        for site, width in (('f1:l1:x', 64), ('f1:l2:a', 64), ('f1:l3:y', 16), ('f1:l4:b', 16))
    ]
    assert [address - POOL_BASE for address, _, _ in got] == [  # type: ignore[misc]
        wide,  # x: the rank after `a`'s
        0,  # a: the cheapest 64-op slot
        narrow_bucket + narrow,  # y: the rank after `b`'s
        narrow_bucket,  # b: the cheapest 16-op slot
    ]


def test_three_listed_sites_take_three_distinct_cheapest_indices() -> None:
    heat = {'g': [('a', 0, 16), ('b', 0, 16), ('c', 0, 16)]}
    pool = BlockPool(W, POOL_BASE, counts={'g': 4}, widths={'g': 16}, heat=heat)
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
    pool = BlockPool(
        W,
        POOL_BASE,
        counts={'g': 3},
        widths={'g': 64},
        width_hist={'g': {64: 2, 16: 1}},
        width_buckets=True,
        max_slot_ops=512,
        heat={'g': [('a', 0, 16), ('c', 0, 64)]},
    )
    got = {
        site: pool.reserve(16, width, group='g', group_expr=Expr('g'), labels_prefix=f'f1:l{line}:{site}')
        for line, (site, width) in enumerate((('a', 64), ('c', 64), ('b', 16)))
    }
    assert got['a'] == (POOL_BASE + 64 * OP_BITS, True, 0)  # unlisted in its new width: after `c`'s rank
    assert got['c'] == (POOL_BASE, True, 0)
    assert pool.hot_sites_width_changed == 1 and pool.hot_sites_matched == 1


def test_occurrences_count_the_tables_that_decline() -> None:
    # the counting pass saw both tables, so the heat list numbers the 16-op one as occurrence 1
    pool = BlockPool(W, POOL_BASE, counts={'g': 2}, widths={'g': 64}, heat={'g': [('site', 1, 16)]})
    assert pool.reserve(16, 64, group='g', group_expr=Expr('g'), labels_prefix='f1:l1:site') is None  # too wide
    assert pool.reserve(16, 16, group='g', group_expr=Expr('g'), labels_prefix='f1:l2:site') == (POOL_BASE, True, 0)
    assert pool.hot_sites_matched == 1


def test_a_listed_site_the_program_lost_pushes_no_table_out() -> None:
    sites = [f'f1:l{line}:t' for line in range(4)]
    for heat in (None, {'g': [('gone', 0, 16)]}):
        pool = BlockPool(W, POOL_BASE, counts={'g': 4}, widths={'g': 16}, heat=heat)
        assert sorted(_place(pool, sites).values()) == ([0, 1, 2, 3] if heat is None else [1, 2, 3, 4])
        assert pool.declined == 0 and not pool.broken_groups


def test_a_hot_width_grows_only_when_its_ranks_do_not_fit() -> None:
    # 300 tables with spread 2 own 1,024 slots, so 300 reserved ranks (600 in all) fit and the block
    # keeps its size; sizing the next power of two of 600 before the spread would double it
    kwargs = dict(counts={'g': 300}, widths={'g': 16}, spread=2, spread_min_count=256)
    plain = BlockPool(W, POOL_BASE, **kwargs)  # type: ignore[arg-type]
    heat = {'g': [(f's{i}', 0, 16) for i in range(300)]}
    hot = BlockPool(W, POOL_BASE, heat=heat, **kwargs)  # type: ignore[arg-type]
    assert hot._block_bits('g') == plain._block_bits('g')
    # 4 tables own 4 slots: 3 more ranks do not fit, so the block doubles
    small = BlockPool(W, POOL_BASE, counts={'g': 4}, widths={'g': 16})
    grown = BlockPool(W, POOL_BASE, counts={'g': 4}, widths={'g': 16}, heat={'g': [(s, 0, 16) for s in 'abc']})
    assert grown._block_bits('g') == 2 * small._block_bits('g')


def test_a_hot_bucket_grows_only_when_its_ranks_do_not_fit() -> None:
    # the width-bucket layout: the same rule, width by width
    kwargs = dict(
        counts={'g': 300},
        widths={'g': 16},
        width_hist={'g': {16: 300}},
        width_buckets=True,
        max_slot_ops=512,
        spread=2,
        spread_min_count=256,
    )
    plain = BlockPool(W, POOL_BASE, **kwargs)  # type: ignore[arg-type]
    heat = {'g': [(f's{i}', 0, 16) for i in range(300)]}
    hot = BlockPool(W, POOL_BASE, heat=heat, **kwargs)  # type: ignore[arg-type]
    assert hot._block_bits('g') == plain._block_bits('g')


def test_a_bucket_pool_group_without_a_histogram_keeps_room_for_its_ranks() -> None:
    # width buckets on, but no width histogram for the group: its layout falls back to the uniform
    # shape, which must still find the ranks the bucket mode keyed by width
    pool = BlockPool(W, POOL_BASE, counts={'g': 4}, widths={'g': 16}, width_buckets=True, heat={'g': [('gone', 0, 16)]})
    sites = [f'f1:l{line}:t' for line in range(4)]
    assert sorted(_place(pool, sites).values()) == [1, 2, 3, 4]
    assert pool.declined == 0 and not pool.broken_groups


def test_eviction_counts_the_hole_hot_first_placement_leaves() -> None:
    # 16 slots; `h` first leaves a hole in front of `big` (8 slots, aligned to 8), so the plain
    # sum of 14 slots fits and the layout does not. Every group lost must be one evicted by value.
    kwargs = dict(
        counts={'h': 1, 'big': 8, 'mid': 4, 'low': 1},
        widths={'h': 16, 'big': 16, 'mid': 16, 'low': 16},
        span_bits=16 * 16 * OP_BITS,
        evict_by_value=True,
    )
    assert set(BlockPool(W, POOL_BASE, **kwargs).groups) == {'h', 'big', 'mid', 'low'}  # type: ignore[arg-type]
    hot = BlockPool(W, POOL_BASE, heat={'h': [('x', 0, 16)]}, **kwargs)  # type: ignore[arg-type]
    assert 'h' in hot.groups
    assert len(hot.broken_groups) == hot.evicted_low_value == 1  # none lost to placement order


def test_eviction_counts_the_holes_among_hot_blocks() -> None:
    # heat order h1 (1 slot), then h2 (4 slots, aligned to 4): 3 slots stay empty, so 9 one-slot
    # groups need 17 of 16 slots and one must be evicted by value, not lost to placement order
    counts = {'h1': 1, 'h2': 4, **{f'n{i}': 1 for i in range(9)}}
    kwargs = dict(counts=counts, widths={g: 16 for g in counts}, span_bits=16 * 16 * OP_BITS, evict_by_value=True)
    pool = BlockPool(W, POOL_BASE, heat={'h1': [], 'h2': []}, **kwargs)  # type: ignore[arg-type]
    assert len(pool.broken_groups) == pool.evicted_low_value == 1


def test_eviction_stops_once_the_biggest_block_is_gone() -> None:
    # `big` (8 slots) is evicted first; that also removes the hole in front of it, so the 9 one-slot
    # groups then fit: exactly one eviction
    counts = {'h': 1, 'big': 8, **{f's{i}': 1 for i in range(9)}}
    kwargs = dict(counts=counts, widths={g: 16 for g in counts}, span_bits=16 * 16 * OP_BITS, evict_by_value=True)
    pool = BlockPool(W, POOL_BASE, heat={'h': []}, **kwargs)  # type: ignore[arg-type]
    assert pool.evicted_low_value == 1 and 'big' not in pool.groups and len(pool.groups) == 10


def test_eviction_without_heat_keeps_the_plain_sum() -> None:
    # no list must evict exactly as the plain sum does, also where the pool base is not aligned to the
    # biggest block: the plain sum (16 slots) fits, nothing is evicted, `big` takes its aligned
    # slot and `q`, `r` are lost to placement order
    base = POOL_BASE + 16 * OP_BITS
    counts = {'big': 8, 'q': 3, 'r': 4}
    pool = BlockPool(
        W, base, counts=counts, widths={g: 16 for g in counts}, span_bits=16 * 16 * OP_BITS, evict_by_value=True
    )
    assert pool.evicted_low_value == 0 and set(pool.groups) == {'big'}


def test_hot_groups_are_placed_first_from_the_pool_base() -> None:
    kwargs = dict(counts={'big': 8, 'small': 1}, widths={'big': 16, 'small': 16})
    plain = BlockPool(W, POOL_BASE, **kwargs)  # type: ignore[arg-type]
    assert plain.groups['big'][0] == POOL_BASE  # biggest first
    hot = BlockPool(W, POOL_BASE, heat={'small': [('h', 0, 16)]}, **kwargs)  # type: ignore[arg-type]
    assert hot.groups['small'][0] == POOL_BASE and hot.groups['big'][0] > POOL_BASE


def test_sites_that_share_a_key_are_told_apart_by_occurrence() -> None:
    # two calls of one macro on different lines strip to one key; the heat list names the second
    pool = BlockPool(W, POOL_BASE, counts={'g': 2}, widths={'g': 16}, heat={'g': [('site', 1, 16)]})
    assert _place(pool, ['f1:l5:site', 'f1:l8:site']) == {'f1:l5:site': 1, 'f1:l8:site': 0}


def test_a_hot_group_is_pinned_even_when_it_breaks() -> None:
    kwargs = dict(counts={'g': 2}, widths={'g': 16})
    for heat, pinned in ((None, False), ({'g': [('h', 0, 16)]}, True)):
        pool = BlockPool(W, POOL_BASE, heat=heat, **kwargs)  # type: ignore[arg-type]
        # 2 tables counted: with no list the third overflows; with one (2 + 1 rank -> 4 slots,
        # rank 0 kept for `h`) the fourth does
        for line in range(5):
            pool.reserve(16, 16, group='g', group_expr=Expr('g'), labels_prefix=f'f1:l{line}:t')
        assert pool.broken_groups == {'g'}
        assert bool(pool.pinned_words()) == pinned


def test_a_hot_group_that_gets_no_block_raises() -> None:
    kwargs = dict(counts={'a': 1, 'g': 2}, widths={'a': 16, 'g': 16}, span_bits=16 * OP_BITS)
    assert 'g' in BlockPool(W, POOL_BASE, **kwargs).broken_groups  # type: ignore[arg-type]  # silent without heat
    with pytest.raises(FlipJumpPreprocessorException, match='pin protection'):
        BlockPool(W, POOL_BASE, heat={'g': [('h', 0, 16)]}, **kwargs)  # type: ignore[arg-type]


def test_eviction_never_drops_a_hot_group() -> None:
    kwargs = dict(counts={'a': 1, 'b': 3}, widths={'a': 16, 'b': 16}, span_bits=4 * 16 * OP_BITS, evict_by_value=True)
    assert set(BlockPool(W, POOL_BASE, **kwargs).groups) == {'a'}  # type: ignore[arg-type]  # `b` is worth least
    hot = BlockPool(W, POOL_BASE, heat={'b': [('h', 0, 16)]}, **kwargs)  # type: ignore[arg-type]
    assert set(hot.groups) == {'b'} and hot.evicted_low_value == 1


def test_heat_names_are_matched_without_coordinates_and_reported() -> None:
    heat = {'((x + 64) + 32)': [('h', 0, 16)], '(gone + 32)': [('h', 0, 16)], '(twice + 32)': [('h', 0, 16)]}
    counts = {'((f2:l7:x + 64) + 32)': 1, '(f1:l1:twice + 32)': 1, '(f1:l2:twice + 32)': 1}
    pool = BlockPool(W, POOL_BASE, counts=counts, widths={g: 16 for g in counts}, heat=heat)
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
        BlockPool(W, POOL_BASE, counts={'g': 2}, widths={'g': 16}, heat={'g': [('h', 0, 16), ('h', 0, 16)]})


def test_no_heat_and_an_empty_heat_list_change_nothing() -> None:
    kwargs = dict(counts={'g': 300, 'k': 5}, widths={'g': 16, 'k': 16}, spread=2, spread_min_count=256)
    sites = [(group, f'f1:l{i}:t') for i in range(40) for group in ('g', 'k') if group == 'g' or i < 5]
    results = []
    for heat in (None, {}, {'(absent + 32)': [('h', 0, 16)]}):
        pool = BlockPool(W, POOL_BASE, heat=heat, **kwargs)  # type: ignore[arg-type]
        results.append([pool.reserve(16, 16, group=g, group_expr=Expr(g), labels_prefix=site) for g, site in sites])
    assert results[0] == results[1] == results[2]


class _SiteRecorder(BlockPool):
    """a counting pool that also records each table's group and site path, in encounter order"""

    def __init__(self) -> None:
        super().__init__(W, POOL_BASE)
        self.sites: List[Tuple[str, str]] = []

    def reserve(
        self,
        ops_alignment: int,
        table_ops: int,
        group: Optional[str] = None,
        group_expr: Optional[Expr] = None,
        labels_prefix: str = '',
    ) -> Optional[Tuple[int, bool, int]]:
        if group is not None:
            self.sites.append((group, labels_prefix))
        return super().reserve(ops_alignment, table_ops, group, group_expr, labels_prefix)


def test_a_heat_ordered_program_is_the_same_program_in_fewer_ops(tmp_path: Path) -> None:
    counting = _SiteRecorder()
    build_and_run(XOR_PROGRAM, tmp_path, counting, name='counting')
    reference, plain_ops, _ = build_and_run(
        XOR_PROGRAM, tmp_path, BlockPool(W, POOL_BASE, counts=counting.counts, widths=counting.widths), name='plain'
    )
    # the busiest group's LAST table, which encounter order hands its dearest index
    group = max(counting.counts, key=lambda g: (counting.counts[g], g))
    last_site = heat_key([site for g, site in counting.sites if g == group][-1])
    assert last_site.endswith('hex.exact_xor(5)')  # a real table's path, not an empty one
    occurrence = sum(1 for g, site in counting.sites if g == group and heat_key(site) == last_site) - 1
    placing = BlockPool(
        W,
        POOL_BASE,
        counts=counting.counts,
        widths=counting.widths,
        heat={heat_key(group): [(last_site, occurrence, 16)]},
    )
    output, ops, _ = build_and_run(XOR_PROGRAM, tmp_path, placing, name='heat')
    assert output == reference
    assert placing.hot_sites_matched == 1 and placing.pinned_words()
    assert ops < plain_ops


# --- end to end: a relocated or blocked program is the same program, in fewer ops ---


@pytest.mark.parametrize('memory_width', [32, 64])
def test_relocated_tables_leave_the_output_unchanged(tmp_path: Path, memory_width: int) -> None:
    reference, reference_ops, inline_fjm = build_and_run(
        XOR_PROGRAM, tmp_path, None, name='inline', memory_width=memory_width
    )
    assert reference and not pool_segments(inline_fjm)
    pool = TablePool(memory_width, POOL_BASE, run_ops=256)
    output, ops, fjm_path = build_and_run(XOR_PROGRAM, tmp_path, pool, memory_width=memory_width)
    assert output == reference
    assert pool.allocated > 0 and pool_segments(fjm_path, memory_width)
    assert ops < reference_ops


def test_a_pool_with_no_capacity_is_inert(tmp_path: Path) -> None:
    reference, reference_ops, _ = build_and_run(XOR_PROGRAM, tmp_path, None, name='inline')
    pool = TablePool(W, POOL_BASE, capacity=0)
    output, ops, fjm_path = build_and_run(XOR_PROGRAM, tmp_path, pool)
    assert (output, ops) == (reference, reference_ops)
    assert pool.allocated == 0 and not pool_segments(fjm_path)


@pytest.mark.parametrize('blocking', [False, True], ids=['TablePool', 'BlockPool'])
def test_an_input_table_is_never_split(tmp_path: Path, blocking: bool) -> None:
    reference, _, _ = build_and_run(INPUT_PROGRAM, tmp_path, None, name='inline', fixed_input=b'A')
    assert reference == b'0x41'
    pool: TablePool
    if blocking:
        counting = BlockPool(W, POOL_BASE)
        build_and_run(INPUT_PROGRAM, tmp_path, counting, name='counting', fixed_input=b'A')
        pool = BlockPool(W, POOL_BASE, counts=counting.counts, widths=counting.widths)
    else:
        pool = TablePool(W, POOL_BASE)
    output, _, _ = build_and_run(INPUT_PROGRAM, tmp_path, pool, fixed_input=b'A')
    assert output == reference


def test_a_pinned_word_read_through_a_pointer_needs_the_callers_exclusion(tmp_path: Path) -> None:
    reference, _, _ = build_and_run(POINTER_PROGRAM, tmp_path, None, name='inline')
    assert reference == b'B'
    # relocation alone is fine: the word still holds its bare value
    output, _, _ = build_and_run(POINTER_PROGRAM, tmp_path, TablePool(W, POOL_BASE), name='relocated')
    assert output == reference
    counting = BlockPool(W, POOL_BASE)
    build_and_run(POINTER_PROGRAM, tmp_path, counting, name='counting')
    assert counting.reads_words_raw
    # without a declaration of the cells pointers reach, no word is pinned: the same program, slower
    unpinned_pool = BlockPool(W, POOL_BASE, counts=counting.counts, widths=counting.widths)
    output, _, _ = build_and_run(POINTER_PROGRAM, tmp_path, unpinned_pool, name='unpinned')
    assert output == reference and list(unpinned_pool.unpins) == ['the pointer runtime']
    # with the pointed cell excluded from pinning the blocked program is the same program; the
    # veto sees the bit address of the cell's JUMP word, `d + w`
    blocked_pool = BlockPool(W, POOL_BASE, counts=counting.counts, widths=counting.widths)
    exclude: Callable[[int, Dict[str, int]], bool] = lambda address, labels: address == labels['d'] + W  # noqa: E731
    blocked_pool.pin_exclude = exclude
    output, _, _ = build_and_run(POINTER_PROGRAM, tmp_path, blocked_pool, name='excluded')
    assert output == reference
    assert blocked_pool.pinned_words()  # the other armed words are still pinned


def test_an_undeclared_table_stays_inline(tmp_path: Path) -> None:
    reference, _, _ = build_and_run(FALL_THROUGH_PROGRAM, tmp_path, None, name='inline')
    pool = TablePool(W, POOL_BASE)
    output, _, _ = build_and_run(FALL_THROUGH_PROGRAM, tmp_path, pool)
    assert output == reference and pool.allocated == 0


def test_a_program_that_grows_into_the_pool_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FlipJumpException):
        build_and_run(XOR_PROGRAM, tmp_path, TablePool(W, 1 << 12))


@pytest.mark.parametrize('source', [XOR_PROGRAM, CMP_PROGRAM], ids=['hex.xor', 'hex.cmp/shift/mul'])
def test_blocked_tables_leave_the_output_unchanged_and_cost_fewer_ops(source: str, tmp_path: Path) -> None:
    reference, reference_ops, _ = build_and_run(source, tmp_path, None, name='inline')
    output, ops, placing = blocked(source, tmp_path)
    assert output == reference
    assert placing.allocated > 0 and placing.pinned_words()
    assert ops < reference_ops


def test_the_shift_tables_are_tables_a_pool_can_see(tmp_path: Path) -> None:
    # shl_bit_once / shr_bit_once dispatch like exact_xor, and declare their tables relocatable
    reference, reference_ops, _ = build_and_run(SHIFT_PROGRAM, tmp_path, None, name='inline')
    assert reference == b'fdb97530eca864207f6e5d4c3b2a1908'
    counting = BlockPool(W, POOL_BASE)
    build_and_run(SHIFT_PROGRAM, tmp_path, counting, name='counting')
    assert sum(counting.counts.values()) == 32  # shl_bit 16 + shr_bit 16, one table per call
    placing = BlockPool(W, POOL_BASE, counts=counting.counts, widths=counting.widths)
    output, ops, _ = build_and_run(SHIFT_PROGRAM, tmp_path, placing, name='placing')
    assert output == reference
    assert placing.allocated == 32 and placing.pinned_words()
    assert ops < reference_ops


def test_a_blocked_program_with_declined_tables_is_still_correct(tmp_path: Path) -> None:
    # a tiny span forces declines: the broken groups are un-pinned, and their tables stay inline
    reference, _, _ = build_and_run(CMP_PROGRAM, tmp_path, None, name='inline')
    output, _, placing = blocked(CMP_PROGRAM, tmp_path, span_bits=1 << 16)
    assert output == reference
    assert placing.declined > 0 and placing.broken_groups
    pinned = set(placing.pinned_words())
    for group, (_, expr) in placing.groups.items():
        assert (expr in pinned) == (expr is not None and group not in placing.broken_groups)


# --- the census: a placing pass records what it met, so the next build needs no counting pass ---

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

CENSUS_PROGRAMS = {'hex.xor': XOR_PROGRAM, 'hex.cmp/shift/mul': CMP_PROGRAM, 'aliased words': ALIAS_PROGRAM}
STALE_KINDS = ('fresh', 'too_few', 'too_narrow', 'missing_groups', 'too_many', 'other_program')


@pytest.fixture(scope='module')
def counted_programs(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Tuple[bytes, BlockPool]]:
    """{program: (its inline output, the counting pool that counted it)}, built once per module"""
    tmp_path = tmp_path_factory.mktemp('counted')
    counted = {}
    for index, source in enumerate(CENSUS_PROGRAMS.values()):
        reference, _, _ = build_and_run(source, tmp_path, None, name=f'inline{index}')
        counting = BlockPool(W, POOL_BASE)
        build_and_run(source, tmp_path, counting, name=f'counting{index}')
        counted[source] = (reference, counting)
    return counted


def _stale(kind: str, census: BlockCensus, other: BlockCensus) -> Dict[str, Any]:
    """placing-pool arguments made from `census` the way a cached census goes stale"""
    kwargs = census.pool_kwargs()
    if kind == 'too_few':  # every group lost tables since: the extra ones overflow
        kwargs['counts'] = {group: max(1, count // 2) for group, count in census.counts.items()}
    elif kind == 'too_narrow':  # every table grew: none fits its slot
        kwargs['widths'] = {group: 1 for group in census.widths}
        kwargs['width_hist'] = {group: {1: count} for group, count in census.counts.items()}
    elif kind == 'missing_groups':  # half the groups are new since
        kept = sorted(census.counts)[::2]
        kwargs = BlockCensus(
            {g: census.counts[g] for g in kept},
            {g: census.widths[g] for g in kept},
            {g: census.width_hist[g] for g in kept},
            {g: c for g, c in census.alias.items() if c in kept},
        ).pool_kwargs()
    elif kind == 'too_many':  # every group shrank since: room to spare, nothing declines
        kwargs['counts'] = {group: 2 * count for group, count in census.counts.items()}
    elif kind == 'other_program':  # a cache of a different program altogether
        kwargs = other.pool_kwargs()
    else:
        assert kind == 'fresh'
    return kwargs


def assert_recorded_as_counted(placing: BlockPool, counting: BlockPool) -> None:
    """the placing pass's recording is what the counting pass reported: every field a census
    carries (raw and alias-merged), the group expressions, and the tables that had no group"""
    recording = placing.recording
    assert recording is not placing and recording.counting
    assert recording.counts == counting.counts
    assert recording.widths == counting.widths
    assert recording.width_hist == counting.width_hist
    assert {g: str(e) for g, e in recording.exprs.items()} == {g: str(e) for g, e in counting.exprs.items()}
    assert recording.ungrouped == counting.ungrouped
    assert placing.census() == counting.census()
    assert placing.census(merge_aliases=True) == counting.census(merge_aliases=True)


@pytest.mark.parametrize('pin_broken', [False, True], ids=['unpin-broken', 'pin-broken'])
@pytest.mark.parametrize('kind', STALE_KINDS)
@pytest.mark.parametrize('program', list(CENSUS_PROGRAMS), ids=list(CENSUS_PROGRAMS))
def test_a_placing_pass_records_what_a_counting_pass_reports_and_stays_correct(
    tmp_path: Path,
    counted_programs: Dict[str, Tuple[bytes, BlockPool]],
    program: str,
    kind: str,
    pin_broken: bool,
) -> None:
    source = CENSUS_PROGRAMS[program]
    reference, counting = counted_programs[source]
    other_source = CMP_PROGRAM if source is XOR_PROGRAM else XOR_PROGRAM
    other = counted_programs[other_source][1].census(merge_aliases=True)
    given = _stale(kind, counting.census(merge_aliases=True), other)
    placing = BlockPool(W, POOL_BASE, pin_broken=pin_broken, **given)
    output, _, _ = build_and_run(source, tmp_path, placing)

    assert output == reference  # stale counts cost ops, never correctness
    assert_recorded_as_counted(placing, counting)
    report = placing.decline_report()
    # the per-cause report against the counters kept apart from it, decline by decline
    aggregates = (placing.declined_no_block, placing.declined_too_wide, placing.declined_overflow)
    assert sum(report.values()) == placing.declined == sum(aggregates)
    assert (
        report['no_block'] + report['new_group'],
        report['too_wide'] + report['width_overflow'],
        report['overflow'],
    ) == (aggregates)
    assert placing.stale_declines == placing.declined  # nothing else declines in these programs
    expected_cause = {'too_few': 'overflow', 'too_narrow': 'width_overflow', 'missing_groups': 'new_group'}.get(kind)
    if kind in ('fresh', 'too_many'):  # the program's own counts, or more room than it needs
        assert placing.declined == 0 and not placing.broken_groups
    elif expected_cause is None:  # another program's: shares some groups (`a`, `b`), not their counts
        assert placing.stale_declines > 0
    else:
        assert report[expected_cause] > 0
    # the census of what it placed is the program's own whatever it was given, so comparing the two
    # tells a build whether its cache was stale
    fresh = placing.census(merge_aliases=True)
    assert fresh == counting.census(merge_aliases=True)
    assert (fresh.pool_kwargs() == given) == (kind == 'fresh')


@pytest.mark.parametrize('program', list(CENSUS_PROGRAMS), ids=list(CENSUS_PROGRAMS))
def test_a_cached_census_rebuilds_the_two_pass_binary(
    tmp_path: Path, counted_programs: Dict[str, Tuple[bytes, BlockPool]], program: str
) -> None:
    """build 1 places from a STALE cache (a different program's) and caches its census; build 2
    places from that census and is the two-pass build's binary, byte for byte"""
    source = CENSUS_PROGRAMS[program]
    reference, counting = counted_programs[source]
    _, two_pass_ops, two_pass_fjm = build_and_run(
        source, tmp_path, BlockPool(W, POOL_BASE, **counting.census(merge_aliases=True).pool_kwargs()), name='two'
    )
    other_source = CMP_PROGRAM if source is XOR_PROGRAM else XOR_PROGRAM
    stale = counted_programs[other_source][1].census(merge_aliases=True)
    first = BlockPool(W, POOL_BASE, **json_round_trip(stale).pool_kwargs())
    output, _, _ = build_and_run(source, tmp_path, first, name='first')
    assert output == reference and first.stale_declines > 0
    cached = json_round_trip(first.census(merge_aliases=True))
    assert cached != stale

    second = BlockPool(W, POOL_BASE, **cached.pool_kwargs())
    output, ops, fjm_path = build_and_run(source, tmp_path, second, name='second')
    assert output == reference and ops == two_pass_ops
    assert fjm_path.read_bytes() == two_pass_fjm.read_bytes()
    assert second.declined == 0 and second.stale_declines == 0
    assert second.census(merge_aliases=True) == cached  # a fixed point: the next build changes nothing


def json_round_trip(census: BlockCensus) -> BlockCensus:
    """through the JSON a build caches it as"""
    return BlockCensus.from_json(json.loads(json.dumps(census.to_json())))


def test_the_census_survives_json_and_rebuilds_its_pool() -> None:
    census = BlockCensus(
        {'g': 3, 'h': 1}, {'g': 31, 'h': 16}, {'g': {16: 2, 31: 1}, 'h': {16: 1}}, {'h2': 'h'}, unblocked=['h']
    )
    assert json_round_trip(census) == census
    pool = BlockPool(W, POOL_BASE, **json_round_trip(census).pool_kwargs())
    assert pool.counts == census.counts and pool.width_hist == census.width_hist and pool.alias == census.alias
    assert set(pool.groups) == {'g'} and 'h' in pool.broken_groups  # an unblocked group gets no block
    assert BlockCensus.from_json({'counts': {}, 'widths': {}, 'width_hist': {}}).unblocked == []  # an older cache


def test_merging_aliases_adds_counts_and_histograms_and_keeps_the_widest() -> None:
    merged = merge_aliased_groups(
        {'x': 2, 'y': 3, 'z': 1},
        {'x': 16, 'y': 31, 'z': 16},
        {'x': {16: 2}, 'y': {16: 2, 31: 1}, 'z': {16: 1}},
        {'y': 'x'},
    )
    assert merged == ({'x': 5, 'z': 1}, {'x': 31, 'z': 16}, {'x': {16: 4, 31: 1}, 'z': {16: 1}})


def test_a_census_merges_the_aliases_its_own_labels_imply() -> None:
    placing = BlockPool(W, POOL_BASE, counts={'(x + 32)': 1}, widths={'(x + 32)': 16})
    placing.reserve(16, 16, group='(x + 32)', group_expr=_sum('x', 32))
    placing.reserve(16, 16, group='(x + w)', group_expr=_sum('x', 'w'))  # the same word: 'new_group'
    with pytest.raises(FlipJumpPreprocessorException, match='after assemble'):
        placing.census(merge_aliases=True)  # no labels yet
    assert placing.census() == BlockCensus(
        {'(x + 32)': 1, '(x + w)': 1}, {'(x + 32)': 16, '(x + w)': 16}, {'(x + 32)': {16: 1}, '(x + w)': {16: 1}}, {}
    )
    placing.labels_resolved({'x': 4096, 'w': 32})
    assert placing.census(merge_aliases=True) == BlockCensus(
        {'(x + 32)': 2}, {'(x + 32)': 16}, {'(x + 32)': {16: 2}}, {'(x + w)': '(x + 32)'}
    )


def test_declines_are_told_apart_per_group_by_cause() -> None:
    word = Expr('g')
    # uniform slots: g's tables are counted 16 ops wide, max_slot_ops is 32
    pool = BlockPool(W, POOL_BASE, counts={'g': 2, 'big': 1 << 20}, widths={'g': 16, 'big': 16}, span_bits=1 << 20)
    assert pool.reserve(16, 16, group='g', group_expr=word) is not None
    assert pool.reserve(16, 24, group='g', group_expr=word) is None  # fits a 32-op slot, not g's 16
    assert pool.reserve(16, 40, group='g', group_expr=word) is None  # fits no slot max_slot_ops allows
    assert pool.reserve(16, 16, group='g', group_expr=word) is not None
    assert pool.reserve(16, 16, group='g', group_expr=word) is None  # a third table: g counted 2
    assert pool.reserve(16, 16, group='new', group_expr=Expr('new')) is None
    assert pool.reserve(16, 16, group='big', group_expr=Expr('big')) is None  # counted, no room
    assert pool.declines_by_group == {
        'g': {'width_overflow': 1, 'too_wide': 1, 'overflow': 1},
        'new': {'new_group': 1},
        'big': {'no_block': 1},
    }
    assert pool.decline_report() == {
        'new_group': 1,
        'width_overflow': 1,
        'overflow': 1,
        'no_block': 1,
        'too_wide': 1,
    }
    assert pool.stale_declines == 3  # new_group, width_overflow and overflow
    assert not set(DECLINE_STALE) & set(DECLINE_OTHER)
    # the aggregate counters keep their meaning: no block (new or not), too wide (either way), overflow
    assert (pool.declined_no_block, pool.declined_too_wide, pool.declined_overflow) == (2, 2, 1)
    assert pool.broken_groups == {'g', 'big'}  # `big` broke when it got no block (_preallocate)

    # width buckets: a width the counts lack for the group is stale, one over max_slot_ops is not
    bucketed = BlockPool(
        W, POOL_BASE, counts={'g': 2}, widths={'g': 16}, width_hist={'g': {16: 2}}, width_buckets=True, max_slot_ops=64
    )
    assert bucketed.reserve(16, 40, group='g', group_expr=word) is None
    assert bucketed.reserve(16, 65, group='g', group_expr=word) is None
    assert bucketed.declines_by_group == {'g': {'width_overflow': 1, 'too_wide': 1}}


def test_a_recording_that_misses_a_table_is_caught(
    tmp_path: Path, counted_programs: Dict[str, Tuple[bytes, BlockPool]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE NEGATIVE CONTROL of the census tests: a recorder that drops one table, or keeps one too
    narrow, must fail assert_recorded_as_counted - else that check proves nothing"""
    reference, counting = counted_programs[XOR_PROGRAM]
    real_reserve = BlockPool.reserve

    for mutant in ('drop_last', 'narrow_one'):
        seen = {'calls': 0}

        def mutated(
            self: BlockPool,
            ops_alignment: int,
            table_ops: int,
            group: Optional[str] = None,
            group_expr: Optional[Expr] = None,
            labels_prefix: str = '',
            _mutant: str = mutant,
        ) -> Optional[Tuple[int, bool, int]]:
            if self.counting and group is not None:  # the RECORDING's reserve, not the placing one
                seen['calls'] += 1
                if seen['calls'] == sum(counting.counts.values()) and _mutant == 'drop_last':
                    return None
                if seen['calls'] == 1 and _mutant == 'narrow_one':
                    table_ops = ops_alignment = 1
            return real_reserve(self, ops_alignment, table_ops, group, group_expr, labels_prefix)

        monkeypatch.setattr(BlockPool, 'reserve', mutated)
        placing = BlockPool(W, POOL_BASE, **counting.census().pool_kwargs())
        output, _, _ = build_and_run(XOR_PROGRAM, tmp_path, placing, name=mutant)
        monkeypatch.setattr(BlockPool, 'reserve', real_reserve)
        assert output == reference  # the program is fine; only the recording is wrong
        with pytest.raises(AssertionError):
            assert_recorded_as_counted(placing, counting)


# --- one pass: tables wait out of the op stream, and are placed once the expansion is done ---

# table labels (externs of their macros) named by an expression evaluated DURING the expansion -
# one pass has no address for them yet. {CODE} goes after the tables, {DATA} after the variables.
EXPANSION_TIME_TEMPLATE = (
    """
def ext_table x @ end < ext_switch {
    wflip x+w, ext_switch, x
    pad 16
    ext_table.__relocatable__.table ext_switch, end
  end:
    wflip x+w, ext_switch
}
"""
    + relocatable('ext_table', 'switch, end', 'switch', '    ;end\n' * 16)
    + """
def ext_table2 x @ end < ext2_switch {
    wflip x+w, ext2_switch, x
    pad 16
    ext_table2.__relocatable__.table ext2_switch, end
  end:
    wflip x+w, ext2_switch
}
"""
    + relocatable('ext_table2', 'switch, end', 'switch', '    ;end\n' * 16)
    + """
def nothing a {
    ;$ + (a - a)
}
stl.startup_and_init_all
    hex.set x, 5
    ext_table x
    ext_table2 x
{CODE}
    hex.print_as_digit x, 0
    stl.loop
x: hex.hex
{DATA}
"""
)
EXPANSION_TIME_CASES = {
    # the count names ext_switch; the argument names ext2_switch, which is never evaluated here
    'rep': ('    rep(ext_switch - ext_switch + 1, i) nothing ext2_switch', ''),
    'pad': ('    ;pad_end\n    pad ext_switch - ext_switch + 4\n  pad_end:', ''),  # jumped over, never run
    'segment': ('', 'segment ext_switch - ext_switch + (1 << 20)\ny: hex.hex'),
    'reserve': ('', 'reserve ext_switch - ext_switch + 64'),
}

# XOR_PROGRAM with more code after its variables: unreachable, but its tables are expanded
APPENDED_PROGRAM = XOR_PROGRAM + """
    hex.xor 4, a, b
    hex.xor 4, b, a
    stl.loop
"""


def two_pass_build(
    source: str, tmp_path: Path, merge_aliases: bool = False, memory_width: int = W, **kwargs: Any
) -> Tuple[bytes, int, Path, BlockPool, BlockPool]:
    """(output, ops, fjm, counting pool, placing pool) of the two-pass blocking build"""
    counting = BlockPool(memory_width, POOL_BASE, **kwargs)
    build_and_run(source, tmp_path, counting, name='counting', memory_width=memory_width)
    census = counting.census(merge_aliases=merge_aliases).pool_kwargs()
    placing = BlockPool(memory_width, POOL_BASE, **census, **kwargs)
    output, ops, fjm_path = build_and_run(source, tmp_path, placing, name='two_pass', memory_width=memory_width)
    return output, ops, fjm_path, counting, placing


def expand_labels(source: str, tmp_path: Path, pool: Optional[TablePool], name: str = 'p') -> Dict[str, int]:
    """the labels of `source` expanded with the stl and `pool` (macro expansion only)"""
    fj_path = tmp_path / f'{name}.fj'
    fj_path.write_text(source)
    macros = parse_macro_tree(get_file_tuples([str(fj_path)]), W, True)
    _, labels = resolve_macros(W, macros, table_pool=pool, save_debug_labels=False)
    return labels


ONE_PASS_CASES: Dict[str, Tuple[str, Dict[str, Any]]] = {
    'hex.xor': (XOR_PROGRAM, {}),
    'hex.cmp/shift/mul': (CMP_PROGRAM, {}),
    'aliased words': (ALIAS_PROGRAM, {}),
    'shifts': (SHIFT_PROGRAM, {}),
}
ONE_PASS_LAYOUTS: Dict[str, Dict[str, Any]] = {
    'uniform': {},
    'buckets': {'width_buckets': True, 'max_slot_ops': 512},
    'spread': {'spread': 2, 'spread_min_count': 4},
}


@pytest.mark.parametrize('merge_aliases', [False, True], ids=['no-alias', 'merge-aliases'])
@pytest.mark.parametrize('layout', list(ONE_PASS_LAYOUTS))
@pytest.mark.parametrize('case', list(ONE_PASS_CASES))
def test_one_pass_builds_the_two_pass_binary_byte_for_byte(
    tmp_path: Path, case: str, layout: str, merge_aliases: bool
) -> None:
    source, case_kwargs = ONE_PASS_CASES[case]
    kwargs = {**case_kwargs, **ONE_PASS_LAYOUTS[layout]}
    reference, _, _ = build_and_run(source, tmp_path, None, name='inline')
    two_output, two_ops, two_fjm, counting, placing = two_pass_build(source, tmp_path, merge_aliases, **kwargs)
    assert placing.declined == 0  # the precondition for identical binaries

    one = BlockPool(W, POOL_BASE, one_pass=True, merge_aliases=merge_aliases, **kwargs)
    output, ops, fjm_path = build_and_run(source, tmp_path, one, name='one_pass')
    assert output == two_output == reference
    assert fjm_path.read_bytes() == two_fjm.read_bytes() and ops == two_ops
    assert one.deferred == placing.allocated == one.allocated > 0
    assert one.overflowed == one.declined == 0
    assert one.census(merge_aliases=merge_aliases) == counting.census(merge_aliases=merge_aliases)
    assert {g: base for g, (base, _) in one.groups.items()} == {g: base for g, (base, _) in placing.groups.items()}


def test_one_pass_merges_its_census_by_a_given_alias(
    tmp_path: Path, counted_programs: Dict[str, Tuple[bytes, BlockPool]]
) -> None:
    reference, counting = counted_programs[ALIAS_PROGRAM]
    merged = counting.census(merge_aliases=True)
    assert merged.alias  # the program does alias its words
    _, _, two_fjm = build_and_run(ALIAS_PROGRAM, tmp_path, BlockPool(W, POOL_BASE, **merged.pool_kwargs()), name='two')
    one = BlockPool(W, POOL_BASE, one_pass=True, alias=merged.alias)  # sized from its census, merged by the alias
    output, _, fjm_path = build_and_run(ALIAS_PROGRAM, tmp_path, one, name='one')
    assert output == reference and fjm_path.read_bytes() == two_fjm.read_bytes()
    assert one.counts == merged.counts


@pytest.mark.parametrize('memory_width', [32, 64])
def test_one_pass_reads_input_and_runs_at_both_widths(tmp_path: Path, memory_width: int) -> None:
    reference, _, _ = build_and_run(
        INPUT_PROGRAM, tmp_path, None, name='inline', fixed_input=b'A', memory_width=memory_width
    )
    counting = BlockPool(memory_width, POOL_BASE)
    build_and_run(INPUT_PROGRAM, tmp_path, counting, name='counting', fixed_input=b'A', memory_width=memory_width)
    placing = BlockPool(memory_width, POOL_BASE, **counting.census().pool_kwargs())
    _, _, two_fjm = build_and_run(
        INPUT_PROGRAM, tmp_path, placing, name='two', fixed_input=b'A', memory_width=memory_width
    )
    one = BlockPool(memory_width, POOL_BASE, one_pass=True)
    output, _, fjm_path = build_and_run(
        INPUT_PROGRAM, tmp_path, one, name='one', fixed_input=b'A', memory_width=memory_width
    )
    assert output == reference == b'0x41'
    assert fjm_path.read_bytes() == two_fjm.read_bytes() and one.deferred > 0


def test_one_pass_with_pin_protection_builds_the_two_pass_binary(tmp_path: Path) -> None:
    counting = _SiteRecorder()
    build_and_run(XOR_PROGRAM, tmp_path, counting, name='counting')
    group = max(counting.counts, key=lambda g: (counting.counts[g], g))
    last_site = heat_key([site for g, site in counting.sites if g == group][-1])
    occurrence = sum(1 for g, site in counting.sites if g == group and heat_key(site) == last_site) - 1
    heat = {heat_key(group): [(last_site, occurrence, 16)]}
    placing = BlockPool(W, POOL_BASE, counts=counting.counts, widths=counting.widths, heat=heat)
    two_output, _, two_fjm = build_and_run(XOR_PROGRAM, tmp_path, placing, name='two')
    one = BlockPool(W, POOL_BASE, one_pass=True, heat=heat)
    output, _, fjm_path = build_and_run(XOR_PROGRAM, tmp_path, one, name='one')
    assert output == two_output
    assert one.hot_sites_matched == placing.hot_sites_matched == 1
    assert fjm_path.read_bytes() == two_fjm.read_bytes()


def test_a_one_pass_placement_in_another_order_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE NEGATIVE CONTROL of the byte-identity tests: placing the same tables in another order is
    a correct program, and a DIFFERENT binary - which the byte comparison must see"""
    reference, _, two_fjm, _, _ = two_pass_build(XOR_PROGRAM, tmp_path)
    real_iter = DeferredTables.__iter__

    def reversed_order(self: DeferredTables) -> Iterator[Any]:
        return iter(list(real_iter(self))[::-1])

    monkeypatch.setattr(DeferredTables, '__iter__', reversed_order)
    output, _, fjm_path = build_and_run(XOR_PROGRAM, tmp_path, BlockPool(W, POOL_BASE, one_pass=True), name='reversed')
    assert output == reference
    assert fjm_path.read_bytes() != two_fjm.read_bytes()


@pytest.mark.parametrize('buckets', [False, True], ids=['uniform', 'buckets'])
@pytest.mark.parametrize('pin_broken', [False, True], ids=['unpin-broken', 'pin-broken'])
def test_one_pass_keeps_a_table_too_wide_for_any_slot_inline_like_two_pass(
    tmp_path: Path, buckets: bool, pin_broken: bool
) -> None:
    """no counts can give hex.cmp's 31-op table a slot of max_slot_ops=16, so a placing pass
    declines it at its pad; one pass decides it there too, and the binaries stay identical"""
    kwargs: Dict[str, Any] = {'max_slot_ops': 16, 'pin_broken': pin_broken, 'width_buckets': buckets}
    counting_kwargs = {k: v for k, v in kwargs.items() if k != 'pin_broken'}
    counting = BlockPool(W, POOL_BASE, **counting_kwargs)
    build_and_run(CMP_PROGRAM, tmp_path, counting, name='counting')
    placing = BlockPool(W, POOL_BASE, **counting.census().pool_kwargs(), **kwargs)
    two_output, _, two_fjm = build_and_run(CMP_PROGRAM, tmp_path, placing, name='two')
    one = BlockPool(W, POOL_BASE, one_pass=True, **kwargs)
    output, _, fjm_path = build_and_run(CMP_PROGRAM, tmp_path, one, name='one')
    assert output == two_output
    assert placing.decline_report()['too_wide'] == one.decline_report()['too_wide'] == one.declined > 0
    assert one.overflowed == 0 and one.broken_groups == placing.broken_groups
    assert fjm_path.read_bytes() == two_fjm.read_bytes()


# a span too small for hex.cmp's 30 blocks: one pass leaves 5 groups without a block, and room for
# their 31 tables in the overflow region
NO_ROOM_SPAN = 0x30000
ONE_PASS_DECLINES: Dict[str, Tuple[Optional[Dict[str, Any]], str]] = {
    'no room': ({'span_bits': NO_ROOM_SPAN}, 'no_block'),
    'stale counts': (None, 'overflow'),  # half of the program's own counts, frozen
    'empty counts': ({'counts': {}, 'widths': {}}, 'new_group'),
}


@pytest.mark.parametrize('pin_broken', [False, True], ids=['unpin-broken', 'pin-broken'])
@pytest.mark.parametrize('scenario', list(ONE_PASS_DECLINES))
def test_one_pass_sends_what_gets_no_block_to_the_overflow_region(
    tmp_path: Path, counted_programs: Dict[str, Tuple[bytes, BlockPool]], scenario: str, pin_broken: bool
) -> None:
    reference, counting = counted_programs[CMP_PROGRAM]
    kwargs, cause = ONE_PASS_DECLINES[scenario]
    if kwargs is None:
        census = counting.census()
        kwargs = {**census.pool_kwargs(), 'counts': {g: max(1, n // 2) for g, n in census.counts.items()}}
    one = BlockPool(W, POOL_BASE, one_pass=True, pin_broken=pin_broken, **kwargs)
    output, _, fjm_path = build_and_run(CMP_PROGRAM, tmp_path, one)
    assert output == reference
    assert one.decline_report()[cause] > 0 and one.overflowed == one.declined == len(one.overflow_tables)
    assert one.deferred == one.allocated + one.overflowed  # nothing went back inline
    blocks_end = POOL_BASE + one._used
    segments = {s.segment_start * W: (s.segment_start + s.segment_length) * W for s in Reader(fjm_path).memory_segments}
    for address, ops_alignment in one.overflow_tables:
        assert address >= blocks_end and address in segments  # a segment of its own, above the blocks
        assert address % (ops_alignment * OP_BITS) == 0  # aligned as its pad asks
    limit = POOL_BASE + kwargs['span_bits'] if 'span_bits' in kwargs else 1 << W
    assert max(segments[address] for address, _ in one.overflow_tables) <= limit  # inside the pool


def test_a_pool_too_small_for_its_overflow_is_refused(
    tmp_path: Path, counted_programs: Dict[str, Tuple[bytes, BlockPool]]
) -> None:
    """the overflow region stays inside the pool's span: a one-pass pool sized from its census keeps
    room for it, and says so when even that cannot fit; a frozen one says by how much it fell short"""
    with pytest.raises(FlipJumpException, match='cannot hold the overflow region'):
        build_and_run(CMP_PROGRAM, tmp_path, BlockPool(W, POOL_BASE, one_pass=True, span_bits=1 << 14), name='tiny')
    _, counting = counted_programs[XOR_PROGRAM]
    too_few = counting.census().pool_kwargs()
    too_few['counts'] = {group: count // 2 for group, count in counting.counts.items()}
    blocks_only = BlockPool(W, POOL_BASE, **too_few)._used  # a span its blocks fill to the last bit
    frozen = BlockPool(W, POOL_BASE, one_pass=True, span_bits=blocks_only, **too_few)
    with pytest.raises(FlipJumpException, match='short by'):
        build_and_run(XOR_PROGRAM, tmp_path, frozen, name='frozen')


@pytest.mark.parametrize('evict_by_value', [False, True], ids=['plain', 'evict-by-value'])
def test_a_second_assembly_given_the_firsts_sizing_lays_out_alike(
    tmp_path: Path, counted_programs: Dict[str, Tuple[bytes, BlockPool]], evict_by_value: bool
) -> None:
    """the same program, on a pool too small for every block: the first assembly leaves groups out
    and overflows their tables; given its sizing() the second makes the same binary (and evicting by
    value cannot undo the layout the room for the overflow was planned on)"""
    reference, _ = counted_programs[CMP_PROGRAM]
    first = BlockPool(W, POOL_BASE, one_pass=True, span_bits=NO_ROOM_SPAN, evict_by_value=evict_by_value)
    output, _, first_fjm = build_and_run(CMP_PROGRAM, tmp_path, first, name='first')
    assert output == reference
    sizing = json_round_trip(first.sizing())
    assert sizing.unblocked == first.unblocked and first.unblocked and first.overflowed
    second = BlockPool(
        W, POOL_BASE, one_pass=True, span_bits=NO_ROOM_SPAN, evict_by_value=evict_by_value, **sizing.pool_kwargs()
    )
    _, _, second_fjm = build_and_run(CMP_PROGRAM, tmp_path, second, name='second')
    assert second.overflow_tables == first.overflow_tables and second_fjm.read_bytes() == first_fjm.read_bytes()


WIDER_TAIL = '    rep(2, i) hex.shl_bit 4, a\n    hex.shr_hex 4, a\n    stl.loop\n'  # 32-op tables after 16-op ones


@pytest.mark.parametrize('given', ['no counts', 'a group short'])
def test_appended_code_never_moves_the_first_assemblys_overflow_tables(
    tmp_path: Path, counted_programs: Dict[str, Tuple[bytes, BlockPool]], given: str
) -> None:
    """the overflow region fills in encounter order, so a second assembly with wider tables appended
    puts them after the first's - whose labels all stay where they were"""
    if given == 'no counts':
        kwargs: Dict[str, Any] = {'counts': {}, 'widths': {}}
    else:  # the program's own census, less one group: that group's tables overflow
        kwargs = counted_programs[XOR_PROGRAM][1].census().pool_kwargs()
        dropped = sorted(kwargs['counts'])[0]
        for key in ('counts', 'widths', 'width_hist'):
            kwargs[key].pop(dropped)
    first = BlockPool(W, POOL_BASE, one_pass=True, **kwargs)
    first_labels = expand_labels(XOR_PROGRAM, tmp_path, first, name='first')
    second = BlockPool(W, POOL_BASE, one_pass=True, **kwargs)
    second_labels = expand_labels(XOR_PROGRAM + WIDER_TAIL, tmp_path, second, name='second')
    assert 0 < first.overflowed < second.overflowed
    assert {label: second_labels[label] for label in first_labels} == first_labels
    assert second.overflow_tables[: first.overflowed] == first.overflow_tables


def _synthetic_tables(counts: Dict[str, int]) -> DeferredTables:
    """the deferred tables of groups of 16-op, `pad 16` tables, as a one-pass pool would queue them"""
    tables = DeferredTables(False)
    for group, count in counts.items():
        for _ in range(count):
            tables.add(group, Expr(group), 16, 16, '', NewSegment(0))
    return tables


def _one_pass_sized(counts: Dict[str, int], **kwargs: Any) -> BlockPool:
    """a one-pass pool with `counts` as its census, its unblocked groups chosen and its blocks laid out"""
    pool = BlockPool(W, POOL_BASE, one_pass=True, **kwargs)
    pool.counts, pool.widths = dict(counts), {group: 16 for group in counts}
    pool.width_hist = {group: {16: count} for group, count in counts.items()}
    if pool._heat:
        pool._resolve_heat(pool._heat)
    pool.unblocked = pool._choose_unblocked(_synthetic_tables(counts))
    pool._preallocate(pool.unblocked)
    return pool


def test_one_pass_leaves_out_the_groups_that_free_the_most_room_per_table() -> None:
    """leaving a group out frees only its block's padding: 17 tables in a 32-slot block free 15 slots,
    a full block frees nothing - nor does the 1-table group's, whose table fills its one slot - so
    those are never left out"""
    slot = 16 * OP_BITS
    counts = {'padded': 17, 'full': 16, 'single': 1, 'pair': 2}
    blocks = 32 + 16 + 1 + 2  # slots
    pool = _one_pass_sized(counts, span_bits=(blocks - 10) * slot)
    assert pool.unblocked == ['padded'] and set(pool.groups) == {'full', 'single', 'pair'}
    assert pool.overflow_rounds == 2  # the first layout fell short; leaving 'padded' out fit


def test_one_pass_leaves_about_as_many_tables_without_a_block_as_two_pass() -> None:
    """on a pool a little short of room, the tables one pass leaves without a block - the groups that
    do not fit, as in the two-pass build, plus those it leaves out to make room for their overflow -
    are not many more than the two-pass build leaves inline"""
    rng = random.Random(369)  # nosec B311 -- a seeded test fixture, not security
    counts = {f'g{i}': max(1, int(rng.paretovariate(1.3) * 4)) for i in range(3000)}
    total = sum(counts.values())
    full = BlockPool(W, POOL_BASE, counts=counts, widths={g: 16 for g in counts})._used
    span = full * 97 // 100
    two = BlockPool(W, POOL_BASE, counts=counts, widths={g: 16 for g in counts}, span_bits=span)
    one = _one_pass_sized(counts, span_bits=span)
    two_out = total - sum(counts[g] for g in two.groups)
    one_out = total - sum(counts[g] for g in one.groups)
    assert 0 < one_out <= 1.3 * two_out
    # and the groups without a block are exactly the ones it names - so a pool given its sizing() lays them
    # out alike, whatever else (evict_by_value) its _preallocate would do
    assert set(one.groups) == set(counts) - set(one.unblocked)


def test_a_pool_short_for_its_hot_blocks_and_overflow_says_so() -> None:
    """a hot group is never left out, so a pool with room for its block but not for the others'
    overflow names the overflow shortfall; and one with no room for the hot block itself says so at
    once - the hot blocks are laid out first, so no group left out could make room for it"""
    slot = 16 * OP_BITS
    counts = {'hot': 16, 'other': 20}  # blocks of 16 and 32 slots; 'other' overflows into 20
    heat: Dict[str, Sequence[Tuple[str, int, int]]] = {'hot': []}
    with pytest.raises(FlipJumpPreprocessorException, match='cannot hold the overflow region .* short by 0x1000 bits'):
        _one_pass_sized(counts, span_bits=32 * slot, heat=heat)  # 'other' overflows 4 slots past the span
    with pytest.raises(
        FlipJumpPreprocessorException,
        match="cannot hold its hot groups' blocks and the overflow region .* short by 0x2000 bits for the hot blocks",
    ):
        _one_pass_sized(counts, span_bits=8 * slot, heat=heat)  # the 16-slot hot block alone is 8 slots over


def _queued(pool: BlockPool, spec: Sequence[Tuple[str, int, int]]) -> DeferredTables:
    """`spec` - (group, ops_alignment, table_ops) per table, in encounter order - offered to a one-pass
    pool as the preprocessor offers its tables: defer() at each table's pad, then queued"""
    tables = DeferredTables(pool.keeps_site_paths)
    exprs = {group: Expr(group) for group, _, _ in spec}
    for group, align, ops in spec:
        verdict = pool.defer(align, ops, group, exprs[group], '')
        out = verdict == DEFER_OUT
        tables.add(
            group,
            exprs[group],
            align,
            ops,
            pool.kept_site(''),
            NewSegment(0) if out else None,
            None if out else verdict,
        )
    return tables


def _one_pass_placed(
    spec: Sequence[Tuple[str, int, int]], pool_base: int = POOL_BASE, **kwargs: Any
) -> Tuple[BlockPool, List[int], DeferredTables]:
    """a one-pass pool sized from the census of `spec`'s tables, every table's address (0: it stayed
    inline) and the queue it placed"""
    pool = BlockPool(W, pool_base, one_pass=True, **kwargs)
    tables = _queued(pool, spec)
    return pool, list(pool.place_deferred(tables, {})), tables


def test_one_pass_does_not_refuse_a_pool_its_estimate_calls_short() -> None:
    """THE CR-3 B1 REPRODUCER. H is hot, so its block is laid out first and A's 16-slot block sits
    behind a 240-op alignment hole that no estimate of A's room counts; C's block does not fit. The
    first layout is 184 ops short and leaving A out frees 112 by the estimate - which used to refuse
    the pool - but 352 in fact: the next layout fits"""
    spec = [('H', 16, 16)] + [('A', 16, 16)] * 9 + [('C', 16, 16)] * 17
    pool, addresses, _ = _one_pass_placed(spec, span_bits=600 * OP_BITS, heat={'H': []})
    assert pool.unblocked == ['A', 'C'] and set(pool.groups) == {'H'}  # A left out; C fits nowhere anyway
    assert pool.overflow_rounds == 2 and pool.overflowed == 26 and all(addresses)
    assert max(address for address, _ in pool.overflow_tables) + 16 * OP_BITS == POOL_BASE + 432 * OP_BITS


# the B1 reproducer as a program: the hot (h + 32) word's block is laid out first, a's behind its hole
B1_PROGRAM = """
stl.startup_and_init_all
    hex.xor x, h
    rep(9, i) hex.xor x, a
    rep(17, i) hex.xor x, c
    hex.print_as_digit x, 0
    stl.loop
x: hex.hex 1
h: hex.hex 2
a: hex.hex 3
c: hex.hex 4
"""


def test_a_program_one_pass_refused_on_its_estimate_builds_and_runs(tmp_path: Path) -> None:
    """at 512 ops of span (to 656) the chooser of e4340ee refused this program - 'short by 0x2800 bits'
    - and leaving a's group out builds it: it now does, and the program prints what the inline one
    prints"""
    counting = BlockPool(W, POOL_BASE)
    expand_labels(B1_PROGRAM, tmp_path, counting, name='counting')
    assert counting.counts == {'(h + 32)': 1, '(a + 32)': 9, '(c + 32)': 17}
    reference, _, _ = build_and_run(B1_PROGRAM, tmp_path, None, name='inline')
    one = BlockPool(W, POOL_BASE, one_pass=True, span_bits=512 * OP_BITS, heat={heat_key('(h + 32)'): []})
    output, _, _ = build_and_run(B1_PROGRAM, tmp_path, one, name='one')
    assert output == reference == b'4'
    assert one.unblocked == ['(a + 32)', '(c + 32)'] and set(one.groups) == {'(h + 32)'}


def test_one_pass_leaves_out_the_group_whose_block_frees_the_most_room_not_the_least_full_one() -> None:
    """X's seven 32-op tables take 7 of its 8 slots and Y's nine 16-op tables 9 of 16: alike blocks of
    256 ops, but leaving X out frees 32 ops and Y 112. W's block fits nowhere, its tables overflow and
    the pool is 100 ops short: Y alone makes room. By the room a block frees per table GROSS of its
    tables, X would go first and Y after it - 16 tables without a block for 9"""
    spec = [('W', 16, 16)] * 70 + [('X', 32, 32)] * 7 + [('Y', 16, 16)] * 9
    pool, addresses, _ = _one_pass_placed(spec, span_bits=1532 * OP_BITS)
    assert pool.unblocked == ['Y', 'W'] and set(pool.groups) == {'X'}  # W is named too: it does not fit
    assert pool.overflowed == 70 + 9 and all(addresses)


def test_one_pass_stops_leaving_groups_out_once_the_estimate_covers_the_shortfall() -> None:
    """V's 64-slot block fits nowhere and its 33 tables overflow: the pool is 7 slots short, exactly
    the padding A's block frees (9 tables in 16 slots). Leaving A out covers it - so B, which frees
    as much, keeps its block, and the region fills the span to its last op"""
    spec = [('V', 16, 16)] * 33 + [('A', 16, 16)] * 9 + [('B', 16, 16)] * 9
    pool, addresses, _ = _one_pass_placed(spec, span_bits=58 * 16 * OP_BITS)
    assert pool.unblocked == ['A', 'V'] and set(pool.groups) == {'B'} and all(addresses)
    assert max(address for address, _ in pool.overflow_tables) + 16 * OP_BITS == POOL_BASE + 58 * 16 * OP_BITS


def test_one_pass_plans_the_overflow_region_at_each_tables_pad() -> None:
    """P's four 1-op tables sit behind `pad 16`, so in the overflow region each starts 16 ops after the
    last: 49 ops, not 4. P's block does not fit beside B's, and its region would end 17 ops past the
    span - so B is left out instead, and P gets its block"""
    spec = [('B', 16, 16)] * 9 + [('P', 16, 1)] * 4
    pool, addresses, _ = _one_pass_placed(spec, span_bits=288 * OP_BITS)
    assert pool.unblocked == ['B'] and set(pool.groups) == {'P'} and all(addresses)


def test_the_overflow_region_keeps_encounter_order_across_the_groups_left_out() -> None:
    """A's and B's tables alternate, and the span has room for neither block beside the other's
    overflow: both are left out, and their tables fill the region in the order the expansion reached
    them - not group by group - so code appended later never moves one"""
    spec = [('A', 16, 16), ('B', 16, 16)] * 9
    pool, addresses, _ = _one_pass_placed(spec, span_bits=300 * OP_BITS)
    assert pool.unblocked == ['A', 'B'] and not pool.groups
    assert addresses == [POOL_BASE + index * 16 * OP_BITS for index in range(len(spec))]


def test_a_table_kept_inline_takes_no_room_in_the_overflow_region() -> None:
    """T's 32-op table is too wide for any slot (max_slot_ops=16) and stays inline, so leaving T out
    sends only its three 16-op tables to the region - which then fills the span to its last op (an
    exact fit is a fit)"""
    spec = [('T', 16, 16)] * 3 + [('T', 32, 32)] + [('V', 16, 16)] * 20
    pool, addresses, tables = _one_pass_placed(spec, span_bits=368 * OP_BITS, max_slot_ops=16)
    assert pool.unblocked == ['T', 'V'] and not pool.groups  # V's 32-slot block fits nowhere
    assert list(tables.inline) == [3] and addresses[3] == 0 and all(addresses[:3] + addresses[4:])
    assert max(address for address, _ in pool.overflow_tables) + 16 * OP_BITS == POOL_BASE + 368 * OP_BITS


class _Unread(List[str]):
    """a queue's group list that refuses to be read"""

    def __iter__(self) -> Iterator[str]:
        raise AssertionError('the queued tables were read')


def test_choosing_the_groups_left_out_builds_nothing_per_table() -> None:
    """the CR-3 F3 measurement: the chooser walks the queue's own arrays (it built a tuple per table,
    ~158 bytes each, even when nothing was short) - and when every block fits it reads no table"""
    counts = {f'g{i}': 1000 for i in range(20)}  # 20,000 tables, 1000 in each 1024-slot block

    def sized(span_bits: int) -> BlockPool:
        pool = BlockPool(W, POOL_BASE, one_pass=True, span_bits=span_bits)
        pool.counts, pool.widths = dict(counts), {group: 16 for group in counts}
        pool.width_hist = {group: {16: count} for group, count in counts.items()}
        return pool

    tables = _synthetic_tables(counts)
    every_block = 20 * 1024 * 16 * OP_BITS
    short = sized(every_block * 99 // 100)
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    unblocked = short._choose_unblocked(tables)
    peak = tracemalloc.get_traced_memory()[1] - before
    tracemalloc.stop()
    assert len(unblocked) == 9  # the fewest that make room: each frees 24 slots
    assert peak / len(tables) < 4, f'{peak / len(tables):.1f} bytes per queued table'
    tables.groups = _Unread(tables.groups)
    assert sized(every_block)._choose_unblocked(tables) == []


def test_code_appended_after_a_short_first_assembly_needs_room_for_its_overflow() -> None:
    """the first assembly leaves A and B out and plans its region to end 12 ops before the end of its
    span: two appended tables of a new group overflow it, as documented - unless the first assembly
    kept room for them (a span_bits smaller than the second's), or both have an overflow_base"""
    first_spec = [('A', 16, 16), ('B', 16, 16)] * 9
    appended = first_spec + [('N', 16, 16)] * 2
    span = 300 * OP_BITS
    first, addresses, _ = _one_pass_placed(first_spec, span_bits=span)
    assert first.unblocked == ['A', 'B']
    sizing = json_round_trip(first.sizing()).pool_kwargs()
    with pytest.raises(FlipJumpPreprocessorException, match='ends past the end of the pool'):
        _one_pass_placed(appended, span_bits=span, **sizing)
    _, appended_addresses, _ = _one_pass_placed(appended, span_bits=span + 32 * OP_BITS, **sizing)
    assert appended_addresses[: len(addresses)] == addresses and all(appended_addresses)
    base = POOL_BASE + (1 << 20)  # above the blocks, outside the span
    first, addresses, _ = _one_pass_placed(first_spec, span_bits=span, overflow_base=base)
    sizing = json_round_trip(first.sizing()).pool_kwargs()
    _, appended_addresses, _ = _one_pass_placed(appended, span_bits=span, overflow_base=base, **sizing)
    assert appended_addresses[: len(addresses)] == addresses and all(appended_addresses)


# table shapes (ops_alignment, table_ops) of the random pools: hex.xor's, hex.cmp's (31 ops behind
# `pad 4`), and odd ones whose room in the overflow region depends on their neighbours
PROPERTY_SHAPES = [(16, 16), (16, 16), (16, 16), (4, 31), (32, 32), (8, 8), (16, 1), (16, 8), (64, 40), (1, 3)]


def test_one_pass_layouts_of_random_pools_keep_their_promises() -> None:
    """seeded random pools - groups of mixed table shapes, spans from 30% to 115% of what every block
    needs, aligned and unaligned pool bases, spread, width buckets, evict_by_value, hot groups and an
    alias - placed by a one-pass pool sized from its own census: it places every table, or refuses
    in the chooser (never in the overflow region it planned); the groups without a block are exactly
    `unblocked`; no two tables overlap and all stay inside the span; the overflow region lies past
    the blocks, each table at its pad, in encounter order; a pool given its sizing() lays out alike,
    and so does one with tables appended and a span with room for them; and with nothing left out
    and nothing overflowing, the layout is the two-pass build's"""
    rng = random.Random(3690)  # nosec B311 -- a seeded test fixture, not security
    outcomes = {'placed': 0, 'left_out': 0, 'refused': 0, 'two_pass': 0}  # nosec B105 -- counters
    for _ in range(300):
        groups = [f'g{i}' for i in range(rng.randint(1, 30))]
        spec: List[Tuple[str, int, int]] = []
        for group in groups:
            shapes = rng.sample(PROPERTY_SHAPES, rng.randint(1, 2))
            count = min(400, max(1, int(rng.paretovariate(1.2) * rng.choice([1, 2, 4]))))
            spec += [(group, *rng.choice(shapes)) for _ in range(count)]
        rng.shuffle(spec)
        base = rng.choice([POOL_BASE, POOL_BASE + 3 * 16 * OP_BITS])  # aligned, or 48 ops past it
        options: Dict[str, Any] = {
            'spread': rng.choice([1, 2]),
            'spread_min_count': rng.choice([4, 256]),
            'width_buckets': rng.random() < 0.5,
            'max_slot_ops': rng.choice([16, 32, 64]),
            'evict_by_value': rng.random() < 0.3,
        }
        if len(groups) > 2 and rng.random() < 0.3:
            options['alias'] = {groups[1]: groups[0]}
        if rng.random() < 0.3:
            hot = rng.choice(groups[1:] if 'alias' in options else groups)  # a canonical group
            options['heat'] = {
                hot: [('', occurrence, rng.choice([8, 16, 32])) for occurrence in range(rng.randint(0, 2))]
            }
        every_block = _one_pass_placed(spec, base, **options)[0]._used
        span = max(1, int(every_block * rng.uniform(0.3, 1.15)) // OP_BITS) * OP_BITS
        try:
            pool, addresses, tables = _one_pass_placed(spec, base, span_bits=span, **options)
        except FlipJumpPreprocessorException as refusal:
            assert 'cannot hold' in str(refusal)  # the chooser's; the region it planned always fits
            outcomes['refused'] += 1
            continue
        outcomes['left_out' if pool.unblocked else 'placed'] += 1
        assert set(pool.groups) == set(pool.counts) - set(pool.unblocked)
        blocks_end, limit = base + pool._used, base + span
        taken = sorted(
            (address, address + ops * OP_BITS)
            for index, ((_, _, ops), address) in enumerate(zip(spec, addresses))
            if index not in tables.inline
        )
        assert all(address == 0 for index, address in enumerate(addresses) if index in tables.inline)
        assert all(base <= start and end <= limit for start, end in taken)
        assert all(end <= start for (_, end), (start, _) in zip(taken, taken[1:]))
        region = [address for address, _ in pool.overflow_tables]
        assert region == sorted(region) and all(address >= blocks_end for address in region)
        assert all(address % (align * OP_BITS) == 0 for address, align in pool.overflow_tables)
        sizing = json_round_trip(pool.sizing()).pool_kwargs()
        assert _one_pass_placed(spec, base, **{**options, 'span_bits': span, **sizing})[1] == addresses
        extra = [(rng.choice(groups + ['new']), *rng.choice(PROPERTY_SHAPES)) for _ in range(rng.randint(1, 6))]
        room = sum(-(-ops // align) * align + align for _, align, ops in extra) * OP_BITS
        _, appended, _ = _one_pass_placed(spec + extra, base, **{**options, 'span_bits': span + room, **sizing})
        assert appended[: len(spec)] == addresses
        if not pool.unblocked and not pool.overflowed:
            placing = BlockPool(W, base, **{**options, 'span_bits': span, **pool.sizing().pool_kwargs()})
            two_pass = [placing.reserve(align, ops, group, Expr(group), '') for group, align, ops in spec]
            assert [0 if reserved is None else reserved[0] for reserved in two_pass] == addresses
            outcomes['two_pass'] += 1
    assert min(outcomes.values()) >= 20, outcomes  # every outcome is exercised


def test_the_op_stream_does_not_depend_on_what_one_pass_decides(tmp_path: Path) -> None:
    """every label outside the tables lands at the same address whatever the pool does with them -
    while the two-pass build, which keeps a declined table inline, moves them"""

    def outside_tables(labels: Dict[str, int]) -> Dict[str, int]:
        return {label: address for label, address in labels.items() if address < POOL_BASE}

    configs: List[Dict[str, Any]] = [
        {},
        {'span_bits': NO_ROOM_SPAN},
        {'span_bits': NO_ROOM_SPAN + 0x8000},
        {'counts': {}, 'widths': {}},
    ]
    one_pass = [
        outside_tables(expand_labels(CMP_PROGRAM, tmp_path, BlockPool(W, POOL_BASE, one_pass=True, **config)))
        for config in configs
    ]
    assert all(labels == one_pass[0] for labels in one_pass[1:])
    counting = BlockPool(W, POOL_BASE)
    expand_labels(CMP_PROGRAM, tmp_path, counting)
    census = counting.census().pool_kwargs()
    extras: List[Dict[str, Any]] = [{}, {'span_bits': 1 << 14}]
    fresh, tiny = (
        outside_tables(expand_labels(CMP_PROGRAM, tmp_path, BlockPool(W, POOL_BASE, **census, **extra)))
        for extra in extras
    )
    assert fresh == one_pass[0]  # with nothing declined, both modes lay the stream out alike
    assert tiny != fresh  # the comparison can see a moved stream


def test_one_pass_with_frozen_counts_keeps_the_layout_when_code_is_appended(tmp_path: Path) -> None:
    """an assembly with code appended, given the first assembly's census, lays out everything the first
    had at the same addresses"""
    first = BlockPool(W, POOL_BASE, one_pass=True)
    first_labels = expand_labels(XOR_PROGRAM, tmp_path, first, name='first')
    census = first.census()
    assert first.sizing() == census  # what it was sized with, ready for the second assembly
    second = BlockPool(W, POOL_BASE, one_pass=True, **first.sizing().pool_kwargs())
    second_labels = expand_labels(APPENDED_PROGRAM, tmp_path, second, name='second')
    assert {label: second_labels[label] for label in first_labels} == first_labels
    assert {g: b for g, (b, _) in second.groups.items()} == {g: b for g, (b, _) in first.groups.items()}
    # each appended table took a spare slot of its group's power-of-two block, or - the block being
    # full - went to the overflow region
    appended = sum(second.census().counts.values()) - sum(census.counts.values())
    assert appended > 0 and second.allocated - first.allocated + second.overflowed == appended
    assert second.overflowed == second.stale_declines > 0
    reference, _, _ = build_and_run(XOR_PROGRAM, tmp_path, None, name='inline')
    output, _, _ = build_and_run(
        APPENDED_PROGRAM, tmp_path, BlockPool(W, POOL_BASE, one_pass=True, **census.pool_kwargs()), name='appended'
    )
    assert output == reference


def test_the_overflow_region_can_be_placed_and_must_fit(
    tmp_path: Path, counted_programs: Dict[str, Tuple[bytes, BlockPool]]
) -> None:
    reference, counting = counted_programs[XOR_PROGRAM]
    base = POOL_BASE // 2  # between the program and the pool
    pool = BlockPool(W, POOL_BASE, one_pass=True, counts={}, widths={}, overflow_base=base)
    output, _, fjm_path = build_and_run(XOR_PROGRAM, tmp_path, pool, name='below')
    assert output == reference and pool.overflowed > 0
    starts = [s.segment_start * W for s in Reader(fjm_path).memory_segments]
    assert sum(1 for start in starts if base <= start < POOL_BASE) == pool.overflowed
    too_few = counting.census().pool_kwargs()
    too_few['counts'] = {group: count // 2 for group, count in counting.counts.items()}  # some overflow
    with pytest.raises(FlipJumpException, match='overlaps the blocks'):
        build_and_run(XOR_PROGRAM, tmp_path, BlockPool(W, POOL_BASE, one_pass=True, overflow_base=POOL_BASE, **too_few))
    with pytest.raises(FlipJumpException, match='the end of memory'):
        build_and_run(
            XOR_PROGRAM,
            tmp_path,
            BlockPool(W, POOL_BASE, one_pass=True, counts={}, widths={}, overflow_base=(1 << W) - OP_BITS),
        )


@pytest.mark.parametrize('case', list(EXPANSION_TIME_CASES))
def test_a_table_label_evaluated_during_the_expansion_is_refused_by_one_pass(tmp_path: Path, case: str) -> None:
    code, data = EXPANSION_TIME_CASES[case]
    source = EXPANSION_TIME_TEMPLATE.replace('{CODE}', code).replace('{DATA}', data)
    reference, _, _ = build_and_run(source, tmp_path, None, name='inline')
    output, _, _ = two_pass_build(source, tmp_path)[:3]
    assert output == reference == b'5'  # two passes know the address when it is needed
    with pytest.raises(FlipJumpException) as refused:
        build_and_run(source, tmp_path, BlockPool(W, POOL_BASE, one_pass=True), name='one')
    # only the expression evaluated is blamed: a rep's argument names ext2_switch, its count does not
    assert 'It names ext_switch, the label of a table that one-pass blocking places' in str(refused.value)


def test_options_that_need_one_pass_or_contradict_each_other_are_refused() -> None:
    with pytest.raises(ValueError, match='merge_aliases'):
        BlockPool(W, POOL_BASE, merge_aliases=True)  # a counting pool
    with pytest.raises(ValueError, match='merge_aliases'):
        BlockPool(W, POOL_BASE, one_pass=True, counts={'g': 1}, widths={'g': 16}, merge_aliases=True)
    with pytest.raises(ValueError, match='not both'):
        BlockPool(W, POOL_BASE, one_pass=True, merge_aliases=True, alias={'a': 'b'})
    with pytest.raises(ValueError, match='overflow_base'):
        BlockPool(W, POOL_BASE, counts={'g': 1}, widths={'g': 16}, overflow_base=POOL_BASE // 2)
    with pytest.raises(ValueError, match='above it'):  # 0 is where the program starts
        BlockPool(W, POOL_BASE, one_pass=True, overflow_base=0)
    with pytest.raises(ValueError, match='unblocked'):
        BlockPool(W, POOL_BASE, unblocked=['g'])  # a counting pool has no blocks to leave out


class _PlacementRecorder(BlockPool):
    """a one-pass pool that records the tables its reserve() is asked to place, with their sites"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(W, POOL_BASE, **kwargs)
        self.sites: List[Tuple[str, str]] = []

    def reserve(
        self,
        ops_alignment: int,
        table_ops: int,
        group: Optional[str] = None,
        group_expr: Optional[Expr] = None,
        labels_prefix: str = '',
    ) -> Optional[Tuple[int, bool, int]]:
        if group is not None:
            self.sites.append((group, labels_prefix))
        return super().reserve(ops_alignment, table_ops, group, group_expr, labels_prefix)


def test_a_one_pass_subclass_reserve_sees_every_table_with_its_site(tmp_path: Path) -> None:
    counting = _SiteRecorder()
    build_and_run(XOR_PROGRAM, tmp_path, counting, name='counting')
    one = _PlacementRecorder(one_pass=True)
    assert one.keeps_site_paths and not BlockPool(W, POOL_BASE, one_pass=True).keeps_site_paths
    build_and_run(XOR_PROGRAM, tmp_path, one, name='one')
    assert one.sites == counting.sites and one.sites


def test_the_census_of_a_counting_pool_given_an_alias_keeps_it() -> None:
    alias = {'(x + w)': '(x + 32)'}
    counting = BlockPool(W, POOL_BASE, alias=alias)
    counting.reserve(16, 16, group='(x + 32)', group_expr=_sum('x', 32))
    counting.reserve(16, 16, group='(x + w)', group_expr=_sum('x', 'w'))
    census = counting.census()
    assert census == BlockCensus({'(x + 32)': 2}, {'(x + 32)': 16}, {'(x + 32)': {16: 2}}, alias)
    placing = BlockPool(W, POOL_BASE, **census.pool_kwargs())
    assert placing.reserve(16, 16, group='(x + w)', group_expr=_sum('x', 'w')) is not None  # not a 'new_group'
    # a third name for the word, merged by the labels: one lookup still reaches the final group
    counting.reserve(16, 16, group='((x + 0) + 32)', group_expr=Expr(('+', (_sum('x', 0), Expr(32)))))
    counting.labels_resolved({'x': 4096, 'w': 32})
    merged = counting.census(merge_aliases=True)
    assert merged.counts == {'((x + 0) + 32)': 3}
    assert merged.alias == {'(x + w)': '((x + 0) + 32)', '(x + 32)': '((x + 0) + 32)'}


# a dispatch word inside a deferred table, named two ways: its alias needs the table's address
ALIAS_DEFERRED_PROGRAM = (
    """
def ext_table x @ end < ext_switch {
    wflip x+w, ext_switch, x
    pad 16
    ext_table.__relocatable__.table ext_switch, end
  end:
    wflip x+w, ext_switch
}
"""
    + relocatable('ext_table', 'switch, end', 'switch', '    ;end\n' * 16)
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


def test_one_pass_counts_the_aliases_it_cannot_resolve_and_reports_what_it_was_sized_with(tmp_path: Path) -> None:
    reference, _, _ = build_and_run(ALIAS_DEFERRED_PROGRAM, tmp_path, None, name='inline')
    counting = BlockPool(W, POOL_BASE)
    build_and_run(ALIAS_DEFERRED_PROGRAM, tmp_path, counting, name='counting')
    assert counting.census(merge_aliases=True).alias == {'(ext_switch + 32)': '((ext_switch + 0) + 32)'}
    one = BlockPool(W, POOL_BASE, one_pass=True, merge_aliases=True)
    output, _, _ = build_and_run(ALIAS_DEFERRED_PROGRAM, tmp_path, one, name='one')
    assert output == reference
    assert one.alias_unresolved == 2 and one.alias == {}  # both words name ext_switch, unplaced then
    assert one.census(merge_aliases=True) == one.sizing()  # not what the final labels would merge


def test_a_deferred_table_costs_tens_of_bytes() -> None:
    """what one pass keeps per queued table, beyond what a placing pass holds too (the op pointers,
    the NewSegment, the label strings, the site path string): see DeferredTables"""
    count = 20000
    groups = [f'((f13:l2210:sim.pass(3)---hp + {128 * (i % 50)}) + 32)' for i in range(count)]
    exprs = {group: Expr(group) for group in groups}
    segments = [NewSegment(0) for _ in range(count)]
    names = [f'f13:l2210:sim.pass(3)---s2:l40:rep{i}:hex.exact_xor(5)---switch' for i in range(count)]
    sites = [name.rpartition('---')[0] for name in names]

    def per_table(labels_per_table: int, keep_sites: bool) -> float:
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        tables = DeferredTables(keep_sites)
        for i in range(count):
            tables.add(groups[i], exprs[groups[i]], 16, 16, sites[i], segments[i])
            for label in range(labels_per_table):
                tables.add_label(names[i], label * 448)
        cost = (tracemalloc.get_traced_memory()[0] - before) / count
        tracemalloc.stop()
        assert len(tables) == count
        return cost

    one_label, two_labels, with_site = per_table(1, False), per_table(2, False), per_table(1, True)
    extra_label, site = two_labels - one_label, with_site - one_label
    print(f'deferred table: {one_label:.1f} B, a 2nd label +{extra_label:.1f} B, a site +{site:.1f} B')
    assert one_label < 64 and extra_label < 24 and site < 16


def test_the_pool_is_importable_from_the_preprocessor_too() -> None:
    # where it was defined before table_pool.py: callers import it from there
    for name in (
        'BlockCensus',
        'BlockPool',
        'DECLINE_OTHER',
        'DECLINE_STALE',
        'DEFER_INLINE',
        'DEFER_OUT',
        'DEFER_TOO_WIDE',
        'DeferredTables',
        'POINTER_RUNTIME_LABELS',
        'RESERVED_BELOW',
        'TablePool',
        'heat_key',
        'merge_aliased_groups',
    ):
        assert getattr(preprocessor, name) is getattr(table_pool, name), name
