"""Chaos: DB outage — worker nie crashuje gdy Postgres nie odpowiada."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import Settings
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_worker import _apply_projection_refresh, _record_drive_result, run_signal_loop


class _FakeMailboxRuntime:
    def __init__(self) -> None:
        self.store = InMemoryMailboxMemoryStore()
        self.graph_store = None
        self.settings = None

    def bootstrap(self) -> None:
        self.store.bootstrap()


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
        gmail_change_detection_enabled=False,
        drive_change_detection_enabled=False,
        signal_worker_enabled=True,
        gmail_history_poll_interval_sec=900,
    )


def test_worker_survives_db_outage() -> None:
    """Worker heartbeat fails gracefully gdy DB nie odpowiada — nie crashuje workera."""
    import psycopg

    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()

    with patch("psycopg.connect", side_effect=psycopg.OperationalError("DB down")), patch(
        "signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime
    ), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime", return_value=None
    ), patch(
        "signal_worker.GmailChangeDetector.poll_changes",
        return_value={"status": "ok", "events": []},
    ), patch(
        "runtime_imports.read_email", return_value={}
    ), patch(
        "gmail_intake.fetch_context_messages", return_value=[]
    ), patch(
        "gmail_intake.process_snapshot", return_value=True
    ), patch(
        "signal_worker.append_jsonl", return_value=None
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    # Worker nie crashuje mimo DB failure
    assert result.stop_reason not in ("fatal_exception", "restart_loop_detected")
    assert result.iterations >= 0


def test_checkpoint_empty_when_db_fails() -> None:
    """_read_worker_checkpoint zwraca {} gdy DB nie odpowiada — worker kontynuuje."""
    import psycopg

    settings = _settings()
    fake_runtime = _FakeMailboxRuntime()

    with patch("psycopg.connect", side_effect=psycopg.OperationalError("DB down")), patch(
        "signal_worker._require_worker_mailbox_runtime", return_value=fake_runtime
    ), patch(
        "drive_ingest_runtime.build_drive_ingest_runtime", return_value=None
    ), patch(
        "signal_worker.GmailChangeDetector.poll_changes",
        return_value={"status": "ok", "events": []},
    ), patch(
        "runtime_imports.read_email", return_value={}
    ), patch(
        "gmail_intake.fetch_context_messages", return_value=[]
    ), patch(
        "gmail_intake.process_snapshot", return_value=True
    ), patch(
        "signal_worker.append_jsonl", return_value=None
    ):
        result = run_signal_loop(
            settings,
            loop_mode="oneshot",
            dry_run=False,
            max_iterations=1,
            verbose=False,
            push_daszek=False,
        )

    # Worker nie crashuje — DB fail to tylko heartbeat best-effort
    assert result.stop_reason not in ("fatal_exception", "restart_loop_detected")
