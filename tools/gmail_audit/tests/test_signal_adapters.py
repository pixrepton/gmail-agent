from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from drive_signal_adapter import (
    build_drive_raw_observation,
    build_drive_signals,
    build_drive_signal_runtime_context,
    run_drive_signal_runtime,
)
from calendar_signal_adapter import build_calendar_signal
from gmail_signal_adapter import build_gmail_raw_observation, build_gmail_signals, run_gmail_signal_runtime
from mailbox_memory_store import InMemoryMailboxMemoryStore
from observation_triage import triage_drive_observation


def _signal_settings(mode: str = "shadow") -> SimpleNamespace:
    return SimpleNamespace(
        mailbox_memory_blob_root=Path(tempfile.gettempdir()) / "signal-runtime-tests",
        signal_journal_jsonl_mirror_enabled=False,
        signal_runtime_mode=mode,
        groq_model="test-model",
    )


def test_build_gmail_signals_emits_message_thread_and_attachment_shapes() -> None:
    snapshot = {
        "mailbox": "ops@example.com",
        "observed_at": "2026-04-13T09:00:00+02:00",
        "source_message": {
            "message_id": "msg-1",
            "thread_id": "thr-1",
            "history_id": "777",
            "date": "2026-04-13T08:59:00+02:00",
            "subject": "Nowa wiadomosc",
            "attachment_parts": [
                {"attachment_id": "att-1", "name": "invoice.pdf"},
            ],
        },
    }
    raw_observation = build_gmail_raw_observation(snapshot=snapshot, created_by_runtime="test")
    signals = build_gmail_signals(
        snapshot=snapshot,
        intake_result_final={"decision": {"action": "review"}},
        preclassification_result={"lane": "finance"},
        lane_stage_plan={"run_case_linking": True},
        context_bundle={"context_messages": [{"message_id": "ctx-1"}]},
        raw_observation=raw_observation,
        created_by_runtime="test",
    )

    assert [item.signal_kind for item in signals] == [
        "gmail_message_observed",
        "gmail_thread_update_observed",
        "gmail_attachment_observed",
    ]
    assert signals[0].source_ref["history_id"] == "777"
    assert signals[2].source_ref["attachment_id"] == "att-1"
    assert signals[0].artifacts["raw_observation_id"] == raw_observation.observation_id


def test_run_gmail_signal_runtime_skips_duplicate_primary_signal() -> None:
    settings = _signal_settings(mode="shadow")
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    run_state = {"signal_store": store}
    snapshot = {
        "mailbox": "ops@example.com",
        "observed_at": "2026-04-13T09:00:00+02:00",
        "source_message": {
            "message_id": "msg-dup",
            "thread_id": "thr-dup",
            "history_id": "901",
            "date": "2026-04-13T08:58:00+02:00",
            "subject": "Duplicate check",
            "attachment_parts": [],
        },
    }
    fake_reconcile = SimpleNamespace(
        signal_id="sig-primary",
        source_kind="gmail",
        signal_kind="gmail_message_observed",
        processing_state="shadowed",
        mailbox_memory_result={},
    )
    with patch("gmail_signal_adapter.reconcile_signal", return_value=fake_reconcile):
        first = run_gmail_signal_runtime(
            settings=settings,
            run_state=run_state,
            snapshot=snapshot,
            intake_result_final={"decision": {"action": "review"}},
            preclassification_result={"lane": "finance"},
            lane_stage_plan={"run_case_linking": True},
            context_bundle={"context_messages": []},
            model="m",
            verbose=False,
            dry_run=True,
        )
        second = run_gmail_signal_runtime(
            settings=settings,
            run_state=run_state,
            snapshot=snapshot,
            intake_result_final={"decision": {"action": "review"}},
            preclassification_result={"lane": "finance"},
            lane_stage_plan={"run_case_linking": True},
            context_bundle={"context_messages": []},
            model="m",
            verbose=False,
            dry_run=True,
        )

    assert first.append_results[0].inserted is True
    assert second.append_results[0].inserted is False
    assert second.reconcile_result.processing_state == "skipped_duplicate"


