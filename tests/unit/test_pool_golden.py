"""
a golden test of the table pool: a real stl program assembled with a one-pass BlockPool configured as fjdoom's
shipped build is (merged aliases, spread 2, max_slot_ops 512, pin_broken, width buckets, a heat list, a
pin_exclude), whose .fjm, labels and census are frozen by their sha256. A refactor of the pool may not move a byte.

The negative controls perturb a real knob (the spread, the heat list's order) and require the digests to change.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from flipjump import assemble
from flipjump.assembler.table_pool import BlockPool, heat_key
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

Heat = Dict[str, List[Tuple[str, int, int]]]


def _slot_ops(width: int) -> int:
    return 1 << (min(max(width, 1), MAX_SLOT_OPS) - 1).bit_length()


class _Recorder(BlockPool):
    """a pool that records each table's (group, site path, width) as the expansion reaches it"""

    seen: List[Tuple[str, str, int]]

    def defer(  # type: ignore[no-untyped-def]
        self, ops_alignment: int, table_ops: int, group: Optional[str], group_expr, labels_prefix: str = ''
    ) -> str:
        if group is not None:
            self.seen.append((group, labels_prefix, max(table_ops, ops_alignment)))
        return super().defer(ops_alignment, table_ops, group, group_expr, labels_prefix)


# fjdoom's shipped knobs, and a narrow variant whose 32-op tables are too wide (declined, their groups broken)
CONFIGS: Dict[str, Dict[str, Any]] = {
    'shipped': {'spread': 2, 'max_slot_ops': MAX_SLOT_OPS, 'pin_broken': True},
    'narrow': {'spread': 2, 'max_slot_ops': 16, 'pin_broken': False},
}


def _pool(heat: Optional[Heat] = None, cls: type = BlockPool, **knobs: Any) -> BlockPool:
    knobs = {**CONFIGS['shipped'], **knobs}
    pool: BlockPool = cls(
        W,
        POOL_BASE,
        span_bits=SPAN_BITS,
        spread_min_count=8,
        **knobs,
        **({'heat': heat} if heat else {}),
    )
    return pool


def _exclude_d(address: int, labels: Dict[str, int]) -> bool:
    return address == labels['d'] + W  # the cell the pointer reads (its jump word)


def _assemble(tmp_path: Path, name: str, pool: Optional[BlockPool]) -> Tuple[bytes, Dict[str, int]]:
    """the .fjm bytes and the debugging labels of PROGRAM assembled with `pool`"""
    source = tmp_path / f'{name}.fj'
    source.write_text(PROGRAM)
    fjm, debug = tmp_path / f'{name}.fjm', tmp_path / f'{name}.fjd'
    if pool is not None:
        pool.pin_exclude = _exclude_d
    assemble([source], fjm, memory_width=W, print_time=False, table_pool=pool, debugging_file_path=debug)
    return fjm.read_bytes(), load_debugging_labels(debug)


def _heat(tmp_path: Path) -> Heat:
    """a heat list over the three biggest groups: each one's last three sites, hottest first"""
    recorder = _pool(cls=_Recorder)
    assert isinstance(recorder, _Recorder)
    recorder.seen = []
    _assemble(tmp_path, 'record', recorder)
    occurrences: Dict[Tuple[str, str], int] = {}
    sites: Heat = {}
    for group, path, width in recorder.seen:
        canonical = recorder.alias.get(group, group)
        site = heat_key(path)
        occurrence = occurrences.get((canonical, site), 0)
        occurrences[(canonical, site)] = occurrence + 1
        sites.setdefault(canonical, []).append((site, occurrence, _slot_ops(width)))
    biggest = sorted(sites, key=lambda group: (-len(sites[group]), group))[:3]
    return {heat_key(group): sites[group][::-1][:3] for group in biggest}


def _digests(tmp_path: Path, heat: Heat, **knobs: Any) -> Dict[str, Any]:
    pool = _pool(heat, **knobs)
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
    digests = _digests(tmp_path, heat, spread=1)
    assert digests['fjm'] != GOLDEN['shipped']['fjm'] and digests['labels'] != GOLDEN['shipped']['labels']


def test_golden_negative_control_heat_order(tmp_path: Path, heat: Heat) -> None:
    """R9: the heat list's order ranks the hot groups and their sites - reversed, the layout changes"""
    reversed_heat = {group: sites[::-1] for group, sites in reversed(list(heat.items()))}
    digests = _digests(tmp_path, reversed_heat)
    assert digests['fjm'] != GOLDEN['shipped']['fjm'] and digests['labels'] != GOLDEN['shipped']['labels']
