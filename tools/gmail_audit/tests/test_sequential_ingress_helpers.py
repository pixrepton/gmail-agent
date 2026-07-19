"""Tests for sequential_ingress_helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from sequential_ingress_helpers import (
    GROQ_429_DETECTED_COUNT_NOTE,
    aggregate_projection_batch_summary,
    build_gmail_intake_message_command,
    build_sequential_operator_summary,
    compute_retry_delay,
    extract_run_dir_from_text,
    is_rate_limit_signal,
    load_completed_message_ids,
    make_child_runs_index_row,
    parse_newer_than_days,
)


class ParseNewerThanTests(unittest.TestCase):
    def test_parse_variants(self) -> None:
        self.assertEqual(parse_newer_than_days("14d"), 14)
        self.assertEqual(parse_newer_than_days("newer_than:7d"), 7)
        self.assertEqual(parse_newer_than_days("1"), 1)
        self.assertIsNone(parse_newer_than_days(""))
        self.assertIsNone(parse_newer_than_days(None))
        self.assertIsNone(parse_newer_than_days("not-a-duration"))


class BuildCommandTests(unittest.TestCase):
    def test_build_message_cmd_minimal(self) -> None:
        cmd = build_gmail_intake_message_command(
            python_executable="python",
            intake_py="/app/tools/gmail_audit/gmail_intake.py",
            message_id="mid",
            gmail_source="google_api",
            push_daszek=False,
            projection_proof=False,
            keep_going=False,
            verbose=False,
        )
        self.assertEqual(cmd[:4], ["python", "/app/tools/gmail_audit/gmail_intake.py", "signal-run", "--oneshot"])
        self.assertIn("--message-id", cmd)
        self.assertIn("mid", cmd)

    def test_build_message_cmd_flags(self) -> None:
        cmd = build_gmail_intake_message_command(
            python_executable="py",
            intake_py="gmail_intake.py",
            message_id="x",
            gmail_source="google_api",
            push_daszek=True,
            projection_proof=True,
            keep_going=True,
            verbose=True,
        )
        self.assertIn("--push-daszek", cmd)
        self.assertIn("--projection-proof", cmd)
        self.assertIn("--keep-going", cmd)
        self.assertIn("--verbose", cmd)


class AggregateBatchTests(unittest.TestCase):
    def test_aggregate_rollups(self) -> None:
        summaries = [
            {"status": "completed", "errors_by_category": {"throttle": 1}},
            {"status": "failed", "errors_by_category": {}},
        ]
        items = [
            {"policy_status": "accepted_projection", "surface": "v2_ingest", "store_readback": "found", "ui_visibility_expected": True},
            {"policy_status": "blocked_policy", "surface": "v2_ingest"},
        ]
        out = aggregate_projection_batch_summary(child_summaries=summaries, proof_items=items)
        self.assertEqual(out["processed_total"], 2)
        self.assertEqual(out["processed_ok"], 1)
        self.assertEqual(out["v2_projection_accepted"], 1)
        self.assertEqual(out["v2_projection_blocked_policy"], 1)
        self.assertEqual(out["v2_readback_found"], 1)

    def test_aggregate_handles_empty_proof_items_gracefully(self) -> None:
        out = aggregate_projection_batch_summary(child_summaries=[], proof_items=[])
        self.assertEqual(out["processed_total"], 0)
        self.assertEqual(out["groq_429_retries"], 0)
        self.assertEqual(out["v2_projection_accepted"], 0)


class ExtractRunDirTests(unittest.TestCase):
    def test_extract_run_dir_from_stdout_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runs_root = Path(td)
            rid = "run-json-1"
            (runs_root / rid).mkdir(parents=True)
            stdout = json.dumps({"run_id": rid, "status": "completed"})
            got = extract_run_dir_from_text(stdout=stdout, stderr="", runs_root=runs_root, parsed_summary=None)
            self.assertEqual(got, runs_root / rid)

    def test_extract_run_dir_from_stderr_info_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runs_root = Path(td)
            rd = runs_root / "from-stderr"
            rd.mkdir(parents=True)
            stderr = f"prep\n[info] Run directory: {rd}\n"
            got = extract_run_dir_from_text(stdout="not-json", stderr=stderr, runs_root=runs_root, parsed_summary=None)
            self.assertEqual(got.resolve(), rd.resolve())


class RateLimitSignalTests(unittest.TestCase):
    def test_is_rate_limit_signal_detects_429_in_stderr(self) -> None:
        self.assertTrue(
            is_rate_limit_signal(
                returncode=1,
                stdout="",
                stderr="HTTP 429 Too Many Requests",
                parsed_summary=None,
            )
        )

    def test_is_rate_limit_signal_detects_throttle_in_summary(self) -> None:
        self.assertTrue(
            is_rate_limit_signal(
                returncode=0,
                stdout="",
                stderr="",
                parsed_summary={"errors_by_category": {"throttle": 2}},
            )
        )


class BackoffTests(unittest.TestCase):
    def test_compute_retry_delay_exponential_with_cap(self) -> None:
        self.assertEqual(compute_retry_delay(attempt=1, base=30.0, cap=300.0), 30.0)
        self.assertEqual(compute_retry_delay(attempt=2, base=30.0, cap=300.0), 60.0)
        self.assertEqual(compute_retry_delay(attempt=10, base=30.0, cap=50.0), 50.0)


class ResumeIndexTests(unittest.TestCase):
    def test_load_completed_message_ids_skips_failed_attempts(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        rows = [
            make_child_runs_index_row(
                message_id="a",
                attempt=1,
                returncode=1,
                run_id="",
                run_dir="",
                parsed_summary_present=False,
                rate_limited=True,
                final=False,
                final_status="retry",
            ),
            make_child_runs_index_row(
                message_id="a",
                attempt=2,
                returncode=0,
                run_id="r1",
                run_dir="/x",
                parsed_summary_present=True,
                rate_limited=False,
                final=True,
                final_status="completed",
            ),
            make_child_runs_index_row(
                message_id="b",
                attempt=3,
                returncode=1,
                run_id="",
                run_dir="",
                parsed_summary_present=False,
                rate_limited=True,
                final=True,
                final_status="rate_limit_exhausted",
            ),
        ]
        (tmp / "child_runs_index.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8",
        )
        done = load_completed_message_ids(tmp)
        self.assertEqual(done, {"a"})

    def test_load_completed_message_ids_force_returns_empty(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "child_runs_index.jsonl").write_text(
            json.dumps(
                make_child_runs_index_row(
                    message_id="a",
                    attempt=1,
                    returncode=0,
                    run_id="r",
                    run_dir="/x",
                    parsed_summary_present=True,
                    rate_limited=False,
                    final=True,
                    final_status="completed",
                ),
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(load_completed_message_ids(tmp, force=True), set())


class OperatorSummaryMergeTests(unittest.TestCase):
    def test_build_sequential_operator_summary_counts_and_paths(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "child_runs_index.jsonl").write_text(
            json.dumps(
                {
                    "message_id": "msg1",
                    "final": True,
                    "parsed_summary_present": True,
                    "final_status": "completed",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        core = aggregate_projection_batch_summary(
            child_summaries=[{"status": "completed"}],
            proof_items=[],
        )
        core["child_summaries_count"] = 1
        core["proof_items_count"] = 0
        out = build_sequential_operator_summary(
            rollup_core=core,
            batch_dir=tmp,
            requested_message_ids=["msg1"],
            dry_run=False,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T01:00:00+00:00",
            groq_429_detected_count=3,
            projection_proof_enabled=False,
            proof_items=[],
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["succeeded_count"], 1)
        self.assertEqual(out["groq_429_detected_count"], 3)
        self.assertEqual(out.get("groq_429_detected_count_note"), GROQ_429_DETECTED_COUNT_NOTE)
        self.assertIn("resume_checkpoint_path", out)


if __name__ == "__main__":
    unittest.main()
