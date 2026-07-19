from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot, build_staging_snapshot
from agent_runtime.turn_journal import InMemoryAgentTurnJournal
from daszek_engagement_feed import (
    build_daszek_feed_doctor_check,
    build_engagement_feed_for_cel,
    engagement_feed_source_enabled,
    resolve_reconcile_case_id_for_feed,
)
from daszek_engagement_feed.build import build_operational_feed_from_engagement_store
from daszek_engagement_feed.desk import snapshot_to_desk_item
from daszek_v3_feed_runtime import (
    _engagement_extra_case_ids,
    accumulate_engagement_feed_case_hint,
    maybe_push_operational_feed_after_reconcile,
)
from llm_contracts.engagement_snapshot_v2 import OperationalStatus


def test_engagement_feed_source_env_legacy_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASZEK_FEED_SOURCE", "legacy")
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "prep")
    assert engagement_feed_source_enabled() is False


def test_resolve_case_id_from_agent_stage_outputs() -> None:
    reconcile = SimpleNamespace(
        case_id="",
        stage_outputs={
            "reconcile_path": "agent_runtime",
            "agent_engagement_snapshot": {"case_id": "case_agent_1", "engagement_id": "eng_1"},
        },
    )
    assert resolve_reconcile_case_id_for_feed(reconcile) == "case_agent_1"


def test_accumulate_engagement_hint_pins_case() -> None:
    run_state: dict = {}
    reconcile = SimpleNamespace(
        case_id="case_hint_1",
        processing_state="reconciled",
        projection_refresh_decision=SimpleNamespace(should_refresh=True),
        stage_outputs={},
    )
    accumulate_engagement_feed_case_hint(run_state, reconcile)
    assert _engagement_extra_case_ids(run_state) == ["case_hint_1"]


def test_build_engagement_feed_for_cel_with_extra_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "0")
    monkeypatch.setenv("DASZEK_FEED_SOURCE", "engagement_snapshot_v2")
    store = InMemoryOperatorEngagementStore()
    snap = build_initial_snapshot(case_id="case_cel_1", engagement_id="eng_cel_1", trace_id="sig_1")
    snap.operational_status = OperationalStatus(code="pending_operator", steps_remaining=0, blocking=True)
    store.init_snapshot_from_signal(signal={"signal_id": "sig_1"}, case_id="case_cel_1", engagement_id="eng_cel_1")
    store._rows["eng_cel_1"]["snapshot_data"] = snap.model_dump(mode="python")
    settings = SimpleNamespace(mailbox_memory_database_url="")
    mailbox = object()
    with patch("daszek_engagement_feed.build_operator_engagement_store", return_value=store), patch(
        "daszek_engagement_feed.build_turn_journal_for_settings",
        return_value=InMemoryAgentTurnJournal(),
    ):
        envelope = build_engagement_feed_for_cel(
            mailbox,
            settings,
            case_limit=10,
            extra_case_ids=["case_cel_1"],
        )
    assert "case_cel_1" in envelope["feed"]["case_details"]
    assert envelope["feed"]["feed_meta"]["agent_runtime"] is True


def test_feed_runtime_uses_engagement_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "prep")
    client = MagicMock()
    client.post_v3_operational_feed_snapshot.return_value = {"ok": True, "snapshot_id": "snap-eng"}
    run_state = {
        "manifest": {"daszek_operational_feed_auto_push_enabled": True},
        "daszek_client": client,
        "mailbox_memory_runtime": SimpleNamespace(store=object()),
        "summary": {},
        "run_id": "run-eng-1",
    }
    reconcile = SimpleNamespace(
        case_id="case_push_1",
        processing_state="reconciled",
        projection_refresh_decision=SimpleNamespace(should_refresh=True),
        stage_outputs={},
    )
    settings = SimpleNamespace(
        daszek_operational_feed_push_min_interval_sec=0,
        daszek_operational_feed_case_limit=5,
        mailbox_memory_database_url="",
    )
    feed_snapshot = {
        "schema_name": "daszek_operational_feed_snapshot",
        "snapshot_id": "snap-eng",
        "feed": {"desk": [], "cases": [], "tasks": [], "feed_meta": {"agent_runtime": True}},
        "source": {},
    }
    with patch("daszek_engagement_feed.build_engagement_feed_for_cel", return_value=feed_snapshot) as build_mock:
        maybe_push_operational_feed_after_reconcile(
            run_state=run_state,
            settings=settings,
            reconcile_result=reconcile,
            trigger_message_id="msg-eng",
        )
    build_mock.assert_called_once()
    assert "case_push_1" in build_mock.call_args.kwargs.get("extra_case_ids", [])
    client.post_v3_operational_feed_snapshot.assert_called_once()
    posted = client.post_v3_operational_feed_snapshot.call_args[0][0]
    assert posted["source"]["cel_path"] == "engagement_snapshot_v2"


