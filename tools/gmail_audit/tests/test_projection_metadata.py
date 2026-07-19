from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from dash_preview import build_dash_preview
from tests.fixture_helpers import run_fixture


class ProjectionMetadataTests(unittest.TestCase):
    def test_preview_metadata_fields_exist(self) -> None:
        result = run_fixture("post_offer_question")
        metadata = result["preview"]["metadata"]
        self.assertIn("business_interpretation_summary", metadata)
        self.assertIn("recommended_next_action", metadata)
        self.assertIn("operator_note", metadata)
        self.assertIn("reply_draft_available", metadata)
        self.assertIn("business_confidence", metadata)
        self.assertIn("action_confidence", metadata)
        self.assertIn("case_link_confidence", metadata)
        self.assertIn("operator_brief_pl", metadata)
        self.assertIn("presence_mode", metadata)
        self.assertIn("surface_zone", metadata)
        self.assertIn("missing_info_summary_pl", metadata)
        self.assertIn("risk_summary_pl", metadata)

    def test_zero_confidence_values_are_not_replaced_in_preview_metadata(self) -> None:
        result = run_fixture("reference_only_mail")
        preview = build_dash_preview(
            result["intake_result"],
            stage_outputs={
                "intake_result_final": result["intake_result"],
                "preclassification_result": result["preclassification"],
                "case_link_result": {"decision": "no_link", "confidence": 0.0},
                "business_reasoning_result": result["business_result"],
                "reply_draft_result": result["reply_result"],
                "action_plan_result": {"confidence": 0.0, "primary_action": "hold"},
                "case_intelligence_result": result["case_intelligence"],
            },
        )
        metadata = preview["metadata"]
        self.assertEqual(metadata["action_confidence"], 0.0)
        self.assertEqual(metadata["case_link_confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
