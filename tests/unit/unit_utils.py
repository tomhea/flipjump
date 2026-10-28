"""
shared helpers for the assembler/interpreter unit-tests.

these are thin wrappers over the public flipjump API for assembling small inline .fj
sources (no stl) to a .fjm path, getting a Reader for the result, or running it with a
FixedIO - so the individual test files stay short and focused.
"""

import struct
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

import pytest

from flipjump import assemble
from flipjump.assembler.inner_classes.expr import Expr
from flipjump.assembler.table_pool import TablePool
from flipjump.fjm.fjm_consts import FJMVersion, _header_base_size
from flipjump.fjm.fjm_reader import GarbageHandling, Reader
from flipjump.interpreter import fjm_run
from flipjump.interpreter.fjm_run import TerminationStatistics
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.utils.constants import DEFAULT_MAX_MACRO_RECURSION_DEPTH

try:
    from flipjump.interpreter import _fjcore  # type: ignore[attr-defined]
except ImportError:
    _fjcore = None

# skip marker for tests that need the compiled native engine
native_engine_required = pytest.mark.skipif(_fjcore is None, reason='the native engine (_fjcore) is not built')


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROGRAMS_DIR = REPO_ROOT / 'programs'
HELLO_NO_STL = PROGRAMS_DIR / 'print_tests' / 'hello_no-stl.fj'
CAT_PROGRAM = PROGRAMS_DIR / 'print_tests' / 'cat.fj'

HELLO_WORLD_OUTPUT = b'Hello, World!'

# a minimal no-stl preamble (taken from hello_no-stl.fj) that places the IO output-cell at
# address 2w and jumps over it to code_start. it's required before any real op in a no-stl
# program, and exposes the global label IO (for output via "IO + bit;").
MINIMAL_STARTUP = """
def startup @ code_start > IO {
    ;code_start
  IO:
    ;0
  code_start:
}
"""

# a no-stl program that loops forever (a clean Looping termination).
INFINITE_LOOP_PROGRAM = MINIMAL_STARTUP + '\nstartup\nloop:\n;loop\n'


def _sources_list(source: Union[str, List[str]]) -> List[str]:
    return [source] if isinstance(source, str) else source


def assemble_to_path(
    source: Union[str, List[str]],
    tmp_path: Path,
    *,
    memory_width: int = 64,
    use_stl: bool = False,
    fjm_version: FJMVersion = FJMVersion.NormalVersion,
    warning_as_errors: bool = True,
    max_recursion_depth: int = DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    with_debug: bool = False,
) -> Path:
    """
    write the inline .fj source(s) under tmp_path and assemble them into an out.fjm.
    @param source: a single .fj source string, or a list of them (assembled in order).
    @return: the path of the produced .fjm file.
    """
    fj_paths: List[Path] = []
    for index, single_source in enumerate(_sources_list(source)):
        fj_path = tmp_path / f'prog{index}.fj'
        fj_path.write_text(single_source)
        fj_paths.append(fj_path)

    fjm_path = tmp_path / 'out.fjm'
    debug_path = tmp_path / 'debug.fjd' if with_debug else None
    assemble(
        fj_paths,
        fjm_path,
        memory_width=memory_width,
        use_stl=use_stl,
        fjm_version=fjm_version,
        warning_as_errors=warning_as_errors,
        max_recursion_depth=max_recursion_depth,
        debugging_file_path=debug_path,
        print_time=False,
    )
    return fjm_path


def compile_and_get_reader(
    source: Union[str, List[str]],
    tmp_path: Path,
    *,
    memory_width: int = 64,
    use_stl: bool = False,
    fjm_version: FJMVersion = FJMVersion.NormalVersion,
    garbage_handling: GarbageHandling = GarbageHandling.Stop,
) -> Reader:
    """assemble the inline source and return a Reader of the produced .fjm."""
    fjm_path = assemble_to_path(source, tmp_path, memory_width=memory_width, use_stl=use_stl, fjm_version=fjm_version)
    return Reader(fjm_path, garbage_handling=garbage_handling)


