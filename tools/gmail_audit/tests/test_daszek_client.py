from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import Settings
from daszek_client import DaszekClient, DaszekClientError


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200, text: str = "", url: str = "https://www.topinstal.com.pl/") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.url = url

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(
        self,
        response: _FakeResponse,
        get_response: _FakeResponse | None = None,
        get_responses: list[_FakeResponse] | None = None,
    ) -> None:
        self._response = response
        self._get_response = get_response if get_response is not None else response
        self._get_responses = list(get_responses or [])
        self.last_url = ""
        self.last_headers = {}
        self.last_timeout = None
        self.last_get_url = ""
        self.last_get_timeout = None
        self.last_get_headers = {}
        self.last_get_params = {}
        self.last_json = None

    def post(self, url: str, headers=None, timeout=None, **kwargs):
        self.last_url = url
        self.last_headers = headers or {}
        self.last_timeout = timeout
        self.last_json = kwargs.get("json")
        return self._response

    def get(self, url: str, timeout=None, headers=None, params=None):
        self.last_get_url = url
        self.last_get_timeout = timeout
        self.last_get_headers = headers or {}
        self.last_get_params = params or {}
        if self._get_responses:
            return self._get_responses.pop(0)
        return self._get_response


class DaszekClientTests(unittest.TestCase):
    def test_read_json_preserves_list_payloads(self) -> None:
        client = DaszekClient(self._make_settings())
        payload = client._read_json(_FakeResponse([{"id": "t1"}]), "list_tasks")
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["id"], "t1")

    def test_read_json_preserves_dict_payloads(self) -> None:
        client = DaszekClient(self._make_settings())
        payload = client._read_json(_FakeResponse({"ok": True}), "login")
        self.assertIsInstance(payload, dict)
        self.assertTrue(payload["ok"])

    def test_read_json_strips_utf8_bom_when_response_json_fails(self) -> None:
        client = DaszekClient(self._make_settings())

        class _BomResponse(_FakeResponse):
            def json(self):
                raise ValueError("no json")

        bom_body = '\ufeff{"ok": true, "csrf_token": "tok"}'
        payload = client._read_json(_BomResponse({}, text=bom_body), "login")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["csrf_token"], "tok")

    def test_list_tasks_uses_cache_busting_headers(self) -> None:
        client = DaszekClient(self._make_settings())
        fake_session = _FakeSession(_FakeResponse({}), get_response=_FakeResponse([{"id": "t1"}]))
        client.session = fake_session

        tasks = client.list_tasks(refresh=True)

        self.assertEqual(tasks[0]["id"], "t1")
        self.assertEqual(fake_session.last_get_headers["Cache-Control"], "no-cache")
        self.assertEqual(fake_session.last_get_headers["Pragma"], "no-cache")
        self.assertIn("_runtime_ts", fake_session.last_get_params)

    def test_mark_done_updates_cached_task(self) -> None:
        client = DaszekClient(self._make_settings())
        client.csrf_token = "csrf-token"
        client.tasks_cache = [{"id": "task_1", "status": "open"}]
        fake_session = _FakeSession(_FakeResponse({"ok": True, "task": {"id": "task_1", "status": "done"}}))
        client.session = fake_session

        task = client.mark_done("task_1")

        self.assertEqual(task["status"], "done")
        self.assertEqual(client.tasks_cache[0]["status"], "done")
        self.assertTrue(fake_session.last_url.endswith("/tasks/task_1/done"))
        self.assertEqual(fake_session.last_headers["X-CSRF-Token"], "csrf-token")
        self.assertTrue(fake_session.last_headers["Referer"].endswith("/daszek/"))

    def test_push_v2_projection_posts_to_v2_ingest(self) -> None:
        client = DaszekClient(self._make_settings())
        client.csrf_token = "csrf-token"
        fake_session = _FakeSession(
            _FakeResponse(
                {
                    "ok": True,
                    "shadow_contract": "daszek_v2_ingest",
                    "persisted": {"signal_id": "sig_1", "trace_id": "trace_1"},
                }
            )
        )
        client.session = fake_session

        result = client.push_v2_projection(
            {
                "message_key": "msg_1",
                "signal_projection": {"signal_id": "sig_1"},
                "case_patch": {"command": "upsert_case"},
                "desk_note_patch": {"command": "create"},
                "decision_trace": {"trace_id": "trace_1"},
            }
        )

        self.assertEqual(result.status, "ingested")
        self.assertEqual(result.message_id, "msg_1")
        self.assertEqual(result.signal_id, "sig_1")
        self.assertEqual(result.trace_id, "trace_1")
        self.assertTrue(fake_session.last_url.endswith("/wp-json/daszek/v2/ingest"))
        self.assertEqual(fake_session.last_headers["X-CSRF-Token"], "csrf-token")

    def test_login_refreshes_csrf_from_endpoint(self) -> None:
        client = DaszekClient(self._make_settings())
        fake_session = _FakeSession(
            _FakeResponse({"ok": True, "user": "daszek", "csrf_token": "login-token"}),
            get_responses=[
                _FakeResponse({}, url="https://topinstal.com.pl/daszek/"),
            ],
        )
        client.session = fake_session

        client.login()

        self.assertEqual(client.csrf_token, "login-token")
        self.assertEqual(client.base_url, "https://topinstal.com.pl")
        self.assertTrue(fake_session.last_url.endswith("/login"))
        self.assertTrue(fake_session.last_headers["Referer"].endswith("/daszek/"))

    def test_login_uses_bootstrap_csrf_header_when_available(self) -> None:
        client = DaszekClient(self._make_settings())
        fake_session = _FakeSession(
            _FakeResponse({"ok": True, "user": "daszek", "csrf_token": "login-token"}),
            get_responses=[
                _FakeResponse(
                    {},
                    text='<meta name="csrf-token" content="page-token">',
                    url="https://topinstal.com.pl/daszek/",
                ),
            ],
        )
        client.session = fake_session

        client.login()

        self.assertEqual(fake_session.last_headers["X-CSRF-Token"], "page-token")
        self.assertTrue(fake_session.last_headers["Referer"].endswith("/daszek/"))
        self.assertEqual(client.csrf_token, "login-token")

    def test_login_falls_back_to_csrf_endpoint_when_login_omits_token(self) -> None:
        client = DaszekClient(self._make_settings())
        fake_session = _FakeSession(
            _FakeResponse({"ok": True, "user": "daszek"}),
            get_responses=[
                _FakeResponse({}, url="https://topinstal.com.pl/daszek/"),
                _FakeResponse({"csrf_token": "session-token"}, url="https://topinstal.com.pl/wp-json/daszek/v1/csrf"),
            ],
        )
        client.session = fake_session

        client.login()

        self.assertEqual(client.csrf_token, "session-token")
        self.assertTrue(fake_session.last_get_url.endswith("/csrf"))

    def test_readback_v2_projection_returns_actionable_note_anchor(self) -> None:
        client = DaszekClient(self._make_settings())
        fake_session = _FakeSession(
            _FakeResponse({}),
            get_response=_FakeResponse(
                {
                    "ok": True,
                    "note": {
                        "note_id": "note_1",
                        "desk_note_id": "note_1",
                        "case_id": "case_1",
                        "title": "Visible title",
                        "source_message_id": "mid_1",
                        "source_signal_ids": ["sig_1"],
                    },
                }
            ),
        )
        client.session = fake_session

        readback = client.readback_v2_projection(
            payload={
                "message_key": "mid_1",
                "signal_projection": {"signal_id": "sig_1"},
                "case_patch": {"case_id": "case_1"},
                "desk_note_patch": {"desk_note_id": "note_1", "case_id": "case_1"},
            },
            ingest_details={"ok": True},
        )

        self.assertEqual(readback["store_readback"], "found")
        self.assertEqual(readback["readback_note_id"], "note_1")
        self.assertEqual(readback["readback_case_id"], "case_1")
        self.assertEqual(readback["readback_title"], "Visible title")
        self.assertEqual(readback["readback_source_message_id"], "mid_1")
        self.assertEqual(readback["readback_source_signal_ids"], ["sig_1"])
        self.assertTrue(readback["operator_action_available"])
        self.assertEqual(readback["allowed_operator_actions"], ["zla_sprawa"])
        self.assertEqual(readback["expected_bridge_domain"], "adjudication")
        self.assertEqual(readback["expected_adjudication_kind"], "reject_same_case")

    def test_bridge_queue_fetch_uses_token_header(self) -> None:
        settings = self._make_settings()
        settings.daszek_bridge_token = "bridge-token"
        client = DaszekClient(settings)
        fake_session = _FakeSession(_FakeResponse({}), get_response=_FakeResponse({"ok": True, "items": []}))
        client.session = fake_session

        data = client.get_v2_bridge_queue(limit=7)

        self.assertTrue(data["ok"])
        self.assertTrue(fake_session.last_get_url.endswith("/wp-json/daszek/v2/bridge-queue"))
        self.assertEqual(fake_session.last_get_headers["X-Daszek-Bridge-Token"], "bridge-token")
        self.assertEqual(fake_session.last_get_params["status"], "pending")
        self.assertEqual(fake_session.last_get_params["limit"], 7)

    def test_bridge_queue_completion_posts_token_header(self) -> None:
        settings = self._make_settings()
        settings.daszek_bridge_token = "bridge-token"
        client = DaszekClient(settings)
        fake_session = _FakeSession(_FakeResponse({"ok": True, "completed": {"queue_id": "bq_1"}}))
        client.session = fake_session

        data = client.complete_v2_bridge_queue_item("bq_1")

        self.assertTrue(data["ok"])
        self.assertTrue(fake_session.last_url.endswith("/wp-json/daszek/v2/bridge-queue/complete"))
        self.assertEqual(fake_session.last_headers["X-Daszek-Bridge-Token"], "bridge-token")
        self.assertEqual(fake_session.last_json["queue_id"], "bq_1")

    def test_bridge_queue_requires_token(self) -> None:
        client = DaszekClient(self._make_settings())
        with self.assertRaises(DaszekClientError):
            client.get_v2_bridge_queue()

    @staticmethod
    def _make_settings() -> Settings:
        return Settings(
            llm_backend="groq",
            openai_compat_base_url="",
            openai_compat_api_key="",
            groq_api_key="",
            google_access_token="",
            google_client_id="",
            google_client_secret="",
            google_refresh_token="",
            google_token_endpoint="https://oauth2.googleapis.com/token",
            google_oauth_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
            groq_model="openai/gpt-oss-120b",
            groq_base_url="https://api.groq.com",
            daszek_base_url="https://www.topinstal.com.pl",
            daszek_login="daszek",
            daszek_password="secret",
            daszek_v2_push_enabled=False,
            case_guidance_enabled=False,
            case_guidance_model="openai/gpt-oss-120b",
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
            http_timeout=60,
            http_max_retries=4,
            http_retry_base_delay=2.0,
            env_path=Path("tools/gmail_audit/.env"),
            config_sources={},
            config_warnings=[],
            google_access_token_had_bearer_prefix=False,
            google_runtime_access_token="",
            google_runtime_access_token_expires_at=0.0,
            google_runtime_token_type="",
            google_active_token_source="",
        )


if __name__ == "__main__":
    unittest.main()
