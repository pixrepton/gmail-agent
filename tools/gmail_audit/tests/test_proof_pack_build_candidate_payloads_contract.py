"""Offline proof-pack generator must not look like a production v2 bypass."""

from __future__ import annotations

import unittest
from pathlib import Path


class ProofPackBuildCandidatePayloadsContractTests(unittest.TestCase):
    def test_script_uses_v2_bundle_and_documents_non_production(self) -> None:
        repo = Path(__file__).resolve().parents[3]
        script = (
            repo
            / "tools"
            / "gmail_audit"
            / "scripts"
            / "build_candidate_payloads.py"
        )
        self.assertTrue(script.is_file(), f"missing proof-pack script: {script}")
        text = script.read_text(encoding="utf-8")
        self.assertIn("build_policy_gated_action_proposals_v2_bundle", text)
        self.assertIn("NOT a production path", text)
        self.assertLess(
            text.find("build_policy_decision("),
            text.find("build_policy_gated_action_proposals_v2_bundle("),
            "policy decision must precede v2 bundle assembly",
        )


if __name__ == "__main__":
    unittest.main()
