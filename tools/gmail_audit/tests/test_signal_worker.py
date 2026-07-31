from __future__ import annotations

import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import Settings
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_worker import _apply_projection_refresh, _record_drive_result, _run_worker_idle_maintenance, run_signal_loop


def _settings() -> Settings:
    return Settings(
        llm_backend="groq",
        openai_compat_base_url="",
        openai_compat_api_key="",
        groq_api_key="gsk_test",
        google_access_token="tok",
        google_client_id="",
        google_client_secret="",
        google_refresh_token="",
        google_token_endpoint="https://oauth2.googleapis.com/token",
        google_oauth_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        groq_model="test-model",
        groq_native_model="test-model",
        groq_base_url="https://api.groq.com",
        daszek_base_url="",
        daszek_login="",
        daszek_password="",
        daszek_v2_push_enabled=False,
        daszek_operational_feed_auto_push_enabled=False,
        case_guidance_enabled=False,
        case_guidance_model="m",
        case_guidance_remote_state_enabled=False,
        attachment_extraction_enabled=True,
        attachment_extraction_max_bytes=8_000_000,
        mailbox_memory_database_url="postgres://unused",
        mailbox_memory_blob_root=Path("tools/gmail_audit/data/mailbox_memory/blobs"),
        mailbox_memory_stage_mode="shadow",
        mailbox_memory_stage_allowlist=(),
        google_drive_enabled=False,
        google_drive_credentials_path=None,
        google_drive_shared_drive_id="",
        google_drive_root_folder_id="",
        google_drive_batch_page_size=100,
        google_drive_max_download_bytes=10_000_000,
        google_drive_ingest_enabled=False,
        google_drive_graph_enabled=False,
        signal_runtime_mode="active",
        signal_journal_jsonl_mirror_enabled=False,
        gmail_change_detection_enabled=True,
        drive_change_detection_enabled=False,
        signal_worker_enabled=True,
        gmail_history_poll_interval_sec=10,
        drive_changes_poll_interval_sec=10,
        http_timeout=30,
        http_max_retries=2,
        http_retry_base_delay=1.0,
        env_path=None,
        config_sources={},
        config_warnings=[],
        google_access_token_had_bearer_prefix=False,
        google_runtime_access_token="",
        google_runtime_access_token_expires_at=0.0,
        google_runtime_token_type="",
        google_active_token_source="",
    )


class _FakeMailboxRuntime:
    def __init__(self, *, settings: Settings | None = None) -> None:
        self.store = InMemoryMailboxMemoryStore()
        self.graph_store = None
        self.settings = settings

    def bootstrap(self) -> None:
        self.store.bootstrap()


