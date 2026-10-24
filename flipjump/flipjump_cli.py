"""
the command-line interface (the `fj` command).
parses the command-line arguments, prepares temporary files, and drives the
assemble and/or run flows according to the chosen --asm / --run options.
"""

import argparse
import lzma
import os
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Tuple, List, Callable, Optional

from flipjump import flipjump_quickstart
from flipjump.assembler import assembler
from flipjump.assembler.table_pool import BlockPool
from flipjump.fjm.fjm_consts import (
    FJMVersion,
    SUPPORTED_VERSIONS_NAMES,
    IO_MODE_NAMES,
    bad_io_mode_message,
    bad_run_flags_message,
)
from flipjump.fjm.fjm_reader import read_fjm_header
from flipjump.interpreter.run_defaults import io_mode_name
from flipjump.fjm.fjm_writer import Writer
from flipjump.interpreter.io_devices.cli_devices import IO_MODES, make_io_device, split_io_mode
from flipjump.utils.constants import (
    LAST_OPS_DEBUGGING_LIST_DEFAULT_LENGTH,
    DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    FLIPJUMP_VERSION,
)
from flipjump.utils.exceptions import FlipJumpFlatMemoryException, FlipJumpReadFjmException, IODeviceException
from flipjump.utils.functions import get_file_tuples, get_temp_directory_suffix

ErrorFunc = Callable[[str], None]


def verify_file_exists(error_func: ErrorFunc, path: Path) -> None:
    """
    verify that the file exists.
    @param error_func: the parser's error function
    @param path: the file's path
    """
    if not path.is_file():
        error_func(f'file {path} does not exist.')


def verify_fj_files(error_func: ErrorFunc, file_tuples: List[Tuple[str, Path]]) -> None:
    """
    verify that all files exist and with the right suffix.
    @param error_func: the parser's error function
    @param file_tuples: a list of file-tuples - (file_short_name, file_path)
    """
    for _, path in file_tuples:
        verify_file_exists(error_func, path)
        if '.fj' != path.suffix:
            error_func(f'file {path} is not a .fj file.')


def verify_fjm_file(error_func: ErrorFunc, path: Path) -> None:
    """
    verify that this file exists and with the right suffix.
    @param error_func: the parser's error function
    @param path: the file's path
    """
    verify_file_exists(error_func, path)
    if '.fjm' != path.suffix:
        error_func(f'file {path} is not a .fjm file.')


def get_files_paths(
    args: argparse.Namespace, error_func: ErrorFunc, temp_dir_name: str
) -> Tuple[Optional[Path], Path, Path]:
    """
    generate the files paths from args, and create temp paths under temp_dir_name if necessary.
    @param args: the parsed arguments
    @param error_func: parser's error function
    @param temp_dir_name: the temp directory's name
    @return: the path of the debug-file, the (to-be-compiled) fjm, and the input fjm
    """
    out_fjm_path = get_fjm_file_path(args, error_func, temp_dir_name)
    debug_path = get_debug_file_path(args, error_func, temp_dir_name)
    in_fjm_path = Path(args.files[0]) if args.run else out_fjm_path

    return debug_path, in_fjm_path, out_fjm_path


def run(in_fjm_path: Path, debug_file: Optional[Path], args: argparse.Namespace, error_func: ErrorFunc) -> None:
    """
    prepare and verify arguments and io_device, and run the .fjm program.
    @param in_fjm_path: the input .fjm-file path
    @param debug_file: the debug-file path
    @param args: the parsed arguments
    @param error_func: the parser's error function
    """
    verify_fjm_file(error_func, in_fjm_path)
    if debug_file:
        verify_file_exists(error_func, debug_file)

    try:
        header = read_fjm_header(in_fjm_path)
    except FlipJumpReadFjmException as read_error:
        error_func(str(read_error).removeprefix('Error: '))
        raise  # error_func exits; re-raise in case a custom error_func returns

    # --io overrides the io mode compiled into the program (--io-mode when it was assembled)
    io_mode = args.io if args.io is not None else io_mode_name(header)
    try:
        io_device = make_io_device(io_mode)
    except IODeviceException as io_device_error:
        error_func(str(io_device_error))
        raise  # error_func exits; re-raise in case a custom error_func returns

    try:
        flipjump_quickstart.debug(
            in_fjm_path,
            debug_file,
            breakpoints_addresses=set(),
            breakpoints=set(args.breakpoint),
            breakpoints_contains=set(args.breakpoint_contains),
            io_device=io_device,
            show_trace=args.trace,
            print_time=not args.silent,
            print_termination=not args.silent,
            last_ops_debugging_list_length=args.debug_ops_list,
            profile=args.profile,
            # the window: --flat-max-words, --flat-memory, --run-flags, else the program's own (run_defaults)
            flat_max_words=args.flat_max_words,
            flat_memory=getattr(args, 'flat_memory', False),
            run_flags=args.run_flags,
        )
    except FlipJumpFlatMemoryException as flat_error:
        sys.exit(str(flat_error))  # what failed, and how to run it anyway (run_defaults.flat_memory_failure_message)
    finally:
        io_device.close()  # the device this run created (e.g. the pc window), also when the run raised


