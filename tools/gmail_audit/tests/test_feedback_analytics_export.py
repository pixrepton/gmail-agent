"""Read-only feedback analytics export (JSONL/CSV)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from artifact_io import read_jsonl, write_jsonl
from feedback_analytics_export import (
    export_feedback_analytics_from_jsonl,
    export_feedback_analytics_from_store,
    export_feedback_analytics_records,
    flatten_analytics_record_for_csv,
    normalize_feedback_event_input,
    write_feedback_analytics_csv,
    write_feedback_analytics_jsonl,
)
from feedback_event_contract import EVENT_TYPE_ADJUDICATION, EVENT_TYPE_FEEDBACK_CALIBRATION
from mailbox_memory_store import InMemoryMailboxMemoryStore
from operator_feedback_runtime import persist_routed_event, route_operator_payload


class FeedbackAnalyticsExportTests(unittest.TestCase):
    def test_calibration_jsonl_exports_evidence_quality_not_truth(self) -> None:
        rows = [
            {
                "event_class": "FeedbackEvent",
                "event_id": "fe_rfc",
                "case_id": "c1",
                "occurred_at": "2026-05-18T10:00:00+02:00",
                "calibration_category": "rejected_fact_claim",
                "detail": "customer complained about wrong invoice total",
                "body": "secret body",
            }
        ]
        exported, summary = export_feedback_analytics_records(rows)
        self.assertEqual(summary.exported_count, 1)
        self.assertEqual(exported[0]["analytics_group"], "evidence_quality")
        self.assertFalse(exported[0]["mutates_truth"])
        dumped = json.dumps(exported[0])
        self.assertNotIn("customer complained", dumped)
        self.assertNotIn("secret body", dumped)

    def test_adjudication_jsonl_exports_truth_adjudication(self) -> None:
        rows = [
            {
                "event_class": "AdjudicationEvent",
                "event_id": "ae_inv",
                "case_id": "c2",
                "occurred_at": "2026-05-18T11:00:00+02:00",
                "adjudication_kind": "invalidate_fact",
                "detail": "operator note with PII",
            }
        ]
        exported, summary = export_feedback_analytics_records(rows)
        self.assertEqual(summary.exported_count, 1)
        self.assertEqual(exported[0]["analytics_group"], "truth_adjudication")
        self.assertTrue(exported[0]["mutates_truth"])

    def test_mailbox_memory_row_adapter(self) -> None:
        shell = {
            "event_id": "row_1",
            "case_id": "c3",
            "occurred_at": "2026-05-18T12:00:00+02:00",
            "event_type": EVENT_TYPE_FEEDBACK_CALIBRATION,
            "payload": {
                "event_class": "FeedbackEvent",
                "calibration_category": "wrong_topic",
                "target_refs": {"decision_candidate_id": "dc_1"},
            },
        }
        normalized = normalize_feedback_event_input(shell)
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized["event_id"], "row_1")
        self.assertEqual(normalized["case_id"], "c3")
        exported, summary = export_feedback_analytics_records([shell])
        self.assertEqual(summary.exported_count, 1)
        self.assertEqual(exported[0]["analytics_group"], "routing_quality")
        self.assertEqual(exported[0]["correlation_refs"]["decision_candidate_id"], "dc_1")

    def test_correlation_refs_preserved_in_export(self) -> None:
        raw = {
            "calibration_category": "policy_block",
            "case_id": "c_corr",
            "decision_candidate_id": "dc_x",
            "policy_decision_id": "pd_y",
            "action_proposal_id": "ap_z",
            "source_signal_id": "sig_9",
        }
        _, ev = route_operator_payload(raw)
        exported, _ = export_feedback_analytics_records([ev])
        refs = exported[0]["correlation_refs"]
        self.assertEqual(refs["case_id"], "c_corr")
        self.assertEqual(refs["decision_candidate_id"], "dc_x")
        self.assertEqual(refs["policy_decision_id"], "pd_y")
        self.assertEqual(refs["proposal_id"], "ap_z")
        self.assertEqual(refs["source_signal_id"], "sig_9")

    def test_invalid_row_skipped_with_summary(self) -> None:
        rows = [
            {"event_class": "FeedbackEvent", "event_id": "ok", "case_id": "c1", "calibration_category": "accurate"},
            {"event_class": "FeedbackEvent", "event_id": "", "case_id": "c1", "calibration_category": "accurate"},
            {"not_a_feedback_event": True},
        ]
        exported, summary = export_feedback_analytics_records(rows)
        self.assertEqual(summary.input_count, 3)
        self.assertEqual(summary.exported_count, 1)
        self.assertEqual(summary.skipped_count, 2)
        self.assertGreaterEqual(summary.invalid_count, 1)

    def test_jsonl_round_trip_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inp = root / "in.jsonl"
            out = root / "out.jsonl"
            csv_path = root / "out.csv"
            write_jsonl(
                inp,
                [
                    {
                        "event_class": "FeedbackEvent",
                        "event_id": "fe_1",
                        "case_id": "c1",
                        "calibration_category": "accepted_draft",
                        "occurred_at": "2026-05-18T09:00:00+02:00",
                    },
                    {
                        "event_type": EVENT_TYPE_ADJUDICATION,
                        "event_id": "ae_1",
                        "case_id": "c1",
                        "occurred_at": "2026-05-18T09:01:00+02:00",
                        "payload": {
                            "event_class": "AdjudicationEvent",
                            "adjudication_kind": "reject_same_case",
                            "target_refs": {"source_signal_id": "sig_a"},
                        },
                    },
                ],
            )
            records, summary = export_feedback_analytics_from_jsonl(inp)
            self.assertEqual(summary.exported_count, 2)
            write_feedback_analytics_jsonl(out, records)
            write_feedback_analytics_csv(csv_path, records)
            reread = read_jsonl(out)
            self.assertEqual(len(reread), 2)
            groups = {r["analytics_group"] for r in reread}
            self.assertIn("draft_quality", groups)
            self.assertIn("truth_adjudication", groups)
            flat = flatten_analytics_record_for_csv(reread[1])
            self.assertEqual(flat["source_signal_id"], "sig_a")

    def test_store_export_read_only(self) -> None:
        store = InMemoryMailboxMemoryStore()
        _, cal = route_operator_payload(
            {
                "calibration_category": "wrong_priority",
                "case_id": "c_store",
                "event_id": "fe_store",
            }
        )
        persist_routed_event(store, "calibration", cal)
        _, adj = route_operator_payload(
            {
                "event_domain": "adjudication",
                "adjudication_kind": "invalidate_fact",
                "case_id": "c_store",
                "event_id": "ae_store",
            }
        )
        persist_routed_event(store, "adjudication", adj)
        before = len(store.fetch_events())
        exported, summary = export_feedback_analytics_from_store(store)
        after = len(store.fetch_events())
        self.assertEqual(before, after)
        self.assertEqual(summary.exported_count, 2)
        self.assertEqual(summary.by_domain.get("calibration"), 1)
        self.assertEqual(summary.by_domain.get("adjudication"), 1)

    def test_input_rows_not_mutated(self) -> None:
        original = {
            "event_class": "FeedbackEvent",
            "event_id": "fe_immutable",
            "case_id": "c1",
            "calibration_category": "operator_correction",
            "detail": "keep me",
        }
        snapshot = json.dumps(original, sort_keys=True)
        export_feedback_analytics_records([original])
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot)


if __name__ == "__main__":
    unittest.main()
