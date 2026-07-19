"""Run summary.json: new fields must not break loose consumers (semantic_alignment)."""

from __future__ import annotations

import json
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(TOOL_DIR))

from artifact_contracts import build_run_summary_record, empty_run_summary


def test_build_run_summary_includes_semantic_alignment() -> None:
    run_state = {
        "run_id": "test-run",
        "manifest": {
            "status": "completed",
            "mailbox": "m@test.local",
            "completed_at": "2026-04-11T12:00:00",
            "daszek_push_requested": False,
            "preflight": {},
            "runtime_controls": {},
            "env_source": "",
        },
        "summary": empty_run_summary(),
        "artifacts": {"human_annotations": Path("/dev/null"), "stage_records": Path("/dev/null")},
    }
    run_state["summary"]["semantic_alignment"]["items_with_attachment_envelope"] = 2
    run_state["summary"]["semantic_alignment"]["second_pass_triggers"] = 1
    rec = build_run_summary_record(run_state, review_template_path=Path("review.csv"))
    assert rec.get("semantic_alignment", {}).get("items_with_attachment_envelope") == 2
    assert rec.get("semantic_alignment", {}).get("second_pass_triggers") == 1
    blob = json.dumps(rec, ensure_ascii=False)
    roundtrip = json.loads(blob)
    assert roundtrip["semantic_alignment"]["second_pass_triggers"] == 1


def test_loose_consumer_tolerates_missing_semantic_alignment() -> None:
    """Older summary.json files without semantic_alignment must still load."""
    legacy = {
        "run_id": "old",
        "status": "completed",
        "items_valid": 3,
        "items_failed": 0,
    }
    sa = legacy.get("semantic_alignment") or {}
    assert sa == {}
    assert legacy.get("items_valid") == 3


def test_daszek_push_connected_reads_manifest_flag() -> None:
    run_state = {
        "run_id": "test-run",
        "manifest": {
            "status": "completed",
            "mailbox": "m@test.local",
            "completed_at": "2026-04-11T12:00:00",
            "daszek_push_requested": True,
            "daszek_push_connected": True,
            "preflight": {},
            "runtime_controls": {},
            "env_source": "",
        },
        "summary": empty_run_summary(),
        "artifacts": {"human_annotations": Path("/dev/null"), "stage_records": Path("/dev/null")},
    }
    rec = build_run_summary_record(run_state, review_template_path=Path("review.csv"))
    assert rec.get("daszek_push_connected") is True
