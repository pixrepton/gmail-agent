from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot
from agent_runtime.turn_journal import InMemoryAgentTurnJournal
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from daszek_engagement_feed import (
    build_feed_from_engagement_snapshots,
    build_operational_feed_from_engagement_store,
    snapshot_to_desk_item,
)
from daszek_engagement_feed.case import snapshot_to_feed_case
from daszek_v3_operational_feed_contract import (
    FORBIDDEN_KEYS_ANYWHERE,
    validate_operational_feed_snapshot,
)
from llm_contracts.engagement_snapshot_v2 import ActionItem, HitlGate, OperationalStatus


def _snapshot(*, case_id: str = "case_feed_1", status: str = "pending_operator") -> object:
    snap = build_initial_snapshot(
        case_id=case_id,
        engagement_id="eng_feed_1",
        trace_id="sig_feed_1",
    )
    snap.hvac_profile.heated_area_m2 = 128
    snap.hvac_profile.location.city = "Radlin"
    snap.operational_status = OperationalStatus(code=status, steps_remaining=0, blocking=True)
    snap.hitl_gate = HitlGate(required=True, reason="agent_stopped")
    snap.gaps = []
    snap.actions = [
        ActionItem(
            id="draft_reply",
            enabled=True,
            payload_pl="Draft odpowiedzi test",
            disabled_reason_pl=None,
        )
    ]
    return snap


def test_excludes_operator_desk_prefix() -> None:
    snap = _snapshot(case_id="_operator_desk_hidden")
    feed = build_feed_from_engagement_snapshots([snap])
    assert feed["cases"] == []
    assert feed["desk"] == []


def test_desk_item_for_pending_operator() -> None:
    snap = _snapshot()
    desk = snapshot_to_desk_item(snap)
    assert desk is not None
    assert desk["case_id"] == "case_feed_1"
    assert desk["hitl_required"] is True


def test_case_details_include_agent_turns() -> None:
    snap = _snapshot()
    journal = InMemoryAgentTurnJournal()
    journal.append_turn(
        engagement_id="eng_feed_1",
        snapshot_version=2,
        trace_id="sig_feed_1",
        plan=ToolCallPlan(tool_name="extract_facts_from_text"),
        result=ToolResult(status="ok", turn_summary_pl="Wyciągnięto 128 m² Radlin", tokens_used=12),
    )
    feed = build_feed_from_engagement_snapshots([snap], journal=journal)
    detail = feed["case_details"]["case_feed_1"]
    assert len(detail["agent_turns"]) == 1
    assert detail["agent_turns"][0]["tool_name"] == "extract_facts_from_text"
    assert "Radlin" in detail["agent_turns"][0]["turn_summary_pl"]


_META = {
    "subject": "Wycena - klimatyzacja",
    "sender_name": "Jan Kowalski",
    "sender_email": "jan.kowalski@example.com",
    "received_at": "2026-06-09T08:15:00Z",
    "message_id": "msg_1",
    "thread_id": "thr_1",
    "attachments": [
        {
            "attachment_id": "att_1",
            "file_name": "rzut_parter.pdf",
            "mime_type": "application/pdf",
            "document_kind": "floor_plan",
            "extraction_status": "done",
            "summary_pl": "Rzut parteru, 128 m2, kotłownia na parterze.",
            "has_text": True,
        }
    ],
}


def test_case_row_carries_sender_date_attachments() -> None:
    snap = _snapshot()
    row = snapshot_to_feed_case(snap, subject=_META["subject"], meta=_META)
    assert row["channel"] == "email"
    assert row["sender_name"] == "Jan Kowalski"
    assert row["customer_email"] == "jan.kowalski@example.com"
    assert row["received_at"] == "2026-06-09T08:15:00Z"
    assert row["attachment_count"] == 1
    assert row["attachments"][0]["file_name"] == "rzut_parter.pdf"
    assert row["has_attachments"] is True


def test_desk_item_carries_sender_date_attachments() -> None:
    snap = _snapshot()
    desk = snapshot_to_desk_item(snap, subject=_META["subject"], meta=_META)
    assert desk is not None
    assert desk["customer_email"] == "jan.kowalski@example.com"
    assert desk["latest_signal_at"] == "2026-06-09T08:15:00Z"
    assert desk["attachment_count"] == 1


def test_case_detail_surfaces_attachments() -> None:
    snap = _snapshot()
    feed = build_feed_from_engagement_snapshots([snap], meta_by_case={"case_feed_1": _META})
    detail = feed["case_details"]["case_feed_1"]
    assert detail["attachments"] and detail["attachments"][0]["file_name"] == "rzut_parter.pdf"
    case_row = feed["cases"][0]
    assert case_row["customer_email"] == "jan.kowalski@example.com"


def test_new_fields_do_not_leak_forbidden_keys() -> None:
    snap = _snapshot()
    feed = build_feed_from_engagement_snapshots([snap], meta_by_case={"case_feed_1": _META})

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                assert k not in FORBIDDEN_KEYS_ANYWHERE, f"forbidden key leaked: {k}"
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(feed)


def test_operational_feed_envelope_validates() -> None:
    from agent_runtime.tool_result import ToolCallPlan, ToolResult

    store = InMemoryOperatorEngagementStore()
    snap = _snapshot()
    store.init_snapshot_from_signal(
        signal={"signal_id": "sig_feed_1"},
        case_id="case_feed_1",
        engagement_id="eng_feed_1",
    )
    store._rows["eng_feed_1"]["snapshot_data"] = snap.model_dump(mode="python")
    journal = InMemoryAgentTurnJournal()
    journal.append_turn(
        engagement_id="eng_feed_1",
        snapshot_version=2,
        trace_id="sig_feed_1",
        plan=ToolCallPlan(tool_name="extract_facts_from_text"),
        result=ToolResult(status="ok", turn_summary_pl="128 m² Radlin", tokens_used=7),
    )
    envelope = build_operational_feed_from_engagement_store(
        store,
        case_ids=["case_feed_1"],
        journal=journal,
    )
    rep = validate_operational_feed_snapshot(envelope)
    assert rep.ok, rep.errors
    assert envelope["read_only"] is True
    feed = envelope["feed"]
    assert "case_feed_1" in feed["case_details"]
    assert feed["feed_meta"]["exporter"].endswith("daszek_engagement_feed")
    assert feed["feed_meta"]["agent_runtime"] is True
    timeline = feed["case_details"]["case_feed_1"]["operational_timeline"]
    assert timeline and timeline[0].get("event_type") == "agent_turn"
