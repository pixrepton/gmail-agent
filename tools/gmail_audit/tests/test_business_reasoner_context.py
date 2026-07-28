"""B2: business_reasoner must pass context_bundle into central_llm_stage."""

from __future__ import annotations

from unittest.mock import patch

from business_reasoner import BUSINESS_REASONING_INSTRUCTIONS, run_business_reasoning
from tests.test_central_llm_stage import _minimal_settings


def test_business_reasoning_instructions_require_polish_output() -> None:
    lowered = BUSINESS_REASONING_INSTRUCTIONS.lower()
    assert "po polsku" in lowered
    assert "business_interpretation" in BUSINESS_REASONING_INSTRUCTIONS
    assert "operator_note" in BUSINESS_REASONING_INSTRUCTIONS
    assert "business_summary_short" in BUSINESS_REASONING_INSTRUCTIONS


def test_run_business_reasoning_passes_context_bundle_to_central_stage() -> None:
    settings = _minimal_settings()
    context_bundle = {
        "case_id": "case-1",
        "engagement_id": "eng-1",
        "case_context_pack": {"case_id": "case-1", "engagement_id": "eng-1"},
    }
    captured: dict[str, object] = {}

    def _fake_stage(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "response_text": (
                '{"business_interpretation":"ok","business_area":"lead",'
                '"customer_state_guess":"interested","recommended_next_action":"wait",'
                '"recommended_action_reason":"r","missing_information":[],"risks":[],'
                '"urgency":"normal","operator_note":"n",'
                '"confidence":{"business_confidence":0.5,"action_confidence":0.5}}'
            ),
            "assembled_context": {"case_id_used": "case-1"},
        }

    snapshot = {"source_message": {"subject": "pompa", "message_id": "m1"}}
    intake_result = {"reason": "lead", "business_area": "sales", "decision": {"action": "wait"}}
    case_link_result = {"case_id": "case-1", "engagement_id": "eng-1"}

    with patch("business_reasoner.run_central_structured_stage", side_effect=_fake_stage):
        result = run_business_reasoning(
            settings=settings,
            snapshot=snapshot,
            intake_result=intake_result,
            case_link_result=case_link_result,
            context_bundle=context_bundle,
            business_context_bundle={"business_areas": {}},
        )

    assert captured.get("context_bundle") == context_bundle
    assert captured.get("engagement_id") == "eng-1"
    assert result.get("execution_metadata", {}).get("assembled_context")


def test_run_business_reasoning_valid_output_does_not_log_schema_import_failure() -> None:
    settings = _minimal_settings()
    warnings: list[tuple[object, dict[str, object]]] = []

    def _fake_stage(*_args, **_kwargs):
        return {
            "response_text": (
                '{"business_interpretation":"Klient pyta o serwis.",'
                '"business_area":"service","customer_state_guess":"needs_response",'
                '"recommended_next_action":"reply",'
                '"recommended_action_reason":"W mailu jest prosba o termin.",'
                '"missing_information":[],"risks":[], "urgency":"normal",'
                '"operator_note":"Odpowiedz po sprawdzeniu kalendarza.",'
                '"confidence":{"business_confidence":0.8,"action_confidence":0.7},'
                '"evidence_refs":[{"source_id":"msg-1"}]}'
            ),
            "assembled_context": {"case_id_used": "case-1"},
        }

    def _capture_warning(message, *args, **kwargs):
        warnings.append((message, kwargs))

    with (
        patch("business_reasoner.run_central_structured_stage", side_effect=_fake_stage),
        patch("business_reasoner.logger.warning", side_effect=_capture_warning),
    ):
        result = run_business_reasoning(
            settings=settings,
            snapshot={"source_message": {"subject": "serwis", "message_id": "m1"}},
            intake_result={"reason": "lead", "case_assessment": {"state_detected": "new"}},
            case_link_result={"case_id": "case-1"},
            context_bundle={"case_id": "case-1"},
            business_context_bundle={},
        )

    assert result["confidence"]["business_confidence"] == 0.8
    assert result["confidence"]["action_confidence"] == 0.7
    assert result["business_interpretation"] == "Klient pyta o serwis."
    assert result["recommended_next_action"] == "reply"
    assert result["evidence_refs"][0]["source_id"] == "msg-1"
    assert not [
        item
        for item in warnings
        if item[0] == "Business reasoning Pydantic validation failed"
    ]