def get_version(version: Optional[int], is_outfile_specified: bool, error_func: ErrorFunc) -> FJMVersion:
    """
    @param version: the fjm version. if None the default version will be taken.
    @param is_outfile_specified: if True, the default is the compressed-version.
     else, the default is the normal version.
    @param error_func: the parser's error function (used to report an invalid version).
    @return: the chosen version, or default if not specified.
    """
    if version is not None:
        if version not in {v.value for v in SUPPORTED_VERSIONS_NAMES}:
            supported = ', '.join(f"{v.value}: {name}" for v, name in SUPPORTED_VERSIONS_NAMES.items())
            error_func(f'invalid fjm version {version}. supported versions are: {supported}.')
        return FJMVersion(version)

    if is_outfile_specified:
        return FJMVersion.CompressedVersion
    return FJMVersion.NormalVersion


def assemble(
    out_fjm_file: Path,
    debug_file: Optional[Path],
    args: argparse.Namespace,
    error_func: ErrorFunc,
    defines_file: Optional[Path] = None,
) -> None:
    """
    prepare and verify arguments, and assemble the .fj files.
    @param out_fjm_file: the to-be-compiled .fjm-file path
    @param debug_file: the debug-file path
    @param args: the parsed arguments
    @param error_func: the parser's error function
    """
    file_tuples = get_file_tuples(args.files, no_stl=args.no_stl)
    if defines_file is not None:
        # BEFORE the stl, not merely before the user files. The parser substitutes a
        # constant's value where it is USED, at parse time, so an override read after the
        # stl arrives too late for anything the stl itself computes from that constant
        # (hex.pointers.ptr_init sizes its decoder table from PTR_CELL_BITS this way).
        # A side effect worth knowing: this puts a non-stl file first, so the parser's
        # stl-prefix cache is disabled for the whole run -- which is exactly right, since
        # a cached prefix was parsed without the override.
        file_tuples.insert(0, ('d1', defines_file))
    verify_fj_files(error_func, file_tuples)

    version = get_version(args.version, args.outfile is not None, error_func)
    if version == FJMVersion.BaseVersion and (args.run_flags or args.io_mode):
        error_func('fjm version 0 has no header room for --run-flags / --io-mode; use version 1 or above.')
    fjm_writer = Writer(
        out_fjm_file,
        args.width,
        version,
        run_flags=args.run_flags if args.run_flags is not None else 0,
        io_mode=args.io_mode,
        lzma_preset=args.lzma_preset,
    )
    # right above the program, ending below the flat window: --flat-max-words, else the automatic-flat threshold
    table_pool = BlockPool.for_flat(args.width, args.flat_max_words) if args.pool_tables else None
    assembler.assemble(
        file_tuples,
        args.width,
        fjm_writer,
        warning_as_errors=args.werror,
        debugging_file_path=debug_file,
        show_statistics=args.stats,
        print_time=not args.silent,
        max_recursion_depth=args.max_recursion_depth,
        defines_file=defines_file,
        table_pool=table_pool,
    )
    if table_pool is not None and not args.silent:
        print(f'  table pool:      {table_pool.allocated} tables moved, based at {table_pool.base_reason}')


