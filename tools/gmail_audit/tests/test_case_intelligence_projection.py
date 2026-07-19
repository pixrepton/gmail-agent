from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from tests.fixture_helpers import run_fixture


class CaseIntelligenceProjectionTests(unittest.TestCase):
    def test_new_lead_projection_carries_intelligence_fields(self) -> None:
        result = run_fixture("new_lead")
        projection = result["v2_projection"]

        self.assertEqual(projection["desk_note_patch"]["surface_zone"], "desk")
        self.assertEqual(projection["desk_note_patch"]["presence_mode"], "advisory")
        self.assertTrue(projection["desk_note_patch"]["operator_brief_pl"].strip())
        self.assertTrue(projection["desk_note_patch"]["assistant_suggestion_pl"].strip())
        self.assertTrue(projection["case_patch"]["operator_brief_pl"].strip())

    def test_reference_only_projection_becomes_silent_case_memory(self) -> None:
        result = run_fixture("reference_only_mail")
        projection = result["v2_projection"]

        self.assertEqual(projection["desk_note_patch"]["surface_zone"], "silent")
        self.assertEqual(projection["desk_note_patch"]["presence_mode"], "silent")
        self.assertEqual(projection["desk_note_patch"]["command"], "suppress")


if __name__ == "__main__":
    unittest.main()