def test_run_signal_loop_keeps_active_mode_in_dry_run() -> None:
    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()
    captured_modes: list[str] = []

    def _fake_process_snapshot(*, settings: Settings, **_: object) -> bool:
        captured_modes.append(settings.signal_runtime_mode)
        return True

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=None,
    ), patch(
        "signal_worker.GmailChangeDetector.poll_changes",
        return_value={
            "status": "ok",
            "events": [
                {
                    "message_id": "msg-1",
                    "history_id": "1001",
                    "mailbox": "ops@example.com",
                    "observed_at": "2026-04-13T12:00:00+02:00",
                }
            ],
        },
    ), patch(
        "runtime_imports.read_email",
        return_value={
            "message_id": "msg-1",
            "thread_id": "thr-1",
            "date": "2026-04-13T11:59:00+02:00",
            "subject": "Test worker",
            "sender": "client@example.com",
            "to": ["ops@example.com"],
        },
    ), patch(
        "gmail_intake.fetch_context_messages",
        return_value=[],
    ), patch(
        "gmail_intake.process_snapshot",
        side_effect=_fake_process_snapshot,
    ):
        result = run_signal_loop(
            replace(settings, signal_runtime_mode="active"),
            loop_mode="oneshot",
            dry_run=True,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    assert result.runtime_mode == "active"
    assert result.dry_run is True
    assert result.gmail_event_count == 1
    assert result.gmail_processed_count == 1
    assert captured_modes == ["active"]
    assert result.run_state is not None
    assert result.run_state["manifest"]["daszek_push_requested"] is False


def test_record_drive_result_skips_duplicate_decision_churn() -> None:
    run_state = {
        "summary": {
            "items_processed": 0,
            "items_valid": 0,
            "processed_message_ids": [],
            "decision_distribution": Counter(),
        },
        "manifest": {"signal_runtime_mode": "active"},
        "artifacts": {"execution_metadata": "execution.jsonl"},
        "stage_records_path": "stage_records.jsonl",
    }
    reconcile_result = SimpleNamespace(
        signal_id="sig-dup",
        signal_kind="drive_document_added",
        source_kind="drive",
        processing_state="skipped_duplicate",
        stage_outputs={},
        v2_projection={},
    )
    processed = {
        "signal_runtime_result": SimpleNamespace(reconcile_result=reconcile_result),
        "document_row": {"drive_item_id": "drv-1"},
    }
    writes: list[dict[str, object]] = []

    with patch("signal_worker.append_jsonl", side_effect=lambda _path, payload: writes.append(dict(payload))):
        _record_drive_result(run_state=run_state, processed=processed)

    assert run_state["summary"]["items_processed"] == 1
    assert run_state["summary"]["items_valid"] == 0
    assert run_state["summary"]["decision_distribution"] == Counter()
    assert any(payload.get("processing_state") == "skipped_duplicate" for payload in writes)


def test_apply_projection_refresh_skips_duplicate_projection_push() -> None:
    run_state = {"manifest": {"daszek_v2_push_enabled": True}}
    reconcile_result = SimpleNamespace(
        processing_state="skipped_duplicate",
        projection_refresh_decision=SimpleNamespace(should_refresh=True),
        v2_projection={"signal_projection": {"message_key": "sig-dup"}},
        stage_outputs={},
    )
    processed = {"signal_runtime_result": SimpleNamespace(reconcile_result=reconcile_result)}

    with patch("v2_runtime.push_v2_projection_to_daszek") as push_mock, patch(
        "daszek_v3_feed_runtime.maybe_push_operational_feed_after_reconcile"
    ) as feed_mock:
        _apply_projection_refresh(run_state=run_state, processed=processed)

    push_mock.assert_not_called()
    feed_mock.assert_not_called()


def test_apply_projection_refresh_feed_when_v2_disabled() -> None:
    settings = _settings()
    settings.daszek_v2_push_enabled = False
    settings.daszek_operational_feed_auto_push_enabled = True
    run_state = {
        "manifest": {"daszek_v2_push_enabled": False, "daszek_operational_feed_auto_push_enabled": True},
        "mailbox_memory_runtime": SimpleNamespace(settings=settings),
    }
    reconcile_result = SimpleNamespace(
        signal_id="sig-1",
        processing_state="reconciled",
        projection_refresh_decision=SimpleNamespace(should_refresh=True),
        v2_projection={"signal_projection": {"message_key": "sig-1"}},
        stage_outputs={},
    )
    processed = {"signal_runtime_result": SimpleNamespace(reconcile_result=reconcile_result)}

    with patch("v2_runtime.push_v2_projection_to_daszek") as push_mock, patch(
        "daszek_v3_feed_runtime.maybe_push_operational_feed_after_reconcile"
    ) as feed_mock:
        _apply_projection_refresh(run_state=run_state, processed=processed)

    push_mock.assert_not_called()
    feed_mock.assert_called_once()


def test_signal_worker_gmail_item_failure_does_not_stop_loop() -> None:
    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()

    def _fake_read_email(*_args: object, message_id: str, **_kwargs: object) -> dict[str, object]:
        if message_id == "msg-1":
            raise RuntimeError("fixture gmail fetch failed")
        return {
            "message_id": message_id,
            "thread_id": f"thr-{message_id}",
            "date": "2026-04-13T11:59:00+02:00",
            "subject": "Test worker",
            "sender": "client@example.com",
            "to": ["ops@example.com"],
        }

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=None,
    ), patch(
        "signal_worker.GmailChangeDetector.poll_changes",
        return_value={
            "status": "ok",
            "events": [
                {"message_id": "msg-1", "history_id": "1001", "mailbox": "ops@example.com", "observed_at": "2026-04-13T12:00:00+02:00"},
                {"message_id": "msg-2", "history_id": "1002", "mailbox": "ops@example.com", "observed_at": "2026-04-13T12:01:00+02:00"},
            ],
        },
    ), patch(
        "runtime_imports.read_email",
        side_effect=_fake_read_email,
    ), patch(
        "gmail_intake.fetch_context_messages",
        return_value=[],
    ), patch(
        "gmail_intake.process_snapshot",
        return_value=True,
    ), patch(
        "signal_worker.append_jsonl",
        return_value=None,
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    assert result.stop_reason == ""
    assert result.gmail_event_count == 2
    assert result.gmail_processed_count == 1
    assert result.run_state is not None
    assert result.failed_item_count >= 1
    assert result.last_errors
    last = result.last_errors[-1]
    assert last["message_id"] == "msg-1"
    assert last["stage"] == "gmail_fetch"
    assert "fixture" in str(last["error"]).lower()
    result_payload = result.to_dict()
    assert result_payload["failed_item_count"] == result.failed_item_count
    assert result_payload["last_errors"] == result.last_errors
    assert result_payload["item_failures"] == result.last_errors
    assert result_payload["last_error_summary"] == result.last_errors[-1]


def test_signal_run_timebox_stops_oneshot_before_poll() -> None:
    """Wall-clock timebox exits cleanly with stop_reason (manifest finalized by gmail_intake signal-run)."""
    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()

    mono_calls = {"n": 0}

    def _fake_monotonic() -> float:
        mono_calls["n"] += 1
        if mono_calls["n"] == 1:
            return 0.0
        return 70.0

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=None,
    ), patch(
        "signal_worker.time.monotonic",
        side_effect=_fake_monotonic,
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
            max_messages=0,
            timebox_seconds=60,
        )

    assert result.stop_reason == "timebox_reached"
    assert result.run_state is not None
    summary = result.run_state.get("summary")
    assert isinstance(summary, dict)
    assert summary.get("stop_reason") == "timebox_reached"


