"""
project-wide constants.
the macro/label separator strings and prefixes, debugging defaults, the io bytes-encoding,
compression settings, and the paths to the bundled standard library (stl).
"""

from __future__ import annotations

import lzma
from importlib.metadata import version as _package_version, PackageNotFoundError
from pathlib import Path
from typing import List, Dict

try:
    FLIPJUMP_VERSION = _package_version('flipjump')
except PackageNotFoundError:  # running from a source tree without an installed distribution
    FLIPJUMP_VERSION = 'unknown'

MACRO_SEPARATOR_STRING = "---"
STARTING_LABEL_IN_MACROS_STRING = ':start:'
WFLIP_LABEL_PREFIX = ':wflips:'

LAST_OPS_DEBUGGING_LIST_DEFAULT_LENGTH = 10
DEFAULT_MAX_MACRO_RECURSION_DEPTH = 900
GAP_BETWEEN_PYTHONS_AND_PREPROCESSOR_MACRO_RECURSION_DEPTH = 100

IO_BYTES_ENCODING = 'raw_unicode_escape'

DEBUG_JSON_ENCODING = 'utf-8'
DEBUG_JSON_LZMA_FORMAT = lzma.FORMAT_RAW
DEBUG_JSON_LZMA_FILTERS: List[Dict[str, int]] = [{"id": lzma.FILTER_LZMA2}]

PACKAGE_ROOT_PATH = Path(__file__).parent.parent
STL_PATH = PACKAGE_ROOT_PATH / 'stl'
