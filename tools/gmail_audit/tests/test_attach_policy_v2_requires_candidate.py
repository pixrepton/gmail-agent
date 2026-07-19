"""Invariant: ActionProposal v2 bundle must not attach without DecisionCandidate id."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import unittest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from policy_action_proposal import attach_policy_and_proposals


class AttachPolicyV2RequiresCandidateTests(unittest.TestCase):
    def test_v2_bundle_not_attached_without_decision_candidate_id(self) -> None:
        intelligence: dict = {"case_understanding": {"case_id": "case-x"}}
        settings = MagicMock()
        settings.action_proposal_v2_enabled = True
        settings.decision_pipeline_dry_run_only = True

        _report, _proposal = attach_policy_and_proposals(
            action_plan_result={"primary_action": "hold", "safe_for_live_push": False},
            intake_result={"decision": {"action": "append_to_existing_case"}},
            case_link_result={"decision": "linked"},
            entity_link_result=None,
            case_intelligence_result=intelligence,
            mailbox_memory_result={"case_id": "case-x", "events": []},
            snapshot={"source_message": {"message_id": "m1"}, "thread": {}},
            case_snapshot_hot_state=None,
            run_state={"run_id": "run-1"},
            settings=settings,
            stage_config={"action_proposal_v2_enabled": True},
        )

        self.assertNotIn("action_proposals_v2", intelligence)
        self.assertNotIn("policy_decision", intelligence)


if __name__ == "__main__":
    unittest.main()
