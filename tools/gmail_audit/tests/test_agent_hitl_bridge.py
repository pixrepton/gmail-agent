from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_hitl_bridge import (
    agent_hitl_payload_from_row,
    approve_hitl_engagement,
    execute_hitl_send_from_bridge_row,
)
from agent_runtime.mcp_service import AgentMcpService
from agent_runtime.settings import load_agent_runtime_settings
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from daszek_bridge_queue_drain import drain_bridge_rows
from llm_contracts.engagement_snapshot_v2 import ActionItem
from mailbox_memory_store import InMemoryMailboxMemoryStore


def _hitl_snapshot(*, enabled: bool = True, gate: bool = True):
    snap = build_initial_snapshot(case_id="case_hitl", engagement_id="eng_hitl", trace_id="t1")
    delta: dict = {
        "hitl_gate": {"required": gate, "reason": "draft_ready_for_approval" if gate else ""},
    }
    if enabled:
        delta["actions"] = [
            ActionItem(id="draft_reply", enabled=True, payload_pl="Draft test").model_dump(mode="python")
        ]
    return apply_snapshot_delta(snap, delta)


def test_agent_hitl_payload_from_row() -> None:
    row = {
        "engagement_id": "eng-1",
        "case_id": "case-1",
        "action_id": "draft_reply",
        "operator_id": "konrad",
    }
    payload = agent_hitl_payload_from_row(row)
    assert payload["domain"] == "agent_hitl"
    assert payload["adjudication_kind"] == "hitl_action_execute"
    assert payload["engagement_id"] == "eng-1"


def _seed_hitl_store(store: InMemoryOperatorEngagementStore, *, gate: bool = True) -> None:
    store.insert_snapshot(_hitl_snapshot(gate=gate))


def _mailbox_runtime(store: InMemoryMailboxMemoryStore) -> SimpleNamespace:
    return SimpleNamespace(store=store, bootstrap=lambda: None)


def _seed_hitl_case(store: InMemoryMailboxMemoryStore) -> None:
    store.upsert_case(
        {
            "case_id": "case_hitl",
            "case_family": "mail_case",
            "status": "open",
            "metadata": {},
        }
    )


