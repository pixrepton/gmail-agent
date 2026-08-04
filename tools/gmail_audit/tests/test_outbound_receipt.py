"""Tests for manual-send receipt / outbound direction close-loop."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from llm_contracts.engagement_snapshot_v2 import (
    CommunicationReceipt,
    EngagementSnapshotV2,
    FeedVisibility,
    HitlGate,
    OperationalStatus,
)
from outbound_receipt import (
    build_communication_sent_receipt,
    build_ready_for_manual_send_receipt,
    counterparty_email_for_message,
    infer_live_direction,
    should_apply_communication_sent,
    try_apply_communication_sent_receipt,
)


def test_infer_direction_sent_label() -> None:
    assert (
        infer_live_direction(
            {"labels": ["SENT"], "sender": "biuro@topinstal.com.pl", "to": ["klient@ex.pl"]},
            mailbox="biuro@topinstal.com.pl",
        )
        == "outbound"
    )


def test_infer_direction_from_mailbox() -> None:
    assert (
        infer_live_direction(
            {"labels": ["INBOX"], "sender": "Biuro <biuro@topinstal.com.pl>", "to": ["a@b.pl"]},
            mailbox="biuro@topinstal.com.pl",
        )
        == "outbound"
    )


def test_infer_direction_inbound() -> None:
    assert (
        infer_live_direction(
            {"labels": ["INBOX"], "sender": "klient@ex.pl", "to": ["biuro@topinstal.com.pl"]},
            mailbox="biuro@topinstal.com.pl",
        )
        == "inbound"
    )


def test_counterparty_outbound_uses_to_not_from() -> None:
    email = counterparty_email_for_message(
        {
            "sender": "biuro@topinstal.com.pl",
            "to": ["Klient <klient@ex.pl>"],
            "labels": ["SENT"],
        },
        direction="outbound",
        mailbox="biuro@topinstal.com.pl",
    )
    assert email == "klient@ex.pl"


def test_should_apply_when_ready_for_manual_send() -> None:
    snap = EngagementSnapshotV2(
        engagement_id="eng_1",
        case_id="case_1",
        version=1,
        operational_status=OperationalStatus(code="ready_for_quote", steps_remaining=0),
        hitl_gate=HitlGate(required=False),
        communication_receipt=CommunicationReceipt(**build_ready_for_manual_send_receipt()),
    )
    assert should_apply_communication_sent(snap) is True


def test_should_not_apply_when_already_sent() -> None:
    snap = EngagementSnapshotV2(
        engagement_id="eng_1",
        case_id="case_1",
        version=1,
        operational_status=OperationalStatus(code="ready_for_quote", steps_remaining=0),
        hitl_gate=HitlGate(required=False),
        communication_receipt=CommunicationReceipt(
            **build_communication_sent_receipt(gmail_message_id="m1")
        ),
    )
    assert should_apply_communication_sent(snap) is False


def test_try_apply_communication_sent_updates_snapshot() -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.snap = EngagementSnapshotV2(
                engagement_id="eng_x",
                case_id="case_x",
                version=3,
                operational_status=OperationalStatus(code="ready_for_quote", steps_remaining=0),
                hitl_gate=HitlGate(required=False),
                feed_visibility=FeedVisibility(execution_attention=True, execution_attention_reason="outcome_unknown"),
                communication_receipt=CommunicationReceipt(**build_ready_for_manual_send_receipt(draft_id="d1")),
            )
            self.saved = None

        def load_snapshot(self, eid: str):
            assert eid == "eng_x"
            return self.snap

        def save_snapshot(self, patched, expected_version: int):
            assert expected_version == 3
            self.saved = patched
            return 4

    class FakeRegistry:
        def lookup_by_case_id(self, case_id: str):
            return {"engagement_id": "eng_x", "case_id": case_id}

    store = FakeStore()
    out = try_apply_communication_sent_receipt(
        case_id="case_x",
        thread_id="thr_1",
        message_id="msg_sent_1",
        occurred_at="2026-08-04T10:00:00+00:00",
        correlation_registry=FakeRegistry(),
        database_url="",
        operator_store=store,
    )
    assert out["ok"] is True
    assert store.saved is not None
    assert store.saved.communication_receipt.state == "communication_sent"
    assert store.saved.communication_receipt.gmail_message_id == "msg_sent_1"
    assert store.saved.feed_visibility.execution_attention is False
