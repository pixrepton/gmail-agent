from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_hitl_bridge import approve_hitl_engagement
from agent_runtime.mcp_service import AgentMcpService
from agent_runtime.settings import load_agent_runtime_settings
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from event_spine.emitter import publish_os_event
from event_spine.query import event_to_api_item, fetch_os_events_for_engagement
from llm_contracts.engagement_snapshot_v2 import ActionItem


def _hitl_snapshot(*, gate: bool = True):
    snap = build_initial_snapshot(case_id="case_hitl", engagement_id="eng_hitl", trace_id="t1")
    delta: dict = {
        "hitl_gate": {"required": gate, "reason": "draft_ready_for_approval" if gate else ""},
        "actions": [
            ActionItem(id="draft_reply", enabled=True, payload_pl="Draft test").model_dump(mode="python")
        ],
    }
    return apply_snapshot_delta(snap, delta)


def test_approve_hitl_emits_gmail_hitl_approved_os_event() -> None:
    store = InMemoryOperatorEngagementStore()
    store.insert_snapshot(_hitl_snapshot(gate=True))
    settings = load_agent_runtime_settings()
    service = AgentMcpService(store=store, settings=settings)
    captured: list[dict] = []

    def _capture_publish(**kwargs):  # type: ignore[no-untyped-def]
        captured.append(dict(kwargs))
        return "osevt_test_approved"

    with patch("agent_hitl_bridge.AgentMcpService.from_env", return_value=service):
        with patch("agent_hitl_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"skipped": True}):
            with patch("agent_hitl_bridge.publish_os_event", side_effect=_capture_publish):
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
    assert out.get("os_event_id") == "osevt_test_approved"
    assert len(captured) == 1
    assert captured[0]["event_type"] == "gmail.hitl.approved"
    assert captured[0]["engagement_id"] == "eng_hitl"
    assert captured[0]["payload"]["summary_pl"]
    assert captured[0]["payload"]["decision_status"] == "approved"
    assert captured[0]["payload"]["execution_status"] == "not_applicable"
    assert captured[0]["payload"]["delivery_mode"] == "manual_operator"
    assert captured[0]["trace_id"] == "t1"
    assert captured[0]["user_id"] == "konrad"


def test_event_to_api_item_keeps_trace_as_correlation_and_case_as_entity_ref() -> None:
    row = type(
        "Evt",
        (),
        {
            "event_id": "osevt_1",
            "event_type": "gmail.hitl.approved",
            "source_repo": "gmail-agent",
            "engagement_id": "eng_1",
            "case_id": "case_1",
            "trace_id": "trace_1",
            "occurred_at": __import__("datetime").datetime(2026, 7, 12, 12, 0, tzinfo=__import__("datetime").timezone.utc),
            "payload": {"summary_pl": "OK", "status": "ok", "signal_id": "sig_1"},
            "correlation": {"signal_id": "sig_1"},
        },
    )()
    item = event_to_api_item(row)
    assert item["engagement_id"] == "eng_1"
    assert item["case_id"] == "case_1"
    assert item["trace_id"] == "trace_1"
    assert item["payload"]["signal_id"] == "sig_1"


@pytest.mark.skipif(
    not __import__("os").environ.get("MAILBOX_MEMORY_DATABASE_URL"),
    reason="integration: requires MAILBOX_MEMORY_DATABASE_URL",
)
def test_fetch_os_events_for_engagement_integration() -> None:
    import os

    db_url = os.environ["MAILBOX_MEMORY_DATABASE_URL"]
    eid = "eng_os_event_query_test"
    event_id = publish_os_event(
        database_url=db_url,
        event_type="gmail.hitl.approved",
        engagement_id=eid,
        source_repo="gmail-agent",
        payload={
            "schema_version": "topinstal.os_event.v1",
            "summary_pl": "Test zdarzenia",
            "status": "ok",
        },
        correlation={"case_id": "case_test"},
    )
    assert event_id
    items = fetch_os_events_for_engagement(db_url, eid, limit=10)
    assert any(str(row.get("event_type") or "") == "gmail.hitl.approved" for row in items)
