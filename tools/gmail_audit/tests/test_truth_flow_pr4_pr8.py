"""PR-4..PR-8 contract tests for policy attach, shadow profile, adjudication refresh, projection transport."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from intelligence_shadow_profile import apply_intelligence_shadow_profile
from policy_action_proposal import attach_policy_and_proposals
from projection_refresh_contract import build_adjudication_projection_refresh
from projection_snapshot_transport import build_operator_projection_snapshot, v2_projection_from_snapshot
from signal_reconciler import ReconcileResult, SnapshotRefreshDecision
from projection_refresh_rules import ProjectionRefreshDecision as Prd


class TruthFlowPr4Tests(unittest.TestCase):
    def test_attach_policy_and_proposals_is_single_evaluate_path(self) -> None:
        mb: dict = {}
        ci: dict = {"decision_candidate": {"decision_candidate_id": "dc-1"}}
        with patch("policy_action_proposal.evaluate_policy_for_intake_stage") as ev, patch(
            "policy_action_proposal.attach_policy_evaluation_to_results"
        ) as att, patch("policy_decision.build_policy_decision", return_value={"status": "APPROVED"}), patch(
            "action_proposal_v2.build_policy_gated_action_proposals_v2_bundle",
            return_value={"action_proposals_v2": [{"proposal_id": "apv2-1"}]},
        ):
            ev.return_value = (SimpleNamespace(to_dict=lambda: {"status": "APPROVED"}), {"proposal_id": "p1"})
            attach_policy_and_proposals(
                action_plan_result={"primary_action": "review"},
                intake_result={"decision": {"action": "review"}},
                case_link_result={},
                entity_link_result={},
                case_intelligence_result=ci,
                mailbox_memory_result=mb,
                snapshot={"source_message": {"message_id": "m1"}},
                case_snapshot_hot_state=None,
                run_state={"run_id": "r1"},
                settings=SimpleNamespace(
                    action_proposal_v2_enabled=True,
                    decision_pipeline_dry_run_only=True,
                ),
            )
            self.assertEqual(ev.call_count, 1)
            self.assertEqual(att.call_count, 1)
            self.assertIn("action_proposals_v2", ci)


class TruthFlowPr5Tests(unittest.TestCase):
    def test_shadow_profile_enables_intelligence_flags(self) -> None:
        cfg: dict = {}
        with patch.dict(os.environ, {"INTELLIGENCE_SHADOW_PROJECTION": "1"}, clear=False):
            applied = apply_intelligence_shadow_profile(cfg, signal_runtime_mode="shadow")
        self.assertTrue(applied)
        self.assertTrue(cfg.get("understanding_output_enabled"))
        self.assertTrue(cfg.get("decision_pipeline_dry_run_only"))


class TruthFlowPr6Tests(unittest.TestCase):
    def test_adjudication_confirm_still_requests_refresh(self) -> None:
        row = build_adjudication_projection_refresh(adjudication_kind="confirm_same_case", case_id="case-1")
        self.assertTrue(row["should_refresh"])
        self.assertFalse(row["reconcile_ran"])

    def test_adjudication_reject_uses_reconcile_decision(self) -> None:
        rr = ReconcileResult(
            signal_id="sig-1",
            source_kind="gmail",
            signal_kind="gmail_message_observed",
            processing_state="reconciled",
            projection_refresh_decision=Prd(
                should_refresh=True,
                refresh_kind="case_and_note",
                reason="gmail_message_refresh",
            ),
            snapshot_refresh_decision=SnapshotRefreshDecision(True, "incremental_refresh", "x"),
        )
        row = build_adjudication_projection_refresh(
            adjudication_kind="reject_same_case",
            case_id="case-1",
            reconcile_result=rr,
        )
        self.assertTrue(row["reconcile_ran"])
        self.assertTrue(row["should_refresh"])


class TruthFlowPr7Pr8Tests(unittest.TestCase):
    def test_operator_snapshot_includes_decision_view_on_v2(self) -> None:
        ci = {
            "understanding_output": {"operator_explanation": {"essence_pl": "Test sedno"}},
            "decision_pipeline": {"outputs": {"decision_candidate": {}}},
            "policy_decision": {},
            "action_proposals_v2": [],
        }
        snap = build_operator_projection_snapshot(
            {"decision": {"action": "review"}, "review": {"flags": []}, "source": {}, "message": {}, "thread": {}},
            stage_outputs={
                "preclassification_result": {"lane": "intake_llm"},
                "case_link_result": {"selected_case_key": "case-1"},
                "business_reasoning_result": {},
                "reply_draft_result": {},
                "action_plan_result": {"primary_action": "review"},
                "case_intelligence_result": ci,
                "mailbox_memory_result": {"context_pack": {"vnext": {"case_summary": {"summary_text": "x"}}}},
            },
            run_id="run-1",
        )
        v2 = v2_projection_from_snapshot(snap)
        self.assertIn("signal_projection", v2)
        self.assertIn("decision_view", v2)
        self.assertTrue(snap.get("decision_view"))
        self.assertEqual(snap.get("context_tray_set", {}).get("schema_version"), "context_tray_set.v1")
        self.assertEqual(snap.get("projection_envelope", {}).get("schema_version"), "projection_envelope.v1")
        self.assertTrue(snap.get("projection_validation", {}).get("ok"))
        self.assertEqual(snap.get("projection_quality_metrics", {}).get("schema_version"), "projection_quality_metrics.v1")


if __name__ == "__main__":
    unittest.main()