def get_fjm_file_path(args: argparse.Namespace, error_func: ErrorFunc, temp_dir_name: str) -> Path:
    """
    get the output-fjm path from args. If unspecified, create a temporary file under temp_dir_name.
    @param args: the parsed arguments
    @param error_func: the parser's error function
    @param temp_dir_name: a temporary directory that files can safely be created in
    @return: the output-fjm path
    """
    out_fjm_file = args.outfile

    if out_fjm_file is None:
        if args.asm:
            error_func('assemble-only is used, but no outfile is specified.')
        out_fjm_file = os.path.join(temp_dir_name, 'out.fjm')
    elif not args.run and not out_fjm_file.endswith('.fjm'):
        error_func(f'output file {out_fjm_file} is not a .fjm file.')

    return Path(out_fjm_file)


def get_debug_file_path(args: argparse.Namespace, error_func: ErrorFunc, temp_dir_name: str) -> Optional[Path]:
    """
    get the debug-file path from args. If unspecified, create a temporary file under temp_dir_name.
    @param args: the parsed arguments
    @param error_func: the parser's error function
    @param temp_dir_name: a temporary directory that files can safely be created in
    @return: the debug-file path. If debug flag isn't set, and it's unneeded, return None
    """
    debug_file: Optional[str] = args.debug  # can be None, '' (should be temp), or path_string
    debug_file_needed = not args.asm and any((args.breakpoint, args.breakpoint_contains))

    if debug_file is None and debug_file_needed:
        if not args.silent:
            parser_warning = 'Parser Warning - breakpoints are used but the debugging flag (-d) is not specified.'
            if args.werror:
                error_func(parser_warning)
            print(f"{parser_warning} Debugging data will be saved.")
        debug_file = ''

    if debug_file == '':
        if args.asm:
            error_func('assemble-only is used with the debug flag, but no debug file is specified.')
        if args.run:
            error_func('run-only is used with the debug flag, but no debug file is specified.')
        debug_file = os.path.join(temp_dir_name, 'debug.fjd')

    if debug_file is None:
        return None

    return Path(debug_file)


def add_run_only_arguments(parser: argparse.ArgumentParser) -> None:
    """
    add the arguments that are usable in run time.
    @param parser: the parser
    """

    def _check_int_positive(value: str) -> int:
        int_value = int(value)
        if int_value <= 0:
            raise argparse.ArgumentTypeError(f"{value} is an invalid positive int value")
        return int_value

    run_arguments = parser.add_argument_group('run arguments', 'Ignored when using the --assemble option')

    run_arguments.add_argument(
        '--debug-ops-list',
        metavar='LENGTH',
        type=_check_int_positive,
        default=None,
        help=f"show the last LENGTH executed opcodes when a run ends abnormally. off by default "
        f"(tracking them slows the engine); auto-enabled (length {LAST_OPS_DEBUGGING_LIST_DEFAULT_LENGTH}) "
        f"when debugging/tracing/profiling. pass a value to force it on for a normal run.",
    )

    run_arguments.add_argument('-t', '--trace', help="output every running opcode", action='store_true')
    run_arguments.add_argument(
        '--profile',
        help="collect the full per-op statistics (flips/jumps percentages). uses the slower featured run-loop",
        action='store_true',
    )
    flat_window = run_arguments.add_mutually_exclusive_group()
    flat_window.add_argument(
        '--flat-memory',
        help="put the whole program in the native engine's fast flat array: the window is sized to the "
        "end of the .fjm's highest segment (read from its header). costs memory for that whole span "
        "(up to 8 bytes per word, gaps included); if that allocation fails, the run stops (without the "
        "flag it would fall back to the slower memory). without this flag or --flat-max-words, programs "
        "spanning up to 2^24 words get this automatically, as do programs whose run flags ask for it",
        action='store_true',
    )
    flat_window.add_argument(
        '--flat-max-words',
        metavar='N',
        type=_check_int_positive,
        default=None,
        help="the native engine's flat-storage window, in words (by default: the program's span when it "
        "is at most 2^24 words, else 2^23). memory below "
        "the window runs in the fast flat array; segments reaching above it keep the slower paged "
        "mode for that part (hybrid). raising it costs startup time and memory (8 bytes per word of "
        "window), never per-op speed. the FLIPJUMP_FLAT_MAX_WORDS environment variable sets the same limit. "
        "when assembling with --pool-tables, the table pool ends below it",
    )

    def _io_mode(value: str) -> str:
        # the mode name (the first whitespace-separated part) must be registered; any
        # parameters after it are validated by the mode's factory at run time.
        mode_name, _ = split_io_mode(value)
        if mode_name not in IO_MODES:
            raise argparse.ArgumentTypeError(f"unknown --io mode {mode_name!r}. supported: {', '.join(IO_MODES)}")
        return value

    run_arguments.add_argument(
        '--io',
        metavar='MODE',
        default=None,
        type=_io_mode,
        help="the IO device. `standard` - input/output over the terminal. "
        "`pc` - an interactive window: live keyboard input + a scaled screen "
        "(F11 toggles fullscreen, closing it stops the run; needs pygame-ce - "
        "`pip install flipjump[io]`). "
        "by default, the io mode compiled into the .fjm (--io-mode; `standard` unless set)",
    )

    run_arguments.add_argument(
        '-b', '--breakpoint', metavar='NAME', default=[], nargs="+", help="pause when reaching this label"
    )
    run_arguments.add_argument(
        '-B',
        '--breakpoint_contains',
        metavar='NAME',
        default=[],
        nargs="+",
        help="pause when reaching any label containing this",
    )