def test_build_drive_signals_emits_primary_extract_link_and_conflict_signals() -> None:
    payload = {
        "document_row": {
            "file_name": "invoice.pdf",
            "probable_case_key": "case-key-1",
            "lane": "finance",
        },
        "fact_rows": [{"fact_key": "invoice_number", "normalized_value": "FV-12"}],
        "case_id": "case-1",
        "case_key": "case-key-1",
        "linkage_status": "deterministic",
        "link_reasons": ["invoice_number_match"],
        "conflicts": ["Drive fact conflict for amount"],
    }
    raw_observation = build_drive_raw_observation(
        source_ref={"file_id": "drv-1", "change_id": "chg-1", "revision_id": "rev-1", "modified_time": "2026-04-13T08:00:00+02:00"},
        observed_at="2026-04-13T08:01:00+02:00",
        payload={"candidate": {"drive_item_id": "drv-1", "title": "invoice.pdf"}},
        created_by_runtime="test",
    )
    signals = build_drive_signals(
        change_kind="drive_document_added",
        source_ref={"file_id": "drv-1", "change_id": "chg-1", "revision_id": "rev-1", "modified_time": "2026-04-13T08:00:00+02:00"},
        observed_at="2026-04-13T08:01:00+02:00",
        signal_summary_pl="Nowa faktura Drive",
        payload=payload,
        raw_observation=raw_observation,
        created_by_runtime="test",
    )

    assert [item.signal_kind for item in signals] == [
        "drive_document_added",
        "drive_extraction_completed",
        "drive_document_link_candidate",
        "drive_conflict_detected",
    ]
    assert signals[0].case_key_hint == "case-key-1"
    assert signals[-1].signal_summary_pl == "Drive fact conflict for amount"
    assert signals[0].artifacts["raw_observation_id"] == raw_observation.observation_id


def test_build_calendar_signal_without_case_uses_empty_store_hints() -> None:
    signal = build_calendar_signal(
        source_ref={"calendar_id": "primary", "calendar_event_id": "evt-1"},
        observed_at="2026-04-24T20:00:00+00:00",
        payload={
            "calendar_event_id": "evt-1",
            "source": "google_calendar",
            "summary": "Serwis",
            "start_at": "2026-04-25T08:00:00+02:00",
            "case_id": "",
        },
        created_by_runtime="test",
    )

    assert signal.case_key_hint == ""
    assert signal.thread_key_hint == ""


def test_run_drive_signal_runtime_skips_duplicate_primary_signal() -> None:
    settings = _signal_settings(mode="active")
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    context = build_drive_signal_runtime_context(settings=settings, store=store, graph_store=None)
    fake_reconcile = SimpleNamespace(
        signal_id="sig-drive",
        source_kind="drive",
        signal_kind="drive_document_added",
        processing_state="reconciled",
        mailbox_memory_result={"events": []},
    )
    with patch("drive_signal_adapter.reconcile_signal", return_value=fake_reconcile):
        first = run_drive_signal_runtime(
            settings=settings,
            runtime_context=context,
            change_kind="drive_document_added",
            source_ref={"file_id": "drv-dup", "change_id": "chg-dup", "revision_id": "rev-dup", "modified_time": "2026-04-13T08:00:00+02:00"},
            observed_at="2026-04-13T08:05:00+02:00",
            signal_summary_pl="Drive duplicate test",
            payload={"document_row": {"file_name": "invoice.pdf", "lane": "finance"}},
            dry_run=False,
        )
        second = run_drive_signal_runtime(
            settings=settings,
            runtime_context=context,
            change_kind="drive_document_added",
            source_ref={"file_id": "drv-dup", "change_id": "chg-dup", "revision_id": "rev-dup", "modified_time": "2026-04-13T08:00:00+02:00"},
            observed_at="2026-04-13T08:05:00+02:00",
            signal_summary_pl="Drive duplicate test",
            payload={"document_row": {"file_name": "invoice.pdf", "lane": "finance"}},
            dry_run=False,
        )

    assert first.append_results[0].inserted is True
    assert second.append_results[0].inserted is False
    assert second.reconcile_result.processing_state == "skipped_duplicate"


