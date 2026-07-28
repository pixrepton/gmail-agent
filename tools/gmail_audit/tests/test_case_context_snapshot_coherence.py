from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory.inmemory import InMemoryMailboxMemoryStore
from mailbox_memory_runtime import MailboxMemoryRuntime, build_case_context_pack


def _case_row(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "case_key": "CASE-CURRENT",
        "thread_id": "thread-current",
        "case_family": "lead_opportunity",
        "mailbox": "test@example.com",
        "subject": "Nowa wiadomość",
        "status": "open",
        "customer_name": "Jan Kowalski",
        "customer_email": "jan@example.com",
        "metadata": {},
        "created_at": "2026-07-28T09:00:00+00:00",
        "updated_at": "2026-07-28T10:00:00+00:00",
    }


def _message_row(case_id: str) -> dict:
    return {
        "message_id": "msg-current",
        "case_id": case_id,
        "thread_id": "thread-current",
        "mailbox": "test@example.com",
        "sender": "Jan Kowalski <jan@example.com>",
        "sender_email": "jan@example.com",
        "recipients": ["biuro@example.com"],
        "subject": "Nowa wiadomość",
        "snippet": "Aktualny stan",
        "body_text": "Proszę o aktualny termin.",
        "labels": ["INBOX"],
        "received_at": "2026-07-28T10:00:00+00:00",
        "raw_snapshot": {},
        "created_at": "2026-07-28T10:00:00+00:00",
        "updated_at": "2026-07-28T10:00:00+00:00",
    }


def test_context_pack_ignores_stale_legacy_snapshot_and_projects_current_store(monkeypatch) -> None:
    store = InMemoryMailboxMemoryStore()
    case_id = "case-current"
    store.upsert_case(_case_row(case_id))
    store.upsert_message(_message_row(case_id))
    store.upsert_next_action(
        case_id,
        {
            "next_action": "confirm_current_date",
            "rationale": "Aktualna wiadomość klienta.",
            "source_stage": "test",
            "payload": {},
            "updated_at": "2026-07-28T10:01:00+00:00",
        },
    )
    store.upsert_snapshot(
        case_id,
        {
            "snapshot_json": {
                "case_id": case_id,
                "summary_text": "STALE LEGACY SNAPSHOT",
                "recommended_next_action": "obsolete_action",
                "recent_message_ids": ["msg-obsolete"],
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        },
    )
    monkeypatch.setattr(
        InMemoryMailboxMemoryStore,
        "fetch_snapshot",
        lambda _self, _case_id: (_ for _ in ()).throw(AssertionError("legacy snapshot read")),
    )

    pack = build_case_context_pack(store=store, case_id=case_id)

    assert pack.snapshot["context_snapshot_status"] == "current"
    assert pack.snapshot["context_snapshot_source"] == "mailbox_memory_live_projection"
    assert pack.snapshot["recommended_next_action"] == "confirm_current_date"
    assert pack.snapshot["recent_message_ids"] == ["msg-current"]
    assert pack.snapshot.get("summary_text") != "STALE LEGACY SNAPSHOT"


def test_context_pack_projects_latest_versioned_hot_state() -> None:
    store = InMemoryMailboxMemoryStore()
    case_id = "case-hot"
    store.upsert_case(_case_row(case_id))
    store.upsert_message(_message_row(case_id))
    hot_state = {
        "schema_version": "case_snapshot_hot_state.v1",
        "snapshot_id": "snap-hot-2",
        "case": {
            "case_id": case_id,
            "case_key": "CASE-HOT",
            "lifecycle_status": "awaiting_review",
            "summary_text": "Aktualny versioned Hot State.",
        },
        "key_facts": [
            {
                "fact_key": "city",
                "value": "Radlin",
                "source_ref": "msg-current",
                "provenance": {"kind": "message", "ref": "msg-current"},
            }
        ],
        "open_loops": ["Potwierdzić termin wizyty."],
        "recommended_next_step": "confirm_visit",
        "snapshot_meta": {
            "version": 2,
            "source_signal_id": "sig-current",
            "created_at": "2026-07-28T10:02:00+00:00",
        },
        "summary_text": "Aktualny versioned Hot State.",
        "status": "awaiting_review",
        "source_signal_id": "sig-current",
    }
    store.append_case_snapshot_version(
        {
            "snapshot_id": "snap-hot-2",
            "case_id": case_id,
            "version": 2,
            "source_signal_id": "sig-current",
            "confidence": 0.9,
            "snapshot_json": hot_state,
            "created_at": "2026-07-28T10:02:00+00:00",
        }
    )

    pack = build_case_context_pack(store=store, case_id=case_id)

    assert pack.snapshot["context_snapshot_status"] == "current"
    assert pack.snapshot["context_snapshot_source"] == "case_snapshot_hot_state"
    assert pack.snapshot["context_snapshot_version"] == 2
    assert pack.snapshot["summary_text"] == "Aktualny versioned Hot State."
    assert pack.snapshot["open_questions"] == ["Potwierdzić termin wizyty."]
    assert pack.snapshot["recommended_next_action"] == "confirm_visit"
    assert pack.snapshot["latest_signal_id"] == "sig-current"


def test_context_pack_rejects_foreign_hot_state_correlation() -> None:
    store = InMemoryMailboxMemoryStore()
    case_id = "case-local"
    store.upsert_case(_case_row(case_id))
    store.upsert_message(_message_row(case_id))
    store.append_case_snapshot_version(
        {
            "snapshot_id": "snap-foreign",
            "case_id": case_id,
            "version": 1,
            "source_signal_id": "sig-foreign",
            "snapshot_json": {
                "schema_version": "case_snapshot_hot_state.v1",
                "case": {
                    "case_id": "case-foreign",
                    "summary_text": "FOREIGN HOT STATE",
                },
                "recommended_next_step": "foreign_action",
            },
            "created_at": "2026-07-28T10:02:00+00:00",
        }
    )

    pack = build_case_context_pack(store=store, case_id=case_id)

    assert pack.snapshot["context_snapshot_source"] == "mailbox_memory_live_projection"
    assert pack.snapshot.get("summary_text") != "FOREIGN HOT STATE"
    assert pack.snapshot.get("recommended_next_action") != "foreign_action"


def test_active_v2_feed_ingest_returns_current_context_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DASZEK_FEED_SOURCE", "engagement_snapshot_v2")
    store = InMemoryMailboxMemoryStore()
    runtime = MailboxMemoryRuntime(store=store, blob_root=tmp_path, stage_mode="live")
    source = {
        "mailbox": "test@example.com",
        "source_message": {
            "message_id": "msg-ingest-current",
            "thread_id": "thread-ingest-current",
            "sender": "Jan Kowalski <jan@example.com>",
            "to": ["biuro@example.com"],
            "subject": "Aktualny ingest",
            "snippet": "Proszę o termin.",
            "body": "Proszę o potwierdzenie terminu wizyty.",
            "labels": ["INBOX"],
            "date": "2026-07-28T11:00:00+00:00",
            "attachments": [],
        },
    }

    result = runtime.ingest_message(
        snapshot=source,
        intake_result={"case_assessment": {"case_family": "lead_opportunity"}},
        case_link_result={"decision": "create_new", "selected_case_key": "CASE-INGEST-CURRENT"},
    )

    assert result.enabled is True
    assert store.fetch_snapshot(result.case_id) is None
    assert result.context_pack is not None
    assert result.context_pack.snapshot["context_snapshot_status"] == "current"
    assert result.context_pack.snapshot["recent_message_ids"] == ["msg-ingest-current"]
