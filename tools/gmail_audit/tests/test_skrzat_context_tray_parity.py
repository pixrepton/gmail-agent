"""Epik 4: Skrzat and projection share the same ContextTraySet builder (CEL diagram)."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from context_tray_set import build_context_tray_set
from projection_snapshot_transport import build_operator_projection_snapshot


def _minimal_vnext_pack() -> dict:
    return {
        "case_id": "case-cel-parity",
        "generated_at": "2026-06-02T12:00:00Z",
        "case_summary": {"summary_text": "Sprawa testowa CEL parity"},
        "facts": [{"fact_key": "topic", "value": "HVAC", "source_type": "inferred"}],
        "gaps": [{"summary": "Brak mocy urządzenia", "source_type": "pack"}],
        "conflicts": [],
        "evidence_refs": [{"source_id": "msg-parity-1", "summary": "Mail klienta"}],
    }


def test_skrzat_api_path_matches_projection_snapshot_trays() -> None:
    """Same vnext pack → identical tray set whether built for Skrzat or operator snapshot."""
    pack = _minimal_vnext_pack()
    trays_direct = build_context_tray_set(pack, generated_at=pack["generated_at"])
    snapshot = build_operator_projection_snapshot(
        {"decision": {"action": "review"}, "review": {"flags": []}, "source": {}, "message": {}, "thread": {}},
        stage_outputs={
            "preclassification_result": {"lane": "intake_llm"},
            "case_link_result": {"case_id": "case-cel-parity"},
            "business_reasoning_result": {},
            "reply_draft_result": {},
            "action_plan_result": {},
            "case_intelligence_result": {},
            "mailbox_memory_result": {"context_pack": {"vnext": pack}},
        },
        run_id="epik4-parity",
    )
    trays_from_projection = snapshot["context_tray_set"]
    assert trays_direct["schema_version"] == trays_from_projection["schema_version"] == "context_tray_set.v1"
    assert trays_direct["case_id"] == trays_from_projection["case_id"] == "case-cel-parity"
    assert len(trays_direct.get("essence_tray") or []) == len(trays_from_projection.get("essence_tray") or [])
    assert len(trays_direct.get("gaps_tray") or []) == len(trays_from_projection.get("gaps_tray") or [])


def test_projection_envelope_built_from_same_trays() -> None:
    pack = _minimal_vnext_pack()
    snapshot = build_operator_projection_snapshot(
        {"decision": {"action": "review"}, "review": {"flags": []}, "source": {}, "message": {}, "thread": {}},
        stage_outputs={
            "mailbox_memory_result": {"context_pack": {"vnext": pack}},
            "case_intelligence_result": {},
        },
        run_id="epik4-envelope",
    )
    envelope = snapshot.get("projection_envelope") or {}
    assert envelope.get("schema_version") == "projection_envelope.v1"
    assert snapshot.get("projection_validation", {}).get("ok") is True
    assert snapshot.get("daszek_routes", {}).get("schema_version") == "daszek_projection_router.v1"
