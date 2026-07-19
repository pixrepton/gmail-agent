"""Invariants for ActionProposal v2 and PolicyDecision."""

from __future__ import annotations

import unittest

from action_proposal_v2 import (
    ACTION_PROPOSAL_V2_SCHEMA_VERSION,
    assert_action_proposal_v2_execution_allowed,
    action_proposal_v2_policy_cleared_envelope,
    build_action_proposals_v2,
    build_policy_gated_action_proposals_v2_bundle,
    normalize_planner_primary_for_v2,
)
from policy_decision import POLICY_DECISION_SCHEMA_VERSION, build_policy_decision


class ActionProposalV2InvariantTests(unittest.TestCase):
    def test_no_v2_without_policy_decision_id(self) -> None:
        raw = {"schema_version": ACTION_PROPOSAL_V2_SCHEMA_VERSION, "allowed_by_policy": True, "action_mode": "preview"}
        ok, err = assert_action_proposal_v2_execution_allowed(raw)
        self.assertFalse(ok)
        self.assertIn("policy", err)

    def test_v2_requires_candidate(self) -> None:
        raw = {
            "schema_version": ACTION_PROPOSAL_V2_SCHEMA_VERSION,
            "policy_decision_id": "pdec_x",
            "allowed_by_policy": True,
            "action_mode": "preview",
        }
        ok, err = assert_action_proposal_v2_execution_allowed(raw)
        self.assertFalse(ok)

    def test_build_chain(self) -> None:
        cand = {"decision_candidate_id": "dc_1", "case_id": "c1", "next_best_action": "wait", "evidence_refs": []}
        pr = {"status": "APPROVED", "effective_risk_class": "low", "trace_id": "t1", "policy_basis": [], "failed_rules": [], "warnings": [], "required_adjustments": [], "requires_review": False}
        pd = build_policy_decision(policy_report=pr, decision_candidate_id="dc_1")
        self.assertEqual(pd["schema_version"], POLICY_DECISION_SCHEMA_VERSION)
        props = build_action_proposals_v2(
            decision_candidate=cand, policy_decision=pd, primary_action_type="hold", dry_run_only=True
        )
        self.assertTrue(props)
        self.assertEqual(props[0]["schema_version"], ACTION_PROPOSAL_V2_SCHEMA_VERSION)
        self.assertEqual(props[0]["decision_candidate_id"], "dc_1")
        self.assertEqual(props[0]["action_mode"], "dry_run")
        self.assertTrue(props[0]["requires_operator_approval"])
        self.assertEqual(props[0]["execution_result_ref"], "")

    def test_needs_human_proposal_is_not_executable(self) -> None:
        cand = {"decision_candidate_id": "dc_2", "case_id": "c2", "next_best_action": "wait", "evidence_refs": []}
        pr = {
            "status": "NEEDS_HUMAN",
            "effective_risk_class": "medium",
            "trace_id": "t2",
            "policy_basis": ["human review"],
            "failed_rules": [],
            "warnings": [],
            "required_adjustments": [],
            "requires_review": True,
        }
        pd = build_policy_decision(policy_report=pr, decision_candidate_id="dc_2")
        props = build_action_proposals_v2(
            decision_candidate=cand,
            policy_decision=pd,
            primary_action_type="hold",
            dry_run_only=True,
        )
        self.assertTrue(props)
        self.assertEqual(props[0]["action_type"], "ask_for_operator_adjudication")
        self.assertFalse(props[0]["allowed_by_policy"])
        ok, err = assert_action_proposal_v2_execution_allowed(props[0])
        self.assertFalse(ok)
        self.assertEqual(err, "not_allowed_by_policy")

    def test_empty_when_missing_ids(self) -> None:
        cand = {"decision_candidate_id": "", "case_id": "c1", "next_best_action": "wait", "evidence_refs": []}
        pr = {"status": "APPROVED", "effective_risk_class": "low", "trace_id": "t1", "policy_basis": [], "failed_rules": [], "warnings": [], "required_adjustments": [], "requires_review": False}
        pd = build_policy_decision(policy_report=pr, decision_candidate_id="")
        props = build_action_proposals_v2(
            decision_candidate=cand, policy_decision=pd, primary_action_type="hold", dry_run_only=True
        )
        self.assertEqual(props, [])

    def test_policy_cleared_envelope_requires_both_ids(self) -> None:
        forged = {
            "schema_version": ACTION_PROPOSAL_V2_SCHEMA_VERSION,
            "allowed_by_policy": True,
            "decision_candidate_id": "",
            "policy_decision_id": "pdec_x",
            "action_mode": "preview",
        }
        self.assertFalse(action_proposal_v2_policy_cleared_envelope(forged))
        forged2 = {
            "schema_version": ACTION_PROPOSAL_V2_SCHEMA_VERSION,
            "allowed_by_policy": True,
            "decision_candidate_id": "dc_x",
            "policy_decision_id": "",
            "action_mode": "preview",
        }
        self.assertFalse(action_proposal_v2_policy_cleared_envelope(forged2))

    def test_hold_planner_plus_nba_answer_customer_can_select_prepare_draft(self) -> None:
        cand = {
            "decision_candidate_id": "dc_nba",
            "case_id": "c1",
            "next_best_action": "answer_customer",
            "evidence_refs": [],
        }
        pr = {
            "status": "APPROVED",
            "effective_risk_class": "low",
            "trace_id": "t1",
            "policy_basis": [],
            "failed_rules": [],
            "warnings": [],
            "required_adjustments": [],
            "requires_review": False,
        }
        pd = build_policy_decision(
            policy_report=pr,
            decision_candidate_id="dc_nba",
            decision_candidate=cand,
            dry_run_only=False,
        )
        props = build_action_proposals_v2(
            decision_candidate=cand,
            policy_decision=pd,
            primary_action_type="hold",
            dry_run_only=True,
        )
        self.assertTrue(props)
        self.assertEqual(props[0]["action_type"], "prepare_reply_draft")
        self.assertTrue(props[0]["allowed_by_policy"])

    def test_deprecated_reply_primary_normalizes_like_prepare_reply(self) -> None:
        cand = {
            "decision_candidate_id": "dc_rep",
            "case_id": "c1",
            "next_best_action": "wait",
            "evidence_refs": [],
        }
        pr = {
            "status": "APPROVED",
            "effective_risk_class": "low",
            "trace_id": "t1",
            "policy_basis": [],
            "failed_rules": [],
            "warnings": [],
            "required_adjustments": [],
            "requires_review": False,
        }
        pd = build_policy_decision(
            policy_report=pr,
            decision_candidate_id="dc_rep",
            decision_candidate=cand,
            dry_run_only=False,
        )
        props = build_action_proposals_v2(
            decision_candidate=cand,
            policy_decision=pd,
            primary_action_type="reply",
            dry_run_only=True,
        )
        self.assertEqual(props[0]["action_type"], "prepare_reply_draft")
        self.assertEqual(normalize_planner_primary_for_v2("reply"), "prepare_reply")

    def test_bundle_reports_missing_spine_diagnostics(self) -> None:
        cand = {"decision_candidate_id": "", "case_id": "c", "next_best_action": "wait", "evidence_refs": []}
        pr = {
            "status": "APPROVED",
            "effective_risk_class": "low",
            "trace_id": "t1",
            "policy_basis": [],
            "failed_rules": [],
            "warnings": [],
            "required_adjustments": [],
            "requires_review": False,
        }
        pd = build_policy_decision(policy_report=pr, decision_candidate_id="")
        bundle = build_policy_gated_action_proposals_v2_bundle(
            decision_candidate=cand,
            policy_decision=pd,
            planner_primary_action="hold",
            dry_run_only=True,
        )
        self.assertFalse(bundle["policy_spine_ok"])
        self.assertFalse(bundle["v2_proposals_built"])
        self.assertIn("missing_spine", bundle["bundle_diagnostics"])
        self.assertEqual(bundle["action_proposals_v2"], [])

    def test_bundle_matches_direct_builder_when_spine_ok(self) -> None:
        cand = {"decision_candidate_id": "dc_eq", "case_id": "c1", "next_best_action": "wait", "evidence_refs": []}
        pr = {
            "status": "APPROVED",
            "effective_risk_class": "low",
            "trace_id": "t1",
            "policy_basis": [],
            "failed_rules": [],
            "warnings": [],
            "required_adjustments": [],
            "requires_review": False,
        }
        pd = build_policy_decision(policy_report=pr, decision_candidate_id="dc_eq", dry_run_only=True)
        direct = build_action_proposals_v2(
            decision_candidate=cand,
            policy_decision=pd,
            primary_action_type="hold",
            dry_run_only=True,
        )
        bundle = build_policy_gated_action_proposals_v2_bundle(
            decision_candidate=cand,
            policy_decision=pd,
            planner_primary_action="hold",
            dry_run_only=True,
        )
        self.assertEqual(bundle["action_proposals_v2"], direct)
        self.assertTrue(bundle["policy_spine_ok"])
        self.assertEqual(bundle["bundle_diagnostics"], "")


if __name__ == "__main__":
    unittest.main()
