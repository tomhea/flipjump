"""
a golden test of the table pool: a real stl program assembled with a one-pass BlockPool configured as fjdoom's
shipped build is (merged aliases, spread factor 2, max_slot_ops 512, pin_incomplete_groups, width buckets, a heat
list, a pin_all_but), whose .fjm, labels and census are frozen by their sha256. A refactor of the pool may not move
a byte.

The negative controls perturb a real knob (the spread, the heat list's order) or the raw placement's index order,
and require the digests to change. EXTRA freezes more configurations: the CLI's flat pool, a raw program, the
overflow region and the constructor's defaults.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from flipjump import assemble
from flipjump.assembler.table_pool import BlockPool, HotSite, heat_key
from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.utils.functions import load_debugging_labels

W = 32
POOL_BASE = 0x80000000  # fjdoom's ship knobs: --pool-base 0x80000000 --span-bits 0x7fffffe0
SPAN_BITS = 0x7FFFFFE0
MAX_SLOT_OPS = 512

# hundreds of tables over many hex.* macros, a pointer read (the pointer runtime) and a pointed cell to exclude
PROGRAM = """
stl.startup_and_init_all
    hex.set 4, a, 0x1234
    hex.set 4, b, 0x5678
    rep(12, i) hex.xor 4, a, b
    rep(6, i) hex.add 4, a, b
    rep(6, i) hex.sub 4, b, a
    rep(4, i) hex.or 4, a, b
    rep(4, i) hex.and 4, b, a
    rep(3, i) hex.shl_bit 4, a
    rep(3, i) hex.shr_hex 4, b
    hex.mul 4, r, a, b
    hex.cmp 4, r, b, LT, EQ, GT
LT:
    hex.print_uint 4, r, 1, 1
    ;after
EQ:
    hex.print_uint 4, b, 1, 1
    ;after
GT:
    hex.print_uint 4, a, 1, 1
after:
    hex.set d, 6
    hex.xor e, d
    hex.set v, 0xD
    hex.xor_hex_from_ptr v, p
    hex.print_as_digit v, 1
    rep(8, i) hex.inc 4, c
    hex.print_uint 4, c, 1, 1
    stl.loop
