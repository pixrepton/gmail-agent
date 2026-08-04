from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from eval_capability_batch_harness import (  # noqa: E402
    classify_run_failure,
    run_resilient_batch,
)


def _cases(*ids: str) -> list[dict]:
    return [{"id": case_id} for case_id in ids]


def test_classify_capacity_from_rate_limit() -> None:
    assert classify_run_failure(RuntimeError("HTTP 429 Too Many Requests")) == "CAPACITY"


def test_classify_delivery_from_timeout() -> None:
    assert classify_run_failure(TimeoutError("connection timed out")) == "DELIVERY"


def test_classify_harness_from_logic_error() -> None:
    assert classify_run_failure(ValueError("unexpected capture shape")) == "HARNESS"


def test_one_failure_does_not_abort_batch() -> None:
    def runner(case: dict) -> dict:
        if case["id"] == "B":
            raise RuntimeError("429 quota exhausted")
        return {"id": case["id"], "primary_outcome": "CLEAN_PASS"}

    report = run_resilient_batch(_cases("A", "B", "C"), runner)

    assert report.aborted is False
    assert report.total == 3
    assert report.completed == 2
    assert report.failed == 1
    assert report.outcomes["CLEAN_PASS"] == 2
    assert report.outcomes["CAPACITY"] == 1


def test_failed_case_is_classified_not_capability() -> None:
    def runner(case: dict) -> dict:
        raise ConnectionError("connection refused")

    report = run_resilient_batch(_cases("X"), runner)
    row = report.cases[0]

    assert row.status == "failed"
    assert row.primary_outcome == "DELIVERY"


def test_runner_returning_invalid_primary_outcome_becomes_harness() -> None:
    report = run_resilient_batch(
        _cases("Z"),
        lambda case: {"id": case["id"], "primary_outcome": "NOT_A_REAL_OUTCOME"},
    )

    assert report.cases[0].primary_outcome == "HARNESS"


def test_corpus_file_smoke(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"cases": [{"id": "A"}, {"id": "B"}]}), encoding="utf-8")

    from eval_capability_batch_harness import run_corpus_file  # noqa: E402

    report = run_corpus_file(corpus, lambda case: {"id": case["id"], "primary_outcome": "CLEAN_PASS"})
    assert report.completed == 2
    assert report.failed == 0
