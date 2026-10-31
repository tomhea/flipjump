"""
the command-line interface (the `fj` command).
parses the command-line arguments, prepares temporary files, and drives the
assemble and/or run flows according to the chosen --asm / --run options.
"""

import argparse
import lzma
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, Tuple, List, Callable, Optional

from flipjump import flipjump_quickstart
from flipjump.assembler import assembler
from flipjump.assembler.table_pool import BlockPool, UnpinnedCell
from flipjump.fjm.fjm_consts import (
    FJMVersion,
    SUPPORTED_VERSIONS_NAMES,
    IO_MODE_NAMES,
    bad_io_mode_message,
    bad_run_flags_message,
)
from flipjump.fjm.fjm_reader import read_fjm_header
from flipjump.fjm.fjm_writer import Writer
from flipjump.interpreter.io_devices.cli_devices import IO_MODES, make_io_device, split_io_mode
from flipjump.utils.constants import (
    LAST_OPS_DEBUGGING_LIST_DEFAULT_LENGTH,
    DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    FLIPJUMP_VERSION,
)
from flipjump.utils.exceptions import FlipJumpFlatMemoryException, FlipJumpReadFjmException, IODeviceException
from flipjump.utils.functions import bad_define_message, get_file_tuples, get_temp_directory_suffix

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
    io_mode = args.io if args.io is not None else IO_MODE_NAMES[header.io_mode]
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
            flat_memory=args.flat_memory,
            run_flags=args.run_flags,
        )
    except FlipJumpFlatMemoryException as flat_error:
        sys.exit(str(flat_error))  # what failed, and how to run it anyway (run_defaults.flat_memory_failure_message)
    finally:
        io_device.close()  # the device this run created (e.g. the pc window), also when the run raised


def parse_unpinned_cells(text: str, error_func: ErrorFunc) -> List[UnpinnedCell]:
    """`--pin-all-but a,b:4` -> ['a', ('b', 4)] (see BlockPool's pin_all_but)"""
    cells: List[UnpinnedCell] = []
    for item in filter(None, (part.strip() for part in text.split(','))):
        label, colon, ops = item.partition(':')
        if not colon:
            cells.append(label)
        elif ops.isdigit() and int(ops) > 0:
            cells.append((label, int(ops)))
        else:
            error_func(f'--pin-all-but expects LABEL or LABEL:OPS (OPS a positive number), got {item!r}')
    return cells


def get_version(version: Optional[int], is_outfile_specified: bool, error_func: ErrorFunc) -> FJMVersion:
    """
    @param version: the fjm version. if None the default version will be taken.
    @param is_outfile_specified: if True, the default is the compressed-segments version (4).
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
        return FJMVersion.CompressedSegmentsVersion
    return FJMVersion.NormalVersion


def assemble(
    out_fjm_file: Path,
    debug_file: Optional[Path],
    args: argparse.Namespace,
    error_func: ErrorFunc,
) -> None:
    """
    prepare and verify arguments, and assemble the .fj files.
    @param out_fjm_file: the to-be-compiled .fjm-file path
    @param debug_file: the debug-file path
    @param args: the parsed arguments
    @param error_func: the parser's error function
    """
    file_tuples = get_file_tuples(args.files, no_stl=args.no_stl)
    verify_fj_files(error_func, file_tuples)
    defines = parse_defines(args.define, error_func)

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
    if args.pin_all_but is not None and not args.pool_tables:
        error_func('--pin-all-but needs --pool-tables.')
    pin_all_but = None if args.pin_all_but is None else parse_unpinned_cells(args.pin_all_but, error_func)
    table_pool = BlockPool.for_flat(args.width, pin_all_but) if args.pool_tables else None
    assembler.assemble(
        file_tuples,
        args.width,
        fjm_writer,
        warning_as_errors=args.werror,
        debugging_file_path=debug_file,
        show_statistics=args.stats,
        print_time=not args.silent,
        max_recursion_depth=args.max_recursion_depth,
        defines=defines,
        table_pool=table_pool,
    )
    if table_pool is not None and not args.silent:
        print(f'  table pool:      {table_pool.deferred} tables moved, from {hex(table_pool.pool_base)}')


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
        "mode for that part (hybrid). raising it costs startup time and memory (4 or 8 bytes per word "
        "of window, w<=32 / w=64), never per-op speed. the FLIPJUMP_FLAT_MAX_WORDS environment variable "
        "sets the same limit",
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
        help=f"fjm version (default of {FJMVersion.CompressedSegmentsVersion.value} - the data and the segment "
        f"table compressed - if --outfile specified; version {FJMVersion.NormalVersion.value} otherwise). "
        f"supported versions: {supported_versions}.",
    )

    asm_arguments.add_argument(
        '--io-mode',
        metavar='NUMBER',
        type=_checked_number(bad_io_mode_message),
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
        f"used by versions {FJMVersion.CompressedVersion.value} and {FJMVersion.CompressedSegmentsVersion.value}).",
    )

    asm_arguments.add_argument(
        '--pool-tables',
        action='store_true',
        help="move the stl's lookup tables (the `__relocatable__` macros) to blocks right above the program, where "
        "they are cheap to arm: fewer ops per table dispatch, the same program",
    )

    asm_arguments.add_argument(
        '--pin-all-but',
        metavar='LABEL[:OPS],...',
        help="with --pool-tables: pin every dispatch word (fewer ops) but the words of these cells - the cells the "
        "program reads or writes raw, through a pointer. Each is a LABEL (the cell runs to the next label) or "
        "LABEL:OPS. Leaving such a cell out makes the program run wrong. Without this flag, a program that uses "
        "pointers pins nothing",
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


def _checked_number(bad_message: Callable[[int], Optional[str]]) -> Callable[[str], int]:
    """an argparse type: a number (_parse_number) that bad_message (fjm_consts.bad_*_message) accepts"""

    def checked_number(value: str) -> int:
        number = _parse_number(value)
        bad = bad_message(number)
        if bad is not None:
            raise argparse.ArgumentTypeError(bad)
        return number

    return checked_number


def add_universal_arguments(parser: argparse.ArgumentParser) -> None:
    """
    add the arguments that are usable in both --asm and --run options.
    @param parser: the parser
    """
    parser.add_argument(
        '--run-flags',
        metavar='NUMBER',
        type=_checked_number(bad_run_flags_message),
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


def parse_defines(define_args: List[str], error_func: ErrorFunc) -> Dict[str, str]:
    """
    `-D NAME=VALUE ...` -> {NAME: VALUE}, the defines of assembler.assemble (a NAME given again takes the later VALUE).
    @param define_args: the -D arguments, in order
    @param error_func: parser's error function
    @return: the defines
    """
    defines: Dict[str, str] = {}
    for define in define_args:
        if '=' not in define:
            error_func(f"-D expects NAME=VALUE, got {define!r}")
        name, _, value = (part.strip() for part in define.partition('='))
        bad = bad_define_message(name, value)
        if bad is not None:
            error_func(f"-D expects NAME=VALUE: {bad}")
        defines[name] = value
    return defines


def execute_assemble_run(args: argparse.Namespace, error_func: ErrorFunc) -> None:
    """
    prepare temp files, and execute the run and assemble functions.
    @param args: the parsed arguments
    @param error_func: parser's error function
    """
    with TemporaryDirectory(suffix=get_temp_directory_suffix(args.files)) as temp_dir_name:
        debug_path, in_fjm_path, out_fjm_path = get_files_paths(args, error_func, temp_dir_name)

        if not args.run:
            assemble(out_fjm_path, debug_path, args, error_func)

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
