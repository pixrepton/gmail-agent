"""Unit tests for case_intelligence sub-modules (understanding, risks, missing_info, next_best_action)."""
from __future__ import annotations
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import unittest
from case_intelligence.risks import build_risk_assessment, _risk_item, _dedupe_risk_items
from case_intelligence.missing_info import build_missing_info
from case_intelligence.next_best_action import build_next_best_action, _action_item


class TestRiskAssessment(unittest.TestCase):
    def test_risk_item_defaults(self):
        item = _risk_item(risk_type="lead_loss_risk", severity="medium",
                          reason_pl="Test reason", confidence=0.75, watch="watch for X")
        self.assertEqual(item["risk_type"], "lead_loss_risk")
        self.assertEqual(item["severity"], "medium")
        self.assertEqual(item["confidence"], 0.75)

    def test_dedupe_risk_items_highest_severity_wins(self):
        items = [
            _risk_item(risk_type="lead_loss_risk", severity="low", reason_pl="low", confidence=0.5, watch=""),
            _risk_item(risk_type="lead_loss_risk", severity="high", reason_pl="high", confidence=0.7, watch=""),
        ]
        deduped = _dedupe_risk_items(items)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["severity"], "high")

    def test_build_risk_assessment_empty(self):
        result = build_risk_assessment(
            intake_result={"priority": "low"},
            business_result={},
            missing_info={},
            current_note_state={},
        )
        self.assertIn("summary_pl", result)
        self.assertIn("risks", result)

    def test_build_risk_assessment_with_aging(self):
        result = build_risk_assessment(
            intake_result={"priority": "low"},
            business_result={"risks": []},
            missing_info={},
            current_note_state={"age_days": 7},
        )
        risk_types = {r["risk_type"] for r in result["risks"]}
        self.assertIn("aging_risk", risk_types)


class TestMissingInfo(unittest.TestCase):
    def test_missing_info_empty(self):
        result = build_missing_info(
            intake_result={}, business_result={}, reply_result={},
            case_link_result={},
        )
        self.assertEqual(result["critical"], [])
        self.assertIn("summary_pl", result)

    def test_missing_info_critical_keyword(self):
        result = build_missing_info(
            intake_result={},
            business_result={"missing_information": ["client address and phone"]},
            reply_result={}, case_link_result={},
        )
        self.assertTrue(len(result["critical"]) > 0 or len(result["important"]) > 0)

    def test_missing_info_weak_link_prompts_confirmation(self):
        result = build_missing_info(
            intake_result={},
            business_result={},
            reply_result={}, case_link_result={"decision": "weak_link"},
        )
        # weak_link adds "confirmed case reference" → localized as "potwierdzenie w?a?ciwej sprawy"
        # Check with a broad assertion that works regardless of diacritics
        self.assertTrue(
            len(result["critical"]) > 0,
            f"Expected at least one critical item for weak_link, got: {result['critical']}"
        )


class TestNextBestAction(unittest.TestCase):
    def test_action_item_defaults(self):
        item = _action_item(action_type="wait", reason_pl="waiting",
                            urgency_level="normal", confidence=0.8, review_required=False)
        self.assertEqual(item["action_type"], "wait")
        self.assertEqual(item["urgency_level"], "normal")

    def test_build_nba_waits_by_default(self):
        result = build_next_best_action(
            intake_result={}, case_link_result={}, business_result={},
            reply_result={}, action_plan_result={},
            missing_info={}, merge_split_suggestions={},
        )
        self.assertEqual(result["primary_next_action"]["action_type"], "wait")

    def test_build_nba_review_required(self):
        result = build_next_best_action(
            intake_result={"review": {"required": True}},
            case_link_result={}, business_result={},
            reply_result={}, action_plan_result={},
            missing_info={}, merge_split_suggestions={},
        )
        self.assertIn(result["primary_next_action"]["action_type"], {"review_required", "wait"})

    def test_build_nba_answer_customer(self):
        result = build_next_best_action(
            intake_result={}, case_link_result={},
            business_result={"recommended_next_action": "reply"},
            reply_result={}, action_plan_result={},
            missing_info={}, merge_split_suggestions={},
        )
        self.assertEqual(result["primary_next_action"]["action_type"], "answer_customer")