def test_build_drive_signals_batches_case_media_assets_into_one_primary_signal_window() -> None:
    source_ref_a = {
        "file_id": "drv-media-1",
        "change_id": "chg-media-1",
        "revision_id": "rev-media-1",
        "modified_time": "2026-04-15T08:01:00+02:00",
        "parent_drive_item_id": "folder-77",
        "source_ref": "gdrive://drv-media-1",
    }
    source_ref_b = {
        "file_id": "drv-media-2",
        "change_id": "chg-media-2",
        "revision_id": "rev-media-2",
        "modified_time": "2026-04-15T08:02:10+02:00",
        "parent_drive_item_id": "folder-77",
        "source_ref": "gdrive://drv-media-2",
    }
    payload_a = {
        "candidate": {
            "drive_item_id": "drv-media-1",
            "title": "photo-1.jpg",
            "mime_type": "image/jpeg",
            "folder_path": "case-folder/photos",
            "parent_drive_item_id": "folder-77",
            "source_ref": "gdrive://drv-media-1",
            "is_folder": False,
            "size_bytes": 512,
            "modified_time": "2026-04-15T08:01:00+02:00",
            "lane": "case_folder",
            "document_kind": "media_asset",
            "scope": "case_specific",
            "probable_case_key": "case-key-1",
            "classification_confidence": 0.97,
        },
        "document_row": {
            "file_name": "photo-1.jpg",
            "probable_case_key": "case-key-1",
            "lane": "case_folder",
            "document_kind": "media_asset",
        },
    }
    payload_b = {
        "candidate": {
            "drive_item_id": "drv-media-2",
            "title": "photo-2.jpg",
            "mime_type": "image/jpeg",
            "folder_path": "case-folder/photos",
            "parent_drive_item_id": "folder-77",
            "source_ref": "gdrive://drv-media-2",
            "is_folder": False,
            "size_bytes": 512,
            "modified_time": "2026-04-15T08:02:10+02:00",
            "lane": "case_folder",
            "document_kind": "media_asset",
            "scope": "case_specific",
            "probable_case_key": "case-key-1",
            "classification_confidence": 0.97,
        },
        "document_row": {
            "file_name": "photo-2.jpg",
            "probable_case_key": "case-key-1",
            "lane": "case_folder",
            "document_kind": "media_asset",
        },
    }
    raw_observation_a = build_drive_raw_observation(
        source_ref=source_ref_a,
        observed_at="2026-04-15T08:01:00+02:00",
        payload=payload_a,
        created_by_runtime="test",
    )
    raw_observation_b = build_drive_raw_observation(
        source_ref=source_ref_b,
        observed_at="2026-04-15T08:02:10+02:00",
        payload=payload_b,
        created_by_runtime="test",
    )
    triage_a = triage_drive_observation(raw_observation_a)
    triage_b = triage_drive_observation(raw_observation_b)

    signals_a = build_drive_signals(
        change_kind="drive_document_added",
        source_ref=source_ref_a,
        observed_at="2026-04-15T08:01:00+02:00",
        signal_summary_pl="Media update A",
        payload=payload_a,
        raw_observation=raw_observation_a,
        triage_result=triage_a,
        created_by_runtime="test",
    )
    signals_b = build_drive_signals(
        change_kind="drive_document_added",
        source_ref=source_ref_b,
        observed_at="2026-04-15T08:02:10+02:00",
        signal_summary_pl="Media update B",
        payload=payload_b,
        raw_observation=raw_observation_b,
        triage_result=triage_b,
        created_by_runtime="test",
    )

    assert len(signals_a) == 1
    assert len(signals_b) == 1
    assert signals_a[0].signal_kind == "drive_media_batch_observed"
    assert signals_a[0].signal_id == signals_b[0].signal_id
