from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from projection_validator import validate_projection_envelope


def _valid_envelope() -> dict:
    return {
        "schema_version": "projection_envelope.v1",
        "case_id": "case_val_1",
        "read_only": True,
        "action_allowed": False,
        "desk_cards": [{"title": "Case", "summary": "Safe summary"}],
        "case_detail_blocks": [{"block_type": "essence", "content": "Safe summary"}],
        "task_candidates": [{"title": "Ask", "read_only": True, "action_allowed": False}],
        "gap_blocks": [{"summary": "Missing address"}],
        "conflict_blocks": [{"summary": "Power mismatch"}],
        "risk_blocks": [],
        "evidence_blocks": [{"source_id": "gmail:m1", "source_type": "gmail_message"}],
        "audit_blocks": [],
        "warnings": [],
        "evidence_used": [{"source_id": "gmail:m1"}],
        "evidence_ignored": [],
    }


def test_validator_accepts_projection_safe_envelope() -> None:
    report = validate_projection_envelope(
        _valid_envelope(),
        context_tray_set={"conflicts_tray": [{"summary": "Power mismatch"}]},
    )
    assert report["ok"] is True
    assert report["errors"] == []


def test_validator_rejects_raw_fields_and_action_permissions() -> None:
    bad = _valid_envelope()
    bad["case_detail_blocks"].append({"body": "RAW MAIL BODY"})
    bad["task_candidates"][0]["action_allowed"] = True

    report = validate_projection_envelope(bad)

    assert report["ok"] is False
    assert any("forbidden" in err for err in report["errors"])
    assert any("action_allowed" in err for err in report["errors"])


def test_validator_rejects_hidden_conflicts_from_context() -> None:
    bad = _valid_envelope()
    bad["conflict_blocks"] = []

    report = validate_projection_envelope(
        bad,
        context_tray_set={"conflicts_tray": [{"summary": "Power mismatch"}]},
    )

    assert report["ok"] is False
    assert any("conflict" in err for err in report["errors"])
