from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from context_tray_set import build_context_tray_set
from mailbox_memory_models import CaseContextPack


def _pack() -> CaseContextPack:
    return CaseContextPack(
        case_id="case_trays_1",
        snapshot={"summary_text": "Customer asks for a service visit.", "status": "open", "body": "RAW_BODY"},
        recent_events=[{"event_type": "gmail_message_observed", "summary": "New customer message"}],
        active_facts=[
            {
                "fact_id": "fact-1",
                "fact_key": "device_power",
                "value": "8 kW",
                "evidence_refs": [{"source_type": "gmail_message", "source_id": "gmail:m1"}],
            }
        ],
        conflicting_facts=[
            {
                "fact_key": "device_power",
                "values": ["8 kW", "10 kW"],
                "evidence_refs": [{"source_type": "drive_document", "source_id": "drive:d1"}],
            }
        ],
        completeness_gaps=["Missing installation address"],
        drive_documents_summary=[{"document_id": "doc-1", "title": "Audit PDF", "summary": "Power differs"}],
        relevant_chunks=[{"chunk_id": "chunk-1", "document_id": "doc-1", "score": 0.9}],
        source_refs=[{"type": "gmail_message", "source_id": "gmail:m1"}],
        next_action={"next_action": "ask_for_missing_data"},
        action_proposals=[{"proposal_id": "ap-1", "action_type": "request_missing_info", "summary": "Ask for address"}],
        execution_results=[{"proposal_id": "ap-old", "status": "rejected"}],
        calendar={"events": [{"event_id": "cal-1", "summary": "Service slot"}]},
        runtime_state={"latest_signal_at": "2026-05-19T10:00:00Z"},
    )


def test_context_tray_set_builds_all_trays_without_raw_text() -> None:
    trays = build_context_tray_set(_pack(), generated_at="2026-05-19T10:00:00Z")

    assert trays["schema_version"] == "context_tray_set.v1"
    assert trays["case_id"] == "case_trays_1"
    assert trays["read_only"] is True
    assert trays["source_contract"]["contract_name"] == "CaseContextPack"

    for name in (
        "essence_tray",
        "facts_tray",
        "evidence_tray",
        "gaps_tray",
        "conflicts_tray",
        "documents_tray",
        "calendar_tray",
        "history_tray",
        "operator_feedback_tray",
        "candidate_moves_tray",
        "llm_warnings_tray",
    ):
        assert name in trays
        assert isinstance(trays[name], list)

    rendered = repr(trays)
    assert "RAW_BODY" not in rendered
    assert trays["facts_tray"][0]["fact_key"] == "device_power"
    assert trays["gaps_tray"]
    assert trays["conflicts_tray"]
    assert trays["candidate_moves_tray"][0]["read_only"] is True
    assert trays["candidate_moves_tray"][0]["action_allowed"] is False


def test_context_tray_set_preserves_evidence_and_warnings() -> None:
    trays = build_context_tray_set(_pack(), generated_at="2026-05-19T10:00:00Z")

    assert any(item.get("source_id") for item in trays["evidence_tray"])
    assert any("llm" in str(item.get("warning_code", "")).lower() for item in trays["llm_warnings_tray"])
    assert trays["context_quality"]["readiness_status"]


def test_candidate_moves_tray_polish_label_for_review_required() -> None:
    contract = {
        "contract_name": "CaseContextPack",
        "case_id": "case_review_pl",
        "facts": [],
        "case_summary": {"recommended_next_action": "review_required"},
        "proposed_next_actions": [],
    }
    trays = build_context_tray_set(contract, generated_at="2026-05-19T10:00:00Z")
    moves = trays["candidate_moves_tray"]
    assert len(moves) == 1
    assert moves[0]["action_type"] == "review_required"
    assert moves[0]["summary"] == "Sprawdź i uzupełnij dane sprawy"


def test_candidate_moves_tray_polish_label_for_proposed_next_actions() -> None:
    contract = {
        "contract_name": "CaseContextPack",
        "case_id": "case_prop_pl",
        "facts": [],
        "case_summary": {},
        "proposed_next_actions": [
            {
                "proposal_id": "ap-1",
                "action_type": "request_missing_info",
                "title": "request_missing_info",
                "summary": "Ask for address",
            }
        ],
    }
    trays = build_context_tray_set(contract, generated_at="2026-05-19T10:00:00Z")
    moves = trays["candidate_moves_tray"]
    assert len(moves) == 1
    assert moves[0]["summary"] == "Poproś o brakujące dane"
    assert moves[0]["title"] == "Poproś o brakujące dane"


def test_facts_tray_excludes_superseded_from_current_projection() -> None:
    """FACT-04: UI tray must not treat superseded rows as current facts."""
    pack = CaseContextPack(
        case_id="case_tray_fact04",
        snapshot={"status": "open", "summary_text": "Area updated."},
        active_facts=[
            {
                "fact_id": "f-old",
                "fact_key": "heated_area_m2",
                "value": "120",
                "status": "superseded",
                "confidence": 0.95,
            },
            {
                "fact_id": "f-new",
                "fact_key": "heated_area_m2",
                "value": "150",
                "status": "active",
                "confidence": 0.9,
            },
        ],
    )
    trays = build_context_tray_set(pack, generated_at="2026-08-06T00:00:00Z")
    tray_ids = {str(row.get("fact_id") or "") for row in trays["facts_tray"]}
    tray_values = {str(row.get("value")) for row in trays["facts_tray"]}
    assert "f-new" in tray_ids
    assert "150" in tray_values
    assert "f-old" not in tray_ids
    assert "120" not in tray_values
    for row in trays["facts_tray"]:
        assert str(row.get("status") or "") != "superseded"
        assert not (
            str(row.get("value")) == "120"
            and str(row.get("status") or "") in {"inferred", "confirmed", "active", ""}
        )