def add_assemble_only_arguments(parser: argparse.ArgumentParser) -> None:
    """
    add the arguments that are usable in assemble time.
    @param parser: the parser
    """
    asm_arguments = parser.add_argument_group('assemble arguments', 'Ignored when using the --run option')

    asm_arguments.add_argument('-o', '--outfile', metavar='PATH', help="output assembled file")

    asm_arguments.add_argument(
        '-w',
        '--width',
        type=int,
        default=64,
        choices=[8, 16, 32, 64],
        metavar='WIDTH',
        help="specify memory-width. 64 by default",
    )

    supported_versions = ', '.join(f"{version}: {name}" for version, name in SUPPORTED_VERSIONS_NAMES.items())
    asm_arguments.add_argument(
        '-v',
        '--version',
        metavar='VERSION',
        type=int,
        default=None,  # default enforced in get_version()
        help=f"fjm version (default of {FJMVersion.CompressedVersion}-compressed "
        f"if --outfile specified; version {FJMVersion.NormalVersion} otherwise). "
        f"supported versions: {supported_versions}.",
    )

    def _io_mode_number(value: str) -> int:
        number = _parse_number(value)
        bad = bad_io_mode_message(number)
        if bad is not None:
            raise argparse.ArgumentTypeError(bad)
        return number

    asm_arguments.add_argument(
        '--io-mode',
        metavar='NUMBER',
        type=_io_mode_number,
        default=0,
        help="compile the default io device into the .fjm: "
        + ", ".join(f"{number} = {name}" for number, name in IO_MODE_NAMES.items())
        + ". 0 by default. `fj --run --io MODE` still overrides it",
    )

    asm_arguments.add_argument(
        '--lzma_preset',
        type=int,
        default=lzma.PRESET_DEFAULT,
        choices=list(range(10)),
        help=f"The preset used for the LZMA2 algorithm compression ("
        f"{lzma.PRESET_DEFAULT} by default; "
        f"used when version={FJMVersion.CompressedVersion}).",
    )

    asm_arguments.add_argument(
        '--pool-tables',
        action='store_true',
        help="move the tables declared relocatable (the expansions of macros whose innermost namespace is "
        "`__relocatable__`, such as hex.exact_xor's) to addresses that are cheap to arm, so every table dispatch "
        "runs fewer ops. only declared, `$`-free tables move, and the program runs the same: where moving a table "
        "would change an operand that names a label (an offset from another label into or across the table), the "
        "table stays inline, and where pinning a word would, the word is not pinned; an operand that comes too late "
        "to keep its table inline is refused. the pool sits right above the program (at the first power of two "
        "above its end, or, where the pool would not fit below the flat window there, at its end rounded up to its "
        "largest block) and ends below the flat window - --flat-max-words, else the automatic-flat threshold "
        "(2^24 words) - so a program that runs flat without the pool runs flat with it; a table with no room below "
        "it stays inline. it costs a segment per moved table. with it, `__relocatable__` is a reserved namespace, "
        "and a relocatable that uses `$`, declares a label outside its ops, leaves itself through its own label, "
        "holds more than labels and `a;b` ops, or has no power-of-two `pad` right before its call is an error",
    )

    asm_arguments.add_argument('--werror', help="treat all assemble warnings as errors", action='store_true')
    asm_arguments.add_argument(
        '--max_recursion_depth',
        type=int,
        default=DEFAULT_MAX_MACRO_RECURSION_DEPTH,
        help='The compiler supports macros that recursively uses other macros, ' 'up to the this specified depth',
    )
    asm_arguments.add_argument('--no_stl', help="don't assemble/link the standard library files", action='store_true')

    asm_arguments.add_argument(
        '-D',
        '--define',
        action='append',
        default=[],
        metavar='NAME=VALUE',
        help="override a constant the assembled program declares (repeatable). "
        "the overridden declaration is ignored as if it were never written. "
        "NAME is the full name, so a namespaced constant is addressed as a.b.NAME. "
        "overriding a constant that nothing declares is an error, not a new definition",
    )
    asm_arguments.add_argument('--stats', help="show macro code-size statistics", action='store_true')


