from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app
from mailbox_memory_models import CaseContextPack


class _Runtime:
    def __init__(self) -> None:
        self.pack = CaseContextPack(case_id="case_api_1")

    def get_context_pack(self, *, case_id: str = "", message_id: str = "", query_text: str = "") -> CaseContextPack:
        return self.pack


def _make_client() -> TestClient:
    app = create_app(
        runtime_provider=lambda: _Runtime(),
        cohort_reader=lambda run_id: None,
        registry_provider=lambda: None,
    )
    return TestClient(app)


def _mock_settings(db_url: str = "postgresql://test/test") -> MagicMock:
    settings = MagicMock()
    settings.mailbox_memory_database_url = db_url
    return settings


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_list_tasks_returns_tasks_from_db() -> None:
    client = _make_client()
    meta = {"task_title": "Test task", "task_status": "confirmed", "priority": "normalny"}
    row = ("task_1", "internal_task", json.dumps(meta), "2026-01-01", "2026-01-02", "Test task", None)
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = [row]
    mock_conn.cursor.return_value = mock_cur

    with patch("api_app.load_settings", return_value=_mock_settings()):
        with patch("psycopg.connect", return_value=mock_conn):
            response = client.get("/tasks")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["case_id"] == "task_1"
    assert body["tasks"][0]["task_title"] == "Test task"
    assert body["deprecated"] is True
    assert "migration" in body


def test_create_task_inserts_row() -> None:
    client = _make_client()
    routing = MagicMock()
    routing.desk_eligible = True
    enriched = {
        "case_id": "task_20260706120000_abcd1234",
        "case_family": "operations",
        "metadata": {
            "task_title": "Nowe zadanie",
            "source_kind": "manual",
            "requires_action": True,
            "priority": "pilne",
        },
    }

    os.environ["NODE_B_REGISTRY_TOKEN"] = "service-token"
    try:
        with patch("api_app.load_settings", return_value=_mock_settings()):
            with patch("mailbox_memory_store.PostgresMailboxMemoryStore"):
                with patch("case_write_gateway.write_case_row", return_value=(enriched, routing)) as mock_write:
                    with patch("api_app.datetime") as mock_dt:
                        mock_dt.now.return_value.strftime.return_value = "20260706120000"
                        mock_dt.now.return_value.isoformat.return_value = "2026-07-06T12:00:00"
                        with patch("api_app.uuid4") as mock_uuid:
                            mock_uuid.return_value.hex = "abcd1234"
                            response = client.post(
                                "/tasks",
                                json={"title": "Nowe zadanie", "priority": "pilne"},
                                headers=_auth_headers("service-token"),
                            )
    finally:
        os.environ.pop("NODE_B_REGISTRY_TOKEN", None)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["case_id"].startswith("task_")
    assert body["task"]["task_title"] == "Nowe zadanie"
    mock_write.assert_called_once()


def test_create_task_requires_title() -> None:
    client = _make_client()
    os.environ["NODE_B_REGISTRY_TOKEN"] = "service-token"
    try:
        response = client.post("/tasks", json={"title": ""}, headers=_auth_headers("service-token"))
    finally:
        os.environ.pop("NODE_B_REGISTRY_TOKEN", None)
    assert response.status_code == 400


def test_task_write_routes_require_auth_before_store_touch() -> None:
    client = _make_client()
    with patch("case_write_gateway.write_case_row") as write_mock:
        with patch("case_write_gateway.patch_case_row") as patch_mock:
            assert client.post("/tasks", json={"title": "x"}).status_code == 401
            assert client.post("/tasks/task_1/done").status_code == 401
            assert client.post("/tasks/task_1/confirm", json={"feedback": "ok"}).status_code == 401
            assert client.post("/tasks/task_1/reject", json={"feedback": "nie"}).status_code == 401
    write_mock.assert_not_called()
    patch_mock.assert_not_called()


def test_task_write_routes_reject_bad_token() -> None:
    client = _make_client()
    os.environ["NODE_B_REGISTRY_TOKEN"] = "good-token"
    try:
        response = client.post("/tasks", json={"title": "Nowe zadanie"}, headers=_auth_headers("bad-token"))
    finally:
        os.environ.pop("NODE_B_REGISTRY_TOKEN", None)
    assert response.status_code == 401


