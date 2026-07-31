"""Unit tests for Daszek bridge_queue drain helpers."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from execution_runtime import create_action_proposal
from mailbox_memory_store import InMemoryMailboxMemoryStore
from daszek_bridge_queue_drain import (  # noqa: E402
    _accumulate_bridge_reconcile_overlays,
    _reconcile_result_from_bridge_row,
    drain_bridge_rows,
    fetch_remote_pending_bridge_rows,
    filter_bridge_rows_by_domain,
    format_bridge_error,
    load_completion_ids,
    operator_payload_from_row,
    pending_adjudication_rows,
    pending_bridge_rows,
)


class FakeSignal:
    def __init__(
        self,
        signal_id: str,
        *,
        message_id: str = "",
        signal_kind: str = "gmail_message_observed",
    ) -> None:
        self.signal_id = signal_id
        self.signal_kind = signal_kind
        self.source_ref = {"message_id": message_id} if message_id else {}
        self.payload = {}


class FakeJournal:
    def __init__(self, signals: list[FakeSignal] | None = None) -> None:
        self.signals = list(signals or [])
        self.by_id = {s.signal_id: s for s in self.signals}

    def fetch_signal(self, signal_id: str) -> FakeSignal | None:
        return self.by_id.get(signal_id)

    def fetch_signals_for_source(self, source_kind: str, *, limit: int = 200) -> list[FakeSignal]:
        self.last_fetch = {"source_kind": source_kind, "limit": limit}
        return self.signals[:limit]


class BridgeQueueDrainTests(unittest.TestCase):
    def test_filter_bridge_rows_by_domain(self) -> None:
        rows = [
            {"queue_id": "a", "domain": "action_decision"},
            {"queue_id": "b", "domain": "adjudication"},
        ]
        self.assertEqual(len(filter_bridge_rows_by_domain(rows, "any")), 2)
        self.assertEqual([r["queue_id"] for r in filter_bridge_rows_by_domain(rows, "adjudication")], ["b"])

    def test_remote_pending_rows_filters_domain_with_wider_fetch(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.last_limit: int | None = None

            def get_v2_bridge_queue(self, *, limit: int, status: str) -> dict[str, object]:
                self.last_limit = limit
                _ = status
                return {
                    "items": [
                        {"queue_id": "ad1", "domain": "action_decision"},
                        {"queue_id": "adj1", "domain": "adjudication"},
                    ],
                }

            def get_v2_note_detail(self, _note_id: str) -> dict[str, object]:
                return {}

        client = FakeClient()
        rows = fetch_remote_pending_bridge_rows(client, max_items=1, domain_filter="adjudication")
        self.assertEqual(client.last_limit, 50)
        self.assertEqual(rows, [{"queue_id": "adj1", "domain": "adjudication"}])

    def test_pending_skips_completed(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        tmp = Path(name)
        try:
            rows = [
                {
                    "queue_id": "bq_1",
                    "schema_version": "daszek_bridge_queue.v1",
                    "domain": "adjudication",
                    "adjudication_kind": "reject_same_case",
                    "case_id": "c1",
                    "source_signal_id": "sig1",
                    "bridge_status": "pending",
                    "created_at": "2026-04-18T10:00:00+00:00",
                },
                {
                    "queue_id": "bq_1",
                    "schema_version": "daszek_bridge_queue.v1",
                    "bridge_status": "completed",
                    "bridge_completed_at": "2026-04-18T10:01:00+00:00",
                },
                {
                    "queue_id": "bq_2",
                    "schema_version": "daszek_bridge_queue.v1",
                    "domain": "adjudication",
                    "adjudication_kind": "reject_same_case",
                    "case_id": "c2",
                    "source_signal_id": "sig2",
                    "bridge_status": "pending",
                    "created_at": "2026-04-18T10:02:00+00:00",
                },
            ]
            tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
            self.assertEqual(load_completion_ids(tmp), {"bq_1"})
            pending = pending_adjudication_rows(tmp)
            self.assertEqual([r["queue_id"] for r in pending], ["bq_2"])
        finally:
            if tmp.exists():
                tmp.unlink()

    def test_pending_includes_retry_rows_when_due(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        tmp = Path(name)
        try:
            rows = [
                {
                    "queue_id": "bq_retry_due",
                    "schema_version": "daszek_bridge_queue.v1",
                    "domain": "agent_hitl",
                    "adjudication_kind": "hitl_action_execute",
                    "engagement_id": "eng-1",
                    "bridge_status": "pending",
                },
                {
                    "queue_id": "bq_retry_due",
                    "schema_version": "daszek_bridge_queue.v1",
                    "bridge_status": "retry",
                    "retry_count": 1,
                    "next_retry_at": "2026-07-31T00:00:00+00:00",
                },
            ]
            tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
            pending = pending_bridge_rows(tmp)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["queue_id"], "bq_retry_due")
            self.assertEqual(pending[0]["bridge_status"], "retry")
            self.assertEqual(pending[0]["retry_count"], 1)
        finally:
            if tmp.exists():
                tmp.unlink()

    def test_pending_skips_retry_rows_before_due_time(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        tmp = Path(name)
        try:
            rows = [
                {
                    "queue_id": "bq_retry_future",
                    "schema_version": "daszek_bridge_queue.v1",
                    "domain": "agent_hitl",
                    "adjudication_kind": "hitl_action_execute",
                    "engagement_id": "eng-1",
                    "bridge_status": "pending",
                },
                {
                    "queue_id": "bq_retry_future",
                    "schema_version": "daszek_bridge_queue.v1",
                    "bridge_status": "retry",
                    "retry_count": 1,
                    "next_retry_at": "2026-08-01T00:00:00+00:00",
                },
            ]
            tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
            self.assertEqual(pending_bridge_rows(tmp), [])
        finally:
            if tmp.exists():
                tmp.unlink()

    def test_operator_payload_shape(self) -> None:
        payload = operator_payload_from_row(
            {"case_id": "cx", "source_signal_id": "sx"},
        )
        self.assertEqual(payload["adjudication_kind"], "reject_same_case")
        self.assertEqual(payload["target_refs"]["signal_id"], "sx")
        self.assertEqual(payload["target_refs"]["rejected_case_id"], "cx")

    def test_remote_pending_rows_uses_client_items(self) -> None:
        class FakeClient:
            def get_v2_bridge_queue(self, *, limit: int, status: str) -> dict[str, object]:
                return {
                    "items": [
                        {"queue_id": "bq_1"},
                        "bad-row",
                        {"queue_id": "bq_2"},
                    ],
                    "limit": limit,
                    "status": status,
                }

        rows = fetch_remote_pending_bridge_rows(FakeClient(), max_items=1)
        self.assertEqual(rows, [{"queue_id": "bq_1"}])

    def test_remote_pending_rows_enriches_note_identity_ids(self) -> None:
        class FakeClient:
            def get_v2_bridge_queue(self, *, limit: int, status: str) -> dict[str, object]:
                return {
                    "items": [
                        {
                            "queue_id": "bq_1",
                            "domain": "adjudication",
                            "desk_note_id": "note_1",
                            "source_signal_id": "sig_shadow",
                        }
                    ]
                }

            def get_v2_note_detail(self, note_id: str) -> dict[str, object]:
                self.note_id = note_id
                return {
                    "ok": True,
                    "note": {
                        "source_message_id": "gmail_msg_1",
                        "source_signal_ids": ["sig_shadow"],
                        "case_id": "case_1",
                    },
                }

        rows = fetch_remote_pending_bridge_rows(FakeClient(), max_items=1)
        self.assertEqual(rows[0]["source_message_id"], "gmail_msg_1")
        self.assertEqual(rows[0]["source_signal_ids"], ["sig_shadow"])
        self.assertEqual(rows[0]["case_id"], "case_1")

    def test_remote_pending_rows_retries_note_detail_after_login_and_reads_signal_ref(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.logged_in = False
                self.login_called = False

            def get_v2_bridge_queue(self, *, limit: int, status: str) -> dict[str, object]:
                return {
                    "items": [
                        {
                            "queue_id": "bq_1",
                            "domain": "adjudication",
                            "desk_note_id": "note_1",
                            "source_signal_id": "sig_shadow",
                            "case_id": "case_1",
                        }
                    ]
                }

            def login(self) -> None:
                self.logged_in = True
                self.login_called = True

            def get_v2_note_detail(self, note_id: str) -> dict[str, object]:
                if not self.logged_in:
                    raise RuntimeError("Wymagane logowanie")
                return {
                    "ok": True,
                    "note": {
                        "note_id": note_id,
                        "source_signal_ids": ["sig_shadow"],
                    },
                    "signals": [
                        {
                            "signal_id": "sig_shadow",
                            "source_ref": {"message_id": "gmail_msg_1"},
                        }
                    ],
                }

        client = FakeClient()
        rows = fetch_remote_pending_bridge_rows(client, max_items=1)
        self.assertTrue(client.login_called)
        self.assertEqual(rows[0]["source_message_id"], "gmail_msg_1")
        self.assertEqual(rows[0]["source_signal_ids"], ["sig_shadow"])

    def test_reconcile_result_from_bridge_row_reads_stage_outputs(self) -> None:
        routes = {"schema_version": "daszek_projection_router.v1", "case_id": "case_bridge", "surfaces": {}}
        row = {
            "ok": True,
            "bridge_out": {
                "reconcile_summary": {
                    "case_id": "case_bridge",
                    "processing_state": "reconciled",
                    "projection_refresh_decision": {"should_refresh": True},
                    "stage_outputs": {
                        "operator_projection_snapshot": {
                            "daszek_routes": routes,
                        }
                    },
                }
            },
        }
        reconcile_like = _reconcile_result_from_bridge_row(row)
        self.assertEqual(reconcile_like.case_id, "case_bridge")
        self.assertIn("operator_projection_snapshot", reconcile_like.stage_outputs)
        run_state: dict[str, object] = {"projection_route_overlays": {}}
        _accumulate_bridge_reconcile_overlays(run_state, [row])
        self.assertIn("case_bridge", run_state["projection_route_overlays"])

    def test_drain_bridge_rows_completes_via_callback(self) -> None:
        completions: list[tuple[str, str, str]] = []

        def fake_bridge_operator_feedback(**kwargs: object) -> dict[str, object]:
            return {
                "ok": True,
                "routed": True,
                "truth_loop_executed": True,
                "reconcile_signal_ran": True,
                "reconcile_summary": {"processing_state": "reconciled"},
            }

        rows = [
            {
                "queue_id": "bq_1",
                "schema_version": "daszek_bridge_queue.v1",
                "domain": "adjudication",
                "adjudication_kind": "reject_same_case",
                "case_id": "case_1",
                "source_signal_id": "sig_1",
                "bridge_status": "pending",
            }
        ]
        results = drain_bridge_rows(
            pending=rows,
            append_completion=lambda queue_id, status, error="": completions.append((queue_id, status, error)),
            bridge_operator_feedback=fake_bridge_operator_feedback,
            store=object(),
            journal=FakeJournal([FakeSignal("sig_1")]),
            runtime_context=object(),
            max_items=10,
            dry_run=False,
        )
        self.assertEqual(results[0]["ok"], True)
        self.assertEqual(completions, [("bq_1", "completed", "")])

    def test_drain_bridge_rows_fails_reject_same_case_without_reconcile(self) -> None:
        completions: list[tuple[str, str, str]] = []

        def fake_bridge_operator_feedback(**kwargs: object) -> dict[str, object]:
            return {
                "ok": True,
                "routed": True,
                "truth_loop_executed": True,
                "reconcile_signal_ran": False,
                "reconcile_summary": {"processing_state": "pending"},
            }

        rows = [
            {
                "queue_id": "bq_1",
                "schema_version": "daszek_bridge_queue.v1",
                "domain": "adjudication",
                "adjudication_kind": "reject_same_case",
                "case_id": "case_1",
                "source_signal_id": "sig_1",
                "bridge_status": "pending",
            }
        ]
        results = drain_bridge_rows(
            pending=rows,
            append_completion=lambda queue_id, status, error="": completions.append((queue_id, status, error)),
            bridge_operator_feedback=fake_bridge_operator_feedback,
            store=object(),
            journal=FakeJournal([FakeSignal("sig_1")]),
            runtime_context=object(),
            max_items=10,
            dry_run=False,
        )
        self.assertEqual(results[0]["ok"], False)
        self.assertIn("reconcile_signal_ran", results[0]["error"])
        self.assertEqual(completions[0][0], "bq_1")
        self.assertEqual(completions[0][1], "failed")
        err_payload = json.loads(completions[0][2])
        self.assertEqual(err_payload.get("stage"), "process_item")
        self.assertIn("reconcile_signal_ran", err_payload.get("error_message", ""))

    def test_drain_bridge_rows_completion_failure_is_item_level_and_continues(self) -> None:
        completions: list[tuple[str, str, str]] = []

        def fake_bridge_operator_feedback(**kwargs: object) -> dict[str, object]:
            return {
                "ok": True,
                "routed": True,
                "truth_loop_executed": True,
                "reconcile_signal_ran": True,
                "reconcile_summary": {"processing_state": "reconciled"},
            }

        def flaky_completion(queue_id: str, status: str, error: str = "") -> None:
            if queue_id == "bq_1":
                raise RuntimeError("remote completion unavailable")
            completions.append((queue_id, status, error))

        rows = [
            {
                "queue_id": "bq_1",
                "schema_version": "daszek_bridge_queue.v1",
                "domain": "adjudication",
                "adjudication_kind": "reject_same_case",
                "case_id": "case_1",
                "source_signal_id": "sig_1",
                "bridge_status": "pending",
            },
            {
                "queue_id": "bq_2",
                "schema_version": "daszek_bridge_queue.v1",
                "domain": "adjudication",
                "adjudication_kind": "reject_same_case",
                "case_id": "case_2",
                "source_signal_id": "sig_2",
                "bridge_status": "pending",
            },
        ]
        results = drain_bridge_rows(
            pending=rows,
            append_completion=flaky_completion,
            bridge_operator_feedback=fake_bridge_operator_feedback,
            store=object(),
            journal=FakeJournal([FakeSignal("sig_1"), FakeSignal("sig_2")]),
            runtime_context=object(),
            max_items=10,
            dry_run=False,
        )

        self.assertEqual([r["queue_id"] for r in results], ["bq_1", "bq_2"])
        self.assertEqual(results[0]["ok"], False)
        self.assertIn("remote completion unavailable", results[0]["error"])
        err_payload = json.loads(results[0]["bridge_error"])
        self.assertEqual(err_payload.get("stage"), "complete_item")
        self.assertEqual(results[1]["ok"], True)
        self.assertEqual(completions, [("bq_2", "completed", "")])

    def test_action_decision_reject_completion_failure_replay_is_idempotent(self) -> None:
        store = InMemoryMailboxMemoryStore()
        proposal = create_action_proposal(store, {"case_id": "case_reject_bridge", "action_type": "prepare_reply_draft"})
        row = {
            "queue_id": "bq_reject_bridge",
            "schema_version": "daszek_bridge_queue.v1",
            "domain": "action_decision",
            "decision": "reject",
            "proposal_id": proposal.proposal_id,
            "actor_id": "konrad",
            "reason": "bad source",
            "bridge_status": "pending",
        }

        def fail_first_completion(queue_id: str, status: str, error: str = "") -> None:
            _ = (queue_id, status, error)
            if fail_first_completion.first:
                fail_first_completion.first = False
                raise RuntimeError("completion append failed")

        fail_first_completion.first = True  # type: ignore[attr-defined]

        first = drain_bridge_rows(
            pending=[row],
            append_completion=fail_first_completion,
            bridge_operator_feedback=object(),
            store=store,
            journal=FakeJournal(),
            runtime_context=object(),
            max_items=1,
            dry_run=False,
        )
        second = drain_bridge_rows(
            pending=[row],
            append_completion=lambda *_a, **_k: None,
            bridge_operator_feedback=object(),
            store=store,
            journal=FakeJournal(),
            runtime_context=object(),
            max_items=1,
            dry_run=False,
        )

        self.assertFalse(first[0]["ok"])
        self.assertTrue(second[0]["ok"])
        events = [row for row in store.fetch_events_for_case("case_reject_bridge", limit=20) if row.get("event_type") == "action_proposal_rejected"]
        self.assertEqual(len(events), 1)

    def test_drain_skips_empty_queue_id_without_completion(self) -> None:
        completions: list[tuple[str, str, str]] = []

        def fake_bridge_operator_feedback(**kwargs: object) -> dict[str, object]:
            raise AssertionError("should not run without queue_id")

        rows = [
            {
                "domain": "adjudication",
                "adjudication_kind": "reject_same_case",
                "case_id": "case_1",
                "source_signal_id": "sig_x",
                "bridge_status": "pending",
            }
        ]
        results = drain_bridge_rows(
            pending=rows,
            append_completion=lambda queue_id, status, error="": completions.append((queue_id, status, error)),
            bridge_operator_feedback=fake_bridge_operator_feedback,
            store=object(),
            journal=object(),
            runtime_context=object(),
            max_items=10,
            dry_run=False,
        )
        self.assertEqual(completions, [])
        self.assertEqual(results[0]["ok"], False)
        self.assertEqual(results[0]["error"], "missing_queue_id")
        self.assertIn("bridge_error", results[0])

    def test_drain_bridge_rows_resolves_shadow_signal_from_message_id(self) -> None:
        completions: list[tuple[str, str, str]] = []
        captured_payloads: list[dict[str, object]] = []

        def fake_bridge_operator_feedback(**kwargs: object) -> dict[str, object]:
            captured_payloads.append(kwargs["raw_operator_payload"])  # type: ignore[arg-type]
            return {
                "ok": True,
                "routed": True,
                "truth_loop_executed": True,
                "reconcile_signal_ran": True,
                "reconcile_summary": {"processing_state": "reconciled"},
            }

        rows = [
            {
                "queue_id": "bq_legacy",
                "schema_version": "daszek_bridge_queue.v1",
                "domain": "adjudication",
                "adjudication_kind": "reject_same_case",
                "case_id": "case_1",
                "source_signal_id": "sig_shadow",
                "source_message_id": "gmail_msg_1",
                "bridge_status": "pending",
            }
        ]
        results = drain_bridge_rows(
            pending=rows,
            append_completion=lambda queue_id, status, error="": completions.append((queue_id, status, error)),
            bridge_operator_feedback=fake_bridge_operator_feedback,
            store=object(),
            journal=FakeJournal([FakeSignal("sig_canonical", message_id="gmail_msg_1")]),
            runtime_context=object(),
            max_items=10,
            dry_run=False,
        )
        target_refs = captured_payloads[0]["target_refs"]  # type: ignore[index]
        self.assertEqual(results[0]["ok"], True)
        self.assertEqual(target_refs["signal_id"], "sig_canonical")
        self.assertEqual(target_refs["original_source_signal_id"], "sig_shadow")
        self.assertEqual(target_refs["signal_resolution"], "message_id_fallback")
        self.assertEqual(completions, [("bq_legacy", "completed", "")])

    def test_drain_bridge_rows_fails_unresolved_source_signal_explicitly(self) -> None:
        completions: list[tuple[str, str, str]] = []

        def fake_bridge_operator_feedback(**kwargs: object) -> dict[str, object]:
            raise AssertionError("should not bridge unresolved source signal")

        rows = [
            {
                "queue_id": "bq_bad",
                "schema_version": "daszek_bridge_queue.v1",
                "domain": "adjudication",
                "adjudication_kind": "reject_same_case",
                "case_id": "case_1",
                "source_signal_id": "sig_shadow",
                "source_message_id": "gmail_msg_missing",
                "bridge_status": "pending",
            }
        ]
        results = drain_bridge_rows(
            pending=rows,
            append_completion=lambda queue_id, status, error="": completions.append((queue_id, status, error)),
            bridge_operator_feedback=fake_bridge_operator_feedback,
            store=object(),
            journal=FakeJournal([FakeSignal("sig_other", message_id="gmail_msg_other")]),
            runtime_context=object(),
            max_items=10,
            dry_run=False,
        )
        self.assertEqual(results[0]["ok"], False)
        self.assertIn("source_signal_id_not_in_journal", results[0]["error"])
        self.assertEqual(completions[0][1], "failed")
        err_payload = json.loads(completions[0][2])
        self.assertEqual(err_payload.get("stage"), "process_item")
        self.assertIn("sig_shadow", err_payload.get("source_signal_ids", []))

    def test_drain_bridge_rows_marks_retryable_transport_error_for_retry(self) -> None:
        completions: list[tuple[str, str, str]] = []

        def fake_bridge_operator_feedback(**kwargs: object) -> dict[str, object]:
            raise RuntimeError("503 service unavailable")

        rows = [
            {
                "queue_id": "bq_retryable",
                "schema_version": "daszek_bridge_queue.v1",
                "domain": "adjudication",
                "adjudication_kind": "reject_same_case",
                "case_id": "case_1",
                "source_signal_id": "sig_1",
                "bridge_status": "pending",
            }
        ]
        results = drain_bridge_rows(
            pending=rows,
            append_completion=lambda queue_id, status, error="": completions.append((queue_id, status, error)),
            bridge_operator_feedback=fake_bridge_operator_feedback,
            store=object(),
            journal=FakeJournal([FakeSignal("sig_1")]),
            runtime_context=object(),
            max_items=10,
            dry_run=False,
        )
        self.assertEqual(results[0]["bridge_status"], "retry")
        self.assertTrue(results[0]["retryable"])
        self.assertEqual(completions[0][1], "retry")
        err_payload = json.loads(completions[0][2])
        self.assertEqual(err_payload.get("retry_count"), 1)
        self.assertTrue(err_payload.get("retryable"))
        self.assertIn("next_retry_at", err_payload)

    def test_drain_bridge_rows_exhausted_retryable_error_becomes_dead_letter(self) -> None:
        completions: list[tuple[str, str, str]] = []

        def fake_bridge_operator_feedback(**kwargs: object) -> dict[str, object]:
            raise RuntimeError("timeout while reaching node b")

        rows = [
            {
                "queue_id": "bq_dead_letter",
                "schema_version": "daszek_bridge_queue.v1",
                "domain": "adjudication",
                "adjudication_kind": "reject_same_case",
                "case_id": "case_1",
                "source_signal_id": "sig_1",
                "bridge_status": "retry",
                "retry_count": 3,
            }
        ]
        results = drain_bridge_rows(
            pending=rows,
            append_completion=lambda queue_id, status, error="": completions.append((queue_id, status, error)),
            bridge_operator_feedback=fake_bridge_operator_feedback,
            store=object(),
            journal=FakeJournal([FakeSignal("sig_1")]),
            runtime_context=object(),
            max_items=10,
            dry_run=False,
        )
        self.assertEqual(results[0]["bridge_status"], "dead_letter")
        self.assertEqual(completions[0][1], "dead_letter")

    def test_format_bridge_error_json_roundtrip(self) -> None:
        s = format_bridge_error(
            error_type="ValueError",
            error_message="boom",
            stage="process_item",
            queue_id="bq_9",
            source_signal_ids=["s1"],
        )
        d = json.loads(s)
        self.assertEqual(d["queue_id"], "bq_9")
        self.assertEqual(d["source_signal_ids"], ["s1"])

    def test_dry_run_remote_fetch_failure_returns_1(self) -> None:
        from argparse import Namespace

        from daszek_bridge_queue_drain import run_daszek_bridge_drain

        args = Namespace(remote=True, queue_path="", max_items=5, dry_run=True, run_id="t")
        with patch(
            "daszek_bridge_queue_drain.load_pending_bridge_rows_for_args",
            side_effect=RuntimeError("upstream unavailable"),
        ):
            rc = run_daszek_bridge_drain(args)
        self.assertEqual(rc, 1)

    def test_run_remote_fetch_failure_returns_1(self) -> None:
        from argparse import Namespace

        from daszek_bridge_queue_drain import run_daszek_bridge_drain

        settings = MagicMock()
        settings.signal_journal_jsonl_mirror_enabled = False
        settings.groq_model = None
        settings.signal_runtime_mode = "active"
        settings.daszek_base_url = "http://example.test"
        settings.daszek_login = "u"
        settings.daszek_password = "p"
        settings.http_timeout = 5
        settings.daszek_operational_feed_auto_push_enabled = False

        rt = MagicMock()
        rt.store = MagicMock()
        rt.graph_store = None
        rt.bootstrap = MagicMock()

        args = Namespace(remote=True, queue_path="", max_items=3, dry_run=False, run_id="t")

        with (
            patch("config.load_settings", return_value=settings),
            patch("mailbox_memory_runtime.build_mailbox_memory_runtime", return_value=rt),
            patch("signal_journal.SignalJournal", return_value=MagicMock()),
            patch("signal_reconciler.SignalRuntimeContext", return_value=MagicMock()),
            patch(
                "daszek_bridge_queue_drain.fetch_remote_pending_bridge_rows",
                side_effect=RuntimeError("bridge queue fetch failed"),
            ),
            patch("daszek_client.DaszekClient", return_value=MagicMock()),
        ):
            rc = run_daszek_bridge_drain(args)
        self.assertEqual(rc, 1)

    def test_run_remote_adjudication_drains_and_completes_matching_row(self) -> None:
        from argparse import Namespace

        from daszek_bridge_queue_drain import run_daszek_bridge_drain

        settings = MagicMock()
        settings.signal_journal_jsonl_mirror_enabled = False
        settings.groq_model = None
        settings.signal_runtime_mode = "active"
        settings.daszek_base_url = "http://example.test"
        settings.daszek_login = "u"
        settings.daszek_password = "p"
        settings.http_timeout = 5
        settings.daszek_operational_feed_auto_push_enabled = False

        rt = MagicMock()
        rt.store = object()
        rt.graph_store = None
        rt.bootstrap = MagicMock()

        class FakeRemoteClient:
            def __init__(self, _settings: object) -> None:
                self.completed: list[tuple[str, str, str]] = []

            def get_v2_bridge_queue(self, *, limit: int, status: str) -> dict[str, object]:
                self.fetch = {"limit": limit, "status": status}
                return {
                    "items": [
                        {"queue_id": "act_1", "domain": "action_decision"},
                        {
                            "queue_id": "bq_adj_1",
                            "schema_version": "daszek_bridge_queue.v1",
                            "domain": "adjudication",
                            "adjudication_kind": "reject_same_case",
                            "case_id": "case_1",
                            "source_signal_id": "sig_1",
                            "bridge_status": "pending",
                        },
                    ]
                }

            def complete_v2_bridge_queue_item(self, queue_id: str, *, status: str, error: str = "") -> None:
                self.completed.append((queue_id, status, error))

        client = FakeRemoteClient(settings)
        fake_journal = MagicMock()
        fake_journal.fetch_signal.return_value = object()
        args = Namespace(remote=True, queue_path="", max_items=1, dry_run=False, run_id="t", domain="adjudication")

        with (
            patch("config.load_settings", return_value=settings),
            patch("mailbox_memory_runtime.build_mailbox_memory_runtime", return_value=rt),
            patch("signal_journal.SignalJournal", return_value=fake_journal),
            patch("signal_reconciler.SignalRuntimeContext", return_value=MagicMock()),
            patch("daszek_client.DaszekClient", return_value=client),
            patch(
                "adjudication_executioner.bridge_operator_feedback",
                return_value={
                    "ok": True,
                    "truth_loop_executed": True,
                    "reconcile_signal_ran": True,
                    "reconcile_summary": {"processing_state": "reconciled"},
                },
            ),
        ):
            rc = run_daszek_bridge_drain(args)

        self.assertEqual(rc, 0)
        self.assertEqual(client.completed, [("bq_adj_1", "completed", "")])


if __name__ == "__main__":
    unittest.main()
