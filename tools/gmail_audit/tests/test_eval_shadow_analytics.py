"""Eval shadow → feedback analytics group alignment."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from eval_shadow import evaluate_annotations
from eval_shadow_analytics import (
    EVAL_SHADOW_EVENT_DOMAIN,
    build_eval_shadow_analytics_record,
    build_eval_shadow_analytics_records_from_eval_detail,
    build_eval_shadow_analytics_records_from_review_row,
    eval_failure_cluster_to_analytics_group,
    eval_match_field_to_analytics_group,
    export_eval_shadow_analytics_from_csv,
    export_eval_shadow_analytics_records,
)
from tests.fixture_helpers import run_fixture
from tests.test_eval_shadow import EvalShadowTests


class EvalShadowAnalyticsTests(unittest.TestCase):
    def test_failure_clusters_map_to_expected_groups(self) -> None:
        self.assertEqual(eval_failure_cluster_to_analytics_group("case_link_mismatch"), "case_link_quality")
        self.assertEqual(eval_failure_cluster_to_analytics_group("missing_reply_draft"), "draft_quality")
        self.assertEqual(eval_failure_cluster_to_analytics_group("missed_review_gate"), "policy_quality")
        self.assertEqual(eval_failure_cluster_to_analytics_group("reference_extraction_miss"), "evidence_quality")
        self.assertEqual(eval_failure_cluster_to_analytics_group("business_action_mismatch"), "decision_quality")
        self.assertEqual(eval_failure_cluster_to_analytics_group("rejected_fact_claim"), "evidence_quality")

    def test_match_fields_map_to_expected_groups(self) -> None:
        self.assertEqual(eval_match_field_to_analytics_group("priority"), "priority_quality")
        self.assertEqual(eval_match_field_to_analytics_group("reply_quality"), "draft_quality")
        self.assertEqual(eval_match_field_to_analytics_group("action_primary"), "decision_quality")

    def test_eval_shadow_never_mutates_truth(self) -> None:
        rec = build_eval_shadow_analytics_record(
            analytics_group="truth_adjudication",
            category_or_kind="invalidate_fact",
            correlation_refs={"message_id": "m1"},
        )
        self.assertFalse(rec["mutates_truth"])
        self.assertEqual(rec["event_domain"], EVAL_SHADOW_EVENT_DOMAIN)

    def test_review_row_exports_without_free_text(self) -> None:
        row = {
            "message_id": "msg_eval_1",
            "subject": "Secret subject line",
            "sender": "customer@example.com",
            "reviewer_notes": "long free text notes",
            "reviewer_failure_cluster": "wrong_priority,missing_reply_draft",
            "prompt_change_hint": "priority,draft",
        }
        records = build_eval_shadow_analytics_records_from_review_row(row)
        self.assertGreaterEqual(len(records), 2)
        dumped = json.dumps(records)
        self.assertNotIn("Secret subject", dumped)
        self.assertNotIn("customer@example.com", dumped)
        self.assertNotIn("long free text", dumped)
        groups = {r["analytics_group"] for r in records}
        self.assertIn("priority_quality", groups)
        self.assertIn("draft_quality", groups)
        self.assertTrue(all(not r["mutates_truth"] for r in records))
        self.assertEqual(records[0]["correlation_refs"]["message_id"], "msg_eval_1")

    def test_eval_detail_mismatch_exports_evidence_not_truth(self) -> None:
        detail = {
            "message_id": "msg_eval_2",
            "status": "compared",
            "matches": {
                "priority": False,
                "reply_quality": False,
            },
            "failure_clusters": ["reference_extraction_miss"],
            "expected": {"case_key_if_known": "case_key_abc"},
        }
        records = build_eval_shadow_analytics_records_from_eval_detail(detail)
        groups = {r["analytics_group"] for r in records}
        self.assertIn("priority_quality", groups)
        self.assertIn("draft_quality", groups)
        self.assertIn("evidence_quality", groups)
        self.assertNotIn("truth_adjudication", groups)
        self.assertTrue(all(not r["mutates_truth"] for r in records))

    def test_correlation_ids_preserved_when_present(self) -> None:
        row = {
            "message_id": "msg_corr",
            "decision_candidate_id": "dc_1",
            "policy_decision_id": "pd_1",
            "proposal_id": "ap_1",
            "source_signal_id": "sig_1",
            "reviewer_failure_cluster": "action_plan_mismatch",
        }
        records = build_eval_shadow_analytics_records_from_review_row(row)
        refs = records[0]["correlation_refs"]
        self.assertEqual(refs["message_id"], "msg_corr")
        self.assertEqual(refs["decision_candidate_id"], "dc_1")
        self.assertEqual(refs["policy_decision_id"], "pd_1")
        self.assertEqual(refs["proposal_id"], "ap_1")
        self.assertEqual(refs["source_signal_id"], "sig_1")

    def test_unknown_cluster_maps_safely(self) -> None:
        self.assertEqual(eval_failure_cluster_to_analytics_group("brand_new_cluster"), "operator_correction")

    def test_export_summary_counts(self) -> None:
        rows = [
            {
                "message_id": "a",
                "reviewer_failure_cluster": "wrong_topic",
            },
            {
                "message_id": "b",
                "reviewer_failure_cluster": "",
            },
        ]
        exported, summary = export_eval_shadow_analytics_records(rows)
        self.assertEqual(summary.input_count, 2)
        self.assertEqual(summary.exported_count, 1)
        self.assertEqual(summary.skipped_count, 1)
        self.assertEqual(exported[0]["analytics_group"], "routing_quality")

    def test_integration_with_evaluate_annotations(self) -> None:
        import csv

        from artifact_contracts import REVIEW_TEMPLATE_FIELDS
        from eval_shadow import build_review_row

        result = run_fixture("new_lead")
        stage_record = EvalShadowTests._build_stage_record(result)
        review_row = build_review_row(result["intake_result"], stage_record=stage_record)
        review_row["expected_decision_action"] = "ignore"
        review_row["reviewer_failure_cluster"] = "false_ignore"

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "ann.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=REVIEW_TEMPLATE_FIELDS)
                writer.writeheader()
                writer.writerow(review_row)
            _, details = evaluate_annotations([result["intake_result"]], csv_path, stage_records=[stage_record])
            exported_csv, _ = export_eval_shadow_analytics_from_csv(csv_path)
            exported_detail, _ = export_eval_shadow_analytics_records(details, from_eval_details=True)
        self.assertTrue(any(r["analytics_group"] == "routing_quality" for r in exported_csv))
        self.assertTrue(exported_detail)


if __name__ == "__main__":
    unittest.main()