def test_signal_run_max_messages_stops_oneshot_loop() -> None:
    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=None,
    ), patch(
        "signal_worker.GmailChangeDetector.poll_changes",
        return_value={
            "status": "ok",
            "events": [
                {"message_id": "msg-1", "history_id": "1001", "mailbox": "ops@example.com", "observed_at": "2026-04-13T12:00:00+02:00"},
                {"message_id": "msg-2", "history_id": "1002", "mailbox": "ops@example.com", "observed_at": "2026-04-13T12:01:00+02:00"},
            ],
        },
    ), patch(
        "runtime_imports.read_email",
        return_value={
            "message_id": "msg-1",
            "thread_id": "thr-1",
            "date": "2026-04-13T11:59:00+02:00",
            "subject": "Test worker",
            "sender": "client@example.com",
            "to": ["ops@example.com"],
        },
    ), patch(
        "gmail_intake.fetch_context_messages",
        return_value=[],
    ), patch(
        "gmail_intake.process_snapshot",
        return_value=True,
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
            max_messages=1,
            timebox_seconds=0,
        )

    assert result.stop_reason == "max_messages_reached"
    assert result.gmail_event_count == 2
    assert result.gmail_processed_count == 1


def test_signal_run_fatal_exception_is_captured_and_returns_result() -> None:
    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=None,
    ), patch(
        "gmail_intake.render_system_prompt",
        side_effect=RuntimeError("fixture render prompt failed"),
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
            max_messages=1,
            timebox_seconds=1,
        )

    assert result.stop_reason == "fatal_exception"
    assert result.run_state is not None