a: hex.vec 4
b: hex.vec 4
r: hex.vec 4
c: hex.vec 4
d: hex.hex
e: hex.hex
v: hex.hex
p: hex.vec w/4, d
"""

# each configuration's digests (frozen on 1.6.0 = e44ff3f; the labels re-frozen on 2e78aa9, whose stl comment trims
# moved the call-site line numbers the labels carry - the .fjm and census digests did not move)
GOLDEN: Dict[str, Dict[str, Any]] = {
    'shipped': {
        'fjm': 'dca7d91cc81896ec582654f2f52571dafb5782279fa640f0b98808c708cbc2c4',
        'labels': 'a7a8f67f9419c2063e6705d1faf55392f88d32de7cdf2c0005844a697eefa1a6',
        'census': '20d6836eccf20d893928fb4207643af6cb391fb73d48d8b2d6934f42ff8214b3',
        'stats': {
            'allocated': 416,
            'deferred': 416,
            'ungrouped': 0,
            'declined': 0,
            'groups': 41,
            'pinned_words': 41,
            'broken_groups': 0,
            'unpins': {},
            'heat': {
                'hot_groups': 3,
                'hot_groups_missing': 0,
                'hot_groups_ambiguous': 0,
                'hot_sites_listed': 9,
                'hot_sites_matched': 9,
                'hot_sites_width_changed': 0,
            },
        },
    },
    'narrow': {
        'fjm': 'd56c6e1dc5e83425177b7fcf05285b46ce979e79a81f368b671aa7a0cc38d40b',
        'labels': '9a178b30a0ac750cb388b85195ee778b867bd301591e7f836f34c50c8a7cfedc',
        'census': '20d6836eccf20d893928fb4207643af6cb391fb73d48d8b2d6934f42ff8214b3',
        'stats': {
            'allocated': 289,
            'deferred': 289,
            'ungrouped': 0,
            'declined': 127,
            'groups': 41,
            'pinned_words': 13,
            'broken_groups': 30,
            'unpins': {},
            'heat': {
                'hot_groups': 3,
                'hot_groups_missing': 0,
                'hot_groups_ambiguous': 0,
                'hot_sites_listed': 9,
                'hot_sites_matched': 7,
                'hot_sites_width_changed': 0,
            },
        },
    },
}

Heat = Dict[str, List[HotSite]]


def _slot_ops(width: int) -> int:
    return 1 << (min(max(width, 1), MAX_SLOT_OPS) - 1).bit_length()


# fjdoom's shipped knobs, and a narrow variant whose 32-op tables are too wide (declined, their groups broken)
CONFIGS: Dict[str, Dict[str, Any]] = {
    'shipped': {'spread_factor': 2, 'max_slot_ops': MAX_SLOT_OPS, 'pin_incomplete_groups': True},
    'narrow': {'spread_factor': 2, 'max_slot_ops': 16, 'pin_incomplete_groups': False},
}


ALL_BUT_D = [('d', 1)]  # the cell the pointer reads


def _pool(heat: Optional[Heat] = None, cls: type = BlockPool, pin_all_but: Any = ALL_BUT_D, **knobs: Any) -> BlockPool:
    knobs = {**CONFIGS['shipped'], **knobs}
    pool: BlockPool = cls(
        W,
        POOL_BASE,
        pool_end=POOL_BASE + SPAN_BITS,
        spread_min_tables=8,
        pin_all_but=pin_all_but,
        **knobs,
        **({'heat': heat} if heat else {}),
    )
    return pool


def _assemble(tmp_path: Path, name: str, pool: Optional[BlockPool]) -> Tuple[bytes, Dict[str, int]]:
    """the .fjm bytes and the debugging labels of PROGRAM assembled with `pool`"""
    source = tmp_path / f'{name}.fj'
    source.write_text(PROGRAM)
    fjm, debug = tmp_path / f'{name}.fjm', tmp_path / f'{name}.fjd'
    assemble([source], fjm, memory_width=W, print_time=False, table_pool=pool, debugging_file_path=debug)
    return fjm.read_bytes(), load_debugging_labels(debug)


def _heat(tmp_path: Path) -> Heat:
    """a heat list over the three biggest groups: each one's last three sites, hottest first"""
    recorder = _pool()
    _assemble(tmp_path, 'record', recorder)
    assert recorder.report is not None
    sites: Heat = {}
    for table in recorder.report.table_sites:
        sites.setdefault(table.group, []).append(HotSite(table.site, table.occurrence, _slot_ops(table.slot_ops)))
    biggest = sorted(sites, key=lambda group: (-len(sites[group]), group))[:3]
    return {heat_key(group): sites[group][::-1][:3] for group in biggest}


def _digests(tmp_path: Path, heat: Heat, pool: Optional[BlockPool] = None, **knobs: Any) -> Dict[str, Any]:
    pool = _pool(heat, **knobs) if pool is None else pool
    fjm, labels = _assemble(tmp_path, 'golden', pool)
    census = pool.census()
    census_json = json.dumps([census.counts, census.widths, census.width_hist, census.alias])
    return {
        'fjm': hashlib.sha256(fjm).hexdigest(),
        'labels': hashlib.sha256(json.dumps(sorted(labels.items())).encode()).hexdigest(),
        'census': hashlib.sha256(census_json.encode()).hexdigest(),
        'stats': {
            'allocated': pool.allocated,
            'deferred': pool.deferred,
            'ungrouped': pool.ungrouped,
            'declined': pool.declined,
            'groups': len(pool.groups),
            'pinned_words': len(pool.pinned_words()),
            'broken_groups': len(pool.broken_groups),
            'unpins': dict(sorted(pool.unpins.items())),
            'heat': pool.heat_report(),
        },
    }


@pytest.fixture(scope='module')
def heat(tmp_path_factory: pytest.TempPathFactory) -> Heat:
    return _heat(tmp_path_factory.mktemp('heat'))


@pytest.mark.parametrize('config', sorted(CONFIGS))
def test_golden(tmp_path: Path, heat: Heat, config: str) -> None:
    digests = _digests(tmp_path, heat, **CONFIGS[config])
    print(json.dumps(digests, indent=1))
    assert digests['stats']['allocated'] >= 200 and digests['stats']['heat']['hot_sites_listed'] == 9
    assert digests == GOLDEN[config]


