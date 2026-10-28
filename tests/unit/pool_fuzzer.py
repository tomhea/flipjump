"""
a differential fuzzer of the table pool: random programs of the stl's table macros - the declared relocatables
and the undeclared table users - assembled without a pool and with several pools, every build run in a
subprocess (a broken pool may never halt), and the runs compared. The relocation proof is asked too
(test_pool_fuzz.py runs it).
"""

import random
import subprocess  # nosec B404 - the fuzzer drives the fj CLI
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pytest

from flipjump import assemble
from flipjump.assembler.fj_parser import parse_macro_tree
from tests.unit.relocation_proof import prove_relocation
from flipjump.assembler.table_pool import BlockPool
from flipjump.utils.functions import get_file_tuples

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# refusals that say why (any other refusal of a pooled build is a finding)
# the refusals a random program may meet: a span too small, a program reaching the pool, and a label distance across
# a moved table's place (the `distance` op)
KNOWN_REFUSALS = ('inside the table pool', 'Raise span_bits', 'cannot hold', 'takes the distance across the place')

_RUNNER = """
import sys
from pathlib import Path
from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
io = FixedIO(b'')
try:
    cause = str(fjm_run.run(Path(sys.argv[1]), io_device=io, print_time=False).termination_cause)
except Exception as e:
    cause = 'EXC ' + type(e).__name__
print(io.get_output(allow_incomplete_output=True).hex(), cause)
"""


