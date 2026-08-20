"""SVC-05: ambiguous service gaps should ask the customer, not the operator.

The fix is a general BusinessReasoning normalization, not a case-specific
exception: when intake has already marked the signal ``ambiguous_signal`` in the
service area, review is required only because customer data is missing, and the
reasoner names concrete gaps, then ``collect_data`` is the correct action and
the drafter must not be skipped.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from business_reasoner import parse_and_validate_business_reasoning
from draft_path_observability import evaluate_draft_gate


def _br_raw(
    *,
    next_action: str = "escalate_review",
    business_area: str = "service",
    customer_state: str = "unclear",
    missing_information: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "business_interpretation": "Ogolne zgloszenie serwisowe bez danych.",
            "business_area": business_area,
            "customer_state_guess": customer_state,
            "recommended_next_action": next_action,
            "recommended_action_reason": "Brak danych do bezpiecznego dzialania.",
            "missing_information": missing_information
            if missing_information is not None
            else ["opis usterki / objawow", "adres instalacji"],
            "risks": [],
            "urgency": "normal",
            "operator_note": "",
            "confidence": {"business_confidence": 0.3, "action_confidence": 0.7},
        },
        ensure_ascii=False,
    )


def _intake(
    *,
    business_area: str = "service",
    review_required: bool = True,
    flags: list[str] | None = None,
) -> dict[str, Any]:
    flags = flags if flags is not None else ["ambiguous_signal", "insufficient_thread_context"]
    return {
        "business_area": business_area,
        "review_required": review_required,
        "review": {"required": review_required, "flags": flags},
        "decision": {"action": "review" if review_required else "create_case"},
    }


class Svc05CustomerClarificationTests(unittest.TestCase):
    def test_ambiguous_service_gap_normalizes_to_collect_data(self) -> None:
        intake = _intake()
        result = parse_and_validate_business_reasoning(_br_raw(), intake_result=intake)
        self.assertEqual(result["recommended_next_action"], "collect_data")
        self.assertTrue(result["reply_recommended"])
        self.assertTrue(result["customer_clarification_possible"])
        notes = result.get("normalization_notes") or []
        self.assertTrue(
            any(str(note.get("reason_code")) == "customer_clarification_possible" for note in notes if isinstance(note, dict))
        )

    def test_normalized_result_runs_draft_gate_despite_intake_review(self) -> None:
        intake = _intake()
        business = parse_and_validate_business_reasoning(_br_raw(), intake_result=intake)
        gate = evaluate_draft_gate(
            {"source_message": {"sender": "klient@example.com", "message_id": "svc05"}},
            intake,
            business,
        )
        self.assertEqual(gate["decision"], "RUN")
        self.assertEqual(gate["primary_reason_code"], "BR_ACTION_COLLECT_DATA")

    def test_svc01_like_real_fault_not_rewritten(self) -> None:
        intake = _intake(review_required=False, flags=["weak_case_link"])
        result = parse_and_validate_business_reasoning(
            _br_raw(customer_state="unclear", missing_information=["opis objawow", "adres"]),
            intake_result=intake,
        )
        self.assertEqual(result["recommended_next_action"], "escalate_review")

    def test_svc02_like_no_ambiguous_flag_not_rewritten(self) -> None:
        intake = _intake(flags=["weak_case_link"])
        result = parse_and_validate_business_reasoning(_br_raw(), intake_result=intake)
        self.assertEqual(result["recommended_next_action"], "escalate_review")

    def test_mi03_like_competing_signals_not_rewritten(self) -> None:
        intake = _intake(flags=["multiple_competing_signals", "insufficient_thread_context"])
        result = parse_and_validate_business_reasoning(_br_raw(), intake_result=intake)
        self.assertEqual(result["recommended_next_action"], "escalate_review")

    def test_dec01_like_non_service_area_not_rewritten(self) -> None:
        intake = _intake(
            business_area="internal_coordination",
            flags=["insufficient_thread_context", "legal_or_compliance_risk"],
        )
        result = parse_and_validate_business_reasoning(
            _br_raw(business_area="internal_coordination", customer_state="active_case"),
            intake_result=intake,
        )
        self.assertEqual(result["recommended_next_action"], "escalate_review")

    def test_existing_collect_data_stays_collect_data(self) -> None:
        intake = _intake()
        result = parse_and_validate_business_reasoning(
            _br_raw(next_action="collect_data"),
            intake_result=intake,
        )
        self.assertEqual(result["recommended_next_action"], "collect_data")


if __name__ == "__main__":
    unittest.main()
