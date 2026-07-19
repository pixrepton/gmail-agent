from __future__ import annotations

import json
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


def _sample_case_row(
    *,
    case_id: str = "case_lead_1",
    case_family: str = "lead_oferta",
    requires_action: bool = True,
    priority_label: str = "P1",
) -> tuple:
    meta = {
        "requires_action": requires_action,
        "priority_label": priority_label,
        "export_case_type": "lead_oferta",
    }
    return (
        case_id,
        case_family,
        "Oferta klimatyzacja",
        "open",
        "Jan Kowalski",
        "jan@example.com",
        json.dumps(meta),
        "2026-01-01",
        "2026-01-02",
        "2026-01-02",
    )


def test_list_cases_returns_customer_cases() -> None:
    client = _make_client()
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = [_sample_case_row()]
    mock_conn.cursor.return_value = mock_cur

    with patch("api_app.load_settings", return_value=_mock_settings()):
        with patch("psycopg.connect", return_value=mock_conn):
            response = client.get("/cases?view=full")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["cases"][0]["case_id"] == "case_lead_1"
    assert body["cases"][0]["requires_action"] is True
    assert body["cases"][0]["desk_eligible"] is True


def test_list_cases_filters_requires_action() -> None:
    client = _make_client()
    rows = [
        _sample_case_row(case_id="a1", requires_action=True),
        _sample_case_row(case_id="a2", requires_action=False, priority_label="P3"),
    ]
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = rows
    mock_conn.cursor.return_value = mock_cur

    with patch("api_app.load_settings", return_value=_mock_settings()):
        with patch("psycopg.connect", return_value=mock_conn):
            response = client.get("/cases?requires_action=false")

    body = response.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["cases"][0]["case_id"] == "a2"


def test_list_cases_desk_only_filter() -> None:
    client = _make_client()
    rows = [
        _sample_case_row(case_id="desk1", priority_label="P1"),
        _sample_case_row(case_id="offdesk", priority_label="P3"),
    ]
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = rows
    mock_conn.cursor.return_value = mock_cur

    with patch("api_app.load_settings", return_value=_mock_settings()):
        with patch("psycopg.connect", return_value=mock_conn):
            response = client.get("/cases?desk_only=true")

    body = response.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["cases"][0]["case_id"] == "desk1"


def test_list_cases_filters_case_family() -> None:
    client = _make_client()
    rows = [
        _sample_case_row(case_id="lead1", case_family="lead_oferta"),
        _sample_case_row(case_id="svc1", case_family="serwis"),
    ]
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = [rows[0]]
    mock_conn.cursor.return_value = mock_cur

    with patch("api_app.load_settings", return_value=_mock_settings()):
        with patch("psycopg.connect", return_value=mock_conn):
            response = client.get("/cases?case_family=lead_oferta")

    body = response.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["cases"][0]["case_family"] == "lead_oferta"
    sql = mock_cur.execute.call_args[0][0]
    assert "case_family != 'internal_task'" in sql


def test_list_cases_no_db() -> None:
    client = _make_client()
    settings = _mock_settings("")
    with patch("api_app.load_settings", return_value=settings):
        response = client.get("/cases")
    body = response.json()
    assert body["ok"] is False
    assert body["cases"] == []
