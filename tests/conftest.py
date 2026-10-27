"""
pytest configuration for the flipjump test-suite.
defines the custom command-line options, fixtures, and test-collection logic that build
the compile-and-run test cases from the CSV test catalogs.
"""

import argparse
import csv
import json
from os import environ
from queue import Queue
from threading import Lock
from pathlib import Path
from typing import List, Iterable, Callable, Tuple, Optional, Any, Union

import pytest
from _pytest.config import Config
from _pytest.mark import ParameterSet
from _pytest.nodes import Item, Collector
from _pytest.python import Metafunc
from _pytest.reports import CollectReport

from flipjump.utils.constants import LAST_OPS_DEBUGGING_LIST_DEFAULT_LENGTH
from tests.test_fj import CompileTestArgs, RunTestArgs

TestsType = Tuple[List[ParameterSet], List[ParameterSet]]

build_tests_lock = Lock()
build_tests_queue: "Queue[TestsType]" = Queue()


COMPILE_ARGUMENTS_FIXTURE = "compile_args"
RUN_ARGUMENTS_FIXTURE = "run_args"

fixtures_name_to_type = {
    COMPILE_ARGUMENTS_FIXTURE: CompileTestArgs,
    RUN_ARGUMENTS_FIXTURE: RunTestArgs,
}


TESTS_PATH = Path(__file__).parent
TESTS_TABLES_PATH = TESTS_PATH / 'tests_tables'
with open(TESTS_PATH / 'conf.json', 'r') as tests_json:
    TESTS_OPTIONS = json.load(tests_json)

TEST_TYPES = TESTS_OPTIONS['all_speed_ordered']
assert TEST_TYPES
REGULAR_TYPES = TESTS_OPTIONS['regular_speed_ordered']
assert REGULAR_TYPES
CATALOG_TYPES = TESTS_OPTIONS['catalog_speed_ordered']
assert CATALOG_TYPES
DEFAULT_TYPE = TESTS_OPTIONS['default_type']
assert DEFAULT_TYPE in TEST_TYPES


COMPILE_ORDER_INDEX = 1
RUN_ORDER_INDEX = 2


DEBUG_INFO_FLAG = 'debuginfo'
ALL_FLAG = 'all'
REGULAR_FLAG = 'regular'
CATALOG_FLAG = 'catalog'
COMPILE_FLAG = 'compile'
RUN_FLAG = 'run'
# the --unit-tests flag marks the run as the assembler/interpreter unit-tests (tests/unit),
# which are unrelated to the .fj compile/run tests. config.getoption can't resolve the
# dashed name, so the flag is registered with the explicit UNIT_TESTS_DEST and read by it.
UNIT_TESTS_FLAG = 'unit-tests'
UNIT_TESTS_DEST = 'unit_tests'
NAME_EXACT_FLAG = 'name'
NAME_CONTAINS_FLAG = 'contains'
NAME_STARTSWITH_FLAG = 'startswith'
NAME_ENDSWITH_FLAG = 'endswith'
SAVED_KEYWORDS = {
    ALL_FLAG,
    CATALOG_FLAG,
    COMPILE_FLAG,
    RUN_FLAG,
    UNIT_TESTS_FLAG,
    NAME_EXACT_FLAG,
    NAME_CONTAINS_FLAG,
    NAME_STARTSWITH_FLAG,
    NAME_ENDSWITH_FLAG,
}


def is_parallel_active() -> bool:
    """
    check if parallel is used - xdist works and uses >1 workers.
    @return: is parallel used
    """
    return int(environ.get('PYTEST_XDIST_WORKER_COUNT', default='0')) > 1


def argument_line_iterator(csv_file_path: Path, num_of_args: int) -> Iterable[List[str]]:
    """
    Iterate over the lines (with exact number of parameters)
    @note a line is a stripped argument list
    @param csv_file_path: the csv file
    @param num_of_args: right number of arguments per line
    @return: the line iterator
    """
    with open(csv_file_path, 'r') as csv_file:
        for line_index, line in enumerate(csv.reader(csv_file)):
            if line:
                assert len(line) == num_of_args, (
                    f'expects {num_of_args} args, got {len(line)} '
                    f'(file {Path(csv_file_path).absolute()}, line {line_index + 1})'
                )
                yield list(map(str.strip, line))


