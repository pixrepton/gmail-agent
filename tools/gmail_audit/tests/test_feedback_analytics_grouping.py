"""Feedback / adjudication → stable analytics groups (QualityHub-ready contract)."""

from __future__ import annotations

import unittest

import feedback_event_contract as fec
from feedback_event_contract import (
    _adjudication_values,
    _calibration_values,
    adjudication_kind_to_analytics_group,
    build_feedback_analytics_record,
    extract_feedback_correlation_refs,
    feedback_category_to_analytics_group,
    is_truth_affecting_adjudication,
)
from operator_feedback_runtime import route_operator_payload


class FeedbackAnalyticsGroupingTests(unittest.TestCase):
    def test_each_calibration_category_maps_to_exactly_one_group(self) -> None:
        groups: dict[str, str] = {}
        for cat in _calibration_values():
            group = feedback_category_to_analytics_group(cat)
            self.assertIn(group, fec._FEEDBACK_ANALYTICS_GROUPS, msg=cat)
            groups[cat] = group
        self.assertEqual(len(groups), len(_calibration_values()))

    def test_each_adjudication_kind_maps_to_exactly_one_group(self) -> None:
        for kind in _adjudication_values():
            group = adjudication_kind_to_analytics_group(kind)
            self.assertIn(group, fec._FEEDBACK_ANALYTICS_GROUPS, msg=kind)

    def test_rejected_fact_claim_is_evidence_quality_not_truth(self) -> None:
        self.assertEqual(feedback_category_to_analytics_group("rejected_fact_claim"), "evidence_quality")
        rec = build_feedback_analytics_record(
            {
                "event_class": "FeedbackEvent",
                "event_id": "fe_1",
                "case_id": "c1",
                "calibration_category": "rejected_fact_claim",
                "detail": "should not appear in analytics record",
            }
        )
        self.assertFalse(rec["mutates_truth"])
        self.assertEqual(rec["analytics_group"], "evidence_quality")
        self.assertNotIn("detail", rec)
        self.assertNotIn("should not appear", str(rec))

    def test_invalidate_fact_is_truth_adjudication(self) -> None:
        self.assertEqual(adjudication_kind_to_analytics_group("invalidate_fact"), "truth_adjudication")
        self.assertTrue(is_truth_affecting_adjudication("invalidate_fact"))
        rec = build_feedback_analytics_record(
            {
                "event_class": "AdjudicationEvent",
                "event_id": "ae_1",
                "case_id": "c1",
                "adjudication_kind": "invalidate_fact",
            }
        )
        self.assertTrue(rec["mutates_truth"])
        self.assertEqual(rec["analytics_group"], "truth_adjudication")

    def test_wrong_case_calibration_vs_reject_same_case_adjudication(self) -> None:
        cal_group = feedback_category_to_analytics_group("wrong_case")
        adj_group = adjudication_kind_to_analytics_group("reject_same_case")
        self.assertEqual(cal_group, "case_link_quality")
        self.assertEqual(adj_group, "truth_adjudication")
        cal_rec = build_feedback_analytics_record(
            {
                "event_class": "FeedbackEvent",
                "event_id": "fe_cal",
                "case_id": "c1",
                "calibration_category": "wrong_case",
            }
        )
        adj_rec = build_feedback_analytics_record(
            {
                "event_class": "AdjudicationEvent",
                "event_id": "ae_adj",
                "case_id": "c1",
                "adjudication_kind": "reject_same_case",
            }
        )
        self.assertFalse(cal_rec["mutates_truth"])
        self.assertTrue(adj_rec["mutates_truth"])
        self.assertEqual(cal_rec["event_domain"], "calibration")
        self.assertEqual(adj_rec["event_domain"], "adjudication")

    def test_unknown_calibration_maps_to_operator_correction_not_crash(self) -> None:
        self.assertEqual(feedback_category_to_analytics_group("totally_new_label"), "operator_correction")
        rec = build_feedback_analytics_record(
            {"event_class": "FeedbackEvent", "event_id": "x", "case_id": "c", "calibration_category": "totally_new_label"}
        )
        self.assertEqual(rec["analytics_group"], "operator_correction")

    def test_unknown_adjudication_kind_maps_safely(self) -> None:
        self.assertEqual(adjudication_kind_to_analytics_group("future_kind"), "operator_correction")

    def test_correlation_refs_preserve_spine_ids(self) -> None:
        raw = {
            "calibration_category": "policy_block",
            "case_id": "c1",
            "decision_candidate_id": "dc_abc",
            "policy_decision_id": "pd_xyz",
            "action_proposal_id": "ap_123",
            "source_signal_id": "sig_top",
        }
        _, ev = route_operator_payload(raw)
        refs = extract_feedback_correlation_refs(ev)
        self.assertEqual(refs["case_id"], "c1")
        self.assertEqual(refs["decision_candidate_id"], "dc_abc")
        self.assertEqual(refs["policy_decision_id"], "pd_xyz")
        self.assertEqual(refs["proposal_id"], "ap_123")
        self.assertEqual(refs["source_signal_id"], "sig_top")
        rec = build_feedback_analytics_record(ev)
        self.assertEqual(rec["correlation_refs"]["decision_candidate_id"], "dc_abc")
        self.assertEqual(rec["correlation_refs"]["policy_decision_id"], "pd_xyz")
        self.assertEqual(rec["correlation_refs"]["proposal_id"], "ap_123")

    def test_analytics_record_excludes_free_text_fields(self) -> None:
        raw = {
            "event_domain": "calibration",
            "case_id": "c1",
            "calibration_category": "rejected_draft",
            "detail": "customer said X",
            "body": "full mail body",
            "snippet": "preview",
            "prompt": "system prompt leak",
        }
        _, ev = route_operator_payload(raw)
        rec = build_feedback_analytics_record(ev)
        dumped = str(rec)
        for forbidden in ("customer said", "full mail", "preview", "system prompt"):
            self.assertNotIn(forbidden, dumped)
        self.assertNotIn("body", rec)
        self.assertNotIn("snippet", rec)
        self.assertNotIn("detail", rec.get("correlation_refs", {}))


if __name__ == "__main__":
    unittest.main()
