"""FG-04 — worker path proof for Follow-up Guardian.

Proof level: unit / Gate A with mocks.
Live `signal_worker` process invoking guardian against real Postgres is NOT proven here.

Covers:
* `_maybe_run_follow_up_guardian_tick` calls `follow_up_guardian_oneshot` and records summary
* throttle holds a second call inside `_FOLLOW_UP_GUARDIAN_INTERVAL_SEC`
* `_run_worker_idle_maintenance` invokes the guardian tick
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

from config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_worker import (  # noqa: E402
    _FOLLOW_UP_GUARDIAN_INTERVAL_SEC,
    _maybe_run_follow_up_guardian_tick,
    _run_worker_idle_maintenance,
)


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
    store = object()


def test_maybe_run_follow_up_guardian_tick_invokes_oneshot_and_records_summary() -> None:
    settings = _settings()
    run_state: dict = {"summary": {}}
    oneshot_result = {"ok": True, "checked": 3, "proposed_count": 1}

    with patch(
        "follow_up_guardian.follow_up_guardian_oneshot",
        return_value=oneshot_result,
    ) as oneshot_mock:
        _maybe_run_follow_up_guardian_tick(run_state=run_state, settings=settings)

    oneshot_mock.assert_called_once_with(settings)
    summary = run_state["summary"]
    assert summary["follow_up_guardian_tick_count"] == 1
    assert summary["last_follow_up_guardian_result"] == {
        "ok": True,
        "checked": 3,
        "proposed_count": 1,
    }
    assert float(summary["last_follow_up_guardian_monotonic"]) > 0


def test_maybe_run_follow_up_guardian_tick_throttles_within_interval() -> None:
    settings = _settings()
    now = time.monotonic()
    run_state = {
        "summary": {
            "last_follow_up_guardian_monotonic": now,
            "follow_up_guardian_tick_count": 0,
        }
    }

    with patch(
        "follow_up_guardian.follow_up_guardian_oneshot",
        return_value={"ok": True, "checked": 0, "proposed_count": 0},
    ) as oneshot_mock:
        _maybe_run_follow_up_guardian_tick(run_state=run_state, settings=settings)

    oneshot_mock.assert_not_called()
    assert run_state["summary"]["follow_up_guardian_tick_count"] == 0


def test_maybe_run_follow_up_guardian_tick_runs_after_interval_elapsed() -> None:
    settings = _settings()
    stale = time.monotonic() - (_FOLLOW_UP_GUARDIAN_INTERVAL_SEC + 1)
    run_state = {"summary": {"last_follow_up_guardian_monotonic": stale}}

    with patch(
        "follow_up_guardian.follow_up_guardian_oneshot",
        return_value={"ok": True, "checked": 1, "proposed_count": 0},
    ) as oneshot_mock:
        _maybe_run_follow_up_guardian_tick(run_state=run_state, settings=settings)

    oneshot_mock.assert_called_once()
    assert run_state["summary"]["follow_up_guardian_tick_count"] == 1


def test_maybe_run_follow_up_guardian_tick_records_oneshot_failure() -> None:
    settings = _settings()
    run_state: dict = {"summary": {}}

    with patch(
        "follow_up_guardian.follow_up_guardian_oneshot",
        return_value={"ok": False, "error": "Database not configured."},
    ):
        _maybe_run_follow_up_guardian_tick(run_state=run_state, settings=settings)

    summary = run_state["summary"]
    assert summary["follow_up_guardian_tick_count"] == 1
    assert summary["follow_up_guardian_error_count"] == 1
    assert "Database not configured" in summary["last_follow_up_guardian_error"]


def test_maybe_run_follow_up_guardian_tick_records_exception() -> None:
    settings = _settings()
    run_state: dict = {"summary": {}}

    with patch(
        "follow_up_guardian.follow_up_guardian_oneshot",
        side_effect=RuntimeError("fixture guardian boom"),
    ):
        _maybe_run_follow_up_guardian_tick(run_state=run_state, settings=settings)

    summary = run_state["summary"]
    assert summary["follow_up_guardian_error_count"] == 1
    assert "fixture guardian boom" in summary["last_follow_up_guardian_error"]
    assert "follow_up_guardian_tick_count" not in summary


def test_worker_idle_maintenance_invokes_follow_up_guardian() -> None:
    settings = _settings()
    run_state = {
        "summary": {},
        "manifest": {"daszek_operational_feed_auto_push_enabled": False},
    }

    with patch(
        "sla_watcher.sla_watcher_oneshot",
        return_value={"ok": True, "violations": {}, "escalated": 0},
    ), patch(
        "follow_up_guardian.follow_up_guardian_oneshot",
        return_value={"ok": True, "checked": 2, "proposed_count": 1},
    ) as guardian_mock:
        _run_worker_idle_maintenance(
            run_state=run_state,
            settings=settings,
            mailbox_runtime=_FakeMailboxRuntime(),
            iteration=1,
        )

    guardian_mock.assert_called_once_with(settings)
    summary = run_state["summary"]
    assert summary["follow_up_guardian_tick_count"] == 1
    assert summary["last_follow_up_guardian_result"]["proposed_count"] == 1