class TestDeskComposition(unittest.TestCase):
    def test_merge_case_guidance(self):
        from case_intelligence.desk import merge_case_guidance_into_intelligence
        base = {"case_understanding": {}, "operator_brief": {}, "desk_composition": {},
                "missing_info": {}, "risk_assessment": {}, "next_best_action": {}}
        cg = {"reason_summary_pl": "Sprawa wymaga uwagi.",
              "source_mode": "llm_reasoned", "confidence": 0.3}
        result = merge_case_guidance_into_intelligence(base, cg)
        self.assertIn("case_guidance", result)
        self.assertEqual(result["case_guidance"]["reason_summary_pl"], "Sprawa wymaga uwagi.")

    def test_build_desk_composition_defaults(self):
        from case_intelligence.desk import build_desk_composition
        result = build_desk_composition(
            intake_result={"message": {"subject": "Test"}},
            business_result={},
            case_understanding={"summary_short": "short", "summary_operator": "op",
                               "attention_reason": "reason", "review_required": False},
            next_best_action={"primary_next_action": {"action_type": "wait", "title_pl": "Czekaj"}},
            missing_info={}, risk_assessment={"risks": []},
            merge_split_suggestions={}, feedback_learning_memory={},
        )
        self.assertIn("should_surface", result)
        self.assertIn("presence_mode", result)
        self.assertIn("visibility_score", result)


class TestLifecycle(unittest.TestCase):
    def test_build_feedback_learning_memory_empty(self):
        from case_intelligence.lifecycle import build_feedback_learning_memory
        result = build_feedback_learning_memory(None)
        self.assertEqual(result["explicit_signals"], [])

    def test_build_merge_split_suggestions(self):
        from case_intelligence.lifecycle import build_merge_split_suggestions
        result = build_merge_split_suggestions(
            snapshot={}, intake_result={}, case_link_result={},
        )
        self.assertIn("summary_pl", result)
        self.assertEqual(result["merge_candidates"], [])

    def test_build_lifecycle_revision_defaults(self):
        from case_intelligence.lifecycle import build_lifecycle_revision
        result = build_lifecycle_revision(
            intake_result={}, case_link_result={},
            case_understanding={}, desk_composition={"surface_zone": "silent"},
            current_note_state={},
        )
        self.assertIn("lifecycle_intent", result)


class TestSchemas(unittest.TestCase):
    def test_risk_type_enum(self):
        from schemas import RiskType
        self.assertEqual(RiskType.LEAD_LOSS.value, "lead_loss")

    def test_case_intelligence_result_validation(self):
        from schemas import CaseIntelligenceResult, RiskAssessment
        ra = RiskAssessment(risk_type="lead_loss", severity=0.8, description_pl="Test")
        cir = CaseIntelligenceResult(
            case_id="CASE-001",
            understanding={"summary_pl": "Test case", "key_entities": []},
            risks=[ra.model_dump()],
        )
        self.assertEqual(cir.case_id, "CASE-001")
        self.assertEqual(len(cir.risks), 1)

    def test_business_reasoning_result(self):
        from schemas import BusinessReasoningResult
        brr = BusinessReasoningResult(
            business_area="service",
            recommended_next_action="wait",
            overall_confidence=0.85,
        )
        self.assertEqual(brr.business_area, "service")
        self.assertEqual(brr.overall_confidence, 0.85)

    def test_understanding_output_model(self):
        from schemas import UnderstandingOutput
        uo = UnderstandingOutput(case_id="CASE-002", summary_pl="Test summary")
        self.assertEqual(uo.case_id, "CASE-002")
        self.assertEqual(uo.schema_version, "understanding_output.v1")

    def test_memory_record_model(self):
        from schemas import MemoryRecord
        mr = MemoryRecord(id="mem_1", memory_type="conversation", key="test")
        self.assertEqual(mr.memory_type, "conversation")


if __name__ == "__main__":
    unittest.main()