def run(fjm: Path, timeout: float) -> Tuple[str, str]:
    """(the output in hex, the termination cause) of a run in a subprocess"""
    try:
        process = subprocess.run(  # nosec B603 - a fixed fj command line
            [sys.executable, '-c', _RUNNER, str(fjm)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired:
        return '', 'TIMEOUT'
    words = process.stdout.split()
    return (words[0], ' '.join(words[1:])) if len(words) >= 2 else ('', ' '.join(words) or 'NO-RESULT')


class Generator:
    """a random program over a few hex[:n] variables, printing every one at the end"""

    def __init__(self, rng: random.Random, memory_width: int):
        self.rng = rng
        self.memory_width = memory_width
        self.digits, self.variables = (2, 3) if memory_width == 16 else (4, 6)
        self.init = memory_width >= 32  # hex.init does not fit below a w=16 pool
        self.labels = 0
        self.uses_init = False

    def var(self) -> str:
        return f'v{self.rng.randrange(self.variables)}'

    def vec(self, n: int) -> str:
        offset = self.rng.randrange(self.digits - n + 1)
        variable = self.var()
        return variable if offset == 0 and self.rng.random() < 0.5 else f'{variable}+{offset}*dw'

    def digit(self) -> str:
        """one digit of a variable, in several spellings of one word (the pool groups by expression)"""
        d = self.rng.randrange(self.digits)
        variable = self.var()
        if d:
            return self.rng.choice([f'{variable}+{d}*dw', f'{variable}+{d * 2}*w', f'{variable}+dw*{d}'])
        return self.rng.choice([variable, f'{variable}+0'])

    def bits(self, hex_digit: str) -> str:
        return ', '.join(f'{hex_digit}+dbit+{k}' for k in (3, 2, 1, 0))

    def label(self) -> str:
        self.labels += 1
        return f'L{self.labels}'

    def two_distinct(self, n: int) -> Tuple[str, str]:
        a, b = self.vec(n), self.vec(n)
        while b.split('+')[0] == a.split('+')[0]:
            b = self.vec(n)
        return a, b

    def op(self) -> str:  # noqa: C901 - one branch per macro
        r = self.rng
        n = r.randint(1, self.digits)
        kinds = ['xor', 'xor1', 'xor_zero', 'double_xor', 'triple', 'quad', 'sparse_xor', 'sparse_mov']
        kinds += ['shl_bit', 'shr_bit', 'shl_hex', 'shr_hex', 'inc', 'dec', 'not', 'neg', 'mov', 'zero', 'if']
        kinds += ['rep_xor', 'set', 'count_bits', 'offset_jump', 'skip', 'set_label', 'distance']
        if self.init:
            kinds += ['add', 'sub', 'or', 'and', 'cmp', 'mul']
        kind = r.choice(kinds)
        if kind == 'xor':
            return f'hex.xor {n}, {self.vec(n)}, {self.vec(n)}'
        if kind == 'xor1':
            return f'hex.xor {self.digit()}, {self.digit()}'
        if kind == 'xor_zero':
            return 'hex.xor_zero {}, {}, {}'.format(n, *self.two_distinct(n))
        if kind == 'double_xor':
            return f'hex.double_xor {self.digit()}, {self.digit()}, {self.digit()}'
        if kind == 'triple':
            bits = ', '.join(self.bits(self.digit()) for _ in range(3))
            return f'hex.triple_exact_xor {bits}, {self.digit()}'
        if kind == 'quad':
            bits = ', '.join(self.bits(self.digit()) for _ in range(4))
            return f'hex.quadrupled_exact_xor {bits}, {self.digit()}'
        if kind == 'sparse_xor':
            return f'hex.sparse_xor {r.choice([16, 32, 64])}, {n}, {self.vec(n)}, {self.vec(n)}'
        if kind == 'sparse_mov':
            return f'hex.sparse_mov {r.choice([16, 32, 64])}, {n}, {self.vec(n)}, {self.vec(n)}'
        if kind in ('shl_bit', 'shr_bit', 'inc', 'dec', 'not', 'neg', 'zero'):
            return f'hex.{kind} {n}, {self.vec(n)}'
        if kind in ('shl_hex', 'shr_hex'):
            return f'hex.{kind} {n}, {r.randint(1, n)}, {self.vec(n)}'
        if kind == 'mov':
            return 'hex.mov {}, {}, {}'.format(n, *self.two_distinct(n))
        if kind == 'set':
            return f'hex.set {self.digit()}, {r.randrange(16)}'
        if kind == 'if':
            zero, nonzero, end = self.label(), self.label(), self.label()
            return (
                f"hex.if {n}, {self.vec(n)}, {zero}, {nonzero}\n{zero}:\n    stl.output 'z'\n    ;{end}\n"
                f"{nonzero}:\n    stl.output 'n'\n{end}:"
            )
        if kind == 'rep_xor':
            return (
                f'rep({r.randint(2, 6 if self.memory_width == 16 else 40)}, i) hex.xor {self.digit()}, {self.digit()}'
            )
        if kind == 'count_bits':
            return 'hex.count_bits 2, {}, {}'.format(*self.two_distinct(2))
        if kind in ('offset_jump', 'skip'):  # `label + dw` / `$ + dw` over one op: inside its piece
            label, wrong, end = self.label(), self.label(), self.label()
            jump = f';{label} + dw\n{label}:' if kind == 'offset_jump' else 'stl.skip'
            return f"{jump}\n    ;{wrong}\n    ;{end}\n{wrong}:\n    stl.output '!'\n{end}:"
        if kind == 'set_label':  # a digit of a label's address: a value
            return f'hex.set {self.digit()}, (L0 >> {r.choice([4, 6, 8])}) & 0xf'
        if kind == 'distance':  # a label distance: across a moved table's place it is refused
            first, second = self.label(), self.label()
            inner = f'hex.xor {self.digit()}, {self.digit()}' if r.random() < 0.5 else f'hex.not 1, {self.digit()}'
            return f'{first}:\n    {inner}\n{second}:\n    hex.set {self.digit()}, (({second} - {first}) >> 6) & 0xf'
        self.uses_init = True
        if kind in ('add', 'sub', 'or', 'and'):
            return 'hex.{} {}, {}, {}'.format(kind, n, *self.two_distinct(n))
        if kind == 'cmp':
            a, b = self.two_distinct(n)
            lt, eq, gt, end = self.label(), self.label(), self.label(), self.label()
            return (
                f"hex.cmp {n}, {a}, {b}, {lt}, {eq}, {gt}\n{lt}:\n    stl.output '<'\n    ;{end}\n"
                f"{eq}:\n    stl.output '='\n    ;{end}\n{gt}:\n    stl.output '>'\n{end}:"
            )
        result, a = self.two_distinct(2)
        b = self.vec(2)
        while b.split('+')[0] in (result.split('+')[0], a.split('+')[0]):
            b = self.vec(2)
        return f'hex.mul 2, {result}, {a}, {b}'

    def program(self) -> str:
        lines = ['stl.startup', 'L0:']
        for _ in range(self.rng.randint(2, 4) if self.memory_width == 16 else self.rng.randint(3, 14)):
            lines.append(self.op())
        for v in range(self.variables):
            lines += [f'hex.print_as_digit {self.digits}, v{v}, 0', "stl.output '\\n'"]
        lines.append('stl.loop')
        lines += [
            f'v{v}: hex.vec {self.digits}, {self.rng.randrange(1 << (4 * self.digits))}' for v in range(self.variables)
        ]
        if self.uses_init:
            lines.append('hex.init')
        return '\n'.join(lines) + '\n'


def pools(memory_width: int, rng: random.Random, source: Path, scratch: Path) -> Dict[str, Callable[[], BlockPool]]:
    """name -> a factory, called in order: `fj --pool-tables`'s pool, one based at half the memory, one with spread
    blocks and width buckets, and one with too small a span (the overflow region)"""
    base = 1 << (memory_width - 1)
    made: Dict[str, BlockPool] = {}

    def cli() -> BlockPool:
        made['cli'] = BlockPool.for_flat(memory_width)
        return made['cli']

    def tight() -> BlockPool:
        used = getattr(made['cli'], '_used', 0) or 64 * 2 * memory_width
        span = max(32 * memory_width, int(used * rng.uniform(0.3, 0.95)) // (2 * memory_width) * 2 * memory_width)
        return BlockPool(memory_width, base, span_bits=span, pin_broken=rng.random() < 0.5)

    def spread_buckets() -> BlockPool:
        return BlockPool(memory_width, base, spread=2, spread_min_count=4, max_slot_ops=512, pin_broken=True)

    return {
        'cli': cli,
        'half': lambda: BlockPool(memory_width, base),
        'spread_buckets': spread_buckets,
        'tight': tight,
    }


def fuzz_case(seed: int, memory_width: int, scratch: Path, timeout: float = 30.0) -> List[str]:
    """the findings of one random program: a pooled run that differs from the plain one, a refusal with no reason,
    and the relocation proof's verdict where it disagrees with the runs. [] when every pool agrees; the case is
    skipped (inconclusive) if the plain run times out."""
    rng = random.Random(seed)  # nosec B311 - deterministic test data, not security
    source = scratch / f'case_{seed}.fj'
    source.write_text(Generator(rng, memory_width).program())
    plain = scratch / 'plain.fjm'
    assemble([source], plain, memory_width=memory_width, print_time=False)
    expected = run(plain, timeout)
    if expected[1] == 'TIMEOUT':  # a generated program always halts: a loaded machine, not a pool's finding
        pytest.skip(f'{seed} w={memory_width}: the plain run timed out - inconclusive')
    findings: List[str] = []
    cli_same: Optional[bool] = None
    for name, make in pools(memory_width, rng, source, scratch).items():
        pooled = scratch / f'{name}.fjm'
        try:
            assemble([source], pooled, memory_width=memory_width, print_time=False, table_pool=make())
        except Exception as refused:  # noqa: BLE001 - a refusal must say why
            if not any(reason in str(refused) for reason in KNOWN_REFUSALS):
                findings.append(f'{seed} w={memory_width} {name}: refused with no reason: {str(refused)[:200]}')
            continue
        same = run(pooled, timeout) == expected
        cli_same = same if name == 'cli' else cli_same
        if not same:
            findings.append(f'{seed} w={memory_width} {name}: the pooled run differs from the plain one')
    if cli_same is not None:
        macros = parse_macro_tree(get_file_tuples([str(source)]), memory_width, True)
        proof = prove_relocation(memory_width, macros, BlockPool.for_flat(memory_width))
        if bool(proof.mismatches) == cli_same:
            findings.append(
                f'{seed} w={memory_width} proof: {proof.mismatches[:1]} where the cli pool runs the same: {cli_same}'
            )
    return findings
