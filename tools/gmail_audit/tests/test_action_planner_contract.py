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
        self.assertIn("unsupported claim", joined)
        self.assertLess(result["confidence"], 0.85)

    def test_weighted_confidence_penalizes_weak_evidence_ledger(self) -> None:
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
                "unsupported_claims": ["Brak dowodu"],
            },
            {},
        )
        self.assertEqual(result["execution_metadata"]["confidence_components"]["evidence_ledger"], 0.2)
        self.assertLess(result["confidence"], 1.0)


if __name__ == "__main__":
    unittest.main()
