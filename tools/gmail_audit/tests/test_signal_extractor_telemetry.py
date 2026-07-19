"""B3: signal_extractor explicit parse_status on failure paths."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import Settings
from signal_extractor import run_signal_extraction


def _minimal_settings() -> Settings:
    return Settings(
        llm_backend="groq",
        openai_compat_base_url="",
        openai_compat_api_key="",
        groq_api_key="gsk_test",
        google_access_token="",
        google_client_id="",
        google_client_secret="",
        google_refresh_token="",
        google_token_endpoint="https://oauth2.googleapis.com/token",
        google_oauth_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        groq_model="openai/gpt-oss-120b",
        groq_base_url="https://api.groq.com",
        daszek_base_url="",
        daszek_login="",
        daszek_password="",
        daszek_v2_push_enabled=False,
        case_guidance_enabled=False,
        case_guidance_model="openai/gpt-oss-120b",
        case_guidance_remote_state_enabled=True,
        anthropic_api_key="",
        anthropic_model="claude-sonnet-4-20250514",
    )


def _minimal_snapshot() -> dict[str, object]:
    return {
        "snapshot_version": "1",
        "mailbox": "x@test.local",
        "observed_at": "2026-01-01T00:00:00",
        "source_message": {
            "message_id": "msg_test_1",
            "thread_id": "t1",
            "subject": "Test HVAC lead",
            "body": "pompa ciepla wycena",
            "snippet": "pompa ciepla",
            "sender": "a@b.c",
            "has_attachments": False,
            "attachment_names": [],
        },
        "context_messages": [],
        "normalized_subject": "test hvac lead",
        "thread_context_quality": "weak",
        "thread_context": {"quality": "weak", "reasons": []},
        "case_link_candidates": [],
        "routing_hints": {"self_forward": False, "reasons": []},
        "summary_text": "pompa ciepla wycena",
    }


def test_stage_none_returns_extraction_failed() -> None:
    with patch("signal_extractor.run_central_structured_stage", return_value=None):
        result = run_signal_extraction(
            settings=_minimal_settings(),
            snapshot=_minimal_snapshot(),
        )
    assert result["parse_status"] == "extraction_failed"
    assert result["error_reason"] == "central_stage_unavailable"


def test_llm_exception_returns_extraction_failed_with_429_in_reason() -> None:
    with patch(
        "signal_extractor.run_central_structured_stage",
        side_effect=Exception("http-429"),
    ):
        result = run_signal_extraction(
            settings=_minimal_settings(),
            snapshot=_minimal_snapshot(),
        )
    assert result["parse_status"] == "extraction_failed"
    assert "429" in result["error_reason"]


def test_empty_response_returns_empty_result() -> None:
    fake_stage = {"response_json": {}, "response_text": ""}
    with patch("signal_extractor.run_central_structured_stage", return_value=fake_stage):
        result = run_signal_extraction(
            settings=_minimal_settings(),
            snapshot=_minimal_snapshot(),
        )
    assert result["parse_status"] == "empty_result"
