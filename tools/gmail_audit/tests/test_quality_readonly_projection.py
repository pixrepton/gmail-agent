"""Read-only quality projection contract (feedback + eval shadow merge)."""

from __future__ import annotations

import json
import unittest

from quality_readonly_projection import (
    DEFAULT_RECENT_RECORD_LIMIT,
    attach_quality_slice_to_operational_feed,
    build_quality_readonly_projection,
    sanitize_analytics_record_for_projection,
)


def _fb(group: str, domain: str, *, mutates: bool = False, refs: dict | None = None) -> dict:
    return {
        "analytics_group": group,
        "event_domain": domain,
        "category_or_kind": f"kind_{group}",
        "mutates_truth": mutates,
        "correlation_refs": refs or {},
        "analytics_key": f"{group}|{domain}|kind",
        "observed_at": "2026-05-18T12:00:00Z",
    }


def _eval(group: str, refs: dict | None = None) -> dict:
    return _fb(group, "eval_shadow", mutates=False, refs=refs)


class QualityReadonlyProjectionTests(unittest.TestCase):
    def test_feedback_counts_by_group_and_domain(self) -> None:
        records = [
            _fb("routing_quality", "calibration"),
            _fb("priority_quality", "calibration"),
            _fb("truth_adjudication", "adjudication", mutates=True),
        ]
        snap = build_quality_readonly_projection(records, None)
        self.assertEqual(snap["source_summary"]["feedback_record_count"], 3)
        self.assertEqual(snap["source_summary"]["eval_shadow_record_count"], 0)
        self.assertEqual(snap["by_group"]["routing_quality"], 1)
        self.assertEqual(snap["by_domain"]["calibration"], 2)
        self.assertEqual(snap["by_domain"]["adjudication"], 1)

    def test_eval_shadow_counts_by_group(self) -> None:
        records = [_eval("draft_quality"), _eval("evidence_quality")]
        snap = build_quality_readonly_projection(None, records)
        self.assertEqual(snap["source_summary"]["eval_shadow_record_count"], 2)
        self.assertEqual(snap["by_group"]["draft_quality"], 1)
        self.assertEqual(snap["by_domain"]["eval_shadow"], 2)

    def test_mixed_merge(self) -> None:
        snap = build_quality_readonly_projection(
            [_fb("policy_quality", "calibration")],
            [_eval("routing_quality")],
        )
        self.assertEqual(snap["source_summary"]["feedback_record_count"], 1)
        self.assertEqual(snap["source_summary"]["eval_shadow_record_count"], 1)
        self.assertEqual(snap["source_summary"]["exported_count"], 2)

    def test_truth_mutation_summary(self) -> None:
        snap = build_quality_readonly_projection(
            [
                _fb("evidence_quality", "calibration", mutates=False),
                _fb("truth_adjudication", "adjudication", mutates=True),
            ],
            [_eval("evidence_quality")],
        )
        truth = snap["truth_mutation_summary"]
        self.assertEqual(truth["mutates_truth_true_count"], 1)
        self.assertEqual(truth["mutates_truth_false_count"], 2)

    def test_correlation_summary(self) -> None:
        refs = {
            "case_id": "c1",
            "source_signal_id": "sig1",
            "decision_candidate_id": "dc1",
            "policy_decision_id": "pd1",
            "proposal_id": "ap1",
        }
        snap = build_quality_readonly_projection([_fb("decision_quality", "calibration", refs=refs)], None)
        cs = snap["correlation_summary"]
        self.assertEqual(cs["case_id"], 1)
        self.assertEqual(cs["source_signal_id"], 1)
        self.assertEqual(cs["decision_candidate_id"], 1)
        self.assertEqual(cs["policy_decision_id"], 1)
        self.assertEqual(cs["proposal_id"], 1)

    def test_recent_records_bounded(self) -> None:
        many = [_fb("operator_correction", "calibration", refs={"case_id": f"c{i}"}) for i in range(40)]
        snap = build_quality_readonly_projection(many, None, recent_limit=5)
        self.assertEqual(len(snap["recent_records"]), 5)

    def test_raw_fields_stripped_or_ignored(self) -> None:
        row = _fb("routing_quality", "calibration")
        row["detail"] = "customer secret"
        row["prompt"] = "system prompt leak"
        safe = sanitize_analytics_record_for_projection(row)
        assert safe is not None
        dumped = json.dumps(safe)
        self.assertNotIn("customer secret", dumped)
        self.assertNotIn("system prompt", dumped)
        row2 = _fb("draft_quality", "calibration")
        row2["correlation_refs"] = {"case_id": "c1", "body": "must drop"}
        safe2 = sanitize_analytics_record_for_projection(row2)
        assert safe2 is not None
        self.assertNotIn("body", safe2["correlation_refs"])

    def test_unknown_group_does_not_crash(self) -> None:
        snap = build_quality_readonly_projection([{"analytics_group": "not_a_real_group", "event_domain": "calibration"}], None)
        self.assertIn("unknown", snap["by_group"])

    def test_empty_snapshot_is_safe(self) -> None:
        snap = build_quality_readonly_projection([], [])
        self.assertEqual(snap["source_summary"]["exported_count"], 0)
        self.assertEqual(snap["recent_records"], [])
        self.assertIn("no_analytics_records", snap["warnings"])
        self.assertTrue(snap["not_proven"])

    def test_inputs_not_mutated(self) -> None:
        original = _fb("routing_quality", "calibration", refs={"case_id": "c1"})
        snapshot_before = json.dumps(original, sort_keys=True)
        build_quality_readonly_projection([original], None)
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot_before)

    def test_operational_feed_attach_is_read_only_slice(self) -> None:
        feed = {
            "schema_name": "daszek_operational_feed_snapshot",
            "read_only": True,
            "feed": {"desk": []},
        }
        quality = build_quality_readonly_projection([_fb("routing_quality", "calibration")], None)
        merged = attach_quality_slice_to_operational_feed(feed, quality)
        self.assertIn("quality_readonly", merged["feed"])
        self.assertEqual(merged["feed"]["quality_readonly"]["projection_type"], "quality_readonly")
        self.assertNotIn("body", json.dumps(merged))

    def test_default_recent_limit_constant(self) -> None:
        self.assertEqual(DEFAULT_RECENT_RECORD_LIMIT, 25)


if __name__ == "__main__":
    unittest.main()
