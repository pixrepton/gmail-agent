from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from skrzat_runtime import answer_case_question


def _trays() -> dict:
    return {
        "schema_version": "context_tray_set.v1",
        "case_id": "case_skrzat_1",
        "essence_tray": [{"summary": "Customer asks for service"}],
        "facts_tray": [{"fact_key": "status", "value": "open"}],
        "evidence_tray": [{"source_type": "gmail_message", "source_id": "gmail:m1", "summary": "Customer message"}],
        "gaps_tray": [{"summary": "Missing address"}],
        "conflicts_tray": [{"summary": "Power mismatch"}],
        "documents_tray": [],
        "calendar_tray": [],
        "history_tray": [],
        "operator_feedback_tray": [],
        "candidate_moves_tray": [{"summary": "Ask customer for address", "read_only": True, "action_allowed": False}],
        "llm_warnings_tray": [{"warning_code": "llm_output_not_operational_truth", "summary": "Read-only"}],
    }


def test_skrzat_answer_is_read_only_and_uses_same_trays() -> None:
    answer = answer_case_question(_trays(), question="Czego brakuje?", mode="investigate")

    assert answer["schema_version"] == "conversation_answer_envelope.v1"
    assert answer["case_id"] == "case_skrzat_1"
    assert answer["mode"] == "investigate"
    assert answer["read_only"] is True
    assert answer["action_allowed"] is False
    assert "Missing address" in answer["answer_text"]
    assert answer["evidence"]
    assert answer["gaps"]
    assert answer["conflicts"]
    assert answer["candidate_moves"][0]["action_allowed"] is False


def test_skrzat_unknown_mode_falls_back_to_ask_with_warning() -> None:
    answer = answer_case_question(_trays(), question="Status?", mode="act")

    assert answer["mode"] == "ask"
    assert any("unsupported_mode" in str(w.get("warning_code", "")) for w in answer["warnings"])
