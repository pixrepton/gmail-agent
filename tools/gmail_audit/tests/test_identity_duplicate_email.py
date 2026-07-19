from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import mailbox_memory_runtime
from api_app import _find_duplicate_email_from_mailbox_store, create_app
from mailbox_memory_models import CaseContextPack


class _Runtime:
    def __init__(self) -> None:
        self.pack = CaseContextPack(case_id="case_identity_dup")

    def get_context_pack(self, *, case_id: str = "", message_id: str = "", query_text: str = "") -> CaseContextPack:
        return self.pack


class _Store:
    def _connect(self, row_factory: bool = False):
        return self._conn

    def __init__(self, conn: MagicMock) -> None:
        self._conn = conn


def _make_client() -> TestClient:
    app = create_app(
        runtime_provider=lambda: _Runtime(),
        cohort_reader=lambda run_id: None,
        registry_provider=lambda: None,
    )
    return TestClient(app)


def _mailbox_store_with_rows(rows: list[dict]) -> tuple[_Store, str]:
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = rows
    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    executed_sql = ""

    def _capture_execute(sql: str, params: dict | None = None) -> None:
        nonlocal executed_sql
        executed_sql = sql

    mock_cur.execute.side_effect = _capture_execute
    return _Store(mock_conn), executed_sql


def test_find_duplicate_email_queries_mailbox_memory_cases() -> None:
    settings = MagicMock()
    settings.mailbox_memory_database_url = "postgresql://test/test"

    store, _ = _mailbox_store_with_rows([{"customer_email": "dup@example.com", "cnt": 2}])
    mock_runtime = MagicMock()
    mock_runtime.store = store

    with patch.object(mailbox_memory_runtime, "build_mailbox_memory_runtime", return_value=mock_runtime):
        rows = _find_duplicate_email_from_mailbox_store(settings, limit=10)

    assert rows == [{"customer_email": "dup@example.com", "cnt": 2}]
    store._conn.cursor.return_value.execute.assert_called_once()
    sql = store._conn.cursor.return_value.execute.call_args[0][0]
    assert "mailbox_memory_cases" in sql
    assert "FROM cases" not in sql


def test_identity_suggestions_falls_back_to_mailbox_memory_cases() -> None:
    client = _make_client()
    settings = MagicMock()
    settings.mailbox_memory_database_url = "postgresql://test/test"

    store, _ = _mailbox_store_with_rows([{"customer_email": "merge@example.com", "cnt": 3}])
    mock_runtime = MagicMock()
    mock_runtime.store = store

    with patch("api_app.load_settings", return_value=settings):
        with patch.object(mailbox_memory_runtime, "build_mailbox_memory_runtime", return_value=mock_runtime):
            response = client.get("/identity/suggestions?limit=5")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["items"][0]["customer_email"] == "merge@example.com"
