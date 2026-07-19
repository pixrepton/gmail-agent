from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import gmail_intake
from tests.fixture_helpers import run_fixture
from v2_runtime import build_v2_ingest_payload, extract_v2_projection_from_stage_record


class V2IngestRuntimeTests(unittest.TestCase):
    def test_gmail_intake_re_exports_v2_runtime_helpers_for_compatibility(self) -> None:
        self.assertIs(gmail_intake.build_v2_ingest_payload, build_v2_ingest_payload)
        self.assertIs(gmail_intake.extract_v2_projection_from_stage_record, extract_v2_projection_from_stage_record)

    def test_build_v2_ingest_payload_wraps_projection_with_metadata(self) -> None:
        result = run_fixture("new_lead")
        payload = build_v2_ingest_payload(
            run_id="run_123",
            message_key=result["snapshot"]["source_message"]["message_id"],
            v2_projection=result["v2_projection"],
        )

        self.assertEqual(payload["projection_version"], "1.0")
        self.assertEqual(payload["run_id"], "run_123")
        self.assertEqual(payload["message_key"], result["snapshot"]["source_message"]["message_id"])
        self.assertIn("emitted_at", payload)
        self.assertEqual(payload["signal_projection"]["signal_id"], result["v2_projection"]["signal_projection"]["signal_id"])

    def test_extract_v2_projection_from_stage_record_validates_shape(self) -> None:
        result = run_fixture("active_case_follow_up")
        stage_record = {
            "signal_projection": result["v2_projection"]["signal_projection"],
            "case_patch": result["v2_projection"]["case_patch"],
            "desk_note_patch": result["v2_projection"]["desk_note_patch"],
            "decision_trace": result["v2_projection"]["decision_trace"],
        }

        projection = extract_v2_projection_from_stage_record(stage_record)

        self.assertIsNotNone(projection)
        self.assertEqual(projection["desk_note_patch"]["command"], "update")


if __name__ == "__main__":
    unittest.main()
