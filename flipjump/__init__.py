"""
the flipjump package.
a toolchain for the FlipJump esoteric language. this top-level module exposes the public
API: the assemble/run/debug entry points, the high-level convenience functions, and the
commonly-used classes and constants.
"""

from flipjump.flipjump_cli import assemble_run_according_to_cmd_line_args
from flipjump.flipjump_quickstart import (
    assemble,
    run,
    debug,
    run_test_output,
    assemble_and_run,
    assemble_and_debug,
    assemble_and_run_test_output,
)
from flipjump.fjm.fjm_consts import FJMVersion, FJ_MAGIC
from flipjump.interpreter.fjm_run import TerminationStatistics, is_native_engine_active
from flipjump.utils.classes import TerminationCause
from flipjump.utils.constants import FLIPJUMP_VERSION as __version__
from flipjump.utils.functions import get_stl_paths
from flipjump.utils.exceptions import IODeviceException, FlipJumpException
from flipjump.interpreter.io_devices.IODevice import IODevice
from flipjump.interpreter.io_devices.FixedIO import FixedIO
from flipjump.interpreter.io_devices.StandardIO import StandardIO
from flipjump.interpreter.io_devices.BrokenIO import BrokenIO

__all__ = [
    '__version__',
    'assemble_run_according_to_cmd_line_args',
    'assemble',
    'run',
    'debug',
    'run_test_output',
    'assemble_and_run',
    'assemble_and_debug',
    'assemble_and_run_test_output',
    'FJMVersion',
    'FJ_MAGIC',
    'TerminationCause',
    'TerminationStatistics',
    'is_native_engine_active',
    'FlipJumpException',
    'get_stl_paths',
]
