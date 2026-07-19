from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from business_reasoner import fallback_business_reasoning, parse_and_validate_business_reasoning
from tests.fixture_helpers import run_fixture


class BusinessReasonerContractTests(unittest.TestCase):
    def test_fallback_contract_is_safe(self) -> None:
        result = fallback_business_reasoning(reason="fixture")
        self.assertEqual(result["recommended_next_action"], "escalate_review")
        self.assertEqual(result["business_area"], "unknown")
        self.assertIn("unsupported_claims", result)
        self.assertIn("fixture", result["unsupported_claims"][0])

    def test_business_reasoning_preserves_evidence_ledger(self) -> None:
        raw = """
        {
          "business_interpretation": "Klient pyta o serwis.",
          "business_area": "service",
          "customer_state_guess": "needs_response",
          "recommended_next_action": "reply",
          "recommended_action_reason": "W mailu jest prosba o termin.",
          "missing_information": [],
          "risks": [],
          "urgency": "normal",
          "operator_note": "Odpowiedz po sprawdzeniu kalendarza.",
          "confidence": {"business_confidence": 0.8, "action_confidence": 0.7},
          "evidence_refs": [{"source_id": "msg-1", "excerpt": "prosze o termin"}],
          "assumptions": ["Termin wymaga potwierdzenia"],
          "unsupported_claims": ["Brak potwierdzonego slotu"],
          "conflict_refs": [{"source_id": "doc-1", "field_name": "service_date", "excerpt": "should_strip"}]
        }
        """
        result = parse_and_validate_business_reasoning(raw)
        self.assertEqual(result["evidence_refs"][0]["source_id"], "msg-1")
        self.assertNotIn("excerpt", result["evidence_refs"][0])
        self.assertEqual(result["evidence_refs"][0].get("trust_level"), "low")
        self.assertFalse(result["evidence_refs"][0].get("can_answer_customer"))
        self.assertEqual(result["assumptions"], ["Termin wymaga potwierdzenia"])
        self.assertEqual(result["unsupported_claims"], ["Brak potwierdzonego slotu"])
        self.assertEqual(result["conflict_refs"][0]["field_name"], "service_date")
        self.assertNotIn("excerpt", result["conflict_refs"][0])

    def test_fixture_contract_validates(self) -> None:
        result = run_fixture("new_lead")
        self.assertEqual(result["business_result"]["business_area"], "lead")


if __name__ == "__main__":
    unittest.main()
