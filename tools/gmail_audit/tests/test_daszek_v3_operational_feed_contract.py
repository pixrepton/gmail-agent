from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from daszek_v3_operational_feed_contract import (
    FORBIDDEN_KEYS_ANYWHERE,
    desk_note_ref_warnings,
    validate_operational_feed_snapshot,
)


def test_validate_accepts_minimal_valid_snapshot() -> None:
    snap = {
        "schema_name": "daszek_operational_feed_snapshot",
        "schema_version": "1",
        "snapshot_id": "snap-contract-1",
        "read_only": True,
        "creates_cases": False,
        "executes_actions": False,
        "feed": {
            "feed_meta": {"exporter": "test"},
            "desk": [],
            "day": {"sections": []},
            "cases": [],
            "tasks": [],
            "case_details": {},
        },
    }
    rep = validate_operational_feed_snapshot(snap)
    assert rep.ok
    assert not rep.errors


def test_validate_rejects_nested_forbidden_key() -> None:
    snap = {
        "schema_name": "daszek_operational_feed_snapshot",
        "schema_version": "1",
        "snapshot_id": "snap-bad",
        "read_only": True,
        "creates_cases": False,
        "executes_actions": False,
        "feed": {
            "desk": [],
            "day": {"sections": []},
            "cases": [],
            "tasks": [],
            "case_details": {"c1": {"case": {"snippet": "x"}, "ok": True}},
        },
    }
    rep = validate_operational_feed_snapshot(snap)
    assert not rep.ok
    assert any("snippet" in e for e in rep.errors)


def test_forbidden_set_matches_php_contract_comment() -> None:
    assert "attachment_bytes" in FORBIDDEN_KEYS_ANYWHERE
    assert "subject" in FORBIDDEN_KEYS_ANYWHERE
    assert "raw_body" in FORBIDDEN_KEYS_ANYWHERE
    assert "message_body" in FORBIDDEN_KEYS_ANYWHERE


def test_validate_schema_1_2_requires_action_items() -> None:
    snap = {
        "schema_name": "daszek_operational_feed_snapshot",
        "schema_version": "1.2",
        "snapshot_id": "snap-12",
        "read_only": True,
        "creates_cases": False,
        "executes_actions": False,
        "feed": {
            "desk": [],
            "day": {"sections": []},
            "cases": [],
            "tasks": [{"task_id": "t1", "title": "x"}],
            "case_details": {},
        },
    }
    rep = validate_operational_feed_snapshot(snap)
    assert not rep.ok
    assert any("action_items" in e for e in rep.errors)


def test_validate_schema_1_2_dual_emit_passes() -> None:
    item = {"task_id": "t1", "title": "Propozycja", "feed_read_only": True}
    snap = {
        "schema_name": "daszek_operational_feed_snapshot",
        "schema_version": "1.2",
        "snapshot_id": "snap-12-dual",
        "read_only": True,
        "creates_cases": False,
        "executes_actions": False,
        "feed": {
            "desk": [],
            "day": {"sections": []},
            "cases": [],
            "action_items": [item],
            "tasks": [item],
            "case_details": {},
        },
    }
    rep = validate_operational_feed_snapshot(snap)
    assert rep.ok, rep.errors


def test_validate_schema_1_1_tasks_only_passes() -> None:
    snap = {
        "schema_name": "daszek_operational_feed_snapshot",
        "schema_version": "1.1",
        "snapshot_id": "snap-11",
        "read_only": True,
        "creates_cases": False,
        "executes_actions": False,
        "feed": {
            "desk": [],
            "day": {"sections": []},
            "cases": [],
            "tasks": [],
            "case_details": {},
        },
    }
    rep = validate_operational_feed_snapshot(snap)
    assert rep.ok, rep.errors


# Phase 6 proof token (gate): FEED_ACTION_ITEMS_CONTRACT_PROOF_OK


def test_desk_note_ref_warnings_skips_synthetic_desk_prefix() -> None:
    feed = {"desk": [{"note_id": "desk-case-1-0", "title": "x"}]}
    assert desk_note_ref_warnings(feed, frozenset()) == []


def test_validate_accepts_optional_quality_readonly_slice() -> None:
    snap = {
        "schema_name": "daszek_operational_feed_snapshot",
        "schema_version": "1",
        "snapshot_id": "snap-quality",
        "read_only": True,
        "creates_cases": False,
        "executes_actions": False,
        "feed": {
            "desk": [],
            "day": {"sections": []},
            "cases": [],
            "tasks": [],
            "case_details": {},
            "quality_readonly": {
                "schema_version": "quality_readonly_projection.v1",
                "projection_type": "quality_readonly",
                "read_only": True,
                "by_group": {"routing_quality": 1},
                "by_domain": {"calibration": 1},
                "truth_mutation_summary": {"mutates_truth_true_count": 0, "mutates_truth_false_count": 1},
                "correlation_summary": {},
                "recent_records": [],
                "warnings": [],
                "not_proven": ["local_fixture_or_export_file_only"],
            },
        },
    }
    rep = validate_operational_feed_snapshot(snap)
    assert rep.ok, rep.errors


def test_desk_note_ref_warnings_reports_missing() -> None:
    feed = {"desk": [{"note_id": "real-note-1", "title": "x"}]}
    w = desk_note_ref_warnings(feed, frozenset({"other"}))
    assert len(w) == 1
    assert "real-note-1" in w[0]
