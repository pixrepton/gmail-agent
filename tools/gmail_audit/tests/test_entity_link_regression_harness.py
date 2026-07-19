from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import sys

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from entity_link_regression_harness import default_corpus_path, main, run_corpus_file


def test_entity_link_regression_corpus_synthetic_passes() -> None:
    report = run_corpus_file(default_corpus_path())
    assert report.failed == 0, "misses:\n" + "\n".join(report.misses)
    assert report.passed == report.total
    assert report.total >= 1


def test_entity_link_regression_cli_writes_report_json() -> None:
    with TemporaryDirectory() as tmpdir:
        report_path = Path(tmpdir) / "entity-link-report.json"
        exit_code = main(
            [
                "--corpus",
                str(default_corpus_path()),
                "--json-out",
                str(report_path),
            ]
        )

        assert exit_code == 0
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        assert payload["failed"] == 0
        assert payload["thresholds"]["min_confidence_fuzzy"] == 0.85
        assert "cohort_update_procedure" in payload["quality_gates"]
