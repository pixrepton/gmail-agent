from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from artifact_contracts import empty_run_summary
import daszek_v3_feed_runtime
from daszek_v3_feed_runtime import _push_feed_snapshot_sync, maybe_push_operational_feed_after_reconcile


class DaszekV3FeedRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        daszek_v3_feed_runtime._feed_cache = {}
        daszek_v3_feed_runtime._feed_cache_ts = 0.0

    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            daszek_operational_feed_push_min_interval_sec=0,
            daszek_operational_feed_case_limit=5,
            daszek_operational_feed_task_limit=10,
        )

    def _reconcile(self, *, should_refresh: bool = True) -> SimpleNamespace:
        return SimpleNamespace(
            processing_state="reconciled",
            projection_refresh_decision=SimpleNamespace(should_refresh=should_refresh),
            v2_projection={},
            stage_outputs={},
        )

    def test_projection_proof_mode_uses_sync_push(self) -> None:
        client = MagicMock()
        client.post_v3_operational_feed_snapshot.return_value = {
            "ok": True,
            "snapshot_id": "operational-feed-sync",
        }
        run_dir = Path(tempfile.mkdtemp())
        log_path = run_dir / "daszek_v3_feed_push_results.jsonl"
        run_state = {
            "run_id": "run-feed-sync",
            "manifest": {"daszek_operational_feed_auto_push_enabled": True},
            "runtime_controls": {"projection_proof": True},
            "daszek_client": client,
            "mailbox_memory_runtime": SimpleNamespace(store=object()),
            "daszek_v3_feed_push_path": log_path,
            "summary": empty_run_summary(),
        }
        feed_snapshot = {
            "schema_name": "daszek_operational_feed_snapshot",
            "snapshot_id": "snap-local",
            "feed": {"desk": [], "cases": [{"case_id": "c1"}], "tasks": []},
        }
        with patch("daszek_v3_feed_runtime._use_engagement_feed_builder", return_value=False), patch(
            "daszek_v3_operational_feed.build_operational_feed_for_cel",
            return_value=feed_snapshot,
        ) as build_mock, patch("daszek_v3_feed_runtime._push_feed_snapshot_async") as async_mock:
            maybe_push_operational_feed_after_reconcile(
                run_state=run_state,
                settings=self._settings(),
                reconcile_result=self._reconcile(),
                trigger_message_id="msg-sync",
            )
        build_mock.assert_called_once()
        async_mock.assert_not_called()
        client.post_v3_operational_feed_snapshot.assert_called_once()
        self.assertTrue(log_path.is_file())

    def test_skips_when_auto_push_disabled(self) -> None:
        client = MagicMock()
        run_state = {
            "manifest": {"daszek_operational_feed_auto_push_enabled": False},
            "daszek_client": client,
            "summary": empty_run_summary(),
        }
        maybe_push_operational_feed_after_reconcile(
            run_state=run_state,
            settings=self._settings(),
            reconcile_result=self._reconcile(),
            trigger_message_id="msg-1",
        )
        client.post_v3_operational_feed_snapshot.assert_not_called()

    def test_pushes_feed_when_enabled(self) -> None:
        client = MagicMock()
        client.post_v3_operational_feed_snapshot.return_value = {
            "ok": True,
            "snapshot_id": "operational-feed-test",
        }
        store = object()
        run_dir = Path(tempfile.mkdtemp())
        log_path = run_dir / "daszek_v3_feed_push_results.jsonl"
        run_state = {
            "run_id": "run-feed-1",
            "manifest": {"daszek_operational_feed_auto_push_enabled": True},
            "daszek_client": client,
            "mailbox_memory_runtime": SimpleNamespace(store=store),
            "daszek_v3_feed_push_path": log_path,
            "summary": empty_run_summary(),
        }
        feed_snapshot = {
            "schema_name": "daszek_operational_feed_snapshot",
            "snapshot_id": "snap-local",
            "feed": {"desk": [], "cases": [{"case_id": "c1"}], "tasks": []},
        }
        def _sync_push(*, run_state, settings, snapshot, trigger_message_id):
            _push_feed_snapshot_sync(
                run_state=run_state,
                settings=settings,
                snapshot=snapshot,
                trigger_message_id=trigger_message_id,
            )

        with patch("daszek_v3_feed_runtime._use_engagement_feed_builder", return_value=False), patch(
            "daszek_v3_operational_feed.build_operational_feed_for_cel",
            return_value=feed_snapshot,
        ) as build_mock, patch(
            "daszek_v3_feed_runtime._push_feed_snapshot_async",
            side_effect=_sync_push,
        ):
            maybe_push_operational_feed_after_reconcile(
                run_state=run_state,
                settings=self._settings(),
                reconcile_result=self._reconcile(),
                trigger_message_id="msg-feed-1",
            )
        build_mock.assert_called_once()
        client.post_v3_operational_feed_snapshot.assert_called_once()
        posted = client.post_v3_operational_feed_snapshot.call_args[0][0]
        self.assertEqual(posted["source"]["trigger_message_id"], "msg-feed-1")
        self.assertEqual(run_state["summary"]["operational_feed_push_count"], 1)
        self.assertEqual(run_state["summary"]["last_operational_feed_snapshot_id"], "operational-feed-test")

    def test_debounces_within_min_interval(self) -> None:
        client = MagicMock()
        settings = SimpleNamespace(
            daszek_operational_feed_push_min_interval_sec=3600,
            daszek_operational_feed_case_limit=5,
            daszek_operational_feed_task_limit=10,
        )
        run_state = {
            "manifest": {"daszek_operational_feed_auto_push_enabled": True},
            "daszek_client": client,
            "mailbox_memory_runtime": SimpleNamespace(store=object()),
            "summary": {
                **empty_run_summary(),
                "last_operational_feed_push_monotonic": time.monotonic(),
            },
        }
        with patch("daszek_v3_operational_feed.build_operational_feed_for_cel") as build_mock:
            maybe_push_operational_feed_after_reconcile(
                run_state=run_state,
                settings=settings,
                reconcile_result=self._reconcile(),
                trigger_message_id="msg-2",
            )
        build_mock.assert_not_called()
        client.post_v3_operational_feed_snapshot.assert_not_called()
        self.assertEqual(run_state["summary"]["operational_feed_push_debounced"], 1)

    def test_skips_duplicate_reconcile(self) -> None:
        client = MagicMock()
        run_state = {
            "manifest": {"daszek_operational_feed_auto_push_enabled": True},
            "daszek_client": client,
            "summary": empty_run_summary(),
        }
        reconcile = SimpleNamespace(
            processing_state="skipped_duplicate",
            projection_refresh_decision=SimpleNamespace(should_refresh=True),
        )
        maybe_push_operational_feed_after_reconcile(
            run_state=run_state,
            settings=self._settings(),
            reconcile_result=reconcile,
            trigger_message_id="dup",
        )
        client.post_v3_operational_feed_snapshot.assert_not_called()

    def test_engagement_feed_pushes_when_staging_agent_without_case_id(self) -> None:
        client = MagicMock()
        client.post_v3_operational_feed_snapshot.return_value = {
            "ok": True,
            "snapshot_id": "operational-feed-staging",
        }
        run_dir = Path(tempfile.mkdtemp())
        log_path = run_dir / "daszek_v3_feed_push_results.jsonl"
        run_state = {
            "run_id": "run-feed-staging",
            "manifest": {"daszek_operational_feed_auto_push_enabled": True},
            "daszek_client": client,
            "mailbox_memory_runtime": SimpleNamespace(store=object()),
            "daszek_v3_feed_push_path": log_path,
            "summary": empty_run_summary(),
        }
        reconcile = SimpleNamespace(
            processing_state="reconciled",
            case_id="",
            projection_refresh_decision=SimpleNamespace(should_refresh=False),
            mailbox_memory_result={"engagement_id": "stg_sig_test", "agent_runtime": True},
            stage_outputs={},
        )
        feed_snapshot = {
            "schema_name": "daszek_operational_feed_snapshot",
            "snapshot_id": "snap-staging",
            "feed": {"desk": [], "cases": [], "tasks": []},
        }

        def _sync_push(*, run_state, settings, snapshot, trigger_message_id):
            _push_feed_snapshot_sync(
                run_state=run_state,
                settings=settings,
                snapshot=snapshot,
                trigger_message_id=trigger_message_id,
            )

        with patch("daszek_v3_feed_runtime._use_engagement_feed_builder", return_value=True), patch(
            "daszek_engagement_feed.build_engagement_feed_for_cel",
            return_value=feed_snapshot,
        ) as build_mock, patch(
            "daszek_v3_feed_runtime._push_feed_snapshot_async",
            side_effect=_sync_push,
        ):
            maybe_push_operational_feed_after_reconcile(
                run_state=run_state,
                settings=self._settings(),
                reconcile_result=reconcile,
                trigger_message_id="msg-staging",
            )
        build_mock.assert_called_once()
        client.post_v3_operational_feed_snapshot.assert_called_once()
        self.assertEqual(run_state["summary"]["operational_feed_push_count"], 1)

    def test_projection_proof_mode_logs_snapshot_payload_for_exact_membership(self) -> None:
        client = MagicMock()
        client.post_v3_operational_feed_snapshot.return_value = {
            "ok": True,
            "snapshot_id": "operational-feed-proof",
        }
        run_dir = Path(tempfile.mkdtemp())
        log_path = run_dir / "daszek_v3_feed_push_results.jsonl"
        run_state = {
            "run_id": "run-feed-proof",
            "manifest": {"daszek_operational_feed_auto_push_enabled": True},
            "runtime_controls": {"projection_proof": True},
            "daszek_client": client,
            "mailbox_memory_runtime": SimpleNamespace(store=object()),
            "daszek_v3_feed_push_path": log_path,
            "summary": empty_run_summary(),
        }
        snapshot = {
            "schema_name": "daszek_operational_feed_snapshot",
            "snapshot_id": "snap-proof",
            "feed": {
                "desk": [
                    {
                        "note_id": "desk-eng-proof",
                        "engagement_id": "eng-proof",
                        "case_id": "",
                        "title": "Proof title",
                        "source_message_id": "msg-proof",
                        "source_signal_ids": ["sig-proof"],
                    }
                ],
                "cases": [],
                "tasks": [],
            },
        }

        _push_feed_snapshot_sync(
            run_state=run_state,
            settings=self._settings(),
            snapshot=snapshot,
            trigger_message_id="msg-proof",
        )

        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(rows[-1]["record_type"], "feed_success")
        self.assertEqual(rows[-1]["snapshot_payload"]["snapshot_id"], "snap-proof")

    def test_engagement_feed_runtime_hint_backfills_source_message_id_for_staging_card(self) -> None:
        client = MagicMock()
        client.post_v3_operational_feed_snapshot.return_value = {
            "ok": True,
            "snapshot_id": "operational-feed-hint",
        }
        run_state = {
            "run_id": "run-feed-hint",
            "manifest": {"daszek_operational_feed_auto_push_enabled": True},
            "daszek_client": client,
            "mailbox_memory_runtime": SimpleNamespace(store=object()),
            "summary": empty_run_summary(),
        }
        reconcile = SimpleNamespace(
            processing_state="reconciled",
            case_id="",
            mailbox_memory_result={"engagement_id": "stg_sig_hint"},
            stage_outputs={
                "signal_projection": {
                    "signal_id": "sig-hint",
                    "source_ref": {"message_id": "msg-hint", "thread_id": "thr-hint"},
                }
            },
            projection_refresh_decision=SimpleNamespace(should_refresh=True),
        )
        feed_snapshot = {
            "schema_name": "daszek_operational_feed_snapshot",
            "snapshot_id": "snap-hint",
            "feed": {
                "desk": [
                    {
                        "note_id": "desk-stg_sig_hint",
                        "engagement_id": "stg_sig_hint",
                        "case_id": "",
                        "title": "Hint title",
                        "source_signal_ids": ["sig-hint"],
                        "source_message_id": "",
                        "thread_id": "",
                    }
                ],
                "cases": [],
                "tasks": [],
                "feed_meta": {"agent_runtime": True},
            },
            "source": {},
        }

        def _sync_push(*, run_state, settings, snapshot, trigger_message_id):
            _push_feed_snapshot_sync(
                run_state=run_state,
                settings=settings,
                snapshot=snapshot,
                trigger_message_id=trigger_message_id,
            )

        with patch("daszek_v3_feed_runtime._use_engagement_feed_builder", return_value=True), patch(
            "daszek_engagement_feed.build_engagement_feed_for_cel",
            return_value=feed_snapshot,
        ), patch(
            "daszek_v3_feed_runtime._push_feed_snapshot_async",
            side_effect=_sync_push,
        ):
            maybe_push_operational_feed_after_reconcile(
                run_state=run_state,
                settings=self._settings(),
                reconcile_result=reconcile,
                trigger_message_id="msg-hint",
            )

        posted = client.post_v3_operational_feed_snapshot.call_args[0][0]
        desk = posted["feed"]["desk"][0]
        self.assertEqual(desk["source_message_id"], "msg-hint")
        self.assertEqual(desk["thread_id"], "thr-hint")

    def test_engagement_feed_runtime_hints_do_not_swap_two_staging_cards(self) -> None:
        client = MagicMock()
        client.post_v3_operational_feed_snapshot.return_value = {
            "ok": True,
            "snapshot_id": "operational-feed-hints-two",
        }
        run_state = {
            "run_id": "run-feed-hints-two",
            "manifest": {"daszek_operational_feed_auto_push_enabled": True},
            "daszek_client": client,
            "mailbox_memory_runtime": SimpleNamespace(store=object()),
            "summary": empty_run_summary(),
        }
        feed_snapshot = {
            "schema_name": "daszek_operational_feed_snapshot",
            "snapshot_id": "snap-hints-two",
            "feed": {
                "desk": [
                    {
                        "note_id": "desk-stg_sig_a",
                        "engagement_id": "stg_sig_a",
                        "case_id": "",
                        "title": "Card A",
                        "source_signal_ids": ["sig-a"],
                        "source_message_id": "",
                        "thread_id": "",
                    },
                    {
                        "note_id": "desk-stg_sig_b",
                        "engagement_id": "stg_sig_b",
                        "case_id": "",
                        "title": "Card B",
                        "source_signal_ids": ["sig-b"],
                        "source_message_id": "",
                        "thread_id": "",
                    },
                ],
                "cases": [],
                "tasks": [],
                "feed_meta": {"agent_runtime": True},
            },
            "source": {},
        }

        def _sync_push(*, run_state, settings, snapshot, trigger_message_id):
            _push_feed_snapshot_sync(
                run_state=run_state,
                settings=settings,
                snapshot=snapshot,
                trigger_message_id=trigger_message_id,
            )

        reconcile_a = SimpleNamespace(
            processing_state="reconciled",
            case_id="",
            mailbox_memory_result={"engagement_id": "stg_sig_a"},
            stage_outputs={
                "canonical_signal_id": "sig-a",
                "signal_projection": {"source_ref": {"message_id": "msg-a", "thread_id": "thr-a"}},
            },
            projection_refresh_decision=SimpleNamespace(should_refresh=True),
        )
        reconcile_b = SimpleNamespace(
            processing_state="reconciled",
            case_id="",
            mailbox_memory_result={"engagement_id": "stg_sig_b"},
            stage_outputs={
                "canonical_signal_id": "sig-b",
                "signal_projection": {"source_ref": {"message_id": "msg-b", "thread_id": "thr-b"}},
            },
            projection_refresh_decision=SimpleNamespace(should_refresh=True),
        )

        with patch("daszek_v3_feed_runtime._use_engagement_feed_builder", return_value=True), patch(
            "daszek_engagement_feed.build_engagement_feed_for_cel",
            return_value=feed_snapshot,
        ), patch(
            "daszek_v3_feed_runtime._push_feed_snapshot_async",
            side_effect=_sync_push,
        ):
            daszek_v3_feed_runtime.accumulate_engagement_feed_runtime_hint(
                run_state,
                reconcile_a,
                trigger_message_id="msg-a",
            )
            daszek_v3_feed_runtime.accumulate_engagement_feed_runtime_hint(
                run_state,
                reconcile_b,
                trigger_message_id="msg-b",
            )
            maybe_push_operational_feed_after_reconcile(
                run_state=run_state,
                settings=self._settings(),
                reconcile_result=reconcile_b,
                trigger_message_id="msg-b",
            )

        posted = client.post_v3_operational_feed_snapshot.call_args[0][0]
        desk_by_engagement = {row["engagement_id"]: row for row in posted["feed"]["desk"]}
        self.assertEqual(desk_by_engagement["stg_sig_a"]["source_message_id"], "msg-a")
        self.assertEqual(desk_by_engagement["stg_sig_a"]["thread_id"], "thr-a")
        self.assertEqual(desk_by_engagement["stg_sig_b"]["source_message_id"], "msg-b")
        self.assertEqual(desk_by_engagement["stg_sig_b"]["thread_id"], "thr-b")
        self.assertEqual(run_state.get("engagement_feed_runtime_hints"), [])


if __name__ == "__main__":
    unittest.main()
