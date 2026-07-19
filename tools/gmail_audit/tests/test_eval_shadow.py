from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from artifact_contracts import REVIEW_TEMPLATE_FIELDS
from eval_shadow import (
    _classify_operator_note_quality,
    _classify_reply_quality,
    _classify_reply_usefulness,
    build_review_row,
    evaluate_annotations,
)
from tests.fixture_helpers import run_fixture


class EvalShadowTests(unittest.TestCase):
    def test_review_row_exposes_v2_stage_fields(self) -> None:
        result = run_fixture("new_lead")
        row = build_review_row(
            result["intake_result"],
            stage_record=self._build_stage_record(result),
        )

        self.assertEqual(row["agent_preclassification_lane"], result["preclassification"]["lane"])
        self.assertEqual(row["agent_case_link_decision"], result["case_link_result"]["decision"])
        self.assertEqual(row["agent_recommended_next_action"], result["business_result"]["recommended_next_action"])
        self.assertEqual(row["agent_action_primary"], result["action_plan"]["primary_action"])
        self.assertEqual(row["agent_action_projection_mode"], result["action_plan"]["daszek_projection_mode"])

    def test_evaluate_annotations_scores_business_and_reply_fields(self) -> None:
        result = run_fixture("new_lead")
        stage_record = self._build_stage_record(result)
        row = build_review_row(result["intake_result"], stage_record=stage_record)
        row["expected_primary_signal_code"] = row["agent_primary_signal_code"]
        row["expected_business_area"] = row["agent_business_area"]
        row["expected_case_family"] = row["agent_case_family"]
        row["expected_decision_action"] = row["agent_decision_action"]
        row["expected_priority"] = row["agent_priority"]
        row["expected_review_required"] = row["agent_review_required"]
        row["expected_case_key_if_known"] = row["agent_case_key"]
        row["expected_case_link_decision"] = row["agent_case_link_decision"]
        row["expected_recommended_next_action"] = row["agent_recommended_next_action"]
        row["expected_missing_information"] = row["agent_missing_information"]
        row["expected_operator_note_quality"] = _classify_operator_note_quality(result["business_result"]["operator_note"])
        row["expected_reply_should_exist"] = row["agent_reply_draft_available"]
        row["expected_reply_quality"] = _classify_reply_quality(result["reply_result"])
        row["expected_reply_usefulness"] = _classify_reply_usefulness(result["reply_result"], result["business_result"])
        row["expected_action_primary"] = row["agent_action_primary"]
        row["expected_projection_mode"] = row["agent_action_projection_mode"]

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "annotations.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=REVIEW_TEMPLATE_FIELDS)
                writer.writeheader()
                writer.writerow(row)

            summary, details = evaluate_annotations(
                [result["intake_result"]],
                csv_path,
                stage_records=[stage_record],
            )

        self.assertEqual(summary["compared_items"], 1)
        self.assertEqual(summary["recommended_next_action_accuracy"], 1.0)
        self.assertEqual(summary["action_primary_accuracy"], 1.0)
        self.assertEqual(summary["reply_presence_accuracy"], 1.0)
        self.assertEqual(summary["projection_mode_accuracy"], 1.0)
        self.assertEqual(details[0]["actual"]["action_primary"], result["action_plan"]["primary_action"])

    @staticmethod
    def _build_stage_record(result: dict[str, object]) -> dict[str, object]:
        return {
            "message_id": result["snapshot"]["source_message"]["message_id"],
            "preclassification_result": result["preclassification"],
            "intake_result_raw": {"response_json": result["intake_result"]},
            "intake_result_final": result["intake_result"],
            "case_link_result": result["case_link_result"],
            "business_reasoning_result": result["business_result"],
            "reply_draft_result": result["reply_result"],
            "action_plan_result": result["action_plan"],
            "projection_preview": result["preview"],
            "signal_projection": result["v2_projection"]["signal_projection"],
            "case_patch": result["v2_projection"]["case_patch"],
            "desk_note_patch": result["v2_projection"]["desk_note_patch"],
            "decision_trace": result["v2_projection"]["decision_trace"],
            "review_decision": {
                "required": result["intake_result"]["review_required"],
                "flags": result["intake_result"]["review_reasons"],
            },
            "execution_metadata": {},
        }


if __name__ == "__main__":
    unittest.main()
