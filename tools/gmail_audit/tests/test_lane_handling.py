from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from gmail_intake import _build_lane_stage_plan, draft_reply, run_business_reasoning
from tests.fixture_helpers import run_fixture


class LaneHandlingTests(unittest.TestCase):
    def test_reference_only_lane_skips_deep_shadow_stages(self) -> None:
        result = run_fixture("reference_only_mail")
        config = {
            "preclassification_result": result["preclassification"],
            "lane_stage_plan": _build_lane_stage_plan(result["preclassification"]),
        }

        business_result = run_business_reasoning(
            result["snapshot"],
            result["intake_result"],
            result["case_link_result"],
            {},
            config,
        )
        reply_result = draft_reply(
            result["snapshot"],
            result["intake_result"],
            business_result,
            {},
            config,
        )

        self.assertEqual(business_result["recommended_next_action"], "wait")
        self.assertEqual(business_result["execution_metadata"]["parse_status"], "skipped_for_lane")
        self.assertFalse(reply_result["draft_enabled"])
        self.assertEqual(reply_result["execution_metadata"]["parse_status"], "skipped_for_lane")

    def test_review_direct_lane_keeps_manual_review_semantics(self) -> None:
        result = run_fixture("forwarded_review_chaos")
        config = {
            "preclassification_result": result["preclassification"],
            "lane_stage_plan": _build_lane_stage_plan(result["preclassification"]),
        }

        business_result = run_business_reasoning(
            result["snapshot"],
            result["intake_result"],
            result["case_link_result"],
            {},
            config,
        )
        reply_result = draft_reply(
            result["snapshot"],
            result["intake_result"],
            business_result,
            {},
            config,
        )

        self.assertEqual(business_result["recommended_next_action"], "escalate_review")
        self.assertIn("explicit operator instruction", business_result["missing_information"])
        self.assertEqual(reply_result["do_not_send_reasons"], ["manual_review_first"])


if __name__ == "__main__":
    unittest.main()
