"""
shared utility functions.
locating the standard-library files, building the (name, path) file-tuples the assembler
consumes, writing the -D defines file, and saving/loading the compressed debugging-info
(label dictionaries) used by the debugger.
"""

from __future__ import annotations

import json
import lzma
import os
import re
from pathlib import Path
from typing import List, Dict, Mapping, Tuple, Union, Optional

from flipjump.utils.constants import DEBUG_JSON_ENCODING, DEBUG_JSON_LZMA_FORMAT, DEBUG_JSON_LZMA_FILTERS, STL_PATH


def get_stl_paths() -> List[Path]:
    """
    @return: list of the ordered standard-library paths
    """
    with open(STL_PATH / 'conf.json') as stl_conf:
        stl_options = json.load(stl_conf)
    return [STL_PATH / f'{lib}.fj' for lib in stl_options['all']]


def save_debugging_labels(debugging_file_path: Optional[Path], labels: Dict[str, int]) -> None:
    """
    save the labels' dictionary to the debugging-file as lzma2-compressed json
    @param debugging_file_path: the file's path
    @param labels: the labels' dictionary
    """
    if debugging_file_path:
        with open(debugging_file_path, 'wb') as f:
            data = json.dumps(labels).encode(DEBUG_JSON_ENCODING)
            compressed_data = lzma.compress(data, format=DEBUG_JSON_LZMA_FORMAT, filters=DEBUG_JSON_LZMA_FILTERS)
            f.write(compressed_data)


def load_debugging_labels(debugging_file_path: Path) -> Dict[str, int]:
    """
    loads and decompresses the labels' dictionary from the lzma2-compressed debugging-file
    @param debugging_file_path: the file's path
    @return: the labels' dictionary
    """
    with open(debugging_file_path, 'rb') as f:
        compressed_data = f.read()

    data = lzma.decompress(compressed_data, format=DEBUG_JSON_LZMA_FORMAT, filters=DEBUG_JSON_LZMA_FILTERS)
    debugging_labels: Dict[str, int] = json.loads(data.decode(DEBUG_JSON_ENCODING))
    return debugging_labels


def get_file_tuples(files: List[str], *, no_stl: bool = False) -> List[Tuple[str, Path]]:
    """
    get the list of .fj files to be assembled (stl + files).
    @param files: the list of fj-code files.
    @param no_stl: if True: don't include the standard library.
    @return: a list of file-tuples - (file_short_name, file_path)
    """
    file_tuples = []

    if not no_stl:
        for i, stl_path in enumerate(get_stl_paths(), start=1):
            file_tuples.append((f"s{i}", stl_path))

    for i, file in enumerate(files, start=1):
        file_tuples.append((f"f{i}", Path(file)))

    return file_tuples


_DEFINE_NAME_RE = re.compile(r'[a-zA-Z_][a-zA-Z_0-9]*(\.[a-zA-Z_][a-zA-Z_0-9]*)*$')


def bad_define_message(name: str, value: Union[int, str]) -> Optional[str]:
    """@return: why `name = value` is not a valid define, or None when it is"""
    if not _DEFINE_NAME_RE.match(name):
        return f'a define needs a (possibly dotted) name, like a.b.NAME, got {name!r}'
    if not str(value).strip() or '\n' in str(value):
        return f'the define {name} needs a one-line value, got {value!r}'
    return None


def write_defines_file(defines: Mapping[str, Union[int, str]], path: Path) -> None:
    """
    write the defines into a .fj file of constants, which the assembler parses first (before the stl), as overrides
    of the declarations: a value may use `w` and an earlier define, not an stl constant. A dotted name is wrapped in
    its namespaces: `hex.pointers.PTRSIZE = 12` becomes `ns hex { ns pointers { PTRSIZE = 12 } }`.
    @param defines: the constants' full names, and their values (a number, or an fj expression)
    @param path: the file to write
    @raises ValueError: a define is invalid (see bad_define_message)
    """
    lines = []
    for name, value in defines.items():
        bad = bad_define_message(name, value)
        if bad is not None:
            raise ValueError(bad)
        *namespaces, base_name = name.split('.')
        opening = "".join(f"ns {namespace} {{\n" for namespace in namespaces)
        closing = "\n}" * len(namespaces)
        lines.append(f"{opening}{base_name} = {value}{closing}\n")
    path.write_text(''.join(lines), encoding='utf-8')


def get_temp_directory_suffix(files: Union[List[Path], List[str]]) -> str:
    """
    create a suffix for the temp directory name, using args.
    @param files: the list of fj-code files.
    @return: the suffix
    """
    return f'__{"_".join(os.path.basename(str(file)) for file in files)}__temp_directory'