def get_compile_tests_params_from_csv(
    csv_file_path: Path, xfail_list: List[str], save_debug_file: bool
) -> List[ParameterSet]:
    """
    read the compile-tests from the csv
    @param csv_file_path: read tests from this csv
    @param xfail_list: list of tests names to mark with xfail (expected to fail)
    @param save_debug_file: should save the debugging info file

    @return: the list of pytest.params(CompileTestArgs, marks=...)
    """
    params = []

    for line in argument_line_iterator(csv_file_path, CompileTestArgs.num_of_csv_line_args):
        args = CompileTestArgs(save_debug_file, *line)
        test_marks = [pytest.mark.run(order=COMPILE_ORDER_INDEX)]
        if args.test_name in xfail_list:
            test_marks.append(pytest.mark.xfail())
        params.append(pytest.param(args, marks=test_marks))

    return params


def get_run_tests_params_from_csv(
    csv_file_path: Path, xfail_list: List[str], save_debug_file: bool, debug_info_length: int
) -> List[ParameterSet]:
    """
    read the run-tests from the csv
    @param csv_file_path: read tests from this csv
    @param xfail_list: list of tests names to mark with xfail (expected to fail)
    @param save_debug_file: If not True: should use the debugging info file,
    @param debug_info_length: show the last {debug_info_length} fj-ops if the program failed running.
    @return: the list of pytest.params(RunTestArgs, marks=...)
    """
    params = []

    for line in argument_line_iterator(csv_file_path, RunTestArgs.num_of_csv_line_args):
        args = RunTestArgs(save_debug_file, debug_info_length, *line)
        test_marks = [pytest.mark.run(order=RUN_ORDER_INDEX)]
        if args.test_name in xfail_list:
            test_marks.append(pytest.mark.xfail())
        params.append(pytest.param(args, marks=test_marks))

    return params


def pytest_addoption(parser: pytest.Parser) -> None:
    """
    add the costume flags to pytest
    @param parser: the parser
    """
    colliding_keywords = set(TEST_TYPES) & SAVED_KEYWORDS
    assert not colliding_keywords

    def _check_int_positive_with_true(value: str) -> Tuple[bool, int]:
        int_value = int(value)
        if int_value <= 0:
            raise argparse.ArgumentTypeError(f"{value} is an invalid positive int value")
        return True, int_value

    # This is a tuple: (should_compile_tests_save_debugging_info, run_tests_debugging_ops_list_length)
    parser.addoption(
        f"--{DEBUG_INFO_FLAG}",
        metavar='LENGTH',
        type=_check_int_positive_with_true,
        nargs='?',
        const=(True, LAST_OPS_DEBUGGING_LIST_DEFAULT_LENGTH),
        default=(False, LAST_OPS_DEBUGGING_LIST_DEFAULT_LENGTH),
        help=f"show the last LENGTH executed opcodes on tests that failed during their run "
        f"({LAST_OPS_DEBUGGING_LIST_DEFAULT_LENGTH} by default). "
        f"(if this option is unspecified"
        f" - the tests will be ~20% faster, and will takes ~30% of their size). "
        f"This option is irrelevant (doesn't show last executed opcodes) on parallel tests.",
    )

    for test_type in TEST_TYPES:
        parser.addoption(f"--{test_type}", action="store_true", help=f"run {test_type} tests")
    for catalog_type in CATALOG_TYPES:
        parser.addoption(
            f"--{catalog_type}",
            action="store_true",
            help=f"run {catalog_type} tests (the demo-catalog; disjoint from --all/--regular/individual type flags)",
        )
    parser.addoption(
        f"--{REGULAR_FLAG}", action="store_true", help=f"run all regular tests ({', '.join(REGULAR_TYPES)})"
    )
    parser.addoption(f"--{ALL_FLAG}", action="store_true", help="run all tests")

    parser.addoption(f"--{COMPILE_FLAG}", action='store_true', help='only test compiling .fj files')
    parser.addoption(f"--{RUN_FLAG}", action='store_true', help='only test running .fjm files')

    parser.addoption(
        f"--{UNIT_TESTS_FLAG}",
        action='store_true',
        dest=UNIT_TESTS_DEST,
        help="run the assembler/interpreter unit-tests (tests/unit). marks this run as unrelated "
        "to the --compile/--run .fj tests, so none of them are collected - which also frees it "
        "to be parallelized with -n auto.",
    )

    parser.addoption(f'--{NAME_EXACT_FLAG}', nargs='+', help='only run tests with one of these names')
    parser.addoption(f'--{NAME_CONTAINS_FLAG}', nargs='+', help='only run tests that contains one of these strings')
    parser.addoption(
        f'--{NAME_STARTSWITH_FLAG}', nargs='+', help='only run tests that starts with one of these strings'
    )
    parser.addoption(f'--{NAME_ENDSWITH_FLAG}', nargs='+', help='only run tests that ends with one of these strings')