# more configurations, frozen on f55290d (before the second shrink round): {name: (the pool, its digests)} - the
# CLI's flat pool (with pin_all_but it lays blocks out above the program; without it the
# pointer read makes the program raw: each table takes a cheap address), a raw program at POOL_BASE, a span too small
# for every block (the overflow region) and the constructor's defaults
EXTRA: Dict[str, Tuple[Callable[[Heat], BlockPool], Dict[str, Any]]] = {
    'flat': (
        lambda heat: BlockPool(W, pin_all_but=ALL_BUT_D),
        {
            'fjm': 'bdc8e5dd8aa7a0d2923237d7e5c27793e5a67316598999733b739a8c57704a75',
            'labels': '1646034a149d1a3e3a67d67d4a826df8ff0e43c5bffbc3bc35eb1b581dc09fad',
            'census': '20d6836eccf20d893928fb4207643af6cb391fb73d48d8b2d6934f42ff8214b3',
            'stats': {
                'allocated': 400,
                'deferred': 400,
                'ungrouped': 0,
                'declined': 16,
                'groups': 41,
                'pinned_words': 25,
                'broken_groups': 16,
                'unpins': {},
                'heat': {
                    'hot_groups': 0,
                    'hot_groups_missing': 0,
                    'hot_groups_ambiguous': 0,
                    'hot_sites_listed': 0,
                    'hot_sites_matched': 0,
                    'hot_sites_width_changed': 0,
                },
                'overflowed': 0,
                'end': 2381824,
            },
        },
    ),
    'flat_raw': (
        lambda heat: BlockPool(W),
        {
            'fjm': 'd414bcbf643c069c887c278547824d353cd787defd3bee951e78491c52bad437',
            'labels': '967d5453523f79aff2e00bc4b46035fa33f5dcd1646b727d3cb873379ddc8d73',
            'census': '20d6836eccf20d893928fb4207643af6cb391fb73d48d8b2d6934f42ff8214b3',
            'stats': {
                'allocated': 416,
                'deferred': 416,
                'ungrouped': 0,
                'declined': 0,
                'groups': 0,
                'pinned_words': 0,
                'broken_groups': 0,
                'unpins': {'the pointer runtime': 41},
                'heat': {
                    'hot_groups': 0,
                    'hot_groups_missing': 0,
                    'hot_groups_ambiguous': 0,
                    'hot_sites_listed': 0,
                    'hot_sites_matched': 0,
                    'hot_sites_width_changed': 0,
                },
                'overflowed': 0,
                'end': 234882048,
            },
        },
    ),
    'raw': (
        lambda heat: _pool(pin_all_but=None),
        {
            'fjm': '328751a21ccf58e62e271b8ec042109246127146b5a6a7a85d9d290b6788973a',
            'labels': '44ac4ace5c33a058b4ad637b5f5e9b308612fbf533596695944865bc51d78a62',
            'census': '20d6836eccf20d893928fb4207643af6cb391fb73d48d8b2d6934f42ff8214b3',
            'stats': {
                'allocated': 416,
                'deferred': 416,
                'ungrouped': 0,
                'declined': 0,
                'groups': 0,
                'pinned_words': 0,
                'broken_groups': 0,
                'unpins': {'the pointer runtime': 41},
                'heat': {
                    'hot_groups': 0,
                    'hot_groups_missing': 0,
                    'hot_groups_ambiguous': 0,
                    'hot_sites_listed': 0,
                    'hot_sites_matched': 0,
                    'hot_sites_width_changed': 0,
                },
                'overflowed': 0,
                'end': 2382365696,
            },
        },
    ),
    'overflow': (
        lambda heat: BlockPool(
            W,
            POOL_BASE,
            pool_end=POOL_BASE + 0x278000,
            spread_factor=2,
            spread_min_tables=8,
            max_slot_ops=MAX_SLOT_OPS,
            heat=heat,
            pin_all_but=ALL_BUT_D,
        ),
        {
            'fjm': '4c0d3ce9fcd2d1655d70b939c1fa92b0118df4e9837e07df2fda32cc9c78967f',
            'labels': '08fc67ca5ffa1e60561c0e19ab8b3ce45935a06e5a22dd5d2ca227d7044c41dd',
            'census': '20d6836eccf20d893928fb4207643af6cb391fb73d48d8b2d6934f42ff8214b3',
            'stats': {
                'allocated': 306,
                'deferred': 416,
                'ungrouped': 0,
                'declined': 0,
                'groups': 7,
                'pinned_words': 7,
                'broken_groups': 0,
                'unpins': {},
                'heat': {
                    'hot_groups': 3,
                    'hot_groups_missing': 0,
                    'hot_groups_ambiguous': 0,
                    'hot_sites_listed': 9,
                    'hot_sites_matched': 9,
                    'hot_sites_width_changed': 0,
                },
                'overflowed': 110,
                'end': 2150017024,
            },
        },
    ),
    'defaults': (
        lambda heat: BlockPool(W, POOL_BASE, pin_all_but=ALL_BUT_D),
        {
            'fjm': '0e4a821802c1075e2eba8f89641f74b48bcea5d862befaab9ddfb3b1ab1a54ed',
            'labels': 'd18b90a2a771ce9a098d737caccb050abd3788bb81a6dbc659a20e4dbce2f632',
            'census': '20d6836eccf20d893928fb4207643af6cb391fb73d48d8b2d6934f42ff8214b3',
            'stats': {
                'allocated': 400,
                'deferred': 400,
                'ungrouped': 0,
                'declined': 16,
                'groups': 41,
                'pinned_words': 25,
                'broken_groups': 16,
                'unpins': {},
                'heat': {
                    'hot_groups': 0,
                    'hot_groups_missing': 0,
                    'hot_groups_ambiguous': 0,
                    'hot_sites_listed': 0,
                    'hot_sites_matched': 0,
                    'hot_sites_width_changed': 0,
                },
                'overflowed': 0,
                'end': 2148816896,
            },
        },
    ),
}


