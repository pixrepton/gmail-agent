from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from google.auth.exceptions import RefreshError

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import ConfigError, Settings, default_env_candidates, load_settings
from gmail_auth import (
    GoogleOAuthError,
    _build_refresh_error,
    build_google_credentials,
    get_gmail_credentials,
    load_google_oauth_config,
)
from google_gmail_api import get_profile


class GmailAuthTests(unittest.TestCase):
    def test_default_env_candidates_prefers_tool_env_then_repo_root(self) -> None:
        fake_tool = Path("/tmp/fake-tool-root")
        with mock.patch("config.CONFIG_DIR", fake_tool):
            candidates = default_env_candidates()

        self.assertEqual(
            candidates,
            [fake_tool / ".env", fake_tool.parent.parent / ".env"],
        )

    def test_load_settings_honors_explicit_env_file_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            canonical_env_path = temp_root / ".env"
            override_env_path = temp_root / "custom.runtime.env"
            canonical_env_path.write_text(
                "GOOGLE_CLIENT_ID=canonical-client\n",
                encoding="utf-8",
            )
            override_env_path.write_text(
                "GOOGLE_CLIENT_ID=override-client\n",
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {"GMAIL_AGENT_ENV_FILE": str(override_env_path)},
                clear=True,
            ):
                with mock.patch("config.CONFIG_DIR", temp_root):
                    settings = load_settings(require_groq=False, require_google=False)

        self.assertEqual(settings.env_path, override_env_path)
        self.assertEqual(settings.google_client_id, "override-client")

    def test_case_guidance_remote_state_defaults_to_false(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = load_settings(require_groq=False, require_google=False)

        self.assertFalse(settings.case_guidance_remote_state_enabled)

    def test_load_settings_warns_when_legacy_dot_env_local_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_tool = Path(tmpdir)
            (fake_tool / ".env.local").write_text("# legacy\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}, clear=False):
                with mock.patch("config.CONFIG_DIR", fake_tool):
                    with mock.patch("config._load_env_file", return_value=None):
                        settings = load_settings(require_groq=False, require_google=False)

        self.assertTrue(any(".env.local" in w and "never loaded" in w for w in settings.config_warnings))

    def test_load_settings_reads_dot_env_only_even_if_env_local_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            env_path = temp_root / ".env"
            env_local_path = temp_root / ".env.local"
            env_path.write_text(
                "GOOGLE_CLIENT_ID=primary-client\n"
                "GOOGLE_CLIENT_SECRET=primary-secret\n"
                "GOOGLE_REFRESH_TOKEN=primary-refresh\n",
                encoding="utf-8",
            )
            env_local_path.write_text(
                "GOOGLE_CLIENT_ID=legacy-client\n"
                "GOOGLE_CLIENT_SECRET=legacy-secret\n"
                "GOOGLE_REFRESH_TOKEN=legacy-refresh\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("config.default_env_candidates", return_value=[env_path]):
                    settings = load_settings(require_groq=False, require_google=False)

        self.assertEqual(settings.env_path, env_path)
        self.assertEqual(settings.google_client_id, "primary-client")
        self.assertEqual(settings.google_client_secret, "primary-secret")

    def test_load_settings_ignores_dot_env_local_when_no_dot_env_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            env_path = temp_root / ".env"
            env_local_path = temp_root / ".env.local"
            env_local_path.write_text(
                "GOOGLE_CLIENT_ID=legacy-client\n"
                "GOOGLE_CLIENT_SECRET=legacy-secret\n"
                "GOOGLE_REFRESH_TOKEN=legacy-refresh\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("config.default_env_candidates", return_value=[env_path]):
                    settings = load_settings(require_groq=False, require_google=False)

        self.assertIsNone(settings.env_path)
        self.assertEqual(settings.google_client_id, "")
        self.assertEqual(settings.google_client_secret, "")

    def test_load_settings_uses_database_url_only_as_legacy_mailbox_memory_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "DATABASE_URL=postgresql://legacy-user:legacy-pass@127.0.0.1:54329/mailbox_memory\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("config.default_env_candidates", return_value=[env_path]):
                    settings = load_settings(require_groq=False, require_google=False)

        self.assertEqual(
            settings.mailbox_memory_database_url,
            "postgresql://legacy-user:legacy-pass@127.0.0.1:54329/mailbox_memory",
        )
        self.assertEqual(settings.config_sources["MAILBOX_MEMORY_DATABASE_URL"], "DATABASE_URL")
        self.assertIn("Using legacy DATABASE_URL fallback", " ".join(settings.config_warnings))

    def test_load_settings_marks_empty_env_values_as_empty_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "GOOGLE_DRIVE_ROOT_FOLDER_ID=\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("config.default_env_candidates", return_value=[env_path]):
                    settings = load_settings(require_groq=False, require_google=False)

        self.assertEqual(settings.google_drive_root_folder_id, "")
        self.assertEqual(settings.config_sources["GOOGLE_DRIVE_ROOT_FOLDER_ID"], ".env (empty)")

    def test_missing_client_secret_is_reported_clearly(self) -> None:
        settings = self._make_settings(
            google_client_id="client-id",
            google_client_secret="",
            google_refresh_token="refresh-token",
        )

        with self.assertRaises(GoogleOAuthError) as exc:
            load_google_oauth_config(settings)

        self.assertIn("GOOGLE_CLIENT_SECRET", str(exc.exception))

    def test_load_settings_require_google_rejects_incomplete_refresh_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "GOOGLE_CLIENT_ID=client-id\n"
                "GOOGLE_REFRESH_TOKEN=refresh-token\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("config.default_env_candidates", return_value=[env_path]):
                    with self.assertRaises(ConfigError) as exc:
                        load_settings(require_groq=False, require_google=True)

        self.assertIn("GOOGLE_CLIENT_SECRET", str(exc.exception))

    def test_build_google_credentials_uses_refresh_config(self) -> None:
        credentials = build_google_credentials(
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-token",
                "access_token": "startup-token",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )

        self.assertEqual(credentials.client_id, "client-id")
        self.assertEqual(credentials.client_secret, "client-secret")
        self.assertEqual(credentials.refresh_token, "refresh-token")
        self.assertEqual(credentials.token, "startup-token")
        self.assertEqual(credentials.scopes, ["https://www.googleapis.com/auth/gmail.readonly"])

    def test_get_gmail_credentials_refreshes_and_caches_runtime_token(self) -> None:
        settings = self._make_settings(
            google_client_id="client-id",
            google_client_secret="client-secret",
            google_refresh_token="refresh-token",
        )
        refreshed_expiry = datetime.now(timezone.utc) + timedelta(minutes=55)

        def fake_refresh(credentials, request) -> None:
            credentials.token = "ya29.refreshed-token"
            credentials.expiry = refreshed_expiry

        with mock.patch("gmail_auth.Credentials.refresh", autospec=True, side_effect=fake_refresh):
            credentials = get_gmail_credentials(settings, force_refresh=True)

        self.assertEqual(credentials.token, "ya29.refreshed-token")
        self.assertEqual(settings.google_runtime_access_token, "ya29.refreshed-token")
        self.assertEqual(settings.google_active_token_source, "refresh_token")
        self.assertGreater(settings.google_runtime_access_token_expires_at, 0.0)

    def test_google_gmail_api_fetch_uses_gmail_auth_credentials(self) -> None:
        settings = self._make_settings()
        captured_headers: dict[str, str] = {}

        class FakeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, str]:
                return {"emailAddress": "ops@topinstal.pl"}

        def fake_get(url: str, *, headers=None, params=None, timeout=None):
            captured_headers.update(headers or {})
            self.assertIn("/profile", url)
            self.assertEqual(timeout, settings.http_timeout)
            return FakeResponse()

        fake_credentials = build_google_credentials(
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-token",
                "access_token": "ya29.integration-token",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )

        with mock.patch("google_gmail_api.get_gmail_credentials", return_value=fake_credentials):
            with mock.patch("google_gmail_api.requests.get", side_effect=fake_get):
                profile = get_profile(settings)

        self.assertEqual(captured_headers["Authorization"], "Bearer ya29.integration-token")
        self.assertEqual(profile["email"], "ops@topinstal.pl")
        self.assertEqual(profile["source"], "google_api")

    def test_build_refresh_error_explains_invalid_scope(self) -> None:
        error = _build_refresh_error(
            RefreshError("invalid_scope: Bad Request", {"error": "invalid_scope"})
        )

        self.assertIsInstance(error, GoogleOAuthError)
        self.assertIn("different scope set", str(error))
        self.assertIn("drive.readonly", str(error))

    @staticmethod
    def _make_settings(**overrides: object) -> Settings:
        base = {
            "llm_backend": "groq",
            "openai_compat_base_url": "",
            "openai_compat_api_key": "",
            "groq_api_key": "",
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


if __name__ == "__main__":
    unittest.main()
