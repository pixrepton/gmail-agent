"""Normalized action proposal + intake policy wiring (no new policy rules)."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import unittest

from policy_action_proposal import (
    POLICY_ACTION_PROPOSAL_SCHEMA_VERSION,
    build_fallback_case_snapshot_hot_state,
    build_policy_action_proposal,
    evaluate_policy_for_intake_stage,
)


class PolicyActionProposalTests(unittest.TestCase):
    def test_prepare_reply_maps_to_live_reply_class(self) -> None:
        p = build_policy_action_proposal(
            action_plan_result={"primary_action": "prepare_reply", "safe_for_live_push": False},
            intake_result={"decision": {"action": "append_to_existing_case"}},
            case_link_result={"decision": "linked"},
            case_intelligence_result={},
            entity_link_result={"link_status": "VERIFIED"},
            snapshot={"thread": {"thread_position": "latest", "message_count": 3}},
            run_id="r1",
            message_id="m1",
        )
        self.assertEqual(p["schema_version"], POLICY_ACTION_PROPOSAL_SCHEMA_VERSION)
        self.assertEqual(p["action_class"], "LIVE_REPLY")
        self.assertEqual(p["primary_action"], "prepare_reply")

    def test_evaluate_policy_returns_report_dict(self) -> None:
        report, proposal = evaluate_policy_for_intake_stage(
            action_plan_result={"primary_action": "hold", "safe_for_live_push": False},
            intake_result={},
            case_link_result={"decision": "linked"},
            entity_link_result=None,
            case_intelligence_result=None,
            mailbox_memory_result={"case_id": "c1", "events": []},
            snapshot={"source_message": {"message_id": "x"}, "thread": {}},
            case_snapshot_hot_state=None,
            run_state={"run_id": "run-x"},
        )
        self.assertEqual(proposal["primary_action"], "hold")
        d = report.to_dict()
        self.assertIn("status", d)
        self.assertIn("schema_version", d)

    def test_evaluate_policy_uses_nested_case_snapshot_hot_state_from_mailbox(self) -> None:
        nested = {
            "schema_version": "case_snapshot_hot_state.v1",
            "case": {"case_id": "c-hot", "operational_status": "OK", "summary_text": "from nested"},
            "key_facts": [],
            "active_conflicts": [],
            "snapshot_meta": {"version": 2, "confidence": 0.9, "review_required": False},
            "latest_activity": {"thread_message_count": 1},
        }
        report, _proposal = evaluate_policy_for_intake_stage(
            action_plan_result={"primary_action": "hold", "safe_for_live_push": False},
            intake_result={},
            case_link_result={"decision": "linked"},
            entity_link_result=None,
            case_intelligence_result=None,
            mailbox_memory_result={"case_id": "c-hot", "events": [], "case_snapshot_hot_state": nested},
            snapshot={"source_message": {"message_id": "x"}, "thread": {}},
            case_snapshot_hot_state=None,
            run_state={"run_id": "run-nested"},
        )
        d = report.to_dict()
        self.assertIn("status", d)
        self.assertEqual(_proposal.get("primary_action"), "hold")

    def test_fallback_hot_state_prefers_context_pack_snapshot(self) -> None:
        hot = build_fallback_case_snapshot_hot_state(
            mailbox_memory_result={
                "case_id": "c1",
                "confidence": 0.77,
                "context_pack": {
                    "snapshot": {
                        "summary": "context pack summary",
                        "open_questions": ["Confirm totals."],
                        "metadata": {"source": "context_pack"},
                    }
                },
                "snapshot": {
                    "summary": "legacy summary should lose",
                    "open_questions": ["legacy q"],
                    "metadata": {"source": "legacy"},
                },
            },
            snapshot={"thread": {"message_count": 2}},
            case_link_result={"decision": "linked"},
            intake_result={"review_required": True},
        )
        self.assertEqual(hot["case"]["summary_text"], "context pack summary")
        self.assertEqual(hot["case"]["metadata"], {"source": "context_pack"})
        self.assertEqual(hot["active_conflicts"], [{"severity": "medium", "summary": "open_questions_present"}])
        self.assertTrue(hot["snapshot_meta"]["review_required"])


if __name__ == "__main__":
    unittest.main()
