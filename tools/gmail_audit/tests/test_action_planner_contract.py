from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from tests.fixture_helpers import run_fixture
from action_planner import plan_actions


class ActionPlannerContractTests(unittest.TestCase):
    def test_lead_fixture_prepares_reply(self) -> None:
        result = run_fixture("new_lead")
        self.assertEqual(result["action_plan"]["primary_action"], "prepare_reply")
        self.assertTrue(result["action_plan"]["safe_for_operator_projection"])

    def test_review_fixture_creates_review(self) -> None:
        result = run_fixture("urgent_service")
        self.assertEqual(result["action_plan"]["primary_action"], "create_review")

    def test_document_and_calendar_blockers_raise_review_priority(self) -> None:
        result = plan_actions(
            {
                "decision": {"action": "ignore"},
                "review_required": False,
                "confidence": {"decision_confidence": 0.95, "case_link_confidence": 0.95},
            },
            {"decision": "linked", "confidence": 0.95},
            {
                "recommended_next_action": "wait",
                "urgency": "normal",
                "confidence": {"action_confidence": 0.9},
                "unsupported_claims": ["Niepotwierdzony termin"],
            },
            {},
            case_context_pack={
                "document_intelligence": {
                    "document_conflicts": [{"field_name": "amount_total", "severity": "medium"}],
                    "fields_requiring_review": [{"field_name": "service_date"}],
                },
                "calendar": {"calendar_risk": "possible_conflict"},
            },
        )
        joined = " | ".join(result["operator_checklist"])
        self.assertEqual(result["review_priority"], "high")
        self.assertFalse(result["safe_for_live_push"])
        self.assertFalse(result["safe_for_operator_projection"])
        self.assertIn("document conflict", joined)
        self.assertIn("calendar", joined)
        # "Niepotwierdzony termin" is a calibrated uncertainty disclosure, not a
        # categorical guarantee -- surfaced for awareness under its own label, but
        # review_priority/safe_for_live_push above are already forced by the
        # document/calendar blockers regardless of this claim's classification.
        self.assertIn("unconfirmed claim", joined)
        self.assertLess(result["confidence"], 0.85)

    def test_weighted_confidence_penalizes_unsafe_guarantee_claim(self) -> None:
        # "Gwarantujemy" is a categorical guarantee BusinessReasoning can never
        # actually support -- this is the genuinely unsafe class of unsupported_claims,
        # distinct from a calibrated uncertainty disclosure (see the test below).
        result = plan_actions(
            {
                "decision": {"action": "ignore"},
                "review_required": False,
                "confidence": {"decision_confidence": 1.0, "case_link_confidence": 1.0},
            },
            {"decision": "linked", "confidence": 1.0},
            {
                "recommended_next_action": "wait",
                "urgency": "low",
                "confidence": {"action_confidence": 1.0},
                "evidence_refs": [],
                "unsupported_claims": ["Gwarantujemy termin realizacji na jutro"],
            },
            {},
        )
        self.assertEqual(result["execution_metadata"]["confidence_components"]["evidence_ledger"], 0.2)
        self.assertLess(result["confidence"], 1.0)
        self.assertFalse(result["safe_for_live_push"])

    def test_calibrated_uncertainty_claim_does_not_penalize_confidence_or_block(self) -> None:
        # "Brak dowodu na wskazany termin dostawy" is BusinessReasoning honestly
        # disclosing it could not verify something -- exactly what unsupported_claims
        # is prompted to record (business_reasoner.py's BUSINESS_REASONING_INSTRUCTIONS:
        # "niesprawdzone twierdzenia w unsupported_claims"). It is not a categorical
        # guarantee/promise and must not be treated the same as one.
        result = plan_actions(
            {
                "decision": {"action": "ignore"},
                "review_required": False,
                "confidence": {"decision_confidence": 1.0, "case_link_confidence": 1.0},
            },
            {"decision": "linked", "confidence": 1.0},
            {
                "recommended_next_action": "wait",
                "urgency": "low",
                "confidence": {"action_confidence": 1.0},
                "evidence_refs": [],
                "unsupported_claims": ["Brak dowodu na wskazany termin dostawy"],
            },
            {},
        )
        self.assertEqual(result["execution_metadata"]["confidence_components"]["evidence_ledger"], 0.65)
        self.assertGreaterEqual(result["confidence"], 0.85)
        self.assertTrue(result["safe_for_live_push"])
        joined = " | ".join(result["operator_checklist"])
        self.assertIn("unconfirmed claim", joined)


if __name__ == "__main__":
    unittest.main()
