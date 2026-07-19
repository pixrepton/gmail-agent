from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from reply_drafter import fallback_reply_drafter
from tests.fixture_helpers import run_fixture


class ReplyDrafterContractTests(unittest.TestCase):
    def test_fallback_disables_draft(self) -> None:
        result = fallback_reply_drafter(reason="fixture")
        self.assertFalse(result["draft_enabled"])

    def test_lead_fixture_has_reply_draft(self) -> None:
        result = run_fixture("new_lead")
        self.assertTrue(result["reply_result"]["draft_enabled"])


if __name__ == "__main__":
    unittest.main()
