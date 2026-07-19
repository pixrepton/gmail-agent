"""Unit tests for Gmail history.list helper (R3 spike surface)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import Settings
from google_gmail_api import get_message_metadata, list_history, search_email_metadata


def _minimal_settings() -> Settings:
    return Settings(
        llm_backend="groq",
        openai_compat_base_url="",
        openai_compat_api_key="",
        groq_api_key="",
        google_access_token="tok",
        google_client_id="",
        google_client_secret="",
        google_refresh_token="",
        google_token_endpoint="https://oauth2.googleapis.com/token",
        google_oauth_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        groq_model="m",
        groq_base_url="https://api.groq.com",
        daszek_base_url="",
        daszek_login="",
        daszek_password="",
        daszek_v2_push_enabled=False,
        case_guidance_enabled=False,
        case_guidance_model="m",
        case_guidance_remote_state_enabled=True,
        attachment_extraction_enabled=True,
        attachment_extraction_max_bytes=8_000_000,
        mailbox_memory_database_url="",
        mailbox_memory_blob_root=Path("tools/gmail_audit/data/mailbox_memory/blobs"),
        mailbox_memory_stage_mode="disabled",
        mailbox_memory_stage_allowlist=(),
        google_drive_enabled=False,
        google_drive_credentials_path=None,
        google_drive_shared_drive_id="",
        google_drive_root_folder_id="",
        google_drive_batch_page_size=100,
        google_drive_max_download_bytes=10_000_000,
        google_drive_ingest_enabled=False,
        google_drive_graph_enabled=False,
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


def test_list_history_builds_query_params() -> None:
    settings = _minimal_settings()
    with patch("google_gmail_api._gmail_get_json") as mock_get:
        mock_get.return_value = {"history": [], "historyId": "999"}
        out = list_history(
            settings,
            start_history_id="12345",
            max_results=50,
            page_token="ptok",
            history_types=["messageAdded"],
            verbose=False,
        )
    assert out["historyId"] == "999"
    mock_get.assert_called_once()
    _settings, path, kwargs = mock_get.call_args[0][0], mock_get.call_args[0][1], mock_get.call_args[1]
    assert path == "/history"
    params = kwargs["params"]
    assert isinstance(params, list)
    keys = [k for k, _ in params]
    assert "startHistoryId" in keys
    assert "maxResults" in keys
    assert "pageToken" in keys
    assert params.count(("historyTypes", "messageAdded")) >= 1


def test_get_message_metadata_uses_metadata_format_and_headers() -> None:
    settings = _minimal_settings()
    with patch("google_gmail_api._gmail_get_json") as mock_get:
        mock_get.return_value = {
            "id": "msg-1",
            "threadId": "thr-1",
            "historyId": "777",
            "internalDate": "1770000000000",
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "Jan <jan@example.com>"},
                    {"name": "To", "value": "biuro@topinstal.pl"},
                    {"name": "Bcc", "value": "audit@example.com"},
                    {"name": "Subject", "value": "Oferta"},
                ],
                "parts": [
                    {
                        "filename": "projekt.pdf",
                        "mimeType": "application/pdf",
                        "body": {"attachmentId": "att-1", "size": 123},
                    }
                ],
            },
        }
        out = get_message_metadata(settings, message_id="msg-1", verbose=False)

    assert out["message_id"] == "msg-1"
    assert out["history_id"] == "777"
    assert out["body"] == ""
    assert out["bcc"] == ["audit@example.com"]
    assert out["attachment_parts"][0]["attachment_id"] == "att-1"
    _settings, path, kwargs = mock_get.call_args[0][0], mock_get.call_args[0][1], mock_get.call_args[1]
    assert path == "/messages/msg-1"
    params = kwargs["params"]
    assert ("format", "metadata") in params
    assert ("metadataHeaders", "Bcc") in params


def test_search_email_metadata_never_fetches_full_body() -> None:
    settings = _minimal_settings()
    with patch("google_gmail_api._list_message_refs") as mock_refs, patch("google_gmail_api.get_message_metadata") as mock_meta:
        mock_refs.return_value = {"message_ids": ["msg-1"], "next_page_token": "", "result_size_estimate": 1}
        mock_meta.return_value = {"message_id": "msg-1", "body": ""}
        out = search_email_metadata(settings, query="newer_than:7d", max_results=10, verbose=False)

    assert out["format"] == "metadata"
    assert out["responses"] == [{"message_id": "msg-1", "body": ""}]
    mock_meta.assert_called_once_with(settings, message_id="msg-1", verbose=False)
