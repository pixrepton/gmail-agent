from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from context_tray_set import build_context_tray_set
from mailbox_memory_models import CaseContextPack
from projection_envelope import build_projection_envelope


def _trays() -> dict:
    pack = CaseContextPack(
        case_id="case_env_1",
        snapshot={"summary_text": "Service case summary"},
        active_facts=[{"fact_key": "status", "value": "open", "source_refs": [{"source_type": "gmail_message", "source_id": "m1"}]}],
        conflicting_facts=[{"fact_key": "device_power", "values": ["8", "10"]}],
        completeness_gaps=["Missing customer phone"],
        source_refs=[{"source_type": "gmail_message", "source_id": "m1"}],
        action_proposals=[{"proposal_id": "ap-1", "action_type": "request_missing_info", "summary": "Ask for phone"}],
    )
    return build_context_tray_set(pack, generated_at="2026-05-19T10:00:00Z")


def test_projection_envelope_maps_trays_to_operator_blocks() -> None:
    envelope = build_projection_envelope(
        _trays(),
        decision_view={
            "headline_co_pl": "Service case summary",
            "evidence_cards": [{"source_id": "m1", "title_pl": "gmail_message"}],
            "action_proposals": [{"proposal_id": "ap-1", "action_type": "request_missing_info"}],
        },
        v2_projection={"signal_projection": {"case_id": "case_env_1"}},
    )

    assert envelope["schema_version"] == "projection_envelope.v1"
    assert envelope["case_id"] == "case_env_1"
    assert "context_quality" in envelope
    assert "readiness_facets" in envelope
    assert "context_readiness" in envelope["readiness_facets"]
    assert envelope["read_only"] is True
    assert envelope["action_allowed"] is False
    assert envelope["desk_cards"]
    assert envelope["case_detail_blocks"]
    assert envelope["gap_blocks"]
    assert envelope["conflict_blocks"]
    assert envelope["evidence_blocks"]
    assert envelope["task_candidates"][0]["read_only"] is True
    assert envelope["task_candidates"][0]["action_allowed"] is False
    assert envelope["evidence_used"]


def test_projection_envelope_records_ignored_low_value_evidence() -> None:
    trays = _trays()
    trays["evidence_tray"].append({"source_type": "", "source_id": "", "evidence_role": "weak_signal"})

    envelope = build_projection_envelope(trays)

    assert envelope["evidence_ignored"]
    assert envelope["audit_blocks"]
