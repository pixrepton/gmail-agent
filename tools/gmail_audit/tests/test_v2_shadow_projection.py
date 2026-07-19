from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from dash_projection_v2 import build_v2_shadow_projection, validate_v2_shadow_projection
from tests.fixture_helpers import run_fixture


class V2ShadowProjectionTests(unittest.TestCase):
    def test_new_lead_fixture_creates_signal_case_and_note(self) -> None:
        result = run_fixture("new_lead")
        projection = validate_v2_shadow_projection(result["v2_projection"])

        self.assertTrue(projection["signal_projection"]["signal_id"].startswith("sig_"))
        self.assertEqual(projection["signal_projection"]["intake"]["decision_action"], "create_case_and_task")
        self.assertEqual(projection["case_patch"]["command"], "upsert_case")
        self.assertEqual(projection["desk_note_patch"]["command"], "create")
        self.assertEqual(projection["desk_note_patch"]["presence_mode"], "advisory")
        self.assertEqual(projection["desk_note_patch"]["surface_zone"], "desk")
        self.assertEqual(projection["decision_trace"]["decision_type"], "create_note")

    def test_reference_only_fixture_stays_off_the_desk(self) -> None:
        result = run_fixture("reference_only_mail")
        projection = validate_v2_shadow_projection(result["v2_projection"])

        self.assertEqual(projection["case_patch"]["command"], "noop")
        self.assertEqual(projection["desk_note_patch"]["command"], "suppress")
        self.assertEqual(projection["desk_note_patch"]["presence_mode"], "silent")
        self.assertEqual(projection["desk_note_patch"]["surface_zone"], "silent")
        self.assertEqual(projection["decision_trace"]["decision_type"], "suppress_note")

    def test_active_case_follow_up_updates_existing_note_context(self) -> None:
        result = run_fixture("active_case_follow_up")
        projection = validate_v2_shadow_projection(result["v2_projection"])

        self.assertEqual(projection["case_patch"]["command"], "upsert_case")
        self.assertEqual(projection["case_patch"]["case_key"], "CASE-2026-001")
        self.assertEqual(projection["desk_note_patch"]["command"], "update")
        self.assertIn(projection["desk_note_patch"]["presence_mode"], {"advisory", "strong"})
        self.assertEqual(projection["decision_trace"]["subject_id"], projection["desk_note_patch"]["desk_note_id"])

    def test_urgent_review_fixture_escalates_presence(self) -> None:
        result = run_fixture("urgent_service")
        projection = validate_v2_shadow_projection(result["v2_projection"])

        self.assertEqual(projection["desk_note_patch"]["command"], "escalate_presence")
        self.assertEqual(projection["desk_note_patch"]["presence_mode"], "alarm")
        self.assertEqual(projection["desk_note_patch"]["lifecycle"], "active")

    def test_projection_uses_canonical_signal_id_when_runtime_supplies_it(self) -> None:
        result = run_fixture("new_lead")
        projection = validate_v2_shadow_projection(
            build_v2_shadow_projection(
                result["intake_result"],
                run_id="test-canonical-signal",
                stage_outputs={
                    "preclassification_result": result["preclassification"],
                    "case_link_result": result["case_link_result"],
                    "business_reasoning_result": result["business_result"],
                    "reply_draft_result": result["reply_result"],
                    "action_plan_result": result["action_plan"],
                    "case_intelligence_result": result["case_intelligence"],
                    "canonical_signal_id": "sig_canonical_journal",
                },
            )
        )

        self.assertEqual(projection["signal_projection"]["signal_id"], "sig_canonical_journal")
        self.assertEqual(projection["desk_note_patch"]["source_signal_ids"], ["sig_canonical_journal"])
        self.assertEqual(projection["decision_trace"]["trigger_signal_id"], "sig_canonical_journal")

    def test_review_projection_with_unknown_family_still_binds_visible_note_to_case(self) -> None:
        projection = validate_v2_shadow_projection(
            build_v2_shadow_projection(
                {
                    "source": {"channel": "gmail", "mailbox": "biuro.topinstal@gmail.com"},
                    "message": {
                        "message_id": "mid-review",
                        "date": "2026-05-08T10:00:00+00:00",
                        "subject": "Faktura nr F/15401/26",
                    },
                    "thread": {"thread_id": "thr-review"},
                    "decision": {"action": "review", "action_rationale": "manual check"},
                    "review": {"required": True, "flags": ["insufficient_thread_context"]},
                    "case_assessment": {
                        "case_family": "unknown",
                        "state_detected": "none",
                        "state_change": {"detected": False},
                    },
                    "primary_signal": {"code": "manual_review_required", "name": "Manual review required"},
                    "priority": "medium",
                    "business_area": "internal_coordination",
                    "confidence": {
                        "signal_confidence": 0.84,
                        "case_link_confidence": 0.0,
                        "decision_confidence": 0.84,
                        "extraction_confidence": 0.65,
                    },
                },
                stage_outputs={
                    "action_plan_result": {
                        "primary_action": "create_review",
                        "safe_for_operator_projection": True,
                    },
                    "case_intelligence_result": {
                        "case_understanding": {"case_family": "unknown"},
                        "desk_composition": {"title_pl": "Faktura nr F/15401/26"},
                    },
                },
                run_id="run-review",
            )
        )

        self.assertEqual(projection["case_patch"]["command"], "upsert_case")
        self.assertTrue(projection["case_patch"]["case_id"].startswith("case_"))
        self.assertEqual(projection["desk_note_patch"]["case_id"], projection["case_patch"]["case_id"])
        self.assertEqual(projection["decision_trace"]["case_id"], projection["case_patch"]["case_id"])


if __name__ == "__main__":
    unittest.main()
