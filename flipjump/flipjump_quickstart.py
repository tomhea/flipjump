"""
the high-level programmatic API.
the convenience wrappers for using flipjump from python - assemble, run, debug,
run_test_output, and the combined assemble_and_run / assemble_and_debug /
assemble_and_run_test_output helpers.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Optional, Set

from flipjump.assembler import assembler
from flipjump.interpreter.debugging.breakpoints import get_breakpoint_handler
from flipjump.fjm.fjm_consts import FJMVersion
from flipjump.fjm.fjm_writer import Writer
from flipjump.interpreter import fjm_run
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.interpreter.io_devices.StandardIO import StandardIO
from flipjump.utils.classes import TerminationCause
from flipjump.utils.constants import (
    IO_BYTES_ENCODING,
    DEFAULT_MAX_MACRO_RECURSION_DEPTH,
)
from flipjump.utils.functions import get_file_tuples, get_temp_directory_suffix
from flipjump.interpreter.fjm_run import TerminationStatistics


def assemble(
    fj_file_paths: List[Path],
    output_fjm_path: Path,
    *,
    memory_width: int = 64,
    use_stl: bool = True,
    fjm_version: FJMVersion = FJMVersion.CompressedVersion,
    warning_as_errors: bool = True,
    debugging_file_path: Optional[Path] = None,
    show_statistics: bool = False,
    print_time: bool = True,
    max_recursion_depth: int = DEFAULT_MAX_MACRO_RECURSION_DEPTH,
) -> None:
    """
    runs the assembly pipeline. assembles the input files to a .fjm.
    :param fj_file_paths:[in]: the list of flipjump code files to compile.
    :param output_fjm_path:[out]: the compiled flipjump will be written into this file.
    :param memory_width: the memory-width
    :param use_stl: if True includes the stl fj files into the assembly process.
    :param fjm_version: The fjm version to be used.
    :param warning_as_errors: treat warnings as errors (stop execution on warnings)
    :param debugging_file_path:[out]: is specified, save debug information in this file
    :param show_statistics: if true shows macro-usage statistics
    :param print_time: if true prints the times of each assemble-stage
    :param max_recursion_depth: The compiler supports macros that recursively uses other macros,
    up to the specified recursion depth.

    :note: This is a wrapper function to the assembler.assemble() function.
    """
    file_tuples = get_file_tuples([str(fj_file.absolute()) for fj_file in fj_file_paths], no_stl=not use_stl)
    fjm_writer = Writer(output_fjm_path, memory_width, fjm_version)
    assembler.assemble(
        file_tuples,
        memory_width,
        fjm_writer,
        debugging_file_path=debugging_file_path,
        warning_as_errors=warning_as_errors,
        show_statistics=show_statistics,
        print_time=print_time,
        max_recursion_depth=max_recursion_depth,
    )


def run(
    fjm_path: Path,
    *,
    debugging_file: Optional[Path] = None,
    io_device: Optional[IODevice] = None,
    show_trace: bool = False,
    print_time: bool = True,
    print_termination: bool = True,
    last_ops_debugging_list_length: Optional[int] = None,
    profile: bool = False,
    flat_max_words: Optional[int] = None,
) -> TerminationStatistics:
    """
    runs a .fjm file (with the FlipJump interpreter)
    @param fjm_path:[in]: the path to the .fjm file
    @param debugging_file:[in]: the path to the debugging file
    @param io_device:[in,out]: the device handling input/output. if not specified: Standard IO will be used.
    @param show_trace: if true print every opcode executed
    @param print_time: if true print running times
    @param print_termination: if true print the termination statistics
    @param last_ops_debugging_list_length: The length of the last-ops list
    @param profile: if true collect the full per-op statistics (uses the slower featured run-loop)
    @param flat_max_words: the native engine's flat-storage span limit, in words (2^23 by default).
    only affects native-engine runs - ignored when a pure-python loop runs (e.g. when
    breakpoints/tracing/profiling force the featured loop)
    @return: the run's termination-statistics

    :note: This is a wrapper function to the fjm_run.run() function.
    """
    return debug(
        fjm_path,
        debugging_file,
        breakpoints_addresses=None,
        breakpoints=None,
        breakpoints_contains=None,
        io_device=io_device,
        show_trace=show_trace,
        print_time=print_time,
        print_termination=print_termination,
        last_ops_debugging_list_length=last_ops_debugging_list_length,
        profile=profile,
        flat_max_words=flat_max_words,
    )


def debug(
    fjm_path: Path,
    debugging_file: Optional[Path],
    *,
    breakpoints_addresses: Optional[Set[int]] = None,
    breakpoints: Optional[Set[str]] = None,
    breakpoints_contains: Optional[Set[str]] = None,
    io_device: Optional[IODevice] = None,
    show_trace: bool = False,
    print_time: bool = True,
    print_termination: bool = True,
    last_ops_debugging_list_length: Optional[int] = None,
    profile: bool = False,
    flat_max_words: Optional[int] = None,
) -> TerminationStatistics:
    """
    debugs a .fjm file (with the FlipJump interpreter+debugger)
    @param fjm_path:[in]: the path to the .fjm file
    @param debugging_file:[in]: the path to the debugging file
    @param breakpoints_addresses: a set of breakpoints addresses (will break if the current address is in this set)
    @param breakpoints: a set of breakpoints names (will break if the current label has the exact name of a label
    in this set)
    @param breakpoints_contains: a set of breakpoints mid-names (will break if the current label contains one of
    the labels in this set in its name)
    @param io_device:[in,out]: the device handling input/output. if not specified: Standard IO will be used.
    @param show_trace: if true print every opcode executed
    @param print_time: if true print running times
    @param print_termination: if true print the termination statistics
    @param last_ops_debugging_list_length: The length of the last-ops list
    @param profile: if true collect the full per-op statistics (uses the slower featured run-loop)
    @param flat_max_words: the native engine's flat-storage span limit, in words (2^23 by default).
    only affects native-engine runs - ignored when a pure-python loop runs (e.g. when
    breakpoints/tracing/profiling force the featured loop)
    @return: the run's termination-statistics

    :note: This is a wrapper function to the fjm_run.run() function.
    """
    if io_device is None:
        io_device = StandardIO(True)

    breakpoint_handler = get_breakpoint_handler(
        debugging_file, breakpoints_addresses, breakpoints, breakpoints_contains
    )
    termination_statistics = fjm_run.run(
        fjm_path,
        io_device=io_device,
        show_trace=show_trace,
        print_time=print_time,
        breakpoint_handler=breakpoint_handler if breakpoint_handler.breakpoints else None,
        last_ops_debugging_list_length=last_ops_debugging_list_length,
        profile=profile,
        flat_max_words=flat_max_words,
    )
    if print_termination:
        termination_statistics.print(
            labels_handler=breakpoint_handler, output_to_print=io_device.get_output(allow_incomplete_output=True)
        )

    return termination_statistics


def run_test_output(
    fjm_path: Path,
    fixed_input: bytes,
    expected_output: bytes,
    *,
    expected_termination_cause: TerminationCause = TerminationCause.Looping,
    should_raise_assertion_error: bool = False,
    debugging_file: Optional[Path] = None,
    show_trace: bool = False,
    print_time: bool = True,
    print_termination: bool = True,
    last_ops_debugging_list_length: Optional[int] = None,
) -> bool:
    """
    runs a .fjm file (with the FlipJump interpreter) with the given input, and checks that it finished successfuly and
     generates the expected output.
    @param fjm_path:[in]: the path to the .fjm file
    @param fixed_input: runs the flipjump program with these bytes as input
    @param expected_output: the function checks that the flipjump program output equals to these bytes
    @param expected_termination_cause: the function checks that the flipjump program finished with this
    termination cause
    @param should_raise_assertion_error: in the case that the run finished unexpectedly or with an unexpected output
    - if True it raises the assertion error it failed on, if False it doesn't raise anything and just returns false.
    @param debugging_file:[in]: the path to the debugging file
    @param show_trace: if true print every opcode executed
    @param print_time: if true print running times
    @param print_termination: if true print the termination statistics
    @param last_ops_debugging_list_length: The length of the last-ops list
    @return: True if the run finished successfully, and with the expected output.
    @raises AssertionError: if should_raise_assertion_error, and the run finished unexpectedly or with an
    unexpected output.

    :note: This is a wrapper function to the fjm_run.run() function.

    :note: If you run this function in an external pytest (not the flipjump one),
           In order to see the assertions errors values and mismatches,
           add the next 2 lines to the __init__.py in your test folder:
               import pytest
               pytest.register_assert_rewrite("flipjump.flipjump_quickstart")
    """
    io_device = FixedIO(fixed_input)
    termination_statistics = run(
        fjm_path,
        debugging_file=debugging_file,
        io_device=io_device,
        show_trace=show_trace,
        print_time=print_time,
        print_termination=print_termination,
        last_ops_debugging_list_length=last_ops_debugging_list_length,
    )

    try:
        assert termination_statistics.termination_cause == expected_termination_cause
        assert io_device.get_output(allow_incomplete_output=True).decode(IO_BYTES_ENCODING) == expected_output.decode(
            IO_BYTES_ENCODING
        )
        return True
    except AssertionError as assertion_error:
        if should_raise_assertion_error:
            raise assertion_error
        return False


def assemble_and_run(
    fj_file_paths: List[Path],
    *,
    memory_width: int = 64,
    use_stl: bool = True,
    fjm_version: FJMVersion = FJMVersion.CompressedVersion,
    warning_as_errors: bool = True,
    show_statistics: bool = False,
    print_time: bool = True,
    max_recursion_depth: int = DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    io_device: Optional[IODevice] = None,
    show_trace: bool = False,
    print_termination: bool = True,
    last_ops_debugging_list_length: Optional[int] = None,
) -> TerminationStatistics:
    """
    assembles the .fj files into a temporary .fjm file, then runs it.
    @note: This function combines the functionality of `assemble()` and `run()`.
    For more information about the parameters, see the documentation of these functions.

    @return: the run's termination-statistics.
    """
    return assemble_and_debug(
        fj_file_paths,
        memory_width=memory_width,
        use_stl=use_stl,
        fjm_version=fjm_version,
        warning_as_errors=warning_as_errors,
        show_statistics=show_statistics,
        print_time=print_time,
        max_recursion_depth=max_recursion_depth,
        io_device=io_device,
        show_trace=show_trace,
        print_termination=print_termination,
        last_ops_debugging_list_length=last_ops_debugging_list_length,
    )


def assemble_and_debug(
    fj_file_paths: List[Path],
    *,
    memory_width: int = 64,
    use_stl: bool = True,
    fjm_version: FJMVersion = FJMVersion.CompressedVersion,
    warning_as_errors: bool = True,
    show_statistics: bool = False,
    print_time: bool = True,
    max_recursion_depth: int = DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    breakpoints_addresses: Optional[Set[int]] = None,
    breakpoints: Optional[Set[str]] = None,
    breakpoints_contains: Optional[Set[str]] = None,
    io_device: Optional[IODevice] = None,
    show_trace: bool = False,
    print_termination: bool = True,
    last_ops_debugging_list_length: Optional[int] = None,
) -> TerminationStatistics:
    """
    assembles the .fj files into a temporary .fjm file, then runs and debugs it.
    @note: This function combines the functionality of `assemble()` and `debug()`.
    For more information about the parameters, see the documentation of these functions.

    @return: the run's termination-statistics.
    """
    with TemporaryDirectory(suffix=get_temp_directory_suffix(fj_file_paths)) as temp_dir_name:
        fjm_file = Path(temp_dir_name) / 'out.fjm'
        debug_file = Path(temp_dir_name) / 'debug.fjd'

        assemble(
            fj_file_paths,
            fjm_file,
            memory_width=memory_width,
            use_stl=use_stl,
            fjm_version=fjm_version,
            warning_as_errors=warning_as_errors,
            debugging_file_path=debug_file,
            show_statistics=show_statistics,
            print_time=print_time,
            max_recursion_depth=max_recursion_depth,
        )

        termination_statistics = debug(
            fjm_file,
            debug_file,
            breakpoints_addresses=breakpoints_addresses,
            breakpoints=breakpoints,
            breakpoints_contains=breakpoints_contains,
            io_device=io_device,
            show_trace=show_trace,
            print_time=print_time,
            print_termination=print_termination,
            last_ops_debugging_list_length=last_ops_debugging_list_length,
        )

        return termination_statistics


def assemble_and_run_test_output(
    fj_file_paths: List[Path],
    fixed_input: bytes,
    expected_output: bytes,
    *,
    memory_width: int = 64,
    use_stl: bool = True,
    fjm_version: FJMVersion = FJMVersion.CompressedVersion,
    warning_as_errors: bool = True,
    show_statistics: bool = False,
    print_time: bool = True,
    max_recursion_depth: int = DEFAULT_MAX_MACRO_RECURSION_DEPTH,
    expected_termination_cause: TerminationCause = TerminationCause.Looping,
    should_raise_assertion_error: bool = False,
    show_trace: bool = False,
    print_termination: bool = True,
    last_ops_debugging_list_length: Optional[int] = None,
) -> bool:
    """
    assembles the .fj files into a temporary .fjm file, then runs it with the given input, and checks that
    it finishes successfully and generates the expected output.
    @note: This function combines the functionality of `assemble()` and `test()`.
    For more information about the parameters, see the documentation of these functions.
    @raises AssertionError: if should_raise_assertion_error, and the run finished unexpectedly or with an
    unexpected output.

    @return: True if the run finished successfully, and with the expected output.

    :note: If you run this function in an external pytest (not the flipjump one),
           In order to see the assertions errors values and mismatches,
           add the next 2 lines to the __init__.py in your test folder:
               import pytest
               pytest.register_assert_rewrite("flipjump.flipjump_quickstart")
    """
    with TemporaryDirectory(suffix=get_temp_directory_suffix(fj_file_paths)) as temp_dir_name:
        fjm_file = Path(temp_dir_name) / 'out.fjm'
        debug_file = Path(temp_dir_name) / 'debug.fjd'

        assemble(
            fj_file_paths,
            fjm_file,
            memory_width=memory_width,
            use_stl=use_stl,
            fjm_version=fjm_version,
            warning_as_errors=warning_as_errors,
            debugging_file_path=debug_file,
            show_statistics=show_statistics,
            print_time=print_time,
            max_recursion_depth=max_recursion_depth,
        )

        test_succeeded = run_test_output(
            fjm_file,
            fixed_input,
            expected_output,
            expected_termination_cause=expected_termination_cause,
            should_raise_assertion_error=should_raise_assertion_error,
            debugging_file=debug_file,
            show_trace=show_trace,
            print_time=print_time,
            print_termination=print_termination,
            last_ops_debugging_list_length=last_ops_debugging_list_length,
        )

        return test_succeeded