def run_source(
    source: Union[str, List[str]],
    tmp_path: Path,
    fixed_input: bytes = b'',
    *,
    memory_width: int = 64,
    use_stl: bool = False,
) -> Tuple[TerminationStatistics, FixedIO]:
    """assemble the inline source, run it with a FixedIO, and return (statistics, io_device)."""
    fjm_path = assemble_to_path(source, tmp_path, memory_width=memory_width, use_stl=use_stl)
    io_device = FixedIO(fixed_input)
    statistics = fjm_run.run(fjm_path, io_device=io_device, print_time=False)
    return statistics, io_device


class RecordingIO(FixedIO):
    """
    a FixedIO that logs every device call, with the count of output bits it had received at each input call:
    ('bit', b), ('byte', B), ('read', b, outs).
    """

    def __init__(self, fixed_input: bytes) -> None:
        super().__init__(fixed_input)
        self.events: List[Tuple[Any, ...]] = []
        self.outs = 0

    def read_bit(self) -> bool:
        bit = super().read_bit()
        self.events.append(('read', int(bit), self.outs))
        return bit

    def write_bit(self, bit: bool) -> None:
        self.events.append(('bit', int(bit)))
        self.outs += 1
        super().write_bit(bit)

    def write_byte(self, byte: int) -> None:
        self.events.append(('byte', byte))
        self.outs += 8
        for i in range(8):
            FixedIO.write_bit(self, bool((byte >> i) & 1))


def run_recorded(
    fjm_path: Path, io_device: IODevice, *, native: bool, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> TerminationStatistics:
    """run on the native engine, or on the pure-python fast loop (which calls the device once a bit)."""
    with monkeypatch.context() as patch:
        if native:
            patch.delenv('FLIPJUMP_NO_NATIVE', raising=False)
        else:
            patch.setattr(fjm_run, '_fjcore', None)
        return fjm_run.run(fjm_path, io_device=io_device, print_time=False, **kwargs)


def relocatable(name: str, params: str, start: str, entries: str) -> str:
    """the declaration of the relocatable table `name.__relocatable__.table params`: `start:` then the entries"""
    return (
        f'ns {name} {{\n    ns __relocatable__ {{\n        def table {params} {{\n          {start}:\n'
        + entries
        + '        }\n    }\n}\n'
    )


def build_and_run(
    source: str,
    tmp_path: Path,
    pool: Optional[TablePool],
    name: str = 'p',
    fixed_input: bytes = b'',
    memory_width: int = 32,
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


def sum_expr(*parts: Any) -> Expr:
    """a left-nested `+` tree of label names / ints (as the parser builds `a + b + c`)"""
    expr = Expr(parts[0])
    for part in parts[1:]:
        expr = Expr(('+', (expr, Expr(part))))
    return expr


def high_segment_program(top: int) -> str:
    """a no-stl looping program whose small reserved segment ends 4 words past `top` (its span, at w=32)."""
    return MINIMAL_STARTUP + f'\nstartup\nloop: ;loop\n\nsegment {top} * w\nreserve 4 * w\n'


def patch_run_header(fjm_path: Path, *, run_flags: int = 0, io_mode: int = 0) -> None:
    """rewrite the run flags and the io mode in the .fjm's header."""
    raw = bytearray(fjm_path.read_bytes())
    struct.pack_into('<LL', raw, _header_base_size, run_flags, io_mode)
    fjm_path.write_bytes(bytes(raw))


def spy_engine_runs(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[Optional[int], bool, Optional[str]]]:
    """
    record every fjm_run.run call from now on.
    @return: the list it appends (flat_max_words passed, flat_required passed, the storage mode it ran in) to.
    """
    seen: List[Tuple[Optional[int], bool, Optional[str]]] = []
    real_run = fjm_run.run

    def spy(*args: Any, **kwargs: Any) -> TerminationStatistics:
        statistics = real_run(*args, **kwargs)
        seen.append((kwargs.get('flat_max_words'), kwargs.get('flat_required', False), statistics.storage_mode))
        return statistics

    monkeypatch.setattr(fjm_run, 'run', spy)
    return seen