def _parse_number(value: str) -> int:
    """an int in python syntax (decimal, 0x.., 0b..), or argparse's error naming what is wrong"""
    try:
        return int(value, 0)
    except ValueError:
        pass
    try:
        return int(value, 10)  # e.g. '01', which int(x, 0) refuses
    except ValueError:
        raise argparse.ArgumentTypeError(f'{value!r} is not a number') from None


def _run_flags_number(value: str) -> int:
    number = _parse_number(value)
    bad = bad_run_flags_message(number)
    if bad is not None:
        raise argparse.ArgumentTypeError(bad)
    return number


def add_universal_arguments(parser: argparse.ArgumentParser) -> None:
    """
    add the arguments that are usable in both --asm and --run options.
    @param parser: the parser
    """
    parser.add_argument(
        '--run-flags',
        metavar='NUMBER',
        type=_run_flags_number,
        default=None,
        help="the run flags: 32 bits, all off by default. bit 0 (the value 1) = flat memory, as if "
        "--flat-memory were given. when assembling, they are compiled into the .fjm (0 by default); "
        "when running, they override the ones compiled into it. an explicit --flat-max-words (or the "
        "FLIPJUMP_FLAT_MAX_WORDS environment variable) wins over the flat-memory flag",
    )
    parser.add_argument('files', help="the .fj files to assemble (if run-only, the .fjm file to run)", nargs='+')
    parser.add_argument(
        '-V',
        '--flipjump_version',
        action='version',
        version=f'flipjump {FLIPJUMP_VERSION}',
        help="show the installed flipjump version and exit",
    )
    parser.add_argument(
        '-s', '--silent', action='store_true', help="don't show assemble & run times, and run statistics"
    )
    parser.add_argument(
        '-d',
        '--debug',
        nargs='?',
        const='',
        metavar='PATH',
        type=str,
        help="debug-file path (used for breakpoints). If you both assemble & run, "
        "you may use this option without specifying a path, and a temporary file will be used",
    )


def add_command_arguments(parser: argparse.ArgumentParser) -> None:
    """
    add the mutually exclusive --asm and --run options.
    @param parser: the parser
    """
    action = parser.add_mutually_exclusive_group()
    action.add_argument('-a', '--asm', action='store_true', help="assemble only. Ignores any run-arguments")
    action.add_argument('-r', '--run', action='store_true', help="run only. Ignores any assemble-arguments")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """
    add the parser's arguments.
    @param parser: the parser
    """
    add_command_arguments(parser)
    add_universal_arguments(parser)
    add_assemble_only_arguments(parser)
    add_run_only_arguments(parser)


def get_argument_parser() -> argparse.ArgumentParser:
    """
    create the argument parser (with specific description and usage).
    @return: the argument parser
    """
    return argparse.ArgumentParser(
        description='Assemble and Run FlipJump programs.',
        usage='fj [--asm | --run] [arguments] files [files ...]\n'
        'example usage:\n'
        '  fj  a.fj b.fj                                      // assemble and run\n'
        '  fj  a.fj b.fj  -o out.fjm                          // assemble save and run\n'
        '  fj  code.fj  -d  -B swap_start exit_label          // assemble and debug\n\n'
        '  fj --asm  -o o.fjm a.fj  -d dir/debug.fjd          // assemble and save debug info\n'
        '  fj --asm  -o out.fjm  a.fj b.fj  --no_stl  -w 32   '
        '// assemble without the standard library, 32 bit memory\n\n'
        '  fj --run  prog.fjm                                 // just run\n'
        '  fj --run  o.fjm  -d dir/debug.fjd  -B label        // run and debug\n ',
        epilog="note: the run-time printed on the 'Finished by ...' line is compute-only - "
        "time spent in IO (input/output) is excluded.",
    )