def test_approve_hitl_engagement_clears_gate() -> None:
    store = InMemoryOperatorEngagementStore()
    _seed_hitl_store(store)
    settings = load_agent_runtime_settings()
    service = AgentMcpService(store=store, settings=settings)

    with patch("agent_hitl_bridge.AgentMcpService.from_env", return_value=service):
        with patch("agent_hitl_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"skipped": True}):
            with patch("agent_hitl_bridge.publish_os_event", return_value="osevt_test") as publish_mock:
                out = approve_hitl_engagement(
                    engagement_id="eng_hitl",
                    action_id="draft_reply",
                    operator_id="konrad",
                    settings=__import__("types").SimpleNamespace(
                        daszek_operational_feed_auto_push_enabled=False,
                        mailbox_memory_database_url="postgresql://test",
                    ),
                )
    assert out["ok"] is True
    publish_mock.assert_called_once()
    assert publish_mock.call_args.kwargs.get("event_type") == "gmail.hitl.approved"
    final = store.load_snapshot("eng_hitl")
    assert final is not None
    assert final.hitl_gate.required is False


def test_execute_hitl_send_requires_approve_first() -> None:
    store = InMemoryOperatorEngagementStore()
    _seed_hitl_store(store, gate=True)
    service = AgentMcpService(store=store, settings=load_agent_runtime_settings())

    with patch("agent_hitl_bridge.AgentMcpService.from_env", return_value=service):
        with pytest.raises(ValueError, match="hitl_gate is still active"):
            execute_hitl_send_from_bridge_row(
                row={
                    "queue_id": "bq_test",
                    "engagement_id": "eng_hitl",
                    "action_id": "draft_reply",
                    "operator_id": "konrad",
                },
                settings=__import__("types").SimpleNamespace(
                    daszek_operational_feed_auto_push_enabled=False,
                    mailbox_memory_database_url="",
                ),
            )


def test_execute_hitl_send_after_approve() -> None:
    store = InMemoryOperatorEngagementStore()
    mailbox_store = InMemoryMailboxMemoryStore()
    _seed_hitl_store(store, gate=False)
    _seed_hitl_case(mailbox_store)
    service = AgentMcpService(store=store, settings=load_agent_runtime_settings())

    with patch("agent_hitl_bridge.AgentMcpService.from_env", return_value=service):
        with patch("agent_hitl_bridge.build_mailbox_memory_runtime", return_value=_mailbox_runtime(mailbox_store)):
            with patch("agent_hitl_bridge.publish_os_event", return_value=None):
                with patch("agent_hitl_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"skipped": True}):
                    with patch(
                        "agent_hitl_bridge.execute_hitl_gmail_send",
                        side_effect=lambda **kwargs: (
                            kwargs["on_effect_start"](),
                            {"executed": True, "mode": "bounded_dry_run"},
                        )[1],
                    ):
                        out = execute_hitl_send_from_bridge_row(
                            row={
                                "queue_id": "bq_test",
                                "engagement_id": "eng_hitl",
                                "action_id": "draft_reply",
                                "operator_id": "konrad",
                                "case_id": "case_hitl",
                            },
                            settings=__import__("types").SimpleNamespace(
                                daszek_operational_feed_auto_push_enabled=False,
                                mailbox_memory_database_url="",
                            ),
                        )
    assert out["ok"] is True
    assert out["execution"]["executed"] is True
    assert out["execution"]["mode"] == "bounded_dry_run"


def test_execute_hitl_send_replay_after_completion_failure_does_not_rerun_executor() -> None:
    operator_store = InMemoryOperatorEngagementStore()
    mailbox_store = InMemoryMailboxMemoryStore()
    _seed_hitl_store(operator_store, gate=False)
    _seed_hitl_case(mailbox_store)
    service = AgentMcpService(store=operator_store, settings=load_agent_runtime_settings())
    row = {
        "queue_id": "bq_hitl_send_1",
        "schema_version": "daszek_bridge_queue.v1",
        "domain": "agent_hitl",
        "adjudication_kind": "hitl_action_execute",
        "bridge_status": "pending",
        "engagement_id": "eng_hitl",
        "action_id": "draft_reply",
        "operator_id": "konrad",
        "case_id": "case_hitl",
    }
    calls: list[str] = []

    def fake_execute(**kwargs: object) -> dict[str, object]:
        kwargs["on_effect_start"]()
        calls.append("exec")
        return {"executed": True, "mode": "bounded_dry_run"}

    def fail_first_completion(queue_id: str, status: str, error: str = "") -> None:
        if fail_first_completion.first:
            fail_first_completion.first = False
            raise RuntimeError("completion append failed")

    fail_first_completion.first = True  # type: ignore[attr-defined]

    with patch("agent_hitl_bridge.AgentMcpService.from_env", return_value=service):
        with patch("agent_hitl_bridge.build_mailbox_memory_runtime", return_value=_mailbox_runtime(mailbox_store), create=True):
            with patch("agent_hitl_bridge.publish_os_event", return_value=None):
                with patch("agent_hitl_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"ok": True, "snapshot_id": "snap-1"}):
                    with patch("agent_hitl_bridge.execute_hitl_gmail_send", side_effect=fake_execute):
                        first = drain_bridge_rows(
                            pending=[row],
                            append_completion=fail_first_completion,
                            bridge_operator_feedback=object(),
                            store=mailbox_store,
                            journal=object(),
                            runtime_context=SimpleNamespace(settings=load_agent_runtime_settings()),
                            max_items=1,
                            dry_run=False,
                        )
                        second = drain_bridge_rows(
                            pending=[row],
                            append_completion=lambda *_a, **_k: None,
                            bridge_operator_feedback=object(),
                            store=mailbox_store,
                            journal=object(),
                            runtime_context=SimpleNamespace(settings=load_agent_runtime_settings()),
                            max_items=1,
                            dry_run=False,
                        )

    assert first[0]["ok"] is False
    assert second[0]["ok"] is True
    assert calls == ["exec"]
    events = [row for row in mailbox_store.fetch_events_for_case("case_hitl", limit=20) if row.get("event_type") == "agent_hitl_send_executed"]
    assert len(events) == 1


def test_execute_hitl_send_failure_before_effect_allows_retry() -> None:
    operator_store = InMemoryOperatorEngagementStore()
    mailbox_store = InMemoryMailboxMemoryStore()
    _seed_hitl_store(operator_store, gate=False)
    _seed_hitl_case(mailbox_store)
    service = AgentMcpService(store=operator_store, settings=load_agent_runtime_settings())
    row = {
        "queue_id": "bq_hitl_send_retryable",
        "schema_version": "daszek_bridge_queue.v1",
        "domain": "agent_hitl",
        "adjudication_kind": "hitl_action_execute",
        "bridge_status": "pending",
        "engagement_id": "eng_hitl",
        "action_id": "draft_reply",
        "operator_id": "konrad",
        "case_id": "case_hitl",
    }
    calls: list[str] = []

    def fake_execute(**kwargs: object) -> dict[str, object]:
        calls.append("exec")
        if len(calls) == 1:
            return {"executed": False, "effect_started": False, "decision_status": "failed_before_execution", "reason": "draft_body_empty"}
        kwargs["on_effect_start"]()
        return {"executed": True, "mode": "bounded_dry_run", "decision_status": "executed"}

    with patch("agent_hitl_bridge.AgentMcpService.from_env", return_value=service):
        with patch("agent_hitl_bridge.build_mailbox_memory_runtime", return_value=_mailbox_runtime(mailbox_store), create=True):
            with patch("agent_hitl_bridge.publish_os_event", return_value=None):
                with patch("agent_hitl_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"ok": True, "snapshot_id": "snap-1"}):
                    with patch("agent_hitl_bridge.execute_hitl_gmail_send", side_effect=fake_execute):
                        first = drain_bridge_rows(
                            pending=[row],
                            append_completion=lambda *_a, **_k: None,
                            bridge_operator_feedback=object(),
                            store=mailbox_store,
                            journal=object(),
                            runtime_context=SimpleNamespace(settings=load_agent_runtime_settings()),
                            max_items=1,
                            dry_run=False,
                        )
                        second = drain_bridge_rows(
                            pending=[row],
                            append_completion=lambda *_a, **_k: None,
                            bridge_operator_feedback=object(),
                            store=mailbox_store,
                            journal=object(),
                            runtime_context=SimpleNamespace(settings=load_agent_runtime_settings()),
                            max_items=1,
                            dry_run=False,
                        )

    assert first[0]["ok"] is False
    assert second[0]["ok"] is True
    assert calls == ["exec", "exec"]


def test_execute_hitl_send_outcome_unknown_is_not_rerun_on_replay() -> None:
    operator_store = InMemoryOperatorEngagementStore()
    mailbox_store = InMemoryMailboxMemoryStore()
    _seed_hitl_store(operator_store, gate=False)
    _seed_hitl_case(mailbox_store)
    service = AgentMcpService(store=operator_store, settings=load_agent_runtime_settings())
    row = {
        "queue_id": "bq_hitl_send_unknown",
        "schema_version": "daszek_bridge_queue.v1",
        "domain": "agent_hitl",
        "adjudication_kind": "hitl_action_execute",
        "bridge_status": "pending",
        "engagement_id": "eng_hitl",
        "action_id": "draft_reply",
        "operator_id": "konrad",
        "case_id": "case_hitl",
    }
    calls: list[str] = []

    def fake_execute(**kwargs: object) -> dict[str, object]:
        kwargs["on_effect_start"]()
        calls.append("exec")
        return {
            "executed": False,
            "effect_started": True,
            "decision_status": "outcome_unknown",
            "error": "smtp timeout",
        }

    with patch("agent_hitl_bridge.AgentMcpService.from_env", return_value=service):
        with patch("agent_hitl_bridge.build_mailbox_memory_runtime", return_value=_mailbox_runtime(mailbox_store), create=True):
            with patch("agent_hitl_bridge.publish_os_event", return_value=None):
                with patch("agent_hitl_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"ok": False, "error": "push failed"}):
                    with patch("agent_hitl_bridge.execute_hitl_gmail_send", side_effect=fake_execute):
                        first = drain_bridge_rows(
                            pending=[row],
                            append_completion=lambda *_a, **_k: None,
                            bridge_operator_feedback=object(),
                            store=mailbox_store,
                            journal=object(),
                            runtime_context=SimpleNamespace(settings=load_agent_runtime_settings()),
                            max_items=1,
                            dry_run=False,
                        )
                        second = drain_bridge_rows(
                            pending=[row],
                            append_completion=lambda *_a, **_k: None,
                            bridge_operator_feedback=object(),
                            store=mailbox_store,
                            journal=object(),
                            runtime_context=SimpleNamespace(settings=load_agent_runtime_settings()),
                            max_items=1,
                            dry_run=False,
                        )

    assert first[0]["ok"] is False
    assert second[0]["ok"] is True
    assert calls == ["exec"]


def test_execute_hitl_send_parallel_drains_run_executor_once() -> None:
    operator_store = InMemoryOperatorEngagementStore()
    mailbox_store = InMemoryMailboxMemoryStore()
    _seed_hitl_store(operator_store, gate=False)
    _seed_hitl_case(mailbox_store)
    service = AgentMcpService(store=operator_store, settings=load_agent_runtime_settings())
    row = {
        "queue_id": "bq_hitl_send_parallel",
        "schema_version": "daszek_bridge_queue.v1",
        "domain": "agent_hitl",
        "adjudication_kind": "hitl_action_execute",
        "bridge_status": "pending",
        "engagement_id": "eng_hitl",
        "action_id": "draft_reply",
        "operator_id": "konrad",
        "case_id": "case_hitl",
    }
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def fake_execute(**kwargs: object) -> dict[str, object]:
        kwargs["on_effect_start"]()
        calls.append("exec")
        entered.set()
        release.wait(timeout=2)
        return {"executed": True, "mode": "bounded_dry_run"}

    outputs: list[dict[str, object]] = []

    def _run_drain() -> None:
        result = drain_bridge_rows(
            pending=[row],
            append_completion=lambda *_a, **_k: None,
            bridge_operator_feedback=object(),
            store=mailbox_store,
            journal=object(),
            runtime_context=SimpleNamespace(settings=load_agent_runtime_settings()),
            max_items=1,
            dry_run=False,
        )
        outputs.append(result[0])

    with patch("agent_hitl_bridge.AgentMcpService.from_env", return_value=service):
        with patch("agent_hitl_bridge.build_mailbox_memory_runtime", return_value=_mailbox_runtime(mailbox_store), create=True):
            with patch("agent_hitl_bridge.publish_os_event", return_value=None):
                with patch("agent_hitl_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"ok": True, "snapshot_id": "snap-1"}):
                    with patch("agent_hitl_bridge.execute_hitl_gmail_send", side_effect=fake_execute):
                        t1 = threading.Thread(target=_run_drain)
                        t2 = threading.Thread(target=_run_drain)
                        t1.start()
                        t2.start()
                        assert entered.wait(timeout=2)
                        release.set()
                        t1.join(timeout=2)
                        t2.join(timeout=2)

    assert len(outputs) == 2
    assert all(item["ok"] is True for item in outputs)
    assert calls == ["exec"]


def test_pending_bridge_rows_includes_agent_hitl() -> None:
    from daszek_bridge_queue_drain import pending_bridge_rows

    queue_path = Path(__file__).resolve().parent / "_tmp_agent_hitl_queue.jsonl"
    queue_path.write_text(
        "\n".join(
            [
                '{"queue_id":"hitl1","schema_version":"daszek_bridge_queue.v1","domain":"agent_hitl","adjudication_kind":"hitl_action_execute","bridge_status":"pending","engagement_id":"eng-1"}',
                '{"queue_id":"adj1","schema_version":"daszek_bridge_queue.v1","domain":"adjudication","adjudication_kind":"reject_same_case","bridge_status":"pending"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        pending = pending_bridge_rows(queue_path)
        domains = {str(r.get("domain")) for r in pending}
        assert "agent_hitl" in domains
        assert "adjudication" in domains
    finally:
        queue_path.unlink(missing_ok=True)
