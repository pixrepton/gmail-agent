"""AI-OS Roadmap 3.3 — manual send receipt E2E via canonical outbound intake."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.store import InMemoryOperatorEngagementStore
from correlation_registry.service import CorrelationRegistryService
from correlation_registry.store import InMemoryCorrelationRegistryStore
from llm_contracts.engagement_snapshot_v2 import (
    CommunicationReceipt,
    EngagementSnapshotV2,
    HitlGate,
    OperationalStatus,
)
from mailbox_memory_runtime import MailboxMemoryRuntime
from mailbox_memory_store import InMemoryMailboxMemoryStore
from outbound_receipt import build_ready_for_manual_send_receipt

MAILBOX = "biuro@topinstal.com.pl"
CUSTOMER_EMAIL = "klient@example.com"
CASE_KEY = "CASE-AIOS-33"
THREAD_ID = "thr-aios-33"
INTAKE = {"case_assessment": {"case_family": "lead_opportunity"}}
CASE_LINK = {"decision": "link_existing", "selected_case_key": CASE_KEY}


def _inbound_snapshot(*, message_id: str = "msg-in-33") -> dict:
    return {
        "mailbox": MAILBOX,
        "source_message": {
            "message_id": message_id,
            "thread_id": THREAD_ID,
            "date": "2026-08-04T10:00:00+02:00",
            "from": f"Jan Kowalski <{CUSTOMER_EMAIL}>",
            "sender": f"Jan Kowalski <{CUSTOMER_EMAIL}>",
            "to": [MAILBOX],
            "subject": "Pytanie o oferte",
            "snippet": "Prosze o wycene",
            "body": "Prosze o wycene pompy ciepla.",
            "labels": ["INBOX"],
        },
    }


def _outbound_snapshot(*, message_id: str = "msg-out-33", to_email: str = CUSTOMER_EMAIL) -> dict:
    return {
        "mailbox": MAILBOX,
        "source_message": {
            "message_id": message_id,
            "thread_id": THREAD_ID,
            "date": "2026-08-04T12:00:00+02:00",
            "from": MAILBOX,
            "sender": MAILBOX,
            "to": [to_email],
            "subject": "Re: Pytanie o oferte",
            "snippet": "Wysylamy odpowiedz",
            "body": "Dzien dobry, w zalaczeniu oferta.",
            "labels": ["SENT"],
        },
    }


def _runtime(tmp_path: Path) -> tuple[MailboxMemoryRuntime, CorrelationRegistryService]:
    registry_store = InMemoryCorrelationRegistryStore()
    registry_store.bootstrap()
    registry = CorrelationRegistryService(registry_store)
    runtime = MailboxMemoryRuntime(
        store=InMemoryMailboxMemoryStore(),
        blob_root=tmp_path / "blobs",
        stage_mode="live",
        correlation_registry=registry,
    )
    runtime.bootstrap()
    return runtime, registry


def _seed_ready_for_manual_send(
    *,
    registry: CorrelationRegistryService,
    case_id: str,
    draft_id: str = "draft-33",
    body_hash: str = "hash-33",
    target_email: str = CUSTOMER_EMAIL,
) -> tuple[InMemoryOperatorEngagementStore, str]:
    row = registry.lookup_by_case_id(case_id)
    assert row is not None, "correlation registry must link case_id to engagement"
    engagement_id = str(row.get("engagement_id") or "").strip()
    assert engagement_id

    op_store = InMemoryOperatorEngagementStore()
    op_store.insert_snapshot(
        EngagementSnapshotV2(
            engagement_id=engagement_id,
            case_id=case_id,
            version=1,
            operational_status=OperationalStatus(code="ready_for_quote", steps_remaining=0),
            hitl_gate=HitlGate(required=False),
            communication_receipt=CommunicationReceipt(
                **build_ready_for_manual_send_receipt(
                    draft_id=draft_id,
                    body_hash=body_hash,
                    target_email=target_email,
                )
            ),
        )
    )
    return op_store, engagement_id


@contextmanager
def _operator_store_patch(store: InMemoryOperatorEngagementStore):
    with patch(
        "agent_runtime.agent_reconcile.build_operator_engagement_store",
        return_value=store,
    ):
        yield


def _ingest_outbound(
    runtime: MailboxMemoryRuntime,
    op_store: InMemoryOperatorEngagementStore,
    *,
    message_id: str = "msg-out-33",
    to_email: str = CUSTOMER_EMAIL,
) -> None:
    with _operator_store_patch(op_store):
        result = runtime.ingest_message(
            snapshot=_outbound_snapshot(message_id=message_id, to_email=to_email),
            intake_result=INTAKE,
            case_link_result=CASE_LINK,
        )
    assert result.enabled is True


class TestManualSendReceiptE2E:
    def test_outbound_ingest_closes_ready_for_manual_send_loop(self, tmp_path: Path) -> None:
        runtime, registry = _runtime(tmp_path)
        inbound = runtime.ingest_message(
            snapshot=_inbound_snapshot(),
            intake_result=INTAKE,
            case_link_result=CASE_LINK,
        )
        assert inbound.enabled is True
        case_id = inbound.case_id

        op_store, engagement_id = _seed_ready_for_manual_send(registry=registry, case_id=case_id)
        _ingest_outbound(runtime, op_store)

        sent_events = [
            event
            for event in runtime.store.fetch_events_for_case(case_id, limit=50)
            if event.get("event_type") == "communication_sent"
        ]
        assert len(sent_events) == 1
        assert sent_events[0]["message_id"] == "msg-out-33"

        saved = op_store.load_snapshot(engagement_id)
        assert saved is not None
        assert saved.communication_receipt.state == "communication_sent"
        assert saved.communication_receipt.gmail_message_id == "msg-out-33"
        assert saved.communication_receipt.draft_id == "draft-33"
        assert saved.communication_receipt.body_hash == "hash-33"
        assert saved.communication_receipt.target_email == CUSTOMER_EMAIL
        assert saved.version == 2

    def test_outbound_ingest_is_idempotent_for_events_and_snapshot(self, tmp_path: Path) -> None:
        runtime, registry = _runtime(tmp_path)
        inbound = runtime.ingest_message(
            snapshot=_inbound_snapshot(),
            intake_result=INTAKE,
            case_link_result=CASE_LINK,
        )
        case_id = inbound.case_id
        op_store, engagement_id = _seed_ready_for_manual_send(registry=registry, case_id=case_id)

        _ingest_outbound(runtime, op_store, message_id="msg-out-dup")
        _ingest_outbound(runtime, op_store, message_id="msg-out-dup")

        sent_events = [
            event
            for event in runtime.store.fetch_events_for_case(case_id, limit=50)
            if event.get("event_type") == "communication_sent"
        ]
        assert len(sent_events) == 1

        saved = op_store.load_snapshot(engagement_id)
        assert saved is not None
        assert saved.version == 2
        assert saved.communication_receipt.gmail_message_id == "msg-out-dup"

    def test_outbound_ingest_skips_receipt_when_not_awaiting_manual_send(self, tmp_path: Path) -> None:
        runtime, registry = _runtime(tmp_path)
        inbound = runtime.ingest_message(
            snapshot=_inbound_snapshot(message_id="msg-in-skip"),
            intake_result=INTAKE,
            case_link_result=CASE_LINK,
        )
        case_id = inbound.case_id
        row = registry.lookup_by_case_id(case_id)
        assert row is not None
        engagement_id = str(row["engagement_id"])

        op_store = InMemoryOperatorEngagementStore()
        op_store.insert_snapshot(
            EngagementSnapshotV2(
                engagement_id=engagement_id,
                case_id=case_id,
                version=1,
                operational_status=OperationalStatus(code="enriching", steps_remaining=4),
                hitl_gate=HitlGate(required=True, reason="draft_ready_for_approval"),
            )
        )

        _ingest_outbound(runtime, op_store, message_id="msg-out-skip")

        saved = op_store.load_snapshot(engagement_id)
        assert saved is not None
        assert saved.version == 1
        receipt = getattr(saved, "communication_receipt", None)
        state = str(getattr(receipt, "state", "") or "")
        assert state != "communication_sent"

    def test_outbound_ingest_does_not_close_receipt_for_wrong_recipient(self, tmp_path: Path) -> None:
        runtime, registry = _runtime(tmp_path)
        inbound = runtime.ingest_message(
            snapshot=_inbound_snapshot(message_id="msg-in-wrong-recipient"),
            intake_result=INTAKE,
            case_link_result=CASE_LINK,
        )
        op_store, engagement_id = _seed_ready_for_manual_send(
            registry=registry,
            case_id=inbound.case_id,
            target_email=CUSTOMER_EMAIL,
        )

        _ingest_outbound(
            runtime,
            op_store,
            message_id="msg-out-wrong-recipient",
            to_email="inny@example.com",
        )

        saved = op_store.load_snapshot(engagement_id)
        assert saved is not None
        assert saved.communication_receipt.state == "ready_for_manual_send"
        assert saved.communication_receipt.gmail_message_id == ""
        assert saved.communication_receipt.target_email == CUSTOMER_EMAIL

    def test_outbound_ingest_never_calls_gmail_send(self, tmp_path: Path) -> None:
        runtime, registry = _runtime(tmp_path)
        inbound = runtime.ingest_message(
            snapshot=_inbound_snapshot(message_id="msg-in-nosend"),
            intake_result=INTAKE,
            case_link_result=CASE_LINK,
        )
        op_store, _ = _seed_ready_for_manual_send(registry=registry, case_id=inbound.case_id)

        def _forbidden_send(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("gmail send must not be invoked during outbound receipt intake")

        with (
            patch("google_gmail_api.send_raw_message", side_effect=_forbidden_send),
            patch("hitl_gmail_send.execute_hitl_gmail_send", side_effect=_forbidden_send),
            patch("agent_runtime.tools.write_executors.execute_send_email", side_effect=_forbidden_send),
        ):
            _ingest_outbound(runtime, op_store, message_id="msg-out-nosend")