def test_signal_worker_drive_item_failure_does_not_stop_loop() -> None:
    settings = replace(
        _settings(),
        gmail_change_detection_enabled=False,
        drive_change_detection_enabled=True,
        google_drive_enabled=True,
        google_drive_ingest_enabled=True,
    )
    fake_runtime = _FakeMailboxRuntime()

    class _FakeDriveClient:
        def get_file_metadata(self, _file_id: str) -> dict[str, object]:
            return {"id": _file_id, "name": "x", "mimeType": "application/pdf"}

        def describe_item(self, metadata: dict[str, object], *, folder_path: str) -> dict[str, object]:
            file_id = str(metadata.get("id") or "")
            return {
                "drive_item_id": file_id,
                "title": str(metadata.get("name") or "doc"),
                "mime_type": str(metadata.get("mimeType") or "application/pdf"),
                "folder_path": folder_path,
            }

    class _FakeDriveRuntime:
        def __init__(self) -> None:
            self.client = _FakeDriveClient()

        def bootstrap(self) -> None:
            return None

        def process_removed_item(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"signal_runtime_result": SimpleNamespace(reconcile_result=None)}

        def process_candidate(self, candidate: object, *_args: object, **_kwargs: object) -> dict[str, object]:
            drive_item_id = getattr(candidate, "drive_item_id", "")
            if drive_item_id == "drv-1":
                raise RuntimeError("fixture drive processing failed")
            reconcile_result = SimpleNamespace(
                signal_id="sig-ok",
                signal_kind="drive_document_added",
                source_kind="drive",
                processing_state="reconciled",
                stage_outputs={},
                v2_projection={},
            )
            return {
                "signal_runtime_result": SimpleNamespace(reconcile_result=reconcile_result),
                "document_row": {"drive_item_id": drive_item_id},
            }

    fake_drive_runtime = _FakeDriveRuntime()

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=fake_drive_runtime,
    ), patch(
        "signal_worker.DriveChangeDetector.poll_changes",
        return_value={
            "status": "ok",
            "events": [
                {"file_id": "drv-1", "change_id": "chg-1", "removed": False, "observed_at": "2026-04-13T12:00:00+02:00", "metadata": {"id": "drv-1", "name": "A.pdf", "mimeType": "application/pdf"}},
                {"file_id": "drv-2", "change_id": "chg-2", "removed": False, "observed_at": "2026-04-13T12:01:00+02:00", "metadata": {"id": "drv-2", "name": "B.pdf", "mimeType": "application/pdf"}},
            ],
        },
    ), patch(
        "drive_signal_adapter.build_drive_signal_runtime_context",
        return_value=SimpleNamespace(),
    ), patch(
        "signal_worker._record_drive_result",
        return_value=None,
    ), patch(
        "signal_worker._apply_projection_refresh",
        return_value=None,
    ), patch(
        "signal_worker.append_jsonl",
        return_value=None,
    ), patch(
        "v2_runtime.push_v2_projection_to_daszek",
        return_value=None,
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    assert result.stop_reason == ""
    assert result.drive_event_count == 2
    assert result.drive_processed_count == 1
    assert result.run_state is not None
    assert result.failed_item_count >= 1
    assert result.last_errors
    last = result.last_errors[-1]
    assert last["message_id"] == "drv-1"
    assert last["stage"] == "drive_process"
    assert "fixture" in str(last["error"]).lower()


def test_signal_worker_fatal_config_failure_is_fail_fast() -> None:
    settings = replace(_settings(), signal_runtime_mode="legacy")
    fake_runtime = _FakeMailboxRuntime()
    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=None,
    ):
        try:
            run_signal_loop(
                settings,
                loop_mode="oneshot",
                dry_run=False,
                max_iterations=1,
                verbose=False,
                push_daszek=False,
            )
        except Exception as exc:
            assert exc.__class__.__name__ == "ConfigError"
        else:
            raise AssertionError("Expected ConfigError")


