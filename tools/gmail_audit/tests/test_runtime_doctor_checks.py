from __future__ import annotations

import argparse
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import Settings
from drive_client import GOOGLE_DRIVE_READONLY_SCOPE, build_google_drive_check
from gmail_intake import _emit_json, _require_drive_runtime, _require_mailbox_memory_runtime, build_doctor_config_check, build_ocr_check, run_doctor_command
from mailbox_memory_health import (
    VECTOR_PATH_DISABLED,
    VECTOR_PATH_UNAVAILABLE,
    build_vector_retrieval_readiness_check,
    check_mailbox_memory_database,
)
from signal_worker import run_signal_loop


def make_settings(**overrides: object) -> Settings:
    base = {
        "llm_backend": "groq",
        "openai_compat_base_url": "",
        "openai_compat_api_key": "",
        "groq_api_key": "groq-test",
        "google_access_token": "",
        "google_client_id": "client-id",
        "google_client_secret": "client-secret",
        "google_refresh_token": "refresh-token",
        "google_token_endpoint": "https://oauth2.googleapis.com/token",
        "google_oauth_scopes": ("https://www.googleapis.com/auth/gmail.readonly",),
        "groq_model": "openai/gpt-oss-120b",
        "groq_native_model": "openai/gpt-oss-120b",
        "groq_base_url": "https://api.groq.com",
        "daszek_base_url": "https://topinstal.com.pl",
        "daszek_login": "daszek",
        "daszek_password": "secret",
        "daszek_v2_push_enabled": False,
        "case_guidance_enabled": False,
        "case_guidance_model": "openai/gpt-oss-120b",
        "case_guidance_remote_state_enabled": True,
        "attachment_extraction_enabled": True,
        "attachment_extraction_max_bytes": 8_000_000,
        "mailbox_memory_database_url": "postgresql://mailbox_memory:memorka@127.0.0.1:54329/mailbox_memory",
        "mailbox_memory_blob_root": Path("tools/gmail_audit/data/mailbox_memory/blobs"),
        "mailbox_memory_stage_mode": "shadow",
        "mailbox_memory_stage_allowlist": (),
        "google_drive_enabled": False,
        "google_drive_credentials_path": None,
        "google_drive_shared_drive_id": "",
        "google_drive_root_folder_id": "",
        "google_drive_batch_page_size": 100,
        "google_drive_max_download_bytes": 10_000_000,
        "google_drive_ingest_enabled": False,
        "google_drive_graph_enabled": False,
        "gmail_agent_otel_enabled": False,
        "gmail_agent_otel_local_mirror_enabled": True,
        "otel_service_name": "gmail-agent",
        "otel_exporter_otlp_endpoint": "",
        "otel_exporter_otlp_headers": "",
        "mailbox_memory_vector_enabled": False,
        "openai_compat_embedding_model": "",
        "openai_compat_embedding_dimensions": 0,
        "docling_enabled": False,
        "docling_max_pages": 40,
        "docling_timeout_sec": 45,
        "http_timeout": 60,
        "http_max_retries": 4,
        "http_retry_base_delay": 2.0,
        "env_path": Path("tools/gmail_audit/.env"),
        "config_sources": {
            "_loaded_env_file": "tools/gmail_audit/.env",
            "ATTACHMENT_EXTRACTION_ENABLED": ".env",
            "ATTACHMENT_EXTRACTION_MAX_BYTES": ".env",
            "CASE_GUIDANCE_ENABLED": ".env",
            "CASE_GUIDANCE_MODEL": ".env",
            "CASE_GUIDANCE_REMOTE_STATE": ".env",
            "DASZEK_V2_PUSH": ".env",
            "DASZEK_V2_READBACK_ENABLED": ".env",
            "MAILBOX_MEMORY_DATABASE_URL": ".env",
            "GOOGLE_DRIVE_ENABLED": ".env",
            "GOOGLE_DRIVE_INGEST_ENABLED": ".env",
            "GOOGLE_DRIVE_GRAPH_ENABLED": ".env",
            "GOOGLE_DRIVE_ROOT_FOLDER_ID": ".env",
            "GOOGLE_OAUTH_SCOPES": ".env",
            "GMAIL_AGENT_OTEL_ENABLED": ".env",
            "GMAIL_AGENT_OTEL_LOCAL_MIRROR_ENABLED": ".env",
            "OTEL_SERVICE_NAME": ".env",
            "OTEL_EXPORTER_OTLP_ENDPOINT": ".env",
            "OTEL_EXPORTER_OTLP_HEADERS": ".env",
            "MAILBOX_MEMORY_VECTOR_ENABLED": ".env",
            "OPENAI_COMPAT_EMBEDDING_MODEL": ".env",
            "OPENAI_COMPAT_EMBEDDING_DIMENSIONS": ".env",
            "DOCLING_ENABLED": ".env",
            "DOCLING_MAX_PAGES": ".env",
            "DOCLING_TIMEOUT_SEC": ".env",
            "GMAIL_AGENT_RUNTIME_PROFILE": "unset",
        },
        "config_warnings": [],
        "google_access_token_had_bearer_prefix": False,
        "google_runtime_access_token": "",
        "google_runtime_access_token_expires_at": 0.0,
        "google_runtime_token_type": "",
        "google_active_token_source": "",
        "neo4j_pilot_enabled": False,
        "neo4j_uri": "",
        "neo4j_username": "",
        "neo4j_password": "",
        "neo4j_database": "neo4j",
        "runtime_profile": "",
    }
    base.update(overrides)
    return Settings(**base)


