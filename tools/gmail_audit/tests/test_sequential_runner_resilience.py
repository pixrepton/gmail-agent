"""Resilience tests for scripts/sequential_gmail_ingress_daszek.py (mocked subprocess + Gmail)."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = TOOL_DIR.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "sequential_gmail_ingress_daszek.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("seq_gmail_runner", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


class SequentialRunnerResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._argv = sys.argv[:]

    def tearDown(self) -> None:
        sys.argv[:] = self._argv

    def test_windows_batch_dir_stays_local_outside_container(self) -> None:
        mod = _load_runner()
        with patch.object(mod, "_running_inside_app_container", return_value=False):
            resolved = mod._resolve_batch_dir(r"C:\proof dir\row3-1")
        self.assertEqual(resolved, Path(r"C:\proof dir\row3-1").resolve())

    def test_existing_batch_dir_does_not_enable_implicit_resume(self) -> None:
        mod = _load_runner()
        rid = "pytest-no-implicit-resume"
        run_dir = TOOL_DIR / "runs" / rid
        run_dir.mkdir(parents=True)
        batch_dir = Path(tempfile.mkdtemp())
        try:
            (batch_dir / "child_runs_index.jsonl").write_text(
                json.dumps({"message_id": "m1", "final": True, "final_status": "completed"}) + "\n",
                encoding="utf-8",
            )

            def fake_search(*_a, **_k):
                return {"responses": [{"message_id": "m1"}]}

            calls: list[list[str]] = []

            def fake_run(cmd, *_a, **_k):
                calls.append(list(cmd))
                return subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"status": "completed", "run_id": rid}) + "\n",
                    stderr="",
                )

            sys.argv = [
                "sequential_gmail_ingress_daszek.py",
                "--batch-dir",
                str(batch_dir),
                "--limit",
                "1",
                "--delay",
                "0",
            ]

            with (
                patch("config.load_settings", return_value=MagicMock()),
                patch("gmail_fetch.search_emails", side_effect=fake_search),
                patch.object(mod.subprocess, "run", side_effect=fake_run),
            ):
                rc = mod.main()

            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][calls[0].index("--message-id") + 1], "m1")
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_retry_429_then_ok(self) -> None:
        mod = _load_runner()
        rid = "pytest-seq-retry-ok"
        run_dir = TOOL_DIR / "runs" / rid
        run_dir.mkdir(parents=True)
        try:
            (run_dir / "projection_proof_report.json").write_text(
                json.dumps({"items": [{"policy_status": "accepted_projection", "store_readback": "found"}]}),
                encoding="utf-8",
            )
            batch_dir = Path(tempfile.mkdtemp())

            def fake_search(*_a, **_k):
                return {"responses": [{"message_id": "mid-a"}]}

            proc_sequence = [
                subprocess.CompletedProcess(
                    [],
                    1,
                    stdout="",
                    stderr="Error: 429 Too Many Requests\n",
                ),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"status": "completed", "run_id": rid}) + "\n",
                    stderr="",
                ),
            ]

            def fake_run(*_a, **_k):
                return proc_sequence.pop(0)

            sys.argv = [
                "sequential_gmail_ingress_daszek.py",
                "--batch-dir",
                str(batch_dir),
                "--limit",
                "5",
                "--delay",
                "0",
                "--projection-proof",
                "--max-retries-per-message",
                "2",
                "--retry-base-delay",
                "0",
                "--retry-max-delay",
                "1",
            ]

            with (
                patch("config.load_settings", return_value=MagicMock()),
                patch("gmail_fetch.search_emails", side_effect=fake_search),
                patch.object(mod.subprocess, "run", side_effect=fake_run),
                patch.object(mod.time, "sleep", return_value=None),
            ):
                rc = mod.main()

            self.assertEqual(rc, 0)
            summary_path = batch_dir / "sequential_summary.json"
            self.assertTrue(summary_path.is_file())
            rollup = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(rollup.get("processed_total"), 1)
            self.assertGreaterEqual(int(rollup.get("groq_429_detected_count") or 0), 1)
            self.assertEqual(rollup.get("failed_count"), 0)
            self.assertEqual(rollup.get("succeeded_count"), 1)
            self.assertIn("groq_429_detected_count_note", rollup)
            idx_lines = (batch_dir / "child_runs_index.jsonl").read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(idx_lines), 2)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_permanent_rate_limit_writes_batch_artifacts(self) -> None:
        mod = _load_runner()
        batch_dir = Path(tempfile.mkdtemp())

        def fake_search(*_a, **_k):
            return {"responses": [{"message_id": "mid-perm"}]}

        proc_fail = subprocess.CompletedProcess([], 1, stdout="", stderr="429 Too Many Requests\n")

        def fake_run(*_a, **_k):
            return proc_fail

        sys.argv = [
            "sequential_gmail_ingress_daszek.py",
            "--batch-dir",
            str(batch_dir),
            "--limit",
            "3",
            "--delay",
            "0",
            "--max-retries-per-message",
            "2",
            "--retry-base-delay",
            "0",
            "--retry-max-delay",
            "0.01",
        ]

        with (
            patch("config.load_settings", return_value=MagicMock()),
            patch("gmail_fetch.search_emails", side_effect=fake_search),
            patch.object(mod.subprocess, "run", side_effect=fake_run),
            patch.object(mod.time, "sleep", return_value=None),
        ):
            rc = mod.main()

        self.assertEqual(rc, 0)
        self.assertTrue((batch_dir / "sequential_meta.json").is_file())
        self.assertTrue((batch_dir / "sequential_summary.json").is_file())
        last = (batch_dir / "child_runs_index.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
        row = json.loads(last)
        self.assertEqual(row.get("final_status"), "rate_limit_exhausted")
        self.assertTrue(row.get("final"))
        fi = json.loads((batch_dir / "failed_items.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(fi.get("terminal_attempt"), 3)

    def test_retry_on_429_legacy_flag_warns_stderr(self) -> None:
        mod = _load_runner()
        batch_dir = Path(tempfile.mkdtemp())
        stderr_buf = io.StringIO()
        sys.argv = [
            "sequential_gmail_ingress_daszek.py",
            "--batch-dir",
            str(batch_dir),
            "--dry-run",
            "--limit",
            "1",
            "--retry-on-429",
        ]
        with redirect_stderr(stderr_buf):
            with (
                patch("config.load_settings", return_value=MagicMock()),
                patch("gmail_fetch.search_emails", return_value={"responses": []}),
            ):
                mod.main()
        err = stderr_buf.getvalue()
        self.assertIn("compatibility no-op", err)
        self.assertIn("--max-retries-per-message", err)

    def test_dry_run_projection_proof_writes_batch_json(self) -> None:
        mod = _load_runner()
        batch_dir = Path(tempfile.mkdtemp())
        sys.argv = [
            "sequential_gmail_ingress_daszek.py",
            "--batch-dir",
            str(batch_dir),
            "--dry-run",
            "--projection-proof",
            "--limit",
            "1",
        ]
        with (
            patch("config.load_settings", return_value=MagicMock()),
            patch("gmail_fetch.search_emails", return_value={"responses": []}),
        ):
            rc = mod.main()
        self.assertEqual(rc, 0)
        self.assertTrue((batch_dir / "sequential_summary.json").is_file())
        self.assertTrue((batch_dir / "projection_proof_report.batch.json").is_file())
        doc = json.loads((batch_dir / "projection_proof_report.batch.json").read_text(encoding="utf-8"))
        self.assertEqual(doc.get("items"), [])
        self.assertIn("projection_breakdown", doc)

    def test_exclude_message_id_selects_replacement_before_limit_cutoff(self) -> None:
        mod = _load_runner()
        batch_dir = Path(tempfile.mkdtemp())
        calls: list[list[str]] = []
        search_kwargs: list[dict[str, object]] = []

        def fake_search(*_a, **kwargs):
            search_kwargs.append(dict(kwargs))
            return {"responses": [{"message_id": "excluded"}, {"message_id": "replacement"}]}

        def fake_run(cmd, *_a, **_k):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"status": "completed", "run_id": "missing-run-dir"}) + "\n",
                stderr="",
            )

        sys.argv = [
            "sequential_gmail_ingress_daszek.py",
            "--batch-dir",
            str(batch_dir),
            "--limit",
            "1",
            "--delay",
            "0",
            "--exclude-message-id",
            "excluded",
        ]

        with (
            patch("config.load_settings", return_value=MagicMock()),
            patch("gmail_fetch.search_emails", side_effect=fake_search),
            patch.object(mod.subprocess, "run", side_effect=fake_run),
        ):
            rc = mod.main()

        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        message_id = calls[0][calls[0].index("--message-id") + 1]
        self.assertEqual(message_id, "replacement")
        self.assertGreater(int(search_kwargs[0]["max_results"]), 1)
        summary = json.loads((batch_dir / "sequential_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary.get("requested_count"), 1)
        selected = json.loads((batch_dir / "selected_message_ids.json").read_text(encoding="utf-8"))
        self.assertEqual(selected, ["replacement"])

    def test_without_exclude_keeps_fetch_limit_equal_to_requested_limit(self) -> None:
        mod = _load_runner()
        batch_dir = Path(tempfile.mkdtemp())
        search_kwargs: list[dict[str, object]] = []

        def fake_search(*_a, **kwargs):
            search_kwargs.append(dict(kwargs))
            return {"responses": [{"message_id": "mid-a"}, {"message_id": "mid-b"}]}

        sys.argv = [
            "sequential_gmail_ingress_daszek.py",
            "--batch-dir",
            str(batch_dir),
            "--dry-run",
            "--limit",
            "2",
        ]

        with (
            patch("config.load_settings", return_value=MagicMock()),
            patch("gmail_fetch.search_emails", side_effect=fake_search),
        ):
            rc = mod.main()

        self.assertEqual(rc, 0)
        self.assertEqual(search_kwargs[0]["max_results"], 2)
        summary = json.loads((batch_dir / "sequential_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary.get("requested_count"), 2)

    def test_explicit_message_ids_skip_gmail_search_and_respect_limit(self) -> None:
        mod = _load_runner()
        batch_dir = Path(tempfile.mkdtemp())
        calls: list[list[str]] = []

        def fake_run(cmd, *_a, **_k):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"status": "completed", "run_id": "explicit-run"}) + "\n",
                stderr="",
            )

        sys.argv = [
            "sequential_gmail_ingress_daszek.py",
            "--batch-dir",
            str(batch_dir),
            "--limit",
            "2",
            "--delay",
            "0",
            "--message-id",
            "explicit-a",
            "--message-id",
            "explicit-b",
            "--message-id",
            "explicit-c",
        ]

        with (
            patch("config.load_settings", return_value=MagicMock()),
            patch("gmail_fetch.search_emails", side_effect=AssertionError("gmail search should not run")),
            patch.object(mod.subprocess, "run", side_effect=fake_run),
        ):
            rc = mod.main()

        self.assertEqual(rc, 0)
        selected = json.loads((batch_dir / "selected_message_ids.json").read_text(encoding="utf-8"))
        self.assertEqual(selected, ["explicit-a", "explicit-b"])
        self.assertEqual([cmd[cmd.index("--message-id") + 1] for cmd in calls], ["explicit-a", "explicit-b"])
        summary = json.loads((batch_dir / "sequential_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary.get("requested_count"), 2)

    def test_resume_skips_completed_retries_failed(self) -> None:
        mod = _load_runner()
        rid = "pytest-seq-resume"
        run_dir = TOOL_DIR / "runs" / rid
        run_dir.mkdir(parents=True)
        batch_dir = Path(tempfile.mkdtemp())
        try:
            (run_dir / "projection_proof_report.json").write_text(
                json.dumps({"items": [{"policy_status": "accepted_projection"}]}),
                encoding="utf-8",
            )

            ok_m1 = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"status": "completed", "run_id": rid}) + "\n",
                stderr="",
            )
            fail429 = subprocess.CompletedProcess([], 1, stdout="", stderr="429 Too Many Requests\n")

            round1 = [
                ok_m1,
                fail429,
                fail429,
                fail429,
            ]

            def fake_search(*_a, **_k):
                return {"responses": [{"message_id": "m1"}, {"message_id": "m2"}]}

            def run_round1(*_a, **_k):
                return round1.pop(0)

            sys.argv = [
                "sequential_gmail_ingress_daszek.py",
                "--batch-dir",
                str(batch_dir),
                "--resume",
                "--limit",
                "10",
                "--delay",
                "0",
                "--projection-proof",
                "--max-retries-per-message",
                "2",
                "--retry-base-delay",
                "0",
                "--retry-max-delay",
                "0.01",
            ]

            with (
                patch("config.load_settings", return_value=MagicMock()),
                patch("gmail_fetch.search_emails", side_effect=fake_search),
                patch.object(mod.subprocess, "run", side_effect=run_round1),
                patch.object(mod.time, "sleep", return_value=None),
            ):
                rc1 = mod.main()
            self.assertEqual(rc1, 0)

            ok_m2 = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"status": "completed", "run_id": rid}) + "\n",
                stderr="",
            )

            calls: list[int] = []

            def run_round2(*_a, **_k):
                calls.append(1)
                return ok_m2

            sys.argv = [
                "sequential_gmail_ingress_daszek.py",
                "--batch-dir",
                str(batch_dir),
                "--resume",
                "--limit",
                "10",
                "--delay",
                "0",
                "--projection-proof",
                "--max-retries-per-message",
                "2",
                "--retry-base-delay",
                "0",
                "--retry-max-delay",
                "0.01",
            ]

            with (
                patch("config.load_settings", return_value=MagicMock()),
                patch("gmail_fetch.search_emails", side_effect=fake_search),
                patch.object(mod.subprocess, "run", side_effect=run_round2),
                patch.object(mod.time, "sleep", return_value=None),
            ):
                rc2 = mod.main()
            self.assertEqual(rc2, 0)
            self.assertEqual(len(calls), 1)

            rollup = json.loads((batch_dir / "sequential_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(rollup.get("processed_total"), 2)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_child_stdout_not_json_writes_summary_and_failed_items(self) -> None:
        mod = _load_runner()
        batch_dir = Path(tempfile.mkdtemp())

        def fake_search(*_a, **_k):
            return {"responses": [{"message_id": "mid-not-json"}]}

        bad = subprocess.CompletedProcess([], 1, stdout="not-json-output\n", stderr="stderr tail here\n")

        def fake_run(*_a, **_k):
            return bad

        sys.argv = [
            "sequential_gmail_ingress_daszek.py",
            "--batch-dir",
            str(batch_dir),
            "--limit",
            "5",
            "--delay",
            "0",
        ]

        with (
            patch("config.load_settings", return_value=MagicMock()),
            patch("gmail_fetch.search_emails", side_effect=fake_search),
            patch.object(mod.subprocess, "run", side_effect=fake_run),
        ):
            rc = mod.main()

        self.assertEqual(rc, 0)
        summary = json.loads((batch_dir / "sequential_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary.get("status"), "failed")
        self.assertGreaterEqual(summary.get("missing_summary_count") or 0, 1)
        failed_lines = (batch_dir / "failed_items.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(failed_lines), 1)
        failed_row = json.loads(failed_lines[0])
        self.assertFalse(failed_row.get("stdout_parse_ok"))
        self.assertEqual(failed_row.get("terminal_attempt"), 1)

    def test_partial_success_projection_batch_partial_breakdown(self) -> None:
        mod = _load_runner()
        rid = "pytest-seq-partial"
        run_dir = TOOL_DIR / "runs" / rid
        run_dir.mkdir(parents=True)
        batch_dir = Path(tempfile.mkdtemp())
        try:
            (run_dir / "projection_proof_report.json").write_text(
                json.dumps({"items": [{"policy_status": "accepted_projection", "store_readback": "found"}]}),
                encoding="utf-8",
            )

            ok = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"status": "completed", "run_id": rid}) + "\n",
                stderr="",
            )
            bad = subprocess.CompletedProcess([], 1, stdout="no-json", stderr="")

            procs = [ok, bad]

            def fake_search(*_a, **_k):
                return {"responses": [{"message_id": "a1"}, {"message_id": "b2"}]}

            def fake_run(*_a, **_k):
                return procs.pop(0)

            sys.argv = [
                "sequential_gmail_ingress_daszek.py",
                "--batch-dir",
                str(batch_dir),
                "--limit",
                "10",
                "--delay",
                "0",
                "--projection-proof",
            ]

            with (
                patch("config.load_settings", return_value=MagicMock()),
                patch("gmail_fetch.search_emails", side_effect=fake_search),
                patch.object(mod.subprocess, "run", side_effect=fake_run),
            ):
                rc = mod.main()

            self.assertEqual(rc, 0)
            batch_doc = json.loads((batch_dir / "projection_proof_report.batch.json").read_text(encoding="utf-8"))
            self.assertIn("projection_breakdown", batch_doc)
            inner = batch_doc["summary"]
            self.assertEqual(inner.get("status"), "completed_with_failures")
            pb = inner.get("projection_breakdown") or {}
            self.assertEqual(pb.get("projection_status"), "partial")
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