def pytest_configure(config: Config) -> None:
    config.addinivalue_line('markers', 'slow: a long unit-test, run only with --slow (the slow tests flag)')


def pytest_runtest_setup(item: Item) -> None:
    if 'slow' in item.keywords and not item.config.getoption('slow'):
        pytest.skip('a slow test: run it with --slow')


def get_test_compile_run(get_option: Callable[[str], bool]) -> Tuple[bool, bool]:
    """
    assess whether to run the compile-tests, and whether to run the run-tests.
    exit pytest if running in parallel both compile and run.
    @param get_option: function that returns the flags values
    @return: (test_compile, test_run) booleans
    """
    if get_option(UNIT_TESTS_DEST):
        # a --unit-tests run targets tests/unit, which is unrelated to the .fj compile/run
        # tests - so generate none of them. with neither selected, the parallel-guard below
        # can't fire, so a --unit-tests run is free to use -n auto.
        return False, False

    check_compile_tests = get_option(COMPILE_FLAG)
    check_run_tests = get_option(RUN_FLAG)

    if not check_compile_tests and not check_run_tests:
        check_compile_tests, check_run_tests = True, True

    if check_compile_tests and check_run_tests and is_parallel_active():
        pytest.exit(
            "Can't run both compile and run (both --compile --run flags / none of them) "
            "in parallel (-n auto/number>1), "
            "as the run tests depends on the compile tests"
        )

    return check_compile_tests, check_run_tests


def get_test_types_to_run__heavy_first(get_option: Callable[[str], bool]) -> List[str]:
    """
    get the test types to run (ordered, heavy tests first).
    @param get_option: function that returns the flags values
    @return: list of the test types to run
    """
    all_test_types_heavy_first = TEST_TYPES[::-1]
    regular_test_types_heavy_first = REGULAR_TYPES[::-1]
    catalog_test_types_heavy_first = CATALOG_TYPES[::-1]

    # --catalog is intended to be used alone (disjoint from --all/--regular/individual type flags).
    # That's a convention, not a runtime guard -- combining flags will simply use the catalog branch.
    if get_option(CATALOG_FLAG):
        return list(catalog_test_types_heavy_first)

    if get_option(ALL_FLAG):
        types_to_run = list(all_test_types_heavy_first)
    elif get_option(REGULAR_FLAG):
        types_to_run = list(regular_test_types_heavy_first)
    else:
        types_to_run = list(filter(get_option, all_test_types_heavy_first))
        if not types_to_run:
            types_to_run = [DEFAULT_TYPE]
    return types_to_run


def is_test_name_ok(
    name: str,
    exact: Optional[List[str]],
    contains: Optional[List[str]],
    startswith: Optional[List[str]],
    endswith: Optional[List[str]],
) -> bool:
    """
    Check whether this test should be run (by the test's name).
    True if at least one of the checks below succeeds (checked if not None).
    @param name: the test name
    @param exact: list of strings that the test name should be in it
    @param contains: list of strings that the test name should contain one of them
    @param startswith: list of strings that the test name should start by one of them
    @param endswith: list of strings that the test name should end by one of them
    @return: will this test run?
    """
    if exact is not None:
        for option in exact:
            if name == option:
                return True

    if contains is not None:
        for option in contains:
            if option in name:
                return True

    if startswith is not None:
        for option in startswith:
            if name.startswith(option):
                return True

    if endswith is not None:
        for option in endswith:
            if name.endswith(option):
                return True

    return False


