"""Tests for projection_proof_report aggregation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from artifact_io import write_jsonl
from projection_proof_report import build_projection_proof_rows, write_projection_proof_report


class ProjectionProofReportTests(unittest.TestCase):
    def test_v3_feed_success_feed_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            (rd / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "daszek_operational_feed_auto_push_enabled": True,
                        "agent_runtime": {
                            "daszek_feed_source": "engagement_snapshot_v2",
                            "daszek_legacy_v2_push_allowed": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            write_jsonl(
                rd / "stage_records.jsonl",
                [
                    {
                        "message_id": "m-v3",
                        "action_plan_result": {"primary_action": "prepare_reply"},
                        "signal_projection": {"signal_id": "sig-v3"},
                        "case_patch": {"case_id": "case-v3"},
                        "desk_note_patch": {
                            "case_id": "case-v3",
                            "title_pl": "Feed title",
                            "source_message_id": "m-v3",
                        },
                    }
                ],
            )
            write_jsonl(
                rd / "daszek_v3_feed_push_results.jsonl",
                [
                    {
                        "record_type": "feed_success",
                        "surface": "v3_operational_feed",
                        "message_id": "m-v3",
                        "snapshot_id": "snap-v3",
                        "counts": {"desk": 1, "cases": 1, "tasks": 0},
                        "snapshot_payload": {
                            "snapshot_id": "snap-v3",
                            "feed": {
                                "desk": [
                                    {
                                        "note_id": "desk-case-v3",
                                        "case_id": "case-v3",
                                        "engagement_id": "eng-v3",
                                        "title": "Feed title",
                                        "source_message_id": "m-v3",
                                        "source_signal_ids": ["sig-v3"],
                                    }
                                ],
                                "cases": [],
                                "tasks": [],
                            },
                        },
                    }
                ],
            )
            items, summary = build_projection_proof_rows(rd)
        self.assertEqual(items[0]["policy_status"], "accepted_projection")
        self.assertEqual(items[0]["surface"], "v3_operational_feed")
        self.assertEqual(items[0]["snapshot_id"], "snap-v3")
        self.assertTrue(items[0]["feed_handoff_actionable"])
        self.assertEqual(items[0]["handoff_tier"], "row4a")
        self.assertEqual(summary["v3_feed_push_ok"], 1)
        self.assertEqual(summary["primary_surface_mode"], "feed_first")
        self.assertEqual(summary["feed_handoff_actionable"], 1)

    def test_v3_feed_staging_handoff_with_engagement_id_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            (rd / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "daszek_operational_feed_auto_push_enabled": True,
                        "agent_runtime": {
                            "daszek_feed_source": "engagement_snapshot_v2",
                            "daszek_legacy_v2_push_allowed": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            write_jsonl(
                rd / "stage_records.jsonl",
                [
                    {
                        "message_id": "m-staging",
                        "action_plan_result": {"primary_action": "prepare_reply"},
                        "signal_projection": {"signal_id": "sig-staging", "engagement_id": "eng-staging"},
                        "desk_note_patch": {
                            "title_pl": "Staging feed title",
                            "source_message_id": "m-staging",
                        },
                    }
                ],
            )
            write_jsonl(
                rd / "daszek_v3_feed_push_results.jsonl",
                [
                    {
                        "record_type": "feed_success",
                        "surface": "v3_operational_feed",
                        "message_id": "m-staging",
                        "snapshot_id": "snap-staging",
                        "engagement_id": "eng-staging",
                        "counts": {"desk": 1, "cases": 0, "tasks": 0},
                        "snapshot_payload": {
                            "snapshot_id": "snap-staging",
                            "feed": {
                                "desk": [
                                    {
                                        "note_id": "desk-eng-staging",
                                        "case_id": "",
                                        "engagement_id": "eng-staging",
                                        "title": "Staging feed title",
                                        "source_message_id": "m-staging",
                                        "source_signal_ids": ["sig-staging"],
                                    }
                                ],
                                "cases": [],
                                "tasks": [],
                            },
                        },
                    }
                ],
            )
            items, summary = build_projection_proof_rows(rd)
        self.assertEqual(items[0]["engagement_id"], "eng-staging")
        self.assertEqual(items[0]["case_id"], "")
        self.assertTrue(items[0]["feed_handoff_actionable"])
        self.assertEqual(items[0]["feed_handoff_mode"], "staging")
        self.assertEqual(summary["feed_handoff_actionable"], 1)

    def test_v3_feed_staging_handoff_uses_feed_card_title_for_row4a(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            (rd / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "daszek_operational_feed_auto_push_enabled": True,
                        "agent_runtime": {
                            "daszek_feed_source": "engagement_snapshot_v2",
                            "daszek_legacy_v2_push_allowed": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            write_jsonl(
                rd / "stage_records.jsonl",
                [
                    {
                        "message_id": "m-feed-title",
                        "signal_projection": {
                            "signal_id": "sig-feed-title",
                            "agent_runtime": {
                                "hvac_profile": {
                                    "heated_area_m2": 220,
                                    "location": {"city": "Lędziny (43-140) powiat bieruńsko-lędziński"},
                                }
                            },
                        },
                        "desk_note_patch": {
                            "title_pl": "Wiadomosc Gmail: Zapytanie staging",
                            "source_message_id": "m-feed-title",
                        },
                    }
                ],
            )
            write_jsonl(
                rd / "daszek_v3_feed_push_results.jsonl",
                [
                    {
                        "record_type": "feed_success",
                        "surface": "v3_operational_feed",
                        "message_id": "m-feed-title",
                        "snapshot_id": "snap-feed-title",
                        "engagement_id": "eng-feed-title",
                        "counts": {"desk": 1, "cases": 0, "tasks": 0},
                        "snapshot_payload": {
                            "snapshot_id": "snap-feed-title",
                            "feed": {
                                "desk": [
                                    {
                                        "note_id": "desk-eng-feed-title",
                                        "case_id": "",
                                        "engagement_id": "eng-feed-title",
                                        "title": "LÄ™dziny (43-140) powiat bieruĹ„sko-lÄ™dziĹ„ski â€” 220 mÂ˛",
                                        "source_message_id": "m-feed-title",
                                        "source_signal_ids": ["sig-feed-title"],
                                    }
                                ],
                                "cases": [],
                                "tasks": [],
                            },
                        },
                    }
                ],
            )
            items, _summary = build_projection_proof_rows(rd)
        self.assertEqual(items[0]["engagement_id"], "eng-feed-title")
        self.assertEqual(items[0]["title"], "Lędziny (43-140) powiat bieruńsko-lędziński — 220 m²")
        self.assertTrue(items[0]["feed_handoff_actionable"])

    def test_v3_feed_staging_handoff_uses_nested_agent_runtime_engagement_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            (rd / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "daszek_operational_feed_auto_push_enabled": True,
                        "agent_runtime": {
                            "daszek_feed_source": "engagement_snapshot_v2",
                            "daszek_legacy_v2_push_allowed": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            write_jsonl(
                rd / "stage_records.jsonl",
                [
                    {
                        "message_id": "m-nested-staging",
                        "action_plan_result": {"primary_action": "prepare_reply"},
                        "projection_preview": {"engagement_id": "eng-nested"},
                        "signal_projection": {
                            "signal_id": "sig-nested",
                            "agent_runtime": {"engagement_id": "eng-nested"},
                        },
                        "case_patch": {"agent_engagement_id": "eng-nested"},
                        "desk_note_patch": {
                            "title_pl": "Nested staging title",
                            "source_message_id": "m-nested-staging",
                        },
                    }
                ],
            )
            write_jsonl(
                rd / "daszek_v3_feed_push_results.jsonl",
                [
                    {
                        "record_type": "feed_success",
                        "surface": "v3_operational_feed",
                        "message_id": "m-nested-staging",
                        "snapshot_id": "snap-nested",
                        "counts": {"desk": 1, "cases": 0, "tasks": 0},
                        "snapshot_payload": {
                            "snapshot_id": "snap-nested",
                            "feed": {
                                "desk": [
                                    {
                                        "note_id": "desk-eng-nested",
                                        "case_id": "",
                                        "engagement_id": "eng-nested",
                                        "title": "Nested staging title",
                                        "source_message_id": "m-nested-staging",
                                        "source_signal_ids": ["sig-nested"],
                                    }
                                ],
                                "cases": [],
                                "tasks": [],
                            },
                        },
                    }
                ],
            )
            items, summary = build_projection_proof_rows(rd)
        self.assertEqual(items[0]["engagement_id"], "eng-nested")
        self.assertTrue(items[0]["feed_handoff_actionable"])
        self.assertEqual(items[0]["feed_handoff_mode"], "staging")
        self.assertEqual(summary["feed_handoff_actionable"], 1)

    def test_v3_feed_handoff_requires_exact_snapshot_membership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            (rd / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "daszek_operational_feed_auto_push_enabled": True,
                        "agent_runtime": {
                            "daszek_feed_source": "engagement_snapshot_v2",
                            "daszek_legacy_v2_push_allowed": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            write_jsonl(
                rd / "stage_records.jsonl",
                [
                    {
                        "message_id": "m-no-match",
                        "signal_projection": {"signal_id": "sig-no-match", "engagement_id": "eng-no-match"},
                        "desk_note_patch": {
                            "title_pl": "Fallback title",
                            "source_message_id": "m-no-match",
                        },
                    }
                ],
            )
            write_jsonl(
                rd / "daszek_v3_feed_push_results.jsonl",
                [
                    {
                        "record_type": "feed_success",
                        "surface": "v3_operational_feed",
                        "message_id": "m-no-match",
                        "snapshot_id": "snap-no-match",
                        "engagement_id": "eng-no-match",
                        "counts": {"desk": 3, "cases": 1, "tasks": 0},
                        "snapshot_payload": {
                            "snapshot_id": "snap-no-match",
                            "feed": {
                                "desk": [
                                    {
                                        "note_id": "desk-other",
                                        "engagement_id": "eng-other",
                                        "case_id": "",
                                        "title": "Other title",
                                        "source_message_id": "m-other",
                                        "source_signal_ids": ["sig-other"],
                                    }
                                ],
                                "cases": [],
                                "tasks": [],
                            },
                        },
                    }
                ],
            )
            items, summary = build_projection_proof_rows(rd)
        self.assertEqual(items[0]["surface"], "v3_operational_feed")
        self.assertFalse(items[0]["feed_handoff_actionable"])
        self.assertEqual(items[0]["handoff_tier"], "")
        self.assertEqual(summary["feed_handoff_actionable"], 0)

    def test_run_manifest_fallback_enables_feed_primary_skip_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            (rd / "run_manifest.json").write_text(
                json.dumps(
                    {
                        "daszek_operational_feed_auto_push_enabled": True,
                        "agent_runtime": {
                            "daszek_feed_source": "engagement_snapshot_v2",
                            "daszek_legacy_v2_push_allowed": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            write_jsonl(
                rd / "stage_records.jsonl",
                [{"message_id": "m-skip", "action_plan_result": {}, "signal_projection": {"signal_id": "sig-skip"}}],
            )
            write_jsonl(
                rd / "daszek_v3_feed_push_results.jsonl",
                [
                    {
                        "record_type": "feed_skip",
                        "surface": "v3_operational_feed",
                        "message_id": "m-skip",
                        "reason": "skipped_projection_refresh_not_needed",
                        "push_policy_detail": "projection_refresh_decision.should_refresh=false",
                    }
                ],
            )
            items, summary = build_projection_proof_rows(rd)
        self.assertEqual(items[0]["policy_status"], "skipped_projection_refresh")
        self.assertEqual(items[0]["surface"], "v3_operational_feed")
        self.assertEqual(items[0]["primary_surface_mode"], "feed_first")
        self.assertEqual(summary["primary_surface_mode"], "feed_first")
        self.assertEqual(summary["daszek_feed_source"], "engagement_snapshot_v2")
        self.assertNotEqual(items[0]["policy_status"], "unknown")

    def test_skip_row_surfaces_as_skipped_config_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            write_jsonl(
                rd / "stage_records.jsonl",
                [
                    {
                        "message_id": "m1",
                        "action_plan_result": {"primary_action": "prepare_reply"},
                    }
                ],
            )
            write_jsonl(
                rd / "daszek_v2_push_results.jsonl",
                [
                    {
                        "message_id": "m1",
                        "record_type": "projection_skip",
                        "reason": "skipped_v2_config_disabled",
                        "push_policy_detail": "v2 off",
                    }
                ],
            )
            items, summary = build_projection_proof_rows(rd)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["policy_status"], "skipped_config_disabled")
        self.assertEqual(items[0]["message_id"], "m1")
        self.assertIn("skipped_config_disabled", summary["aggregates_by_policy_status"])

    def test_write_projection_proof_report_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            write_jsonl(rd / "stage_records.jsonl", [{"message_id": "x", "action_plan_result": {}}])
            out = write_projection_proof_report(rd)
            payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["message_count"], 1)
        self.assertIn("items", payload)

    def test_ingest_with_readback_sets_ui_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            write_jsonl(
                rd / "stage_records.jsonl",
                [
                    {
                        "message_id": "m2",
                        "action_plan_result": {"primary_action": "hold"},
                        "signal_projection": {"signal_id": "sig1"},
                        "case_patch": {"case_id": "case_1"},
                        "desk_note_patch": {
                            "desk_note_id": "note_1",
                            "case_id": "case_1",
                            "title_pl": "Visible title",
                            "source_message_id": "m2",
                            "source_signal_ids": ["sig1"],
                        },
                    }
                ],
            )
            write_jsonl(
                rd / "daszek_v2_push_results.jsonl",
                [
                    {
                        "status": "ingested",
                        "message_id": "m2",
                        "signal_id": "sig1",
                        "trace_id": "tr1",
                        "push_policy_reason": "allowed_operator_projection",
                    },
                    {
                        "record_type": "v2_readback",
                        "message_id": "m2",
                        "store_readback": "found",
                        "readback_reason": "",
                        "ui_visibility_expected": True,
                        "readback_note_id": "note_1",
                        "readback_case_id": "case_1",
                        "readback_title": "Visible title",
                        "readback_source_message_id": "m2",
                        "readback_source_signal_ids": ["sig1"],
                        "operator_action_available": True,
                        "allowed_operator_actions": ["zla_sprawa"],
                        "expected_bridge_domain": "adjudication",
                        "expected_adjudication_kind": "reject_same_case",
                    },
                ],
            )
            items, summary = build_projection_proof_rows(rd)
        self.assertEqual(items[0]["policy_status"], "accepted_projection")
        self.assertEqual(items[0]["signal_id"], "sig1")
        self.assertEqual(items[0]["case_id"], "case_1")
        self.assertEqual(items[0]["note_id"], "note_1")
        self.assertEqual(items[0]["title"], "Visible title")
        self.assertEqual(items[0]["source_message_id"], "m2")
        self.assertEqual(items[0]["store_readback"], "found")
        self.assertTrue(items[0]["ui_visibility_expected"])
        self.assertTrue(items[0]["operator_action_available"])
        self.assertTrue(items[0]["handoff_actionable"])
        self.assertEqual(items[0]["allowed_operator_actions"], ["zla_sprawa"])
        self.assertEqual(summary["operator_handoff_actionable"], 1)
        self.assertFalse(items[0]["ui_visibility_verified"])
        self.assertIn("browser/UI verification remains manual", items[0]["ui_visibility_note"])
        self.assertEqual(summary["v2_readback_found"], 1)
        self.assertEqual(summary["v2_projection_accepted"], 1)

    def test_ingest_with_readback_but_empty_note_case_is_not_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            write_jsonl(
                rd / "stage_records.jsonl",
                [
                    {
                        "message_id": "m-no-case",
                        "action_plan_result": {"primary_action": "create_review"},
                        "signal_projection": {"signal_id": "sig-no-case"},
                        "case_patch": {"case_id": ""},
                        "desk_note_patch": {
                            "desk_note_id": "note_no_case",
                            "case_id": "",
                            "title_pl": "Visible but not bound",
                            "source_message_id": "m-no-case",
                            "source_signal_ids": ["sig-no-case"],
                        },
                    }
                ],
            )
            write_jsonl(
                rd / "daszek_v2_push_results.jsonl",
                [
                    {"status": "ingested", "message_id": "m-no-case", "signal_id": "sig-no-case"},
                    {
                        "record_type": "v2_readback",
                        "message_id": "m-no-case",
                        "store_readback": "found",
                        "readback_note_id": "note_no_case",
                        "readback_case_id": "",
                        "readback_title": "Visible but not bound",
                        "readback_source_message_id": "m-no-case",
                        "readback_source_signal_ids": ["sig-no-case"],
                        "operator_action_available": False,
                    },
                ],
            )
            items, summary = build_projection_proof_rows(rd)

        self.assertEqual(items[0]["policy_status"], "accepted_projection")
        self.assertFalse(items[0]["handoff_actionable"])
        self.assertFalse(items[0]["operator_action_available"])
        self.assertEqual(summary["operator_handoff_actionable"], 0)

    def test_projection_failure_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            write_jsonl(
                rd / "stage_records.jsonl",
                [{"message_id": "m3", "action_plan_result": {"primary_action": "hold"}}],
            )
            write_jsonl(
                rd / "daszek_v2_push_results.jsonl",
                [
                    {
                        "record_type": "projection_failure",
                        "message_id": "m3",
                        "error": "Daszek v2 ingest failed: 500",
                        "push_policy_reason": "allowed_operator_projection",
                    }
                ],
            )
            items, summary = build_projection_proof_rows(rd)
        self.assertEqual(items[0]["policy_status"], "projection_failed")
        self.assertEqual(summary["v2_projection_failed"], 1)

    def test_blocked_v2_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            write_jsonl(
                rd / "stage_records.jsonl",
                [{"message_id": "m4", "action_plan_result": {"primary_action": "ignore"}}],
            )
            write_jsonl(
                rd / "daszek_v2_push_results.jsonl",
                [
                    {
                        "record_type": "push_policy",
                        "surface": "v2_operator_projection",
                        "message_id": "m4",
                        "allowed": False,
                        "push_policy_reason": "blocked_not_safe_for_operator_projection",
                        "push_policy_detail": "safe_for_operator_projection is false",
                    }
                ],
            )
            items, summary = build_projection_proof_rows(rd)
        self.assertEqual(items[0]["policy_status"], "blocked_policy")
        self.assertEqual(summary["v2_projection_blocked_policy"], 1)


if __name__ == "__main__":
    unittest.main()
