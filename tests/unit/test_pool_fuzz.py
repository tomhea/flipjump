"""
the table pool's differential fuzzer (tests/unit/pool_fuzzer.py): a few random programs in every run, the full
run with --slow, and its two controls - a pool that moves a table one op below its labels, and one that rewrites
no arm of a pinned word - which it must report.
"""

from pathlib import Path
from typing import Any, List

import pytest

from flipjump.assembler import assembler
from flipjump.assembler.preprocessor import PreprocessorData
from tests.unit.pool_fuzzer import fuzz_case

WIDTHS = (16, 32, 64)


@pytest.mark.parametrize('case', range(3))
def test_random_programs_run_the_same_with_every_pool(tmp_path: Path, case: int) -> None:
    assert fuzz_case(100 + case, WIDTHS[case % 3], tmp_path) == []


@pytest.mark.slow
@pytest.mark.parametrize('case', range(90))
def test_random_programs_run_the_same_with_every_pool_full(tmp_path: Path, case: int) -> None:
    assert fuzz_case(100000 + case, WIDTHS[case % 3], tmp_path) == []


# the controls' case: with either defect, fj --pool-tables' pool runs it differently
CONTROL_FINDING = '700 w=16 cli: the pooled run differs from the plain one'


def _findings(tmp_path: Path) -> List[str]:
    return fuzz_case(700, 16, tmp_path, 10)


def test_the_controls_case_runs_the_same_with_every_pool(tmp_path: Path) -> None:
    assert _findings(tmp_path) == []


def test_the_fuzzer_reports_a_table_moved_one_op_below_its_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PreprocessorData._place_deferred_tables

    def shifted(self: Any, pool: Any) -> None:
        tables = self._deferred
        original(self, pool)
        for segment in tables.segments:  # the first table that left the stream: its ops one op below its labels
            if segment is not None:
                segment.start_address -= 2 * self.memory_width
                segment.wflip_start_address -= 2 * self.memory_width
                break

    monkeypatch.setattr(PreprocessorData, '_place_deferred_tables', shifted)
    findings = _findings(tmp_path)
    assert CONTROL_FINDING in findings, findings


def test_the_fuzzer_reports_a_pin_whose_arms_are_not_rewritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assembler.BinaryData, 'pinned_value', lambda self, word, value, kind: value)
    findings = _findings(tmp_path)
    assert CONTROL_FINDING in findings, findings
