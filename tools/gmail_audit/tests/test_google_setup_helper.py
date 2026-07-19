from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
SCRIPTS_DIR = TOOL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import Settings
from google_setup_helper import (
    build_authorization_url,
    parse_drive_folder_id,
    parse_oauth_callback_request_path,
    run_local_oauth_listen,
    upsert_env_value,
)


def _base_settings(**overrides: object) -> Settings:
    base = {
        "llm_backend": "groq",
        "openai_compat_base_url": "",
        "openai_compat_api_key": "",
        "groq_api_key": "",
        "google_access_token": "",
        "google_client_id": "client-id.apps.googleusercontent.com",
        "google_client_secret": "client-secret",
        "google_refresh_token": "",
        "google_token_endpoint": "https://oauth2.googleapis.com/token",
        "google_oauth_scopes": (
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ),
        "groq_model": "openai/gpt-oss-120b",
        "groq_base_url": "https://api.groq.com",
        "daszek_base_url": "",
        "daszek_login": "",
        "daszek_password": "",
        "daszek_v2_push_enabled": False,
        "case_guidance_enabled": False,
        "case_guidance_model": "openai/gpt-oss-120b",
        "case_guidance_remote_state_enabled": True,
        "attachment_extraction_enabled": True,
        "attachment_extraction_max_bytes": 8_000_000,
        "mailbox_memory_database_url": "",
        "mailbox_memory_blob_root": Path("tools/gmail_audit/data/mailbox_memory/blobs"),
        "mailbox_memory_stage_mode": "disabled",
        "mailbox_memory_stage_allowlist": (),
        "google_drive_enabled": True,
        "google_drive_credentials_path": None,
        "google_drive_shared_drive_id": "",
        "google_drive_root_folder_id": "",
        "google_drive_batch_page_size": 100,
        "google_drive_max_download_bytes": 10_000_000,
        "google_drive_ingest_enabled": True,
        "google_drive_graph_enabled": False,
        "http_timeout": 60,
        "http_max_retries": 4,
        "http_retry_base_delay": 2.0,
        "env_path": Path("tools/gmail_audit/.env"),
        "config_sources": {},
        "config_warnings": [],
        "google_access_token_had_bearer_prefix": False,
        "google_runtime_access_token": "",
        "google_runtime_access_token_expires_at": 0.0,
        "google_runtime_token_type": "",
        "google_active_token_source": "",
    }
    base.update(overrides)
    return Settings(**base)


def test_build_authorization_url_includes_required_google_params() -> None:
    settings = _base_settings()

    auth_url, state = build_authorization_url(
        settings,
        redirect_uri="http://127.0.0.1:8765/callback",
        state="known-state",
    )

    parsed = urlparse(auth_url)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert state == "known-state"
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["redirect_uri"] == ["http://127.0.0.1:8765/callback"]
    assert "https://www.googleapis.com/auth/drive.readonly" in params["scope"][0]


def test_parse_drive_folder_id_accepts_raw_id_and_url() -> None:
    folder_id = "1GfQKAYc8lHKOsCjH9vYErct19tgprvsH"

    assert parse_drive_folder_id(folder_id) == folder_id
    assert (
        parse_drive_folder_id(f"https://drive.google.com/drive/folders/{folder_id}")
        == folder_id
    )


def test_parse_oauth_callback_request_path_extracts_code_and_state() -> None:
    redirect = "http://127.0.0.1:8765/callback"
    parsed = parse_oauth_callback_request_path(
        "/callback?code=abc123&state=known-state",
        redirect_uri=redirect,
    )
    assert parsed.get("path_mismatch") is None
    assert parsed["code"] == "abc123"
    assert parsed["state"] == "known-state"
    assert parsed.get("error") is None


def test_parse_oauth_callback_request_path_reports_path_mismatch() -> None:
    redirect = "http://127.0.0.1:8765/callback"
    parsed = parse_oauth_callback_request_path("/wrong?code=x", redirect_uri=redirect)
    assert parsed.get("path_mismatch") == "1"


def test_parse_oauth_callback_request_path_reads_error() -> None:
    redirect = "http://127.0.0.1:8765/callback"
    parsed = parse_oauth_callback_request_path(
        "/callback?error=access_denied&state=s",
        redirect_uri=redirect,
    )
    assert parsed["error"] == "access_denied"
    assert parsed.get("code") is None


def test_upsert_env_value_replaces_existing_line() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = Path(tmpdir) / ".env"
        env_path.write_text(
            "GOOGLE_DRIVE_ROOT_FOLDER_ID=old-folder\nMAILBOX_MEMORY_STAGE_MODE=shadow\n",
            encoding="utf-8",
        )

        upsert_env_value(env_path, "GOOGLE_DRIVE_ROOT_FOLDER_ID", "new-folder")

        rendered = env_path.read_text(encoding="utf-8")

    assert "GOOGLE_DRIVE_ROOT_FOLDER_ID=new-folder" in rendered
    assert "MAILBOX_MEMORY_STAGE_MODE=shadow" in rendered


def test_upsert_env_value_dedupes_duplicate_keys() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = Path(tmpdir) / ".env"
        env_path.write_text(
            "GOOGLE_REFRESH_TOKEN=first\nGOOGLE_REFRESH_TOKEN=second\nOTHER=1\n",
            encoding="utf-8",
        )
        upsert_env_value(env_path, "GOOGLE_REFRESH_TOKEN", "merged")
        rendered = env_path.read_text(encoding="utf-8").splitlines()
    assert rendered.count("GOOGLE_REFRESH_TOKEN=merged") == 1
    assert "OTHER=1" in rendered


def test_local_oauth_writes_auth_url_file_before_timeout() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        auth_url_path = Path(tmpdir) / "auth-url.json"
        settings = _base_settings(env_path=Path(tmpdir) / ".env")
        payload = run_local_oauth_listen(
            settings,
            redirect_uri="http://127.0.0.1:18765/callback",
            state="fixed-state",
            auth_url_file=auth_url_path,
            write_env=False,
            open_browser_flag=False,
            timeout_sec=0.01,
        )

        rendered = auth_url_path.read_text(encoding="utf-8")

    assert payload["status"] == "failed"
    assert "waiting_for_browser" in rendered
    assert "fixed-state" in rendered
    assert "http://127.0.0.1:18765/callback" in rendered
