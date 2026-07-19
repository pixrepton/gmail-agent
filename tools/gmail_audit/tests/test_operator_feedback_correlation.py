"""Operator feedback correlation IDs (wave 6)."""

from __future__ import annotations

import unittest

from feedback_event_contract import validate_feedback_event
from operator_feedback_runtime import route_operator_payload


class OperatorFeedbackCorrelationTests(unittest.TestCase):
    def test_correlation_ids_in_target_refs(self) -> None:
        raw = {
            "rating": "accurate",
            "calibration_category": "accurate",
            "case_id": "c1",
            "decision_candidate_id": "dc_abc",
            "policy_decision_id": "pd_xyz",
            "action_proposal_id": "ap_123",
            "detail": "ok",
        }
        domain, ev = route_operator_payload(raw)
        self.assertEqual(domain, "calibration")
        self.assertEqual(ev["target_refs"]["decision_candidate_id"], "dc_abc")
        self.assertEqual(ev["target_refs"]["policy_decision_id"], "pd_xyz")

    def test_nested_target_refs_correlation_ids_are_preserved(self) -> None:
        raw = {
            "event_domain": "adjudication",
            "case_id": "c1",
            "adjudication_kind": "reject_same_case",
            "target_refs": {
                "signal_id": "sig_1",
                "rejected_case_id": "c1",
                "source_signal_id": "src_1",
                "decision_candidate_id": "dc_nested",
                "policy_decision_id": "pdec_nested",
                "action_proposal_id": "ap_nested",
            },
        }
        domain, ev = route_operator_payload(raw)
        self.assertEqual(domain, "adjudication")
        self.assertEqual(ev["target_refs"]["signal_id"], "sig_1")
        self.assertEqual(ev["target_refs"]["source_signal_id"], "src_1")
        self.assertEqual(ev["target_refs"]["decision_candidate_id"], "dc_nested")
        self.assertEqual(ev["target_refs"]["policy_decision_id"], "pdec_nested")
        self.assertEqual(ev["target_refs"]["action_proposal_id"], "ap_nested")

    def test_top_level_source_signal_id_is_copied_to_target_refs(self) -> None:
        raw = {
            "event_domain": "adjudication",
            "case_id": "c1",
            "adjudication_kind": "reject_same_case",
            "source_signal_id": "sig_from_row",
            "target_refs": {"rejected_case_id": "c1"},
        }
        domain, ev = route_operator_payload(raw)
        self.assertEqual(domain, "adjudication")
        self.assertEqual(ev["target_refs"]["signal_id"], "sig_from_row")
        self.assertEqual(ev["target_refs"]["source_signal_id"], "sig_from_row")

    def test_new_eval_calibration_categories_validate(self) -> None:
        for cat in (
            "wrong_topic",
            "wrong_case",
            "accepted_draft",
            "rejected_draft",
            "edited_decision",
            "rejected_fact_claim",
            "policy_block",
            "operator_correction",
        ):
            errs = validate_feedback_event(
                {
                    "event_class": "FeedbackEvent",
                    "event_id": f"e_{cat}",
                    "case_id": "c1",
                    "calibration_category": cat,
                }
            )
            self.assertEqual(errs, [], msg=cat)


if __name__ == "__main__":
    unittest.main()