def test_doctor_flags_legacy_vs_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASZEK_FEED_SOURCE", "legacy")
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "prep")
    rep = build_daszek_feed_doctor_check(SimpleNamespace())
    assert rep["status"] == "failed"
    assert rep["issues"]


def test_desk_item_carries_source_message_id() -> None:
    snap = build_initial_snapshot(case_id="case_msg_1", engagement_id="eng_msg_1", trace_id="sig_msg_1")
    snap.operational_status = OperationalStatus(code="pending_operator", steps_remaining=0, blocking=True)

    desk_item = snapshot_to_desk_item(
        snap,
        meta={
            "message_id": "msg-123",
            "sender_name": "Jan",
            "sender_email": "jan@example.com",
            "received_at": "2026-07-12T12:00:00Z",
        },
    )

    assert desk_item is not None
    assert desk_item["source_message_id"] == "msg-123"


def test_staging_feed_falls_back_to_signal_message_meta() -> None:
    store = InMemoryOperatorEngagementStore()
    snap = build_staging_snapshot(
        engagement_id="stg_sig_test_meta",
        signal_id="sig_test_meta",
        trace_id="sig_test_meta",
    )
    snap.operational_status = OperationalStatus(code="pending_operator", steps_remaining=0, blocking=True)
    store.insert_snapshot(snap)

    class _Mailbox:
        def fetch_signal(self, signal_id: str) -> dict[str, object] | None:
            if signal_id != "sig_test_meta":
                return None
            return {
                "source_ref_json": {"message_id": "msg-staging-123", "thread_id": "thr-staging-123"},
                "payload_json": {
                    "snapshot": {
                        "source_message": {
                            "message_id": "msg-staging-123",
                            "thread_id": "thr-staging-123",
                            "subject": "Staging proof subject",
                        }
                    }
                },
                "observed_at": "2026-07-12T12:00:00Z",
            }

    envelope = build_operational_feed_from_engagement_store(
        store,
        mailbox_store=_Mailbox(),
        case_ids=None,
        journal=InMemoryAgentTurnJournal(),
        case_limit=5,
        snapshot_id="snap-staging-meta",
    )

    desk = envelope["feed"]["desk"]
    assert desk[0]["engagement_id"] == "stg_sig_test_meta"
    assert desk[0]["source_message_id"] == "msg-staging-123"
    assert desk[0]["thread_id"] == "thr-staging-123"


def test_staging_feed_keeps_per_engagement_source_message_ids_for_two_cards() -> None:
    store = InMemoryOperatorEngagementStore()
    snap_a = build_staging_snapshot(
        engagement_id="stg_sig_a",
        signal_id="sig-a",
        trace_id="trace-a",
    )
    snap_b = build_staging_snapshot(
        engagement_id="stg_sig_b",
        signal_id="sig-b",
        trace_id="trace-b",
    )
    snap_a.operational_status = OperationalStatus(code="pending_operator", steps_remaining=0, blocking=True)
    snap_b.operational_status = OperationalStatus(code="pending_operator", steps_remaining=0, blocking=True)
    store.insert_snapshot(snap_a)
    store.insert_snapshot(snap_b)

    class _Mailbox:
        def fetch_signal(self, signal_id: str) -> dict[str, object] | None:
            mapping = {
                "sig-a": {
                    "source_ref_json": {"message_id": "msg-a", "thread_id": "thr-a"},
                    "payload_json": {"snapshot": {"source_message": {"message_id": "msg-a", "thread_id": "thr-a", "subject": "A"}}},
                    "observed_at": "2026-07-12T12:00:00Z",
                },
                "sig-b": {
                    "source_ref_json": {"message_id": "msg-b", "thread_id": "thr-b"},
                    "payload_json": {"snapshot": {"source_message": {"message_id": "msg-b", "thread_id": "thr-b", "subject": "B"}}},
                    "observed_at": "2026-07-12T12:01:00Z",
                },
            }
            return mapping.get(signal_id)

    envelope = build_operational_feed_from_engagement_store(
        store,
        mailbox_store=_Mailbox(),
        case_ids=None,
        journal=InMemoryAgentTurnJournal(),
        case_limit=5,
        snapshot_id="snap-staging-meta-two",
    )

    desk_by_engagement = {row["engagement_id"]: row for row in envelope["feed"]["desk"]}
    assert desk_by_engagement["stg_sig_a"]["source_message_id"] == "msg-a"
    assert desk_by_engagement["stg_sig_a"]["source_signal_ids"] == ["sig-a"]
    assert desk_by_engagement["stg_sig_b"]["source_message_id"] == "msg-b"
    assert desk_by_engagement["stg_sig_b"]["source_signal_ids"] == ["sig-b"]