def parse_arguments(*, cmd_line_args: Optional[List[str]] = None) -> Tuple[argparse.Namespace, ErrorFunc]:
    """
    parse the command line arguments.
    @param cmd_line_args: if specified, the command line arguments will be retrieved from this list.
    @return: the parsed arguments, and the parser's error function
    """
    parser = get_argument_parser()
    add_arguments(parser)
    parsed_args = parser.parse_args(args=cmd_line_args)

    return parsed_args, parser.error


_DEFINE_NAME_RE = re.compile(r'[a-zA-Z_][a-zA-Z_0-9]*(\.[a-zA-Z_][a-zA-Z_0-9]*)*$')


def add_defines_file(args: argparse.Namespace, temp_dir_name: str, error_func: ErrorFunc) -> Optional[Path]:
    """
    write the --define overrides into a temporary .fj file, and put it first among the input files.
    it lands BEFORE the stl, so that a constant the stl itself uses is overridden before the stl
    is parsed. A define's VALUE may therefore use `w` (a parser builtin) and an earlier define,
    but not an stl constant such as dw -- write 2*w instead.

    The file is ordinary .fj, which is what keeps the whole expression language available to a
    define. What makes its constants OVERRIDES rather than declarations is that the parser is told
    which file this is - see FJParser.statement.

    A dotted NAME is wrapped in its namespaces, because a constant's full name is the only way to
    address it: `-D hex.pointers.PTRSIZE=12` becomes `ns hex { ns pointers { PTRSIZE = 12 } }`, so
    a bare `-D PTRSIZE=12` does NOT reach it.

    @param args: the parsed arguments
    @param temp_dir_name: the temp directory to write the defines file into
    @param error_func: parser's error function
    @return: the path of the defines file, or None when there are no defines
    """
    if not args.define:
        return None
    lines = []
    for define in args.define:
        if '=' not in define:
            error_func(f"-D expects NAME=VALUE, got {define!r}")
        name, _, value = define.partition('=')
        name = name.strip()
        if not _DEFINE_NAME_RE.match(name):
            error_func(f"-D expects NAME=VALUE with a (possibly dotted) NAME, got {define!r}")
        *namespaces, base_name = name.split('.')
        opening = "".join(f"ns {namespace} {{\n" for namespace in namespaces)
        closing = "\n}" * len(namespaces)
        lines.append(f"{opening}{base_name} ={value}{closing}\n")
    defines_path = Path(temp_dir_name) / '_defines.fj'
    defines_path.write_text(''.join(lines), encoding='utf-8')
    return defines_path


def execute_assemble_run(args: argparse.Namespace, error_func: ErrorFunc) -> None:
    """
    prepare temp files, and execute the run and assemble functions.
    @param args: the parsed arguments
    @param error_func: parser's error function
    """
    with TemporaryDirectory(suffix=get_temp_directory_suffix(args.files)) as temp_dir_name:
        debug_path, in_fjm_path, out_fjm_path = get_files_paths(args, error_func, temp_dir_name)

        if not args.run:
            defines_path = add_defines_file(args, temp_dir_name, error_func)
            assemble(out_fjm_path, debug_path, args, error_func, defines_path)

        if not args.asm:
            run(in_fjm_path, debug_path, args, error_func)


def assemble_run_according_to_cmd_line_args(*, cmd_line_args: Optional[List[str]] = None) -> None:
    """
    parse the command line arguments, prepare temp files, and execute the assemble() / run() functions
     (the command line arguments may indicate to execute only one of them, or to execute both).
    @param cmd_line_args: if specified, the command line arguments will be retrieved from this list.
    @note: call with cmd_line_args=['-h'] to get help.
    """
    args, error_func = parse_arguments(cmd_line_args=cmd_line_args)
    execute_assemble_run(args, error_func)


def main() -> None:
    assemble_run_according_to_cmd_line_args()


if __name__ == '__main__':
    main()