def test_signal_worker_keyboard_interrupt_is_intentional_stop_not_item_failure() -> None:
    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=None,
    ), patch(
        "signal_worker.GmailChangeDetector.poll_changes",
        return_value={"status": "ok", "events": [{"message_id": "msg-1", "history_id": "1001", "mailbox": "ops@example.com"}]},
    ), patch(
        "runtime_imports.read_email",
        side_effect=KeyboardInterrupt(),
    ), patch(
        "gmail_intake.fetch_context_messages",
        return_value=[],
    ), patch(
        "signal_worker.append_jsonl",
        return_value=None,
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    assert result.stop_reason == "keyboard_interrupt"
    assert result.run_state is not None
    assert result.failed_item_count == 0
    assert result.last_errors == []
    assert result.run_state["summary"]["items_failed"] == 0
    assert result.run_state["summary"].get("source_failure_count", 0) == 0
    assert result.run_state["summary"].get("projection_failure_count", 0) == 0
    assert result.run_state["summary"].get("projection_skipped_count", 0) == 0


def test_signal_worker_gmail_poll_failure_is_run_level_not_item_level() -> None:
    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=None,
    ), patch(
        "signal_worker.GmailChangeDetector.poll_changes",
        side_effect=RuntimeError("fixture gmail poll failed"),
    ), patch(
        "signal_worker.append_jsonl",
        return_value=None,
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    assert result.stop_reason in {"gmail_poll_failed", "max_consecutive_source_failures"}
    assert result.run_state is not None
    summary = result.run_state["summary"]
    assert summary["items_failed"] == 0
    assert summary["failed_items"] == []
    assert summary.get("source_failure_count", 0) >= 1
    last = summary.get("last_source_error_summary") or {}
    assert last.get("source_kind") == "gmail"
    assert last.get("stage") == "gmail_poll_changes"


def test_signal_worker_poll_retry_only_for_retryable_errors() -> None:
    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()
    calls: list[str] = []

    def _poll_side_effect(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("poll")
        if len(calls) == 1:
            raise RuntimeError("timeout while calling google api")
        return {"status": "ok", "events": []}

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=None,
    ), patch(
        "signal_worker.GmailChangeDetector.poll_changes",
        side_effect=_poll_side_effect,
    ), patch(
        "signal_worker.append_jsonl",
        return_value=None,
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    assert result.stop_reason == ""
    assert len(calls) == 2


def test_signal_worker_poll_no_retry_for_auth_error() -> None:
    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()
    calls: list[str] = []

    def _poll_side_effect(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("poll")
        raise RuntimeError("401 unauthorized")

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=None,
    ), patch(
        "signal_worker.GmailChangeDetector.poll_changes",
        side_effect=_poll_side_effect,
    ), patch(
        "signal_worker.append_jsonl",
        return_value=None,
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    assert result.stop_reason in {"gmail_poll_failed", "max_consecutive_source_failures"}
    assert len(calls) == 1


def test_signal_worker_runs_sla_watcher_during_idle_maintenance() -> None:
    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()

    run_state = {"summary": {}, "manifest": {"daszek_operational_feed_auto_push_enabled": False}}

    with patch(
        "sla_watcher.sla_watcher_oneshot",
        return_value={
            "ok": True,
            "violations": {"checked_at": "2026-07-31T06:00:00+00:00", "total_pending": 2},
            "escalated": 1,
        },
    ) as watcher_mock:
        _run_worker_idle_maintenance(
            run_state=run_state,
            settings=settings,
            mailbox_runtime=fake_runtime,
            iteration=1,
        )

    watcher_mock.assert_called_once()
    summary = run_state["summary"]
    assert summary["sla_watcher_tick_count"] == 1
    assert summary["last_sla_watcher_result"]["total_pending"] == 2
    assert summary["last_sla_watcher_result"]["escalated"] == 1


def test_signal_worker_drive_poll_failure_is_run_level_not_item_level() -> None:
    settings = replace(
        _settings(),
        gmail_change_detection_enabled=False,
        drive_change_detection_enabled=True,
        google_drive_enabled=True,
        google_drive_ingest_enabled=True,
    )
    fake_runtime = _FakeMailboxRuntime()

    class _FakeDriveRuntime:
        def __init__(self) -> None:
            self.client = object()

        def bootstrap(self) -> None:
            return None

    fake_drive_runtime = _FakeDriveRuntime()

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=fake_drive_runtime,
    ), patch(
        "signal_worker.DriveChangeDetector.poll_changes",
        side_effect=RuntimeError("fixture drive poll failed"),
    ), patch(
        "signal_worker.append_jsonl",
        return_value=None,
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    assert result.stop_reason in {"drive_poll_failed", "max_consecutive_source_failures"}
    assert result.run_state is not None
    summary = result.run_state["summary"]
    assert summary["items_failed"] == 0
    assert summary["failed_items"] == []
    assert summary.get("source_failure_count", 0) >= 1
    last = summary.get("last_source_error_summary") or {}
    assert last.get("source_kind") == "drive"
    assert last.get("stage") == "drive_poll_changes"


def test_signal_worker_projection_circuit_breaker_opens_and_skips() -> None:
    settings = replace(
        _settings(),
        gmail_change_detection_enabled=False,
        drive_change_detection_enabled=True,
        google_drive_enabled=True,
        google_drive_ingest_enabled=True,
    )
    fake_runtime = _FakeMailboxRuntime()

    class _FakeDriveClient:
        def get_file_metadata(self, _file_id: str) -> dict[str, object]:
            return {"id": _file_id, "name": "x", "mimeType": "application/pdf"}

        def describe_item(self, metadata: dict[str, object], *, folder_path: str) -> dict[str, object]:
            file_id = str(metadata.get("id") or "")
            return {
                "drive_item_id": file_id,
                "title": str(metadata.get("name") or "doc"),
                "mime_type": str(metadata.get("mimeType") or "application/pdf"),
                "folder_path": folder_path,
            }

    class _FakeDriveRuntime:
        def __init__(self) -> None:
            self.client = _FakeDriveClient()

        def bootstrap(self) -> None:
            return None

        def process_removed_item(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"signal_runtime_result": SimpleNamespace(reconcile_result=None)}

        def process_candidate(self, candidate: object, *_args: object, **_kwargs: object) -> dict[str, object]:
            drive_item_id = getattr(candidate, "drive_item_id", "")
            reconcile_result = SimpleNamespace(
                signal_id=f"sig-{drive_item_id}",
                signal_kind="drive_document_added",
                source_kind="drive",
                processing_state="reconciled",
                stage_outputs={},
                v2_projection={},
            )
            return {
                "signal_runtime_result": SimpleNamespace(reconcile_result=reconcile_result),
                "document_row": {"drive_item_id": drive_item_id},
            }

    fake_drive_runtime = _FakeDriveRuntime()
    events = [
        {"file_id": "drv-1", "change_id": "chg-1", "removed": False, "metadata": {"id": "drv-1", "name": "A.pdf", "mimeType": "application/pdf"}},
        {"file_id": "drv-2", "change_id": "chg-2", "removed": False, "metadata": {"id": "drv-2", "name": "B.pdf", "mimeType": "application/pdf"}},
        {"file_id": "drv-3", "change_id": "chg-3", "removed": False, "metadata": {"id": "drv-3", "name": "C.pdf", "mimeType": "application/pdf"}},
    ]

    calls: list[str] = []

    def _boom(*_args: object, **_kwargs: object) -> None:
        calls.append("push")
        raise RuntimeError("fixture projection offline")

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=fake_drive_runtime,
    ), patch(
        "signal_worker.DriveChangeDetector.poll_changes",
        return_value={"status": "ok", "events": events},
    ), patch(
        "drive_signal_adapter.build_drive_signal_runtime_context",
        return_value=SimpleNamespace(),
    ), patch(
        "signal_worker._record_drive_result",
        return_value=None,
    ), patch(
        "signal_worker._drive_message_key",
        side_effect=RuntimeError("no message key"),
    ), patch(
        "signal_worker._apply_projection_refresh",
        side_effect=_boom,
    ), patch(
        "signal_worker.append_jsonl",
        return_value=None,
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    assert result.stop_reason == ""
    assert result.drive_event_count == 3
    assert result.drive_processed_count == 3
    assert result.run_state is not None
    summary = result.run_state["summary"]
    assert summary.get("projection_failure_count", 0) >= 2
    assert summary.get("projection_circuit_open") is True
    assert summary.get("projection_skipped_count", 0) >= 1
    # first two attempts fail, then circuit opens and skip suppresses further calls
    assert len(calls) == 2
    rollup = summary.get("run_level_error_rollup") or {}
    assert any("projection_v2_push" in key for key in rollup.keys())


def test_signal_worker_projection_circuit_opens_even_with_message_key() -> None:
    settings = replace(
        _settings(),
        gmail_change_detection_enabled=False,
        drive_change_detection_enabled=True,
        google_drive_enabled=True,
        google_drive_ingest_enabled=True,
    )
    fake_runtime = _FakeMailboxRuntime()

    class _FakeDriveClient:
        def get_file_metadata(self, _file_id: str) -> dict[str, object]:
            return {"id": _file_id, "name": "x", "mimeType": "application/pdf"}

        def describe_item(self, metadata: dict[str, object], *, folder_path: str) -> dict[str, object]:
            file_id = str(metadata.get("id") or "")
            return {
                "drive_item_id": file_id,
                "title": str(metadata.get("name") or "doc"),
                "mime_type": str(metadata.get("mimeType") or "application/pdf"),
                "folder_path": folder_path,
            }

    class _FakeDriveRuntime:
        def __init__(self) -> None:
            self.client = _FakeDriveClient()

        def bootstrap(self) -> None:
            return None

        def process_removed_item(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"signal_runtime_result": SimpleNamespace(reconcile_result=None)}

        def process_candidate(self, candidate: object, *_args: object, **_kwargs: object) -> dict[str, object]:
            drive_item_id = getattr(candidate, "drive_item_id", "")
            reconcile_result = SimpleNamespace(
                signal_id=f"sig-{drive_item_id}",
                signal_kind="drive_document_added",
                source_kind="drive",
                processing_state="reconciled",
                stage_outputs={},
                v2_projection={},
            )
            return {
                "signal_runtime_result": SimpleNamespace(reconcile_result=reconcile_result),
                "document_row": {"drive_item_id": drive_item_id},
            }

    fake_drive_runtime = _FakeDriveRuntime()
    events = [
        {"file_id": "drv-1", "change_id": "chg-1", "removed": False, "metadata": {"id": "drv-1", "name": "A.pdf", "mimeType": "application/pdf"}},
        {"file_id": "drv-2", "change_id": "chg-2", "removed": False, "metadata": {"id": "drv-2", "name": "B.pdf", "mimeType": "application/pdf"}},
        {"file_id": "drv-3", "change_id": "chg-3", "removed": False, "metadata": {"id": "drv-3", "name": "C.pdf", "mimeType": "application/pdf"}},
    ]
    calls: list[str] = []

    def _boom(*_args: object, **_kwargs: object) -> None:
        calls.append("push")
        raise RuntimeError("503 service unavailable")

    with patch("signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime",
        return_value=fake_drive_runtime,
    ), patch(
        "signal_worker.DriveChangeDetector.poll_changes",
        return_value={"status": "ok", "events": events},
    ), patch(
        "drive_signal_adapter.build_drive_signal_runtime_context",
        return_value=SimpleNamespace(),
    ), patch(
        "signal_worker._record_drive_result",
        return_value=None,
    ), patch(
        "signal_worker._drive_message_key",
        side_effect=lambda processed: f"sig-{processed.get('document_row', {}).get('drive_item_id', '')}",
    ), patch(
        "signal_worker._apply_projection_refresh",
        side_effect=_boom,
    ), patch(
        "signal_worker.append_jsonl",
        return_value=None,
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    assert result.stop_reason == ""
    assert len(calls) == 2
    assert result.run_state is not None
    summary = result.run_state["summary"]
    assert summary.get("projection_circuit_open") is True
    assert summary.get("projection_circuit_fingerprint")
    assert summary.get("projection_disabled_for_run_reason") in {"server_5xx", "network", "timeout", "throttle", "unknown", "auth", "bad_request", "config"}
