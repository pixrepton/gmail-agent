from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from tests.fixture_helpers import run_fixture


class CaseLinkerTests(unittest.TestCase):
    def test_explicit_case_reference_links(self) -> None:
        result = run_fixture("active_case_follow_up")
        self.assertEqual(result["case_link_result"]["decision"], "linked")
        self.assertEqual(result["case_link_result"]["selected_case_key"], "CASE-2026-001")

    def test_weak_case_link_stays_weak(self) -> None:
        result = run_fixture("weak_case_link")
        self.assertEqual(result["case_link_result"]["decision"], "weak_link")


if __name__ == "__main__":
    unittest.main()
