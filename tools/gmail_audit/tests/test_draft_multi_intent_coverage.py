"""P1.4: multi-intent draft composer + draft coverage enforcement."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.draft_sanity import evaluate_draft_sanity
from agent_runtime.intent_projection import project_customer_intents
from agent_runtime.tools.handlers import _compose_multi_intent_body


def _three_intent_projection():
    br = {
        "recommended_next_action": "collect_data",
        "missing_information": ["exact_symptoms", "device_model"],
        "customer_intents": [
            {
                "intent_type": "service_problem",
                "description": "Pompa H70, brak CWU.",
                "required_information": ["exact_symptoms", "device_model"],
            },
            {
                "intent_type": "schedule_service",
                "description": "Przegląd w przyszłym tygodniu.",
                "required_information": ["device_model", "preferred_service_date"],
            },
            {
                "intent_type": "document_request",
                "description": "Kopia ostatniej faktury.",
                "required_information": ["invoice_period"],
            },
        ],
    }
    return project_customer_intents(br_result=br, case_id="case_m", source_signal_id="msg_m")


def _sanity(body: str, coverage: dict) -> dict:
    return evaluate_draft_sanity(
        body=body,
        case_kind="awaria_naprawa",
        intent="missing_info",
        intent_coverage=coverage,
    )


def test_positive_multi_intent_draft_covers_all_intents() -> None:
    p = _three_intent_projection()
    body, coverage = _compose_multi_intent_body(
        intent_projection=p,
        epistemic_context=None,
        legacy_body="legacy",
    )
    assert coverage["intent_ids"] == coverage["covered_intent_ids"]
    assert not coverage["ignored_intent_ids"]
    assert set(coverage["unresolved_intent_ids"]) == set(coverage["intent_ids"])
    # Every intent is acknowledged in the body.
    assert "problemu serwisowego" in body
    assert "umówienie przeglądu" in body
    assert "kopii faktury" in body
    assert _sanity(body, coverage)["ok"] is True


def test_single_intent_keeps_legacy_body() -> None:
    p = project_customer_intents(
        br_result={
            "recommended_next_action": "collect_data",
            "missing_information": ["exact_symptoms"],
        },
        case_id="c",
        source_signal_id="m",
    )
    body, coverage = _compose_multi_intent_body(
        intent_projection=p,
        epistemic_context=None,
        legacy_body="LEGACY_BODY",
    )
    assert body == "LEGACY_BODY"
    assert coverage == {}


def test_shared_field_is_asked_once() -> None:
    p = _three_intent_projection()
    body, coverage = _compose_multi_intent_body(
        intent_projection=p,
        epistemic_context=None,
        legacy_body="legacy",
    )
    # device_model is shared by service_problem and schedule_service -> one ask.
    assert body.count("modelu urządzenia") == 1
    requested = coverage["requested_information_by_intent"]
    assert "device_model" in requested[coverage["intent_ids"][0]]
    assert "device_model" in requested[coverage["intent_ids"][1]]


def test_dropped_intent_denied() -> None:
    p = _three_intent_projection()
    body, coverage = _compose_multi_intent_body(
        intent_projection=p,
        epistemic_context=None,
        legacy_body="legacy",
    )
    bad = dict(coverage, ignored_intent_ids=[coverage["intent_ids"][2]])
    result = _sanity(body, bad)
    assert result["ok"] is False
    assert "MULTI_INTENT_DROPPED" in result["reason_codes"]


def test_missing_required_info_request_denied() -> None:
    p = _three_intent_projection()
    body, coverage = _compose_multi_intent_body(
        intent_projection=p,
        epistemic_context=None,
        legacy_body="legacy",
    )
    bad = dict(coverage)
    bad["requested_information_by_intent"] = {
        coverage["intent_ids"][0]: [],
        coverage["intent_ids"][1]: [],
        coverage["intent_ids"][2]: [],
    }
    result = _sanity(body, bad)
    assert result["ok"] is False
    assert "INTENT_REQUIRED_INFO_NOT_REQUESTED" in result["reason_codes"]


def test_execution_assertion_denied_without_evidence() -> None:
    p = _three_intent_projection()
    body, coverage = _compose_multi_intent_body(
        intent_projection=p,
        epistemic_context=None,
        legacy_body="legacy",
    )
    bad_body = body + "\nWizyta została umówiona na poniedziałek."
    result = _sanity(bad_body, coverage)
    assert result["ok"] is False
    assert "INTENT_EXECUTION_ASSERTED_WITHOUT_EVIDENCE" in result["reason_codes"]


def test_false_completion_denied_while_intents_open() -> None:
    p = _three_intent_projection()
    body, coverage = _compose_multi_intent_body(
        intent_projection=p,
        epistemic_context=None,
        legacy_body="legacy",
    )
    bad_body = body.replace(
        "Po otrzymaniu danych sprawa zostanie zweryfikowana",
        "Sprawa została zamknięta. Po otrzymaniu danych sprawa zostanie zweryfikowana",
    )
    result = _sanity(bad_body, coverage)
    assert result["ok"] is False
    assert "INTENT_FALSELY_COMPLETED" in result["reason_codes"]


def test_composer_never_asserts_write_execution() -> None:
    p = _three_intent_projection()
    body, _ = _compose_multi_intent_body(
        intent_projection=p,
        epistemic_context=None,
        legacy_body="legacy",
    )
    # Write intents stay open: no past-tense scheduling/sending claim.
    for forbidden in (
        "została umówiona",
        "wysłaliśmy",
        "przesłaliśmy",
        "faktura została wysłana",
    ):
        assert forbidden not in body


def test_reordering_produces_identical_body_and_coverage() -> None:
    br = {
        "recommended_next_action": "collect_data",
        "missing_information": ["exact_symptoms", "device_model"],
        "customer_intents": [
            {
                "intent_type": "service_problem",
                "required_information": ["exact_symptoms", "device_model"],
            },
            {
                "intent_type": "schedule_service",
                "required_information": ["device_model", "preferred_service_date"],
            },
            {
                "intent_type": "document_request",
                "required_information": ["invoice_period"],
            },
        ],
    }
    p1 = project_customer_intents(br_result=br, case_id="c", source_signal_id="m")
    p2 = project_customer_intents(
        br_result=dict(br, customer_intents=list(reversed(br["customer_intents"]))),
        case_id="c",
        source_signal_id="m",
    )
    b1, c1 = _compose_multi_intent_body(intent_projection=p1, epistemic_context=None, legacy_body="x")
    b2, c2 = _compose_multi_intent_body(intent_projection=p2, epistemic_context=None, legacy_body="x")
    assert b1 == b2
    assert c1 == c2


def test_conflicted_claims_never_emitted_by_composer() -> None:
    p = _three_intent_projection()
    body, _ = _compose_multi_intent_body(
        intent_projection=p,
        epistemic_context=None,
        legacy_body="legacy",
    )
    # No certainty/diagnosis phrasing from multi-intent composer (P1.3 intact).
    assert "na pewno" not in body
    assert "uszkodzony czujnik" not in body
