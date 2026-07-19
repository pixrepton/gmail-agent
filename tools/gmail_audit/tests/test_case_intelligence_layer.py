from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from tests.fixture_helpers import run_fixture
from case_intelligence import build_case_intelligence


class CaseIntelligenceLayerTests(unittest.TestCase):
    def test_new_lead_fixture_builds_operator_facing_intelligence(self) -> None:
        result = run_fixture("new_lead")
        intelligence = result["case_intelligence"]

        self.assertEqual(intelligence["case_understanding"]["business_priority"], "medium")
        self.assertEqual(intelligence["next_best_action"]["primary_next_action"]["action_type"], "ask_for_missing_data")
        self.assertEqual(intelligence["desk_composition"]["presence_mode"], "advisory")
        self.assertEqual(intelligence["desk_composition"]["surface_zone"], "desk")
        self.assertIn("adres", intelligence["missing_info"]["summary_pl"].lower())
        self.assertTrue(intelligence["operator_brief"]["brief_pl"].strip())

    def test_reference_only_fixture_stays_off_the_desk(self) -> None:
        result = run_fixture("reference_only_mail")
        intelligence = result["case_intelligence"]

        self.assertFalse(intelligence["desk_composition"]["should_surface"])
        self.assertEqual(intelligence["desk_composition"]["presence_mode"], "silent")
        self.assertEqual(intelligence["desk_composition"]["surface_zone"], "silent")
        self.assertEqual(intelligence["lifecycle_revision"]["lifecycle_intent"], "suppress")

    def test_weak_case_link_fixture_creates_merge_review_signal(self) -> None:
        result = run_fixture("weak_case_link")
        intelligence = result["case_intelligence"]

        self.assertTrue(intelligence["case_understanding"]["review_required"])
        self.assertEqual(intelligence["next_best_action"]["primary_next_action"]["action_type"], "review_required")
        self.assertGreaterEqual(len(intelligence["merge_split_suggestions"]["merge_candidates"]), 1)

    def test_active_case_follow_up_prefers_update_over_new_case_noise(self) -> None:
        result = run_fixture("active_case_follow_up")
        intelligence = result["case_intelligence"]

        self.assertTrue(intelligence["case_understanding"]["case_id"].startswith("case_"))
        self.assertEqual(intelligence["next_best_action"]["primary_next_action"]["action_type"], "escalate_internal")
        self.assertEqual(intelligence["lifecycle_revision"]["lifecycle_intent"], "update")
        self.assertEqual(intelligence["desk_composition"]["surface_zone"], "desk")

    def test_context_quality_adds_review_context_without_policy_decision(self) -> None:
        intelligence = build_case_intelligence(
            snapshot={"summary_text": "Sprawa serwisowa"},
            intake_result={
                "message_id": "m1",
                "decision": {"action": "update_case"},
                "thread": {},
                "business_area": "service",
                "case_assessment": {"case_family": "service", "state_detected": "open"},
                "confidence": {"case_link_confidence": 0.8, "decision_confidence": 0.8},
                "review": {"required": False, "flags": []},
            },
            case_link_result={"case_id": "case_ctx_q", "decision": "existing_case"},
            business_result={
                "business_interpretation": "Klient zgłasza serwis.",
                "business_summary_short": "Serwis.",
                "confidence": {"business_confidence": 0.8, "action_confidence": 0.7},
            },
            reply_result={},
            action_plan_result={"primary_action": "hold", "confidence": 0.7},
            case_context_pack={
                "context_quality": {
                    "has_blocking_conflicts": True,
                    "has_blocking_gaps": False,
                    "conflict_count": 1,
                    "gap_count": 1,
                    "evidence_warning_count": 1,
                    "ready_for_operator_review": False,
                    "not_ready_reasons": ["blocking_conflicts"],
                },
                "conflicting_facts": [{"summary": "Sprzeczny status sprawy.", "severity": "blocking"}],
                "completeness_gaps": [{"summary": "Brak dowodu umówienia terminu.", "severity": "warning"}],
            },
        )

        self.assertTrue(intelligence["case_understanding"]["review_required"])
        self.assertIn("context_pack_blocking_conflict", intelligence["case_understanding"]["review_flags"])
        self.assertIn("Sprzeczny status sprawy.", intelligence["operator_brief"]["brief_pl"])
        self.assertNotIn("decision_candidate", intelligence)
        self.assertNotIn("policy_decision", intelligence)
        self.assertNotIn("action_proposals_v2", intelligence)

    def test_decision_candidate_attaches_only_when_enabled(self) -> None:
        kwargs = {
            "snapshot": {"source_message": {"message_id": "m-dc"}, "summary_text": "Sprawa serwisowa"},
            "intake_result": {
                "message_id": "m-dc",
                "decision": {"action": "update_case"},
                "thread": {},
                "business_area": "service",
                "priority": "high",
                "case_assessment": {"case_family": "service", "state_detected": "open"},
                "confidence": {"case_link_confidence": 0.8, "decision_confidence": 0.8},
                "review": {"required": False, "flags": []},
            },
            "case_link_result": {"case_id": "case_dc", "decision": "existing_case"},
            "business_result": {
                "business_interpretation": "Klient zgłasza serwis.",
                "business_summary_short": "Serwis.",
                "confidence": {"business_confidence": 0.8, "action_confidence": 0.7},
            },
            "reply_result": {},
            "action_plan_result": {"primary_action": "hold", "confidence": 0.7},
            "case_context_pack": {
                "context_quality": {
                    "ready_for_decision": False,
                    "operator_review_possible": True,
                    "action_readiness": "review_only",
                    "not_ready_reasons": ["weak_or_missing_evidence"],
                    "weak_evidence_count": 1,
                    "evidence_warning_count": 1,
                },
                "conflicting_facts": [
                    {
                        "summary": "client@example.invalid vs other@example.invalid",
                        "projection_summary": "Sprzeczne dane kontaktowe - wymaga weryfikacji operatora.",
                        "decision_usable": False,
                        "evidence_status": "missing",
                        "evidence_refs": [],
                        "values": ["client@example.invalid", "other@example.invalid"],
                    }
                ],
            },
        }

        disabled = build_case_intelligence(**kwargs)
        enabled = build_case_intelligence(**kwargs, decision_candidate_enabled=True)

        self.assertNotIn("decision_candidate", disabled)
        cand = enabled["decision_candidate"]
        self.assertTrue(cand["decision_candidate_id"].startswith("dc_"))
        self.assertEqual(cand["automation_eligibility"], "not_eligible")
        self.assertEqual(cand["recommended_mode"], "operator_review_only")
        self.assertEqual(cand["decision_basis"], [])
        self.assertTrue(cand["review_only_warnings"])
        self.assertNotIn("policy_decision", enabled)
        self.assertNotIn("action_proposals_v2", enabled)
        self.assertNotIn("client@example.invalid", repr(cand))


if __name__ == "__main__":
    unittest.main()
