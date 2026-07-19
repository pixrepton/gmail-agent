"""Tests for LLM_BACKEND / OpenAI-compatible (e.g. Ollama) settings."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import Settings, load_settings


def _base_settings(**overrides: object) -> Settings:
    base = {
        "llm_backend": "groq",
        "openai_compat_base_url": "",
        "openai_compat_api_key": "",
        "groq_api_key": "gsk_test",
        "google_access_token": "",
        "google_client_id": "",
        "google_client_secret": "",
        "google_refresh_token": "",
        "google_token_endpoint": "https://oauth2.googleapis.com/token",
        "google_oauth_scopes": ("https://www.googleapis.com/auth/gmail.readonly",),
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
        "google_drive_enabled": False,
        "google_drive_credentials_path": None,
        "google_drive_shared_drive_id": "",
        "google_drive_root_folder_id": "",
        "google_drive_batch_page_size": 100,
        "google_drive_max_download_bytes": 10_000_000,
        "google_drive_ingest_enabled": False,
        "google_drive_graph_enabled": False,
        "http_timeout": 60,
        "http_max_retries": 4,
        "http_retry_base_delay": 2.0,
        "env_path": None,
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


def test_openai_chat_completions_url_with_v1_suffix() -> None:
    s = _base_settings(
        llm_backend="openai_chat",
        openai_compat_base_url="http://127.0.0.1:11434/v1",
        groq_model="llama3.2",
    )
    assert s.openai_chat_completions_url == "http://127.0.0.1:11434/v1/chat/completions"


def test_openai_chat_completions_url_without_v1_suffix() -> None:
    s = _base_settings(
        llm_backend="openai_chat",
        openai_compat_base_url="http://127.0.0.1:11434",
        groq_model="llama3.2",
    )
    assert s.openai_chat_completions_url == "http://127.0.0.1:11434/v1/chat/completions"


def test_llm_backend_cerebras_aliases_openai_chat_and_defaults() -> None:
    env = {
        "LLM_BACKEND": "cerebras",
        "cerebras_api_key": "csk_test_placeholder",
        "cerebras_model": "gpt-oss-120b",
        "HTTP_TIMEOUT": "60",
        "HTTP_MAX_RETRIES": "4",
        "HTTP_RETRY_BASE_DELAY": "2",
    }
    with patch("config._load_env_file", return_value=None):
        with patch.dict(os.environ, env, clear=True):
            s = load_settings(require_groq=True, require_google=False)
    assert s.llm_backend == "openai_chat"
    assert s.openai_compat_base_url == "https://api.cerebras.ai/v1"
    assert s.openai_compat_api_key == "csk_test_placeholder"
    assert s.groq_model == "gpt-oss-120b"


def test_openai_chat_cerebras_url_picks_up_lowercase_cerebras_api_key() -> None:
    env = {
        "LLM_BACKEND": "openai_chat",
        "OPENAI_COMPAT_BASE_URL": "https://api.cerebras.ai/v1",
        "cerebras_api_key": "csk_from_alias",
        "HTTP_TIMEOUT": "60",
        "HTTP_MAX_RETRIES": "4",
        "HTTP_RETRY_BASE_DELAY": "2",
    }
    with patch("config._load_env_file", return_value=None):
        with patch.dict(os.environ, env, clear=True):
            s = load_settings(require_groq=True, require_google=False)
    assert s.openai_compat_api_key == "csk_from_alias"


def test_cerebras_chat_keeps_embeddings_on_separate_ollama_base() -> None:
    env = {
        "LLM_BACKEND": "cerebras",
        "cerebras_api_key": "csk_test_placeholder",
        "cerebras_model": "gpt-oss-120b",
        "MAILBOX_MEMORY_VECTOR_ENABLED": "1",
        "OPENAI_COMPAT_EMBEDDING_BASE_URL": "http://127.0.0.1:11434/v1",
        "OPENAI_COMPAT_EMBEDDING_MODEL": "nomic-embed-text",
        "OPENAI_COMPAT_EMBEDDING_DIMENSIONS": "768",
        "HTTP_TIMEOUT": "60",
        "HTTP_MAX_RETRIES": "4",
        "HTTP_RETRY_BASE_DELAY": "2",
    }
    with patch("config._load_env_file", return_value=None):
        with patch.dict(os.environ, env, clear=True):
            s = load_settings(require_groq=True, require_google=False)
    assert s.openai_compat_base_url == "https://api.cerebras.ai/v1"
    assert s.openai_compat_embedding_base_url == "http://127.0.0.1:11434/v1"
    assert s.openai_compat_embedding_api_key == ""


def test_build_embedding_runtime_prefers_embedding_base_url() -> None:
    from embedding_runtime import build_embedding_runtime

    class _S:
        mailbox_memory_vector_enabled = True
        openai_compat_base_url = "https://api.cerebras.ai/v1"
        openai_compat_embedding_base_url = "http://ollama:11434/v1"
        openai_compat_embedding_api_key = ""
        openai_compat_embedding_model = "nomic-embed-text"
        openai_compat_embedding_dimensions = 768
        http_timeout = 60

    rt = build_embedding_runtime(_S())
    assert rt is not None
    assert rt.endpoint == "http://ollama:11434/v1/embeddings"
    assert rt.api_key == ""
