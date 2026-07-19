"""Static ordering guard: v2 proposals must not precede PolicyDecision construction (PR-4 path)."""

from __future__ import annotations

import unittest
from pathlib import Path


class V2PolicyGateOrderTests(unittest.TestCase):
    def test_attach_policy_and_proposals_builds_policy_decision_before_v2(self) -> None:
        root = Path(__file__).resolve().parents[1]
        text = (root / "policy_action_proposal.py").read_text(encoding="utf-8")
        fn = text.find("def attach_policy_and_proposals(")
        self.assertGreaterEqual(fn, 0, "attach_policy_and_proposals must exist")
        body = text[fn : fn + 4000]
        i_pd = body.find("build_policy_decision(")
        i_bundle = body.find("build_policy_gated_action_proposals_v2_bundle(")
        self.assertGreaterEqual(i_pd, 0, "build_policy_decision must exist in attach_policy_and_proposals")
        self.assertGreaterEqual(i_bundle, 0, "v2 bundle helper must exist in attach_policy_and_proposals")
        self.assertLess(
            i_pd,
            i_bundle,
            "PolicyDecision must be constructed before the v2 bundle in the canonical path",
        )

    def test_gmail_intake_does_not_invoke_v2_builder_directly(self) -> None:
        root = Path(__file__).resolve().parents[1]
        text = (root / "gmail_intake.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "build_action_proposals_v2(",
            text,
            "intake must use build_policy_gated_action_proposals_v2_bundle only",
        )
        self.assertNotIn(
            "build_policy_gated_action_proposals_v2_bundle(",
            text,
            "v2 bundle construction moved to attach_policy_and_proposals (PR-4)",
        )
        self.assertLessEqual(
            text.count("class PolicyEngine"),
            0,
            "gmail_intake must not embed a second PolicyEngine",
        )


if __name__ == "__main__":
    unittest.main()