def filter_by_test_name(
    tests_args: List[ParameterSet], get_option: Callable[[str], Optional[List[str]]]
) -> List[ParameterSet]:
    """
    filter the test list by the test names (and the namings flags)
    @param tests_args: the tests
    @param get_option: function that returns the flags values
    @return: the filtered test list
    """
    exact = get_option(NAME_EXACT_FLAG)
    contains = get_option(NAME_CONTAINS_FLAG)
    startswith = get_option(NAME_STARTSWITH_FLAG)
    endswith = get_option(NAME_ENDSWITH_FLAG)

    if all(filter_list is None for filter_list in (exact, contains, startswith, endswith)):
        return tests_args

    return [
        args
        for args in tests_args
        if is_test_name_ok(args.values[0].test_name, exact, contains, startswith, endswith)  # type: ignore[union-attr]
    ]


def pytest_generate_tests(metafunc: Metafunc) -> None:
    """
    gather the tests from the csvs, and parametrize the compile-tests and run-tests fixtures with it.
    @param metafunc: enables to get-flags, parametrize-fixtures
    """

    def get_option(opt: str) -> Any:
        return metafunc.config.getoption(opt)

    compile_tests__heavy_first, run_tests__heavy_first = get_tests_from_csvs__heavy_first__execute_once(get_option)

    if COMPILE_ARGUMENTS_FIXTURE in metafunc.fixturenames:
        metafunc.parametrize(COMPILE_ARGUMENTS_FIXTURE, compile_tests__heavy_first, ids=repr)

    if RUN_ARGUMENTS_FIXTURE in metafunc.fixturenames:
        metafunc.parametrize(RUN_ARGUMENTS_FIXTURE, run_tests__heavy_first, ids=repr)


def is_not_skipped(test: Union[Item, Collector]) -> bool:
    if hasattr(test, 'callspec') and hasattr(test.callspec, 'params'):
        params = test.callspec.params
        for fixture_name, fixture_type in fixtures_name_to_type.items():
            if fixture_name in params:
                return isinstance(params[fixture_name], fixture_type)
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_collectreport(report: CollectReport) -> Iterable[None]:
    report.result = list(filter(is_not_skipped, report.result))
    yield


# noinspection PyUnusedLocal
@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(config: Config, items: List[Item]) -> Iterable[None]:
    yield
    items[:] = filter(is_not_skipped, items)


def get_tests_from_csvs__heavy_first__execute_once(get_option: Callable[[str], Any]) -> TestsType:
    """
    get the tests from the csv. heavy first.
    if more than 1 worker - only one worker will do the work, and distribute the result to the other workers.
    @param get_option: function that returns the flags values
    @return: the tests
    """
    if not is_parallel_active():
        return get_tests_from_csvs(get_option)

    with build_tests_lock:
        if build_tests_queue.empty():
            tests = get_tests_from_csvs(get_option)
        else:
            tests = build_tests_queue.get()
        build_tests_queue.put(tests)
        return tests


def get_tests_from_csvs(get_option: Callable[[str], Any]) -> TestsType:
    """
    get the tests from the csv. heavy first.
    @param get_option: function that returns the flags values
    @return: the tests
    """
    check_compile_tests, check_run_tests = get_test_compile_run(get_option)

    types_to_run__heavy_first = get_test_types_to_run__heavy_first(get_option)

    compile_xfail_list = [line[0] for line in argument_line_iterator(TESTS_TABLES_PATH / "xfail_compile.csv", 1)]
    run_xfail_list = [line[0] for line in argument_line_iterator(TESTS_TABLES_PATH / "xfail_run.csv", 1)]

    save_debug_file, debug_info_length = get_option(DEBUG_INFO_FLAG)
    if is_parallel_active():
        save_debug_file = False

    compile_tests: List[ParameterSet] = []
    if check_compile_tests:
        compiles_csvs = {
            test_type: TESTS_TABLES_PATH / f"test_compile_{test_type}.csv" for test_type in types_to_run__heavy_first
        }
        for test_type in types_to_run__heavy_first:
            compile_tests.extend(
                get_compile_tests_params_from_csv(compiles_csvs[test_type], compile_xfail_list, save_debug_file)
            )
        compile_tests = filter_by_test_name(compile_tests, get_option)

    run_tests: List[ParameterSet] = []
    if check_run_tests:
        run_csvs = {
            test_type: TESTS_TABLES_PATH / f"test_run_{test_type}.csv" for test_type in types_to_run__heavy_first
        }
        for test_type in types_to_run__heavy_first:
            run_tests.extend(
                get_run_tests_params_from_csv(run_csvs[test_type], run_xfail_list, save_debug_file, debug_info_length)
            )
        run_tests = filter_by_test_name(run_tests, get_option)

    return compile_tests, run_tests
