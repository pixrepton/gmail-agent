"""P1.4: customer intent contract + deterministic normalization."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.intent_projection import (
    normalize_customer_intents,
    normalize_intent_type,
)
from llm_contracts.customer_intents import CustomerIntentProjection


def _raw_intent(*, intent_type: str, required: list[str] | None = None) -> dict:
    return {
        "intent_type": intent_type,
        "description": "Opis intentu.",
        "required_information": required or [],
        "evidence_refs": [
            {"source_type": "gmail_message", "source_id": "msg1", "evidence_role": "supports"}
        ],
    }


def test_canonical_type_mapping() -> None:
    assert normalize_intent_type("service") == "service_problem"
    assert normalize_intent_type("schedule_visit") == "schedule_service"
    assert normalize_intent_type("faktura") == "document_request"
    assert normalize_intent_type("weird_free_text") == "other"
    assert normalize_intent_type("") == "other"


def test_projection_contract_is_list_based_and_bounded() -> None:
    raw = [_raw_intent(intent_type="service_problem")] * 12
    intents = normalize_customer_intents(raw)
    # Dedupe by type: 12 identical rows collapse to one intent.
    assert len(intents) == 1


def test_unknown_type_is_normalized_not_dropped() -> None:
    intents = normalize_customer_intents([_raw_intent(intent_type="mystery_ask")])
    assert len(intents) == 1
    assert intents[0].intent_type == "other"
    assert intents[0].status == "INFORMATIONAL_ONLY"
    assert intents[0].execution_authority == "NONE"


def test_status_derivation_is_independent_of_confidence() -> None:
    ready = normalize_customer_intents(
        [_raw_intent(intent_type="service_problem")]
    )[0]
    assert ready.status == "READY"
    needs = normalize_customer_intents(
        [_raw_intent(intent_type="service_problem", required=["exact_symptoms"])]
    )[0]
    assert needs.status == "NEEDS_INFORMATION"
    # Confidence must not decide existence: zero-confidence intent still exists.
    low = normalize_customer_intents(
        [
            {
                **_raw_intent(intent_type="document_request"),
                "confidence": 0.0,
            }
        ]
    )[0]
    assert low.intent_type == "document_request"
    assert low.status in {"BLOCKED", "NEEDS_INFORMATION"}


def test_write_intents_never_gain_execution_authority() -> None:
    intents = normalize_customer_intents(
        [
            _raw_intent(intent_type="schedule_service"),
            _raw_intent(intent_type="document_request"),
        ]
    )
    by_type = {item.intent_type: item for item in intents}
    assert by_type["schedule_service"].execution_authority == "HITL_ONLY"
    assert by_type["document_request"].execution_authority == "HITL_ONLY"
    assert "execution_authority_hitl_required" in by_type["schedule_service"].blocking_gaps
    assert "execution_authority_hitl_required" in by_type["document_request"].blocking_gaps
    assert by_type["schedule_service"].status == "BLOCKED"


def test_dedupe_merges_required_information_and_keeps_stable_order() -> None:
    a = normalize_customer_intents(
        [
            _raw_intent(intent_type="schedule_service", required=["device_model"]),
            _raw_intent(intent_type="schedule_service", required=["preferred_service_date"]),
        ]
    )
    assert len(a) == 1
    assert a[0].required_information == ["device_model", "preferred_service_date"]


def test_projection_dict_round_trip() -> None:
    from agent_runtime.intent_projection import project_customer_intents

    projection = project_customer_intents(
        br_result={
            "recommended_next_action": "collect_data",
            "missing_information": ["exact_symptoms"],
            "customer_intents": [_raw_intent(intent_type="service_problem", required=["exact_symptoms"])],
        },
        case_id="case_x",
        source_signal_id="msg_x",
    )
    assert isinstance(projection, CustomerIntentProjection)
    data = projection.to_dict()
    assert data["schema_version"] == "customer_intent_projection.v1"
    assert data["case_id"] == "case_x"
    assert len(data["intents"]) == 1
    assert data["intents"][0]["intent_id"]


def test_extra_fields_are_ignored_not_breaking() -> None:
    intents = normalize_customer_intents(
        [
            {
                **_raw_intent(intent_type="service_problem"),
                "unexpected_field": "should be ignored",
            }
        ]
    )
    assert len(intents) == 1
    assert intents[0].intent_type == "service_problem"
