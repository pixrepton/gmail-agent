"""Unit tests for operator projection gates (planner + v2 runtime skip JSONL)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from action_planner import is_safe_for_operator_projection
from daszek_client import DaszekClientError, DaszekV2PushResult
from v2_runtime import push_v2_projection_to_daszek


class IsSafeForOperatorProjectionTests(unittest.TestCase):
    def test_false_when_primary_ignore(self) -> None:
        self.assertFalse(
            is_safe_for_operator_projection(
                {"decision": {"action": "reply"}},
                "ignore",
                "task",
            )
        )

    def test_false_when_intake_decision_ignore(self) -> None:
        self.assertFalse(
            is_safe_for_operator_projection(
                {"decision": {"action": "ignore"}},
                "hold",
                "task",
            )
        )

    def test_false_when_projection_mode_ignore(self) -> None:
        self.assertFalse(
            is_safe_for_operator_projection(
                {"decision": {"action": "reply"}},
                "prepare_reply",
                "ignore",
            )
        )

    def test_true_for_prepare_reply_task_mode(self) -> None:
        self.assertTrue(
            is_safe_for_operator_projection(
                {"decision": {"action": "create_case"}},
                "prepare_reply",
                "task",
            )
        )


class V2RuntimeProjectionSkipTests(unittest.TestCase):
    def setUp(self) -> None:
        self._feed_patch = mock.patch("daszek_engagement_feed.engagement_feed_source_enabled", return_value=False)
        self._feed_patch.start()

    def tearDown(self) -> None:
        self._feed_patch.stop()

    def test_appends_projection_skip_jsonl_when_v2_disabled(self) -> None:
        captured: list[tuple[Path, dict]] = []

        def fake_append(path: Path, row: dict) -> None:
            captured.append((path, dict(row)))

        run_state = {
            "daszek_client": object(),
            "manifest": {"daszek_v2_push_enabled": False},
            "daszek_v2_push_path": Path("/tmp/does-not-write-real-path.jsonl"),
            "run_id": "run_test",
            "summary": {
                "items_v2_push_skipped": 0,
                "items_v2_push_blocked_by_policy": 0,
                "items_v2_push_failed": 0,
                "items_v2_pushed": 0,
            },
        }

        with mock.patch("v2_runtime.append_jsonl", side_effect=fake_append):
            push_v2_projection_to_daszek(
                run_state=run_state,
                message_id="mid-1",
                v2_projection={"placeholder": True},
            )

        self.assertEqual(run_state["summary"]["items_v2_push_skipped"], 1)
        self.assertEqual(len(captured), 1)
        path, row = captured[0]
        self.assertEqual(path, run_state["daszek_v2_push_path"])
        self.assertEqual(row.get("record_type"), "projection_skip")
        self.assertEqual(row.get("reason"), "skipped_v2_config_disabled")
        self.assertEqual(row.get("push_policy_reason"), "skipped_v2_disabled")
        self.assertEqual(row.get("message_id"), "mid-1")
        self.assertIs(row.get("daszek_v2_push_enabled"), False)

    def test_appends_projection_skip_jsonl_when_projection_empty(self) -> None:
        captured: list[tuple[Path, dict]] = []

        def fake_append(path: Path, row: dict) -> None:
            captured.append((path, dict(row)))

        run_state = {
            "daszek_client": object(),
            "manifest": {"daszek_v2_push_enabled": True},
            "daszek_v2_push_path": Path("/tmp/v2-skip-empty.jsonl"),
            "run_id": "run_test",
            "summary": {
                "items_v2_push_skipped": 0,
                "items_v2_push_blocked_by_policy": 0,
                "items_v2_push_failed": 0,
                "items_v2_pushed": 0,
            },
        }

        with mock.patch("v2_runtime.append_jsonl", side_effect=fake_append):
            push_v2_projection_to_daszek(run_state=run_state, message_id="mid-empty", v2_projection=None)

        self.assertEqual(run_state["summary"]["items_v2_push_skipped"], 1)
        self.assertEqual(len(captured), 1)
        _path, row = captured[0]
        self.assertEqual(row.get("record_type"), "projection_skip")
        self.assertEqual(row.get("push_policy_reason"), "skipped_missing_v2_projection")


class V2RuntimeReadbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._feed_patch = mock.patch("daszek_engagement_feed.engagement_feed_source_enabled", return_value=False)
        self._feed_patch.start()

    def tearDown(self) -> None:
        self._feed_patch.stop()

    def _valid_projection(self, signal_id: str = "sig_rb") -> dict:
        return {
            "signal_projection": {"signal_id": signal_id},
            "case_patch": {"command": "noop", "case_id": "case_rb"},
            "desk_note_patch": {
                "command": "create",
                "presence_mode": "standard",
                "lifecycle": "active",
                "source_signal_ids": [signal_id],
                "desk_note_id": "note_rb",
            },
            "decision_trace": {"trigger_signal_id": signal_id, "presence_mode": "standard"},
        }

    def test_readback_row_after_successful_push(self) -> None:
        captured: list[dict] = []

        def fake_append(_path, row):
            captured.append(dict(row))

        class FakeClient:
            def push_v2_projection(self, payload):
                _ = payload
                return DaszekV2PushResult(
                    status="ingested",
                    message_id="mid-rb",
                    signal_id="sig_rb",
                    trace_id="tr_rb",
                    details={"ok": True},
                )

            def readback_v2_projection(self, *, payload, ingest_details):
                _ = payload, ingest_details
                return {
                    "store_readback": "found",
                    "readback_reason": "",
                    "ui_visibility_expected": True,
                }

        run_state = {
            "daszek_client": FakeClient(),
            "manifest": {"daszek_v2_push_enabled": True, "daszek_v2_readback_enabled": True},
            "daszek_v2_push_path": Path("/tmp/does-not-write-real-path.jsonl"),
            "run_id": "run_rb",
            "summary": {
                "items_v2_push_skipped": 0,
                "items_v2_push_blocked_by_policy": 0,
                "items_v2_push_failed": 0,
                "items_v2_pushed": 0,
            },
        }
        with mock.patch("v2_runtime.append_jsonl", side_effect=fake_append):
            push_v2_projection_to_daszek(
                run_state=run_state,
                message_id="mid-rb",
                v2_projection=self._valid_projection(),
                action_plan_result={"primary_action": "hold", "safe_for_operator_projection": True},
                intake_result_final={"decision": {"action": "hold"}},
            )

        types = [r.get("record_type") for r in captured if r.get("record_type")]
        self.assertIn("push_policy", types)
        self.assertIn("v2_readback", types)
        rb = [r for r in captured if r.get("record_type") == "v2_readback"][0]
        self.assertEqual(rb.get("store_readback"), "found")
        self.assertEqual(run_state["summary"]["items_v2_pushed"], 1)


class V2RuntimeFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._feed_patch = mock.patch("daszek_engagement_feed.engagement_feed_source_enabled", return_value=False)
        self._feed_patch.start()

    def tearDown(self) -> None:
        self._feed_patch.stop()

    def _valid_projection(self, signal_id: str = "sig_x") -> dict:
        return {
            "signal_projection": {"signal_id": signal_id},
            "case_patch": {"command": "noop", "case_id": "case_x"},
            "desk_note_patch": {
                "command": "create",
                "presence_mode": "standard",
                "lifecycle": "active",
                "source_signal_ids": [signal_id],
                "desk_note_id": "note_x",
            },
            "decision_trace": {"trigger_signal_id": signal_id, "presence_mode": "standard"},
        }

    def test_projection_failure_jsonl_on_ingest_error(self) -> None:
        captured: list[dict] = []

        def fake_append(_path, row):
            captured.append(dict(row))

        class BadClient:
            def push_v2_projection(self, payload):
                _ = payload
                raise DaszekClientError("v2 ingest down")

        run_state = {
            "daszek_client": BadClient(),
            "manifest": {"daszek_v2_push_enabled": True, "daszek_v2_readback_enabled": False},
            "daszek_v2_push_path": Path("/tmp/does-not-write-real-path.jsonl"),
            "run_id": "run_fail",
            "summary": {
                "items_v2_push_skipped": 0,
                "items_v2_push_blocked_by_policy": 0,
                "items_v2_push_failed": 0,
                "items_v2_pushed": 0,
            },
            "_record_error": lambda *args, **kwargs: None,
        }
        with mock.patch("v2_runtime.append_jsonl", side_effect=fake_append):
            push_v2_projection_to_daszek(
                run_state=run_state,
                message_id="mid-fail",
                v2_projection=self._valid_projection(),
                action_plan_result={"primary_action": "hold", "safe_for_operator_projection": True},
                intake_result_final={"decision": {"action": "hold"}},
            )

        fail_rows = [r for r in captured if r.get("record_type") == "projection_failure"]
        self.assertEqual(len(fail_rows), 1)
        self.assertIn("ingest down", str(fail_rows[0].get("error") or ""))
        self.assertEqual(run_state["summary"]["items_v2_push_failed"], 1)


if __name__ == "__main__":
    unittest.main()