@pytest.mark.parametrize('config', sorted(EXTRA))
def test_golden_extra(tmp_path: Path, heat: Heat, config: str) -> None:
    make, golden = EXTRA[config]
    pool = make(heat)
    digests = _digests(tmp_path, heat, pool)
    digests['stats']['overflowed'] = pool.overflowed
    digests['stats']['end'] = pool.end
    print(json.dumps(digests, indent=1))
    assert digests['stats']['allocated'] >= 200
    assert digests == golden


def test_golden_program_runs_as_without_the_pool(tmp_path: Path, heat: Heat) -> None:
    outputs = []
    for name, pool in (('plain', None), ('pooled', _pool(heat))):
        _assemble(tmp_path, name, pool)
        io_device = FixedIO(b'')
        fjm_run.run(tmp_path / f'{name}.fjm', io_device=io_device, print_time=False)
        outputs.append(io_device.get_output(allow_incomplete_output=True))
    assert outputs[0] == outputs[1] and outputs[0]


def test_golden_negative_control_spread(tmp_path: Path, heat: Heat) -> None:
    """R9: another spread is another layout - the golden digests must see it"""
    digests = _digests(tmp_path, heat, spread_factor=1)
    assert digests['fjm'] != GOLDEN['shipped']['fjm'] and digests['labels'] != GOLDEN['shipped']['labels']


def test_golden_negative_control_heat_order(tmp_path: Path, heat: Heat) -> None:
    """R9: the heat list's order ranks the hot groups and their sites - reversed, the layout changes"""
    reversed_heat = {group: sites[::-1] for group, sites in reversed(list(heat.items()))}
    digests = _digests(tmp_path, reversed_heat)
    assert digests['fjm'] != GOLDEN['shipped']['fjm'] and digests['labels'] != GOLDEN['shipped']['labels']


def test_golden_negative_control_raw_span(tmp_path: Path, heat: Heat) -> None:
    """R9: a raw program's tables take the cheapest addresses of a region 256 times their slots - in a span with no
    room for that, of their slots alone: the layout changes"""
    make, golden = EXTRA['flat_raw']
    probe = make(heat)
    _digests(tmp_path, heat, probe)  # where the pool goes: the first power of two above the program
    pool = BlockPool(W, pool_end=probe.pool_base + (1 << 24))  # 256 times the slots: 2^27.8 bits; the slots: 2^20
    digests = _digests(tmp_path, heat, pool)
    assert digests['fjm'] != golden['fjm'] and digests['labels'] != golden['labels']
