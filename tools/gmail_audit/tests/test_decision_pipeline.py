"""Tests for Decision Pipeline run and DecisionCandidate (wave 2)."""

from __future__ import annotations

import unittest

from case_intelligence import build_case_intelligence
from decision_candidate import DECISION_CANDIDATE_SCHEMA_VERSION
from decision_pipeline import replay_decision_pipeline_run, run_decision_pipeline


class DecisionPipelineTests(unittest.TestCase):
    def test_pipeline_produces_candidate(self) -> None:
        snapshot = {
            "source_message": {
                "message_id": "m1",
                "thread_id": "t1",
                "subject": "Awaria",
                "body": "Pompa nie działa",
                "date": "2026-01-02T12:00:00Z",
            }
        }
        intake = {
            "decision": {"action": "create_case"},
            "business_area": "service",
            "priority": "high",
            "case_assessment": {"case_family": "unknown"},
            "thread": {"thread_id": "t1"},
        }
        cl = {"decision": "unlinked", "confidence": 0.2}
        ci = build_case_intelligence(
            snapshot=snapshot,
            intake_result=intake,
            case_link_result=cl,
            business_result={"risks": []},
            reply_result={},
            action_plan_result={"primary_action": "hold"},
        )
        run = run_decision_pipeline(
            snapshot=snapshot,
            intake_result=intake,
            case_link_result=cl,
            business_result={},
            intelligence=ci,
            understanding_output=None,
        )
        cand = run["outputs"]["decision_candidate"]
        self.assertEqual(cand["schema_version"], DECISION_CANDIDATE_SCHEMA_VERSION)
        self.assertTrue(str(cand.get("decision_candidate_id") or "").startswith("dc_"))
        self.assertTrue(run.get("projection_ready"))
        self.assertIn("pipeline_run_id", (cand.get("lineage") or {}))

    def test_candidate_understanding_link_and_lineage(self) -> None:
        snapshot = {
            "source_message": {
                "message_id": "m-uo",
                "thread_id": "t-uo",
                "subject": "Serwis",
                "body": "Opis",
                "date": "2026-01-02T12:00:00Z",
            }
        }
        intake = {
            "decision": {"action": "create_case"},
            "business_area": "service",
            "priority": "high",
            "case_assessment": {"case_family": "unknown"},
            "thread": {"thread_id": "t-uo"},
        }
        cl = {"decision": "unlinked", "confidence": 0.2}
        ci = build_case_intelligence(
            snapshot=snapshot,
            intake_result=intake,
            case_link_result=cl,
            business_result={"risks": []},
            reply_result={},
            action_plan_result={"primary_action": "hold"},
        )
        uo = {
            "schema_version": "understanding_output.v1",
            "understanding_output_id": "uo_test_link_123",
            "source_signal_id": "m-uo",
            "case_id": "case_from_ci",
        }
        run = run_decision_pipeline(
            snapshot=snapshot,
            intake_result=intake,
            case_link_result=cl,
            business_result={},
            intelligence=ci,
            understanding_output=uo,
        )
        cand = run["outputs"]["decision_candidate"]
        self.assertEqual(cand.get("understanding_output_id"), "uo_test_link_123")
        lin = cand.get("lineage") or {}
        self.assertEqual(lin.get("pipeline_run_id"), run.get("pipeline_run_id"))
        self.assertEqual(lin.get("intake_case_link_input_hash"), run.get("input_hash"))
        self.assertIn("staleness_scope", lin)

    def test_pipeline_degrades_when_context_not_ready(self) -> None:
        snapshot = {
            "source_message": {
                "message_id": "m-blocked",
                "thread_id": "t-blocked",
                "subject": "Awaria",
                "body": "Pompa nie dziala",
                "date": "2026-01-02T12:00:00Z",
            }
        }
        intake = {
            "decision": {"action": "create_case"},
            "business_area": "service",
            "priority": "high",
            "case_assessment": {"case_family": "service"},
            "thread": {"thread_id": "t-blocked"},
        }
        cl = {"decision": "pending_adjudication", "confidence": 0.2}
        ci = build_case_intelligence(
            snapshot=snapshot,
            intake_result=intake,
            case_link_result=cl,
            business_result={"risks": []},
            reply_result={},
            action_plan_result={"primary_action": "hold"},
            case_context_pack={
                "context_quality": {
                    "ready_for_decision": False,
                    "operator_review_possible": True,
                    "action_readiness": "not_ready",
                    "not_ready_reasons": ["weak_or_missing_evidence"],
                    "weak_evidence_count": 1,
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
        )
        run = run_decision_pipeline(
            snapshot=snapshot,
            intake_result=intake,
            case_link_result=cl,
            business_result={},
            intelligence=ci,
            understanding_output=None,
        )

        cand = run["outputs"]["decision_candidate"]
        self.assertEqual(cand["recommended_mode"], "not_ready")
        self.assertEqual(cand["decision_basis"], [])
        self.assertIn("weak_or_missing_evidence", cand["not_ready_reasons"])
        self.assertNotIn("client@example.invalid", repr(cand))

    def test_replay_hash_match(self) -> None:
        snapshot = {"source_message": {"message_id": "m2", "thread_id": "t2", "subject": "x", "body": "y", "date": "2026-01-03Z"}}
        intake = {"decision": {"action": "hold"}, "business_area": "sales", "priority": "low", "case_assessment": {}, "thread": {}}
        ci = build_case_intelligence(
            snapshot=snapshot,
            intake_result=intake,
            case_link_result={},
            business_result={},
            reply_result={},
            action_plan_result={"primary_action": "hold"},
        )
        first = run_decision_pipeline(
            snapshot=snapshot,
            intake_result=intake,
            case_link_result={},
            business_result={},
            intelligence=ci,
            understanding_output=None,
        )
        second = replay_decision_pipeline_run(
            snapshot=snapshot,
            intake_result=intake,
            case_link_result={},
            business_result={},
            intelligence=ci,
            understanding_output=None,
            saved_run=first,
        )
        self.assertTrue(second.get("replay_input_hash_match"))


if __name__ == "__main__":
    unittest.main()
