"""P1.4: deterministic multi-intent projection invariants."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.intent_projection import project_customer_intents


def _three_intent_br() -> dict:
    return {
        "recommended_next_action": "collect_data",
        "missing_information": ["exact_symptoms", "device_model"],
        "customer_intents": [
            {
                "intent_type": "service_problem",
                "description": "Pompa pokazuje H70, brak CWU.",
                "required_information": ["exact_symptoms", "device_model"],
                "evidence_refs": [{"source_type": "gmail_message", "source_id": "msg1"}],
            },
            {
                "intent_type": "schedule_service",
                "description": "Prośba o przegląd w przyszłym tygodniu.",
                "required_information": ["device_model", "preferred_service_date"],
            },
            {
                "intent_type": "document_request",
                "description": "Prośba o kopię ostatniej faktury.",
                "required_information": ["invoice_period"],
            },
        ],
    }


def test_three_intents_are_all_preserved_with_independent_state() -> None:
    p = project_customer_intents(
        br_result=_three_intent_br(),
        case_id="case_multi",
        source_signal_id="msg_multi",
    )
    assert p is not None
    types = [i.intent_type for i in p.intents]
    assert types == ["service_problem", "schedule_service", "document_request"]
    by_type = {i.intent_type: i for i in p.intents}
    assert by_type["service_problem"].status == "NEEDS_INFORMATION"
    assert by_type["schedule_service"].execution_authority == "HITL_ONLY"
    assert by_type["document_request"].execution_authority == "HITL_ONLY"
    assert p.primary_actionable_intent == by_type["service_problem"].intent_id


def test_shared_required_information_dedup_mapping() -> None:
    p = project_customer_intents(
        br_result=_three_intent_br(),
        case_id="case_multi",
        source_signal_id="msg_multi",
    )
    shared = p.shared_required_information
    device_ids = shared.get("device_model") or []
    # device_model is needed by service_problem AND schedule_service -> dedup.
    assert len(device_ids) == 2
    assert device_ids == sorted(device_ids)
    # Each intent has its own missing-information surface.
    assert set(p.missing_information_by_intent) == {i.intent_id for i in p.intents}


def test_reordering_does_not_change_projection() -> None:
    br = _three_intent_br()
    p1 = project_customer_intents(br_result=br, case_id="c", source_signal_id="m")
    br_reversed = dict(br, customer_intents=list(reversed(br["customer_intents"])))
    p2 = project_customer_intents(br_result=br_reversed, case_id="c", source_signal_id="m")
    assert p1 is not None and p2 is not None
    assert p1.to_dict() == p2.to_dict()


def test_evidence_provenance_is_carried_per_intent() -> None:
    p = project_customer_intents(
        br_result=_three_intent_br(),
        case_id="case_multi",
        source_signal_id="msg_multi",
    )
    service = next(i for i in p.intents if i.intent_type == "service_problem")
    assert service.evidence_refs
    assert any(ref.get("source_id") == "msg1" for ref in service.evidence_refs)


def test_no_intents_returns_none() -> None:
    assert (
        project_customer_intents(
            br_result={"recommended_next_action": "escalate_review"},
            case_id="c",
            source_signal_id="m",
        )
        is None
    )


def test_single_intent_fallback_preserves_legacy_semantics() -> None:
    p = project_customer_intents(
        br_result={
            "recommended_next_action": "collect_data",
            "missing_information": ["exact_symptoms"],
        },
        case_id="c",
        source_signal_id="m",
    )
    assert p is not None
    assert len(p.intents) == 1
    assert p.intents[0].intent_type == "service_problem"
    assert p.intents[0].required_information == ["exact_symptoms"]


def test_raw_intents_snapshot_path_is_deterministic() -> None:
    br = _three_intent_br()
    from agent_runtime.intent_projection import project_customer_intents

    first = project_customer_intents(br_result=br, case_id="c", source_signal_id="m")
    raw = [i.model_dump(mode="python") for i in first.intents]  # type: ignore[union-attr]
    second = project_customer_intents(raw_intents=raw, case_id="c", source_signal_id="m")
    assert first is not None and second is not None
    assert first.to_dict() == second.to_dict()


def test_conflicted_claims_are_not_repaired_by_multi_intent_routing() -> None:
    # P1.3 boundary: multi-intent must never "fix" a conflicted fact.
    br = _three_intent_br()
    p = project_customer_intents(br_result=br, case_id="c", source_signal_id="m")
    # Projection has no epistemic claims at all; conflict handling remains
    # exclusively in P1.3 projection (nothing here upgrades or repairs claims).
    assert p is not None
    for intent in p.intents:
        assert not intent.required_information or intent.status == "NEEDS_INFORMATION"
