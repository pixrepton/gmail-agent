from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from ingress_quality_snapshot import (
    FORBIDDEN_ITEM_KEYS,
    build_ingress_quality_snapshot,
    parse_latest20_run_report,
)


def test_parse_run_report_extracts_rate_limit_and_truncation() -> None:
    md = """
## LLM and rate limits
- stderr http-429 retry lines: 20
- items with `[truncated]` marker in compact LLM input: 17
"""
    p = parse_latest20_run_report(md)
    assert p["rate_limit_events"] == 20
    assert p["truncation_count"] == 17


def test_build_snapshot_minimal_fixture(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    operator = {
        "run_id": "fixture-run-1",
        "generated_at": "2026-05-01",
        "summary_counts": {
            "items_selected": 2,
            "items_processed": 2,
            "items_valid": 1,
            "items_failed": 1,
            "decision_distribution": {"ignore": 1, "review": 1},
        },
        "items": [
            {
                "index": 1,
                "message_id": "aaa111",
                "decision": "ignore",
                "case_id": "",
                "status": "valid",
                "truncated_compact_input": False,
                "operator_prompt": "",
            },
            {
                "index": 2,
                "message_id": "bbb222",
                "decision": "review",
                "case_id": "case_x",
                "status": "needs_review",
                "truncated_compact_input": True,
                "operator_prompt": "Czy wymaga ręcznego review?",
            },
        ],
        "failed_message_ids": ["bbb222"],
        "review_decisions_message_ids": ["bbb222"],
    }
    (run_dir / "latest20_operator_review.json").write_text(
        json.dumps(operator, ensure_ascii=False), encoding="utf-8"
    )
    report = """# Run
- stderr http-429 retry lines: 3
- items with `[truncated]` marker in compact LLM input: 1
## Failure summary
- Message id: `bbb222`
- Non-sensitive reason: semantic check failed fixture.
"""
    (run_dir / "latest20_run_report.md").write_text(report, encoding="utf-8")

    snap = build_ingress_quality_snapshot(run_dir)
    assert snap["schema_name"] == "daszek_ingress_quality_snapshot"
    assert snap["schema_version"] == "1"
    assert snap["run_id"] == "fixture-run-1"
    assert snap["read_only"] is True
    assert snap["creates_cases"] is False
    assert snap["executes_actions"] is False
    assert snap["counts"]["selected_count"] == 2
    assert snap["decision_distribution"]["ignore"] == 1
    assert snap["decision_distribution"]["create_case"] == 0
    assert len(snap["items"]) == 2
    assert snap["failed_items"][0]["message_id"] == "bbb222"
    reason = snap["failed_items"][0]["non_sensitive_reason"].lower()
    assert "semantic" in reason or "fixture" in reason
    assert len(snap["manual_review_items"]) >= 1


def test_sanitize_rejects_body_in_item(tmp_path: Path) -> None:
    run_dir = tmp_path / "bad"
    run_dir.mkdir()
    bad_item = {"index": 1, "message_id": "x", "body": "secret", "status": "valid"}
    (run_dir / "latest20_operator_review.json").write_text(
        json.dumps(
            {
                "run_id": "bad-run",
                "summary_counts": {"items_selected": 1, "items_processed": 1, "items_valid": 0, "items_failed": 1},
                "items": [bad_item],
                "failed_message_ids": ["x"],
                "review_decisions_message_ids": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "latest20_run_report.md").write_text("# x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="forbidden"):
        build_ingress_quality_snapshot(run_dir)


def test_forbidden_set_covers_sensitive_keys() -> None:
    assert "snippet" in FORBIDDEN_ITEM_KEYS
    assert "body" in FORBIDDEN_ITEM_KEYS
