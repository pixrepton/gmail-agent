"""Tests for GMAIL_AGENT_RUNTIME_PROFILE=canonical_production strict contract."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import (
    CANONICAL_PRODUCTION_RUNTIME_PROFILE,
    ConfigError,
    Settings,
    canonical_production_violations,
    load_settings,
)


def _minimal_canonical_settings(**overrides: object) -> Settings:
    base = {
        "llm_backend": "groq",
        "openai_compat_base_url": "",
        "openai_compat_api_key": "",
        "groq_api_key": "k",
        "google_access_token": "",
        "google_client_id": "a",
        "google_client_secret": "b",
        "google_refresh_token": "c",
        "google_token_endpoint": "https://oauth2.googleapis.com/token",
        "google_oauth_scopes": (
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ),
        "groq_model": "m",
        "groq_base_url": "https://api.groq.com",
        "daszek_base_url": "https://example.com",
        "daszek_login": "u",
        "daszek_password": "p",
        "daszek_v2_push_enabled": False,
        "case_guidance_enabled": False,
        "case_guidance_model": "m",
        "case_guidance_remote_state_enabled": False,
        "attachment_extraction_enabled": True,
        "attachment_extraction_max_bytes": 8_000_000,
        "mailbox_memory_database_url": "postgresql://u:p@127.0.0.1:5432/db",
        "mailbox_memory_blob_root": Path("."),
        "mailbox_memory_stage_mode": "live",
        "mailbox_memory_stage_allowlist": (),
        "google_drive_enabled": True,
        "google_drive_credentials_path": None,
        "google_drive_shared_drive_id": "",
        "google_drive_root_folder_id": "folder1",
        "google_drive_batch_page_size": 100,
        "google_drive_max_download_bytes": 10_000_000,
        "google_drive_ingest_enabled": True,
        "google_drive_graph_enabled": False,
        "neo4j_pilot_enabled": True,
        "neo4j_uri": "neo4j://127.0.0.1:7687",
        "neo4j_username": "neo4j",
        "neo4j_password": "secret",
        "neo4j_database": "neo4j",
        "gmail_agent_otel_enabled": False,
        "gmail_agent_otel_local_mirror_enabled": True,
        "otel_service_name": "gmail-agent",
        "otel_exporter_otlp_endpoint": "",
        "otel_exporter_otlp_headers": "",
        "mailbox_memory_vector_enabled": True,
        "openai_compat_embedding_model": "text-embedding-3-small",
        "openai_compat_embedding_dimensions": 1536,
        "docling_enabled": True,
        "docling_max_pages": 40,
        "docling_timeout_sec": 45,
        "signal_runtime_mode": "active",
        "signal_journal_jsonl_mirror_enabled": False,
        "gmail_change_detection_enabled": False,
        "drive_change_detection_enabled": False,
        "signal_worker_enabled": False,
        "gmail_history_poll_interval_sec": 120,
        "drive_changes_poll_interval_sec": 180,
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
        "runtime_profile": CANONICAL_PRODUCTION_RUNTIME_PROFILE,
    }
    base.update(overrides)
    return Settings(**base)


class CanonicalRuntimeProfileTests(unittest.TestCase):
    def test_canonical_production_violations_empty_when_satisfied(self) -> None:
        s = _minimal_canonical_settings()
        self.assertEqual(canonical_production_violations(s), [])

    def test_canonical_production_violations_detects_shadow_stage(self) -> None:
        s = _minimal_canonical_settings(mailbox_memory_stage_mode="shadow")
        v = canonical_production_violations(s)
        self.assertTrue(any("MAILBOX_MEMORY_STAGE_MODE" in x for x in v))

    def test_load_settings_raises_when_canonical_and_incomplete(self) -> None:
        """End-to-end: canonical profile + DB URL but wrong stage triggers contract error."""
        env = {
            "GMAIL_AGENT_RUNTIME_PROFILE": CANONICAL_PRODUCTION_RUNTIME_PROFILE,
            "LLM_BACKEND": "groq",
            "GROQ_API_KEY": "x",
            "GOOGLE_CLIENT_ID": "a",
            "GOOGLE_CLIENT_SECRET": "b",
            "GOOGLE_REFRESH_TOKEN": "c",
            "GOOGLE_TOKEN_ENDPOINT": "https://oauth2.googleapis.com/token",
            "GOOGLE_OAUTH_SCOPES": (
                "https://www.googleapis.com/auth/gmail.readonly "
                "https://www.googleapis.com/auth/drive.readonly"
            ),
            "MAILBOX_MEMORY_DATABASE_URL": "postgresql://u:p@127.0.0.1:5432/db",
            "MAILBOX_MEMORY_STAGE_MODE": "shadow",
            "MAILBOX_MEMORY_VECTOR_ENABLED": "1",
            "OPENAI_COMPAT_EMBEDDING_MODEL": "text-embedding-3-small",
            "OPENAI_COMPAT_EMBEDDING_DIMENSIONS": "1536",
            "DOCLING_ENABLED": "1",
            "ATTACHMENT_EXTRACTION_ENABLED": "1",
            "GOOGLE_DRIVE_ENABLED": "1",
            "GOOGLE_DRIVE_INGEST_ENABLED": "1",
            "GOOGLE_DRIVE_ROOT_FOLDER_ID": "root1",
            "NEO4J_PILOT_ENABLED": "1",
            "NEO4J_URI": "neo4j://127.0.0.1:7687",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "pw",
            "GMAIL_AGENT_OTEL_LOCAL_MIRROR_ENABLED": "1",
            "DASZEK_BASE_URL": "https://example.com",
            "DASZEK_LOGIN": "u",
            "DASZEK_PASSWORD": "p",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("config._load_env_file", return_value=None):
                with self.assertRaises(ConfigError) as ctx:
                    load_settings(require_groq=True, require_google=False)
        self.assertIn("canonical_production", str(ctx.exception).lower())
        self.assertIn("live", str(ctx.exception).lower())