class RuntimeDoctorChecksTests(unittest.TestCase):
    def test_build_vector_retrieval_readiness_disabled(self) -> None:
        settings = make_settings(mailbox_memory_vector_enabled=False)
        check = build_vector_retrieval_readiness_check(settings)
        self.assertEqual(check["status"], "skipped")
        self.assertEqual(check["vector_path_status"], VECTOR_PATH_DISABLED)

    def test_build_vector_retrieval_readiness_unavailable_without_embedding_runtime(self) -> None:
        settings = make_settings(
            mailbox_memory_vector_enabled=True,
            openai_compat_embedding_model="",
            openai_compat_base_url="",
            openai_compat_api_key="",
        )
        with mock.patch("mailbox_memory_health.check_pgvector_extension", return_value={"status": "ok", "extension": "vector"}):
            check = build_vector_retrieval_readiness_check(settings)
        self.assertEqual(check["status"], "ok")
        self.assertEqual(check["vector_path_status"], VECTOR_PATH_UNAVAILABLE)

    def test_mailbox_memory_health_reports_missing_url_classification(self) -> None:
        check = check_mailbox_memory_database("")

        self.assertEqual(check["status"], "skipped")
        self.assertEqual(check["failure_kind"], "missing_url")
        self.assertEqual(check["connection_target"]["host"], "")

    def test_mailbox_memory_health_reports_missing_driver_classification(self) -> None:
        original_import = __import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "psycopg":
                raise ImportError("psycopg missing for test")
            return original_import(name, globals, locals, fromlist, level)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            check = check_mailbox_memory_database("postgresql://mailbox_memory:secret@127.0.0.1:54329/mailbox_memory")

        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["failure_kind"], "missing_driver")
        self.assertEqual(check["connection_target"]["host"], "127.0.0.1")
        self.assertEqual(check["connection_target"]["port"], 54329)

    def test_mailbox_memory_health_classifies_connection_refused(self) -> None:
        import psycopg

        with mock.patch("psycopg.connect", side_effect=psycopg.OperationalError("connection refused")):
            check = check_mailbox_memory_database("postgresql://mailbox_memory:secret@127.0.0.1:54329/mailbox_memory")

        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["failure_kind"], "connection_refused")
        self.assertEqual(check["connection_target"]["database"], "mailbox_memory")

    def test_require_mailbox_memory_runtime_points_to_doctor(self) -> None:
        settings = make_settings(
            mailbox_memory_database_url="",
            mailbox_memory_stage_mode="disabled",
        )

        with self.assertRaises(Exception) as exc:
            _require_mailbox_memory_runtime(settings)

        self.assertIn("doctor --skip-gmail --verbose", str(exc.exception))

    def test_require_drive_runtime_points_to_drive_doctor(self) -> None:
        settings = make_settings(
            google_drive_enabled=False,
            google_drive_ingest_enabled=False,
            mailbox_memory_database_url="",
        )

        with self.assertRaises(Exception) as exc:
            _require_drive_runtime(settings)

        self.assertIn("doctor --gmail-source google_api --check-drive --verbose", str(exc.exception))

    def test_signal_run_requires_worker_flag_with_actionable_message(self) -> None:
        settings = make_settings(
            signal_runtime_mode="active",
            signal_worker_enabled=False,
        )

        with self.assertRaises(Exception) as exc:
            run_signal_loop(settings, loop_mode="oneshot", dry_run=True)

        self.assertIn("SIGNAL_WORKER_ENABLED=1", str(exc.exception))
        self.assertIn("signal-run --oneshot --dry-run --verbose", str(exc.exception))

    def test_build_doctor_config_check_reports_new_runtime_sources(self) -> None:
        settings = make_settings(
            daszek_v2_push_enabled=True,
            case_guidance_enabled=True,
            case_guidance_model="custom-guidance",
            attachment_extraction_enabled=False,
            google_drive_enabled=True,
            google_drive_ingest_enabled=True,
            gmail_agent_otel_enabled=True,
            mailbox_memory_vector_enabled=True,
            openai_compat_embedding_model="text-embedding-3-large",
            openai_compat_embedding_dimensions=3072,
            docling_enabled=True,
        )

        check = build_doctor_config_check(settings)

        self.assertEqual(check["status"], "ok")
        self.assertTrue(check["daszek_v2_push_enabled"])
        self.assertFalse(check["daszek_v2_readback_enabled"])
        self.assertTrue(check["case_guidance_enabled"])
        self.assertFalse(check["attachment_extraction_enabled"])
        self.assertTrue(check["google_drive_enabled"])
        self.assertTrue(check["otel_enabled"])
        self.assertTrue(check["mailbox_memory_vector_enabled"])
        self.assertEqual(check["embedding_model"], "text-embedding-3-large")
        self.assertEqual(check["embedding_dimensions"], 3072)
        self.assertTrue(check["docling_enabled"])
        settings_alt = make_settings(llm_structured_provider_alternation=True, signal_extraction_mode="regex")
        alt_check = build_doctor_config_check(settings_alt)
        self.assertTrue(alt_check["llm_structured_provider_alternation"])
        self.assertEqual(alt_check["signal_extraction_mode"], "regex")
        self.assertEqual(check["config_sources"]["DASZEK_V2_PUSH"], ".env")
        self.assertEqual(check["config_sources"]["DASZEK_V2_READBACK_ENABLED"], ".env")
        self.assertEqual(check["config_sources"]["ATTACHMENT_EXTRACTION_ENABLED"], ".env")
        self.assertEqual(check["config_sources"]["GMAIL_AGENT_OTEL_ENABLED"], ".env")
        self.assertEqual(check["config_sources"]["MAILBOX_MEMORY_VECTOR_ENABLED"], ".env")
        self.assertEqual(check["config_sources"]["DOCLING_ENABLED"], ".env")
        self.assertEqual(check["runtime_profile"], "default")

    def test_build_ocr_check_respects_disabled_flag(self) -> None:
        settings = make_settings(attachment_extraction_enabled=False)

        check = build_ocr_check(settings)

        self.assertEqual(check["status"], "disabled")
        self.assertIn("ATTACHMENT_EXTRACTION_ENABLED=0", check["reason"])

    def test_build_ocr_check_reports_binary_missing_without_real_tesseract(self) -> None:
        settings = make_settings()
        with mock.patch(
            "gmail_intake.inspect_ocr_runtime",
            return_value={"status": "binary_missing", "reason": "Tesseract binary is not available."},
        ):
            check = build_ocr_check(settings)

        self.assertEqual(check["status"], "binary_missing")
        self.assertEqual(check["reason"], "Tesseract binary is not available.")

    def test_build_google_drive_check_fails_without_drive_scope_for_shared_oauth(self) -> None:
        settings = make_settings(
            google_oauth_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
            google_drive_enabled=True,
            google_drive_ingest_enabled=True,
            google_drive_root_folder_id="root-folder",
        )

        check = build_google_drive_check(settings, check_access=True)

        self.assertEqual(check["status"], "failed")
        self.assertIn(GOOGLE_DRIVE_READONLY_SCOPE, check["error"])
        self.assertIn("manual_prerequisite", check)
        self.assertIn("OAuth", check["manual_prerequisite"])

    def test_build_google_drive_check_verifies_bounded_access(self) -> None:
        settings = make_settings(
            google_oauth_scopes=(
                "https://www.googleapis.com/auth/gmail.readonly",
                GOOGLE_DRIVE_READONLY_SCOPE,
            ),
            google_drive_enabled=True,
            google_drive_ingest_enabled=True,
            google_drive_root_folder_id="root-folder",
        )
        with mock.patch("drive_client.GoogleDriveClient.list_children", return_value={"items": [{"id": "doc-1"}], "next_page_token": ""}):
            check = build_google_drive_check(settings, check_access=True)

        self.assertEqual(check["status"], "ok")
        self.assertEqual(check["sample_item_count"], 1)

    def test_run_doctor_command_emits_ocr_and_drive_checks(self) -> None:
        settings = make_settings()
        args = argparse.Namespace(
            model=None,
            verbose=False,
            gmail_source="google_api",
            skip_gmail=True,
            check_daszek=False,
            check_drive=True,
        )
        stdout = io.StringIO()
        with mock.patch("gmail_intake.load_settings", return_value=settings):
            with mock.patch("gmail_intake.build_ocr_check", return_value={"status": "ok"}):
                with mock.patch("gmail_intake.build_google_drive_check", return_value={"status": "ok", "root_folder_id": "root-folder"}):
                    with mock.patch("gmail_intake.build_otel_check", return_value={"status": "ok", "export_mode": "mirror_only"}):
                        with mock.patch("gmail_intake.build_pgvector_check", return_value={"status": "ok", "extension": "vector"}):
                            with mock.patch("gmail_intake.build_docling_check", return_value={"status": "ok", "parser": "docling"}):
                                with mock.patch("gmail_intake.check_mailbox_memory_database", return_value={"status": "ok"}):
                                    with redirect_stdout(stdout):
                                        exit_code = run_doctor_command(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["checks"]["ocr"]["status"], "ok")
        self.assertEqual(payload["checks"]["drive"]["status"], "ok")
        self.assertEqual(payload["checks"]["otel"]["status"], "ok")
        self.assertEqual(payload["checks"]["pgvector"]["status"], "ok")
        self.assertEqual(payload["checks"]["docling"]["status"], "ok")
        self.assertEqual(payload["checks"]["config"]["config_sources"]["ATTACHMENT_EXTRACTION_ENABLED"], ".env")
        self.assertEqual(payload["checks"]["case_snapshot_hot_state"]["status"], "ok")
        self.assertIn("schema_version", payload["checks"]["case_snapshot_hot_state"])
        self.assertIn("vector_retrieval", payload["checks"])
        self.assertEqual(payload["checks"]["vector_retrieval"]["vector_path_status"], "vector_path_disabled")
        self.assertIn("neo4j_pilot", payload["checks"])
        self.assertEqual(payload["checks"]["neo4j_pilot"]["status"], "skipped")

    def test_run_doctor_command_daszek_v1_v2_and_readback_probe(self) -> None:
        settings = make_settings(
            daszek_v2_readback_enabled=True,
            daszek_operational_feed_auto_push_enabled=False,
        )

        class FakeDaszek:
            base_url = "https://daszek.example"

            def login(self) -> None:
                return None

            def list_tasks(self, *, refresh: bool = True):
                _ = refresh
                return [{"id": "t1"}]

            def get_v2_calibration_profile(self):
                return {"calibrated": True}

            def get_v2_desk(self):
                return {"desk": "ok"}

        args = argparse.Namespace(
            model=None,
            verbose=False,
            gmail_source="google_api",
            skip_gmail=True,
            check_daszek=True,
            check_daszek_v2_read=True,
            check_daszek_v3_feed=False,
            check_drive=False,
            check_calendar=False,
        )
        stdout = io.StringIO()
        with mock.patch("gmail_intake.load_settings", return_value=settings):
            with mock.patch("gmail_intake.DaszekClient", return_value=FakeDaszek()):
                with mock.patch("gmail_intake.build_ocr_check", return_value={"status": "ok"}):
                    with mock.patch("gmail_intake.build_google_drive_check", return_value={"status": "skipped"}):
                        with mock.patch("gmail_intake.build_otel_check", return_value={"status": "ok", "export_mode": "mirror_only"}):
                            with mock.patch("gmail_intake.build_pgvector_check", return_value={"status": "ok", "extension": "vector"}):
                                with mock.patch("gmail_intake.build_docling_check", return_value={"status": "ok", "parser": "docling"}):
                                    with mock.patch("gmail_intake.check_mailbox_memory_database", return_value={"status": "ok"}):
                                        with mock.patch("gmail_intake.build_vector_retrieval_readiness_check", return_value={"status": "ok", "vector_path_status": "vector_path_disabled"}):
                                            with redirect_stdout(stdout):
                                                exit_code = run_doctor_command(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        v1 = payload["checks"]["daszek_v1_tasks"]
        self.assertEqual(v1["v1_task_count"], 1)
        self.assertEqual(v1["task_count"], 1)
        self.assertIn("v1_task_count_interpretation", v1)
        dz = payload["checks"]["daszek"]
        self.assertEqual(dz["v1_task_count"], 1)
        self.assertEqual(dz["task_count"], 1)
        self.assertIn("v1_task_count_interpretation", dz)
        v2 = payload["checks"]["daszek_v2_operator_surface"]
        self.assertTrue(v2["daszek_v2_readback_enabled"])
        self.assertEqual(v2["daszek_v2_config_sources"]["DASZEK_V2_READBACK_ENABLED"], ".env")
        self.assertTrue(v2.get("desk_read_ok"))

    def test_run_doctor_command_daszek_v3_feed_probe(self) -> None:
        settings = make_settings(daszek_operational_feed_auto_push_enabled=True)

        class FakeDaszek:
            base_url = "https://daszek.example"

            def login(self) -> None:
                return None

            def list_tasks(self, *, refresh: bool = True):
                _ = refresh
                return []

            def get_v2_calibration_profile(self):
                return {"calibrated": True}

            def get_v3_operational_feed_snapshot_latest(self):
                return {
                    "ok": True,
                    "snapshot": {
                        "snapshot_id": "snap-doctor-v3",
                        "feed": {"desk": [{"note_id": "desk-1"}], "cases": [], "tasks": []},
                    },
                }

        args = argparse.Namespace(
            model=None,
            verbose=False,
            gmail_source="google_api",
            skip_gmail=True,
            check_daszek=True,
            check_daszek_v2_read=False,
            check_daszek_v3_feed=False,
            check_drive=False,
            check_calendar=False,
        )
        stdout = io.StringIO()
        with mock.patch("gmail_intake.load_settings", return_value=settings):
            with mock.patch("gmail_intake.DaszekClient", return_value=FakeDaszek()):
                with mock.patch("gmail_intake.build_ocr_check", return_value={"status": "ok"}):
                    with mock.patch("gmail_intake.build_google_drive_check", return_value={"status": "skipped"}):
                        with mock.patch("gmail_intake.build_otel_check", return_value={"status": "ok", "export_mode": "mirror_only"}):
                            with mock.patch("gmail_intake.build_pgvector_check", return_value={"status": "ok", "extension": "vector"}):
                                with mock.patch("gmail_intake.build_docling_check", return_value={"status": "ok", "parser": "docling"}):
                                    with mock.patch("gmail_intake.check_mailbox_memory_database", return_value={"status": "ok"}):
                                        with mock.patch("gmail_intake.build_vector_retrieval_readiness_check", return_value={"status": "ok", "vector_path_status": "vector_path_disabled"}):
                                            with redirect_stdout(stdout):
                                                exit_code = run_doctor_command(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        v3 = payload["checks"]["daszek_v3_operational_feed"]
        self.assertEqual(v3["status"], "ok")
        self.assertEqual(v3["latest_snapshot_id"], "snap-doctor-v3")
        self.assertTrue(v3["snapshot_present"])
        self.assertEqual(v3["counts"]["desk"], 1)

    def test_emit_json_serializes_datetimes_for_cli_summaries(self) -> None:
        stdout = io.StringIO()
        payload = {"observed_at": datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)}

        with redirect_stdout(stdout):
            _emit_json(payload)

        rendered = json.loads(stdout.getvalue())
        self.assertEqual(rendered["observed_at"], "2026-04-12 12:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
