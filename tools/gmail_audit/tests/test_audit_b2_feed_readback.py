"""Tests for B2 feed readback audit selection."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from audit_b2_feed_readback import _select_sample  # noqa: E402


class AuditB2SelectionTests(unittest.TestCase):
    def test_handoff_case_ids_subset(self) -> None:
        eligible = [
            {"case_id": "case_062a7aa4ed7b", "feedback_eligible": True},
            {"case_id": "case_other", "feedback_eligible": True},
        ]
        picked, mode = _select_sample(eligible, case_ids=["case_062a7aa4ed7b"], sample_n=10, seed=0)
        self.assertEqual(mode, "handoff_bounded")
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["case_id"], "case_062a7aa4ed7b")

    def test_missing_handoff_raises(self) -> None:
        eligible = [{"case_id": "case_x", "feedback_eligible": True}]
        with self.assertRaises(SystemExit):
            _select_sample(eligible, case_ids=["case_missing"], sample_n=10, seed=0)


if __name__ == "__main__":
    unittest.main()
