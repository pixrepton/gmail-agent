"""Quality read-only integration pipeline and proof-pack."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from artifact_io import read_json, read_jsonl
from daszek_v3_operational_feed import build_operational_feed_snapshot
from daszek_v3_operational_feed_contract import validate_operational_feed_snapshot
from eval_shadow_analytics import export_eval_shadow_analytics_from_csv
from feedback_analytics_export import export_feedback_analytics_from_store
from mailbox_memory_store import InMemoryMailboxMemoryStore
from operator_feedback_runtime import persist_routed_event, route_operator_payload
from quality_readonly_integration import (
    build_fixture_mailbox_store,
    build_quality_readonly_proof_pack,
    run_mailbox_feedback_export,
    run_quality_pipeline,
)
from quality_readonly_projection import validate_quality_readonly_slice

TOOL_DIR = Path(__file__).resolve().parent.parent
EVAL_CSV = TOOL_DIR / "examples" / "quality_readonly_proof" / "eval_shadow_review.csv"


class QualityReadonlyIntegrationTests(unittest.TestCase):
    def test_operational_feed_unchanged_without_quality(self) -> None:
        snap = build_operational_feed_snapshot(
            cockpit={"desk": {"items": []}, "cases": {"items": []}},
            day={"sections": []},
            tasks=[],
            snapshot_id="snap-no-quality",
        )
        self.assertNotIn("quality_readonly", snap.get("feed", {}))

    def test_operational_feed_with_quality_slice(self) -> None:
        quality = {
            "schema_version": "quality_readonly_projection.v1",
            "projection_type": "quality_readonly",
            "read_only": True,
            "by_group": {"routing_quality": 1},
            "by_domain": {"calibration": 1},
            "truth_mutation_summary": {"mutates_truth_true_count": 0, "mutates_truth_false_count": 1},
            "correlation_summary": {},
            "recent_records": [],
            "warnings": [],
            "not_proven": ["local_fixture_or_export_file_only"],
            "analytics_key": "routing_quality|calibration|x",
        }
        snap = build_operational_feed_snapshot(
            cockpit={"desk": {"items": []}, "cases": {"items": []}},
            day={"sections": []},
            tasks=[],
            snapshot_id="snap-with-quality",
            quality_readonly=quality,
        )
        qr = snap["feed"]["quality_readonly"]
        self.assertTrue(qr["read_only"])
        rep = validate_operational_feed_snapshot(snap)
        self.assertTrue(rep.ok, rep.errors)

    def test_quality_slice_rejects_forbidden_nested(self) -> None:
        bad = {
            "schema_version": "quality_readonly_projection.v1",
            "projection_type": "quality_readonly",
            "read_only": True,
            "recent_records": [{"body": "secret"}],
        }
        errs = validate_quality_readonly_slice(bad)
        self.assertTrue(errs)

    def test_mailbox_export_read_only_no_mutation(self) -> None:
        store = build_fixture_mailbox_store()
        before = len(store.fetch_events())
        records, summary = export_feedback_analytics_from_store(store)
        after = len(store.fetch_events())
        self.assertEqual(before, after)
        self.assertGreater(summary.exported_count, 0)
        self.assertGreater(len(records), 0)

    def test_mailbox_cli_fixture_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "fb.jsonl"
            meta = run_mailbox_feedback_export(output_jsonl=out, use_fixture_store=True)
            self.assertFalse(meta["store_mutated"])
            rows = read_jsonl(out)
            self.assertGreater(len(rows), 0)
            dumped = json.dumps(rows)
            self.assertNotIn("payload", dumped)

    def test_pipeline_mixed_counts(self) -> None:
        fb = [
            {
                "analytics_group": "routing_quality",
                "event_domain": "calibration",
                "category_or_kind": "wrong_topic",
                "mutates_truth": False,
                "correlation_refs": {"case_id": "c1"},
                "analytics_key": "k1",
            }
        ]
        ev = [
            {
                "analytics_group": "draft_quality",
                "event_domain": "eval_shadow",
                "category_or_kind": "missing_reply_draft",
                "mutates_truth": False,
                "correlation_refs": {"message_id": "m1"},
                "analytics_key": "k2",
            }
        ]
        result = run_quality_pipeline(feedback_records=fb, eval_records=ev)
        self.assertEqual(result.summary["feedback_record_count"], 1)
        self.assertEqual(result.summary["eval_shadow_record_count"], 1)
        self.assertIn("routing_quality", result.quality_snapshot["by_group"])

    def test_load_analytics_jsonl_accepts_pre_sanitized_rows(self) -> None:
        from quality_readonly_integration import _load_analytics_jsonl

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pre.jsonl"
            row = {
                "analytics_group": "routing_quality",
                "event_domain": "calibration",
                "category_or_kind": "wrong_topic",
                "mutates_truth": False,
                "correlation_refs": {"case_id": "c1"},
                "analytics_key": "k",
            }
            from artifact_io import write_jsonl

            write_jsonl(path, [row])
            loaded = _load_analytics_jsonl(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["analytics_group"], "routing_quality")

    def test_proof_pack_produces_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack"
            meta = build_quality_readonly_proof_pack(
                out,
                eval_csv=EVAL_CSV,
                use_fixture_store=True,
            )
            self.assertTrue((out / "README.md").is_file())
            self.assertTrue((out / "feedback_analytics.jsonl").is_file())
            self.assertTrue((out / "eval_analytics.jsonl").is_file())
            self.assertTrue((out / "quality_snapshot.json").is_file())
            self.assertTrue((out / "summary.json").is_file())
            self.assertTrue((out / "operational_feed_with_quality.json").is_file())
            quality = read_json(out / "quality_snapshot.json")
            self.assertEqual(quality["projection_type"], "quality_readonly")
            dumped = json.dumps(quality)
            self.assertNotIn("reviewer_notes", dumped)
            self.assertNotIn('"body"', dumped)

    def test_proof_pack_eval_only_has_eval_domain_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pack-eval-only"
            build_quality_readonly_proof_pack(out, eval_csv=EVAL_CSV, attach_operational_feed=False)
            quality = read_json(out / "quality_snapshot.json")
            self.assertGreater(quality["source_summary"]["eval_shadow_record_count"], 0)
            self.assertIn("eval_shadow", quality.get("by_domain", {}))

    def test_empty_pipeline_inputs_warn_no_records(self) -> None:
        result = run_quality_pipeline()
        self.assertIn("no_analytics_records", result.quality_snapshot.get("warnings", []))

    def test_php_validation_guard_present(self) -> None:
        daszek_root = Path(__file__).resolve().parents[4] / "daszek" / "includes"
        api = (daszek_root / "api-v3-handlers.php").read_text(encoding="utf-8")
        self.assertIn("feed['quality_readonly']", api)
        self.assertIn("projection_type", api)


if __name__ == "__main__":
    unittest.main()