def test_create_task_allows_valid_service_token() -> None:
    client = _make_client()
    routing = MagicMock()
    routing.desk_eligible = True
    enriched = {
        "case_id": "task_20260706120000_abcd1234",
        "case_family": "operations",
        "metadata": {
            "task_title": "Nowe zadanie",
            "source_kind": "manual",
            "requires_action": True,
            "priority": "pilne",
        },
    }

    os.environ["NODE_B_REGISTRY_TOKEN"] = "service-token"
    try:
        with patch("api_app.load_settings", return_value=_mock_settings()):
            with patch("mailbox_memory_store.PostgresMailboxMemoryStore"):
                with patch("case_write_gateway.write_case_row", return_value=(enriched, routing)) as mock_write:
                    with patch("api_app.datetime") as mock_dt:
                        mock_dt.now.return_value.strftime.return_value = "20260706120000"
                        mock_dt.now.return_value.isoformat.return_value = "2026-07-06T12:00:00"
                        with patch("api_app.uuid4") as mock_uuid:
                            mock_uuid.return_value.hex = "abcd1234"
                            response = client.post(
                                "/tasks",
                                json={"title": "Nowe zadanie", "priority": "pilne"},
                                headers=_auth_headers("service-token"),
                            )
    finally:
        os.environ.pop("NODE_B_REGISTRY_TOKEN", None)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    mock_write.assert_called_once()


def test_task_write_routes_reject_read_only_token() -> None:
    client = _make_client()
    os.environ["NODE_B_READ_ONLY_TOKEN"] = "read-only-token"
    try:
        response = client.post(
            "/tasks",
            json={"title": "Nowe zadanie"},
            headers=_auth_headers("read-only-token"),
        )
    finally:
        os.environ.pop("NODE_B_READ_ONLY_TOKEN", None)
    assert response.status_code == 401


def test_task_write_routes_allow_valid_operator_token() -> None:
    client = _make_client()
    enriched = {
        "case_id": "task_done",
        "metadata": {"task_title": "Done me", "task_status": "done"},
    }
    routing = MagicMock()
    os.environ["DASZEK_NODE_B_API_TOKEN"] = "operator-token"
    try:
        with patch("api_app.load_settings", return_value=_mock_settings()):
            with patch("mailbox_memory_store.PostgresMailboxMemoryStore"):
                with patch("case_write_gateway.patch_case_row", return_value=(enriched, routing)) as mock_patch:
                    response = client.post("/tasks/task_done/done", headers=_auth_headers("operator-token"))
    finally:
        os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    mock_patch.assert_called_once()


def test_task_write_routes_do_not_open_when_token_missing_even_in_local_profile() -> None:
    client = _make_client()
    os.environ["GMAIL_AGENT_RUNTIME_PROFILE"] = ""
    try:
        response = client.post("/tasks/task_abc/done")
    finally:
        os.environ.pop("GMAIL_AGENT_RUNTIME_PROFILE", None)
    assert response.status_code == 401


def test_mark_task_done_updates_metadata() -> None:
    client = _make_client()
    enriched = {
        "case_id": "task_abc",
        "metadata": {"task_title": "Done me", "task_status": "done", "done_at": "2026-07-08T12:00:00"},
    }
    routing = MagicMock()

    os.environ["NODE_B_REGISTRY_TOKEN"] = "service-token"
    try:
        with patch("api_app.load_settings", return_value=_mock_settings()):
            with patch("mailbox_memory_store.PostgresMailboxMemoryStore"):
                with patch("case_write_gateway.patch_case_row", return_value=(enriched, routing)) as mock_patch:
                    response = client.post("/tasks/task_abc/done", headers=_auth_headers("service-token"))
    finally:
        os.environ.pop("NODE_B_REGISTRY_TOKEN", None)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["status"] == "done"
    mock_patch.assert_called_once()


def test_confirm_task_not_found() -> None:
    client = _make_client()

    os.environ["NODE_B_REGISTRY_TOKEN"] = "service-token"
    try:
        with patch("api_app.load_settings", return_value=_mock_settings()):
            with patch("mailbox_memory_store.PostgresMailboxMemoryStore"):
                with patch("case_write_gateway.patch_case_row", side_effect=LookupError("case not found")):
                    response = client.post(
                        "/tasks/missing/confirm",
                        json={"feedback": "ok"},
                        headers=_auth_headers("service-token"),
                    )
    finally:
        os.environ.pop("NODE_B_REGISTRY_TOKEN", None)

    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_reject_task_updates_status() -> None:
    client = _make_client()
    enriched = {
        "case_id": "task_rej",
        "metadata": {
            "task_title": "Reject me",
            "task_status": "rejected",
            "operator_feedback": "nie teraz",
        },
    }
    routing = MagicMock()

    os.environ["NODE_B_REGISTRY_TOKEN"] = "service-token"
    try:
        with patch("api_app.load_settings", return_value=_mock_settings()):
            with patch("mailbox_memory_store.PostgresMailboxMemoryStore"):
                with patch("case_write_gateway.patch_case_row", return_value=(enriched, routing)) as mock_patch:
                    response = client.post(
                        "/tasks/task_rej/reject",
                        json={"feedback": "nie teraz"},
                        headers=_auth_headers("service-token"),
                    )
    finally:
        os.environ.pop("NODE_B_REGISTRY_TOKEN", None)

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["status"] == "rejected"
    mock_patch.assert_called_once()
