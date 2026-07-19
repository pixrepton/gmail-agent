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
from daszek_v3_operational_feed import build_operational_feed_from_mailbox_store
from mailbox_memory_models import CaseContextPack
from mailbox_memory_store import InMemoryMailboxMemoryStore


class _Runtime:
    def __init__(self) -> None:
        self.pack = CaseContextPack(case_id="case_api_boundary")

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


def test_operational_feed_excludes_internal_task_from_cases_and_desk() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case(
        {
            "case_id": "case-lead-1",
            "case_key": "lead-1",
            "subject": "Zapytanie o pompę",
            "status": "open",
            "case_family": "lead_opportunity",
            "updated_at": "2026-07-07T10:00:00+00:00",
            "metadata": {"priority_label": "P1 - pilne", "requires_action": True, "source_kind": "gmail_inbound"},
        }
    )
    store.upsert_snapshot(
        "case-lead-1",
        {"snapshot_json": {"status": "open", "summary_text": "Lead HVAC", "recommended_next_action": "Oferta"}},
    )
    store.upsert_case(
        {
            "case_id": "case-task-1",
            "case_key": "task-1",
            "subject": "Zadanie wewnętrzne",
            "status": "open",
            "case_family": "internal_task",
            "updated_at": "2026-07-07T11:00:00+00:00",
            "metadata": {"task_title": "Sprawdź fakturę", "task_status": "pending"},
        }
    )
    store.upsert_snapshot(
        "case-task-1",
        {"snapshot_json": {"status": "open", "summary_text": "Internal", "recommended_next_action": "Done"}},
    )

    snap = build_operational_feed_from_mailbox_store(
        store,
        case_limit=10,
        task_limit=10,
        snapshot_id="snap-case-task-boundary",
    )
    feed = snap["feed"]

    case_ids = [str(c.get("case_id") or "") for c in feed.get("cases", []) if isinstance(c, dict)]
    assert "case-lead-1" in case_ids
    assert "case-task-1" not in case_ids
    assert all(
        str(c.get("case_family") or "") != "internal_task"
        for c in feed.get("cases", [])
        if isinstance(c, dict)
    )

    desk_case_ids = [str(d.get("case_id") or "") for d in feed.get("desk", []) if isinstance(d, dict)]
    assert "case-task-1" not in desk_case_ids
    assert all(cid != "case-task-1" for cid in desk_case_ids)

    task_case_refs = [
        str(t.get("case_id") or "")
        for t in feed.get("tasks", [])
        if isinstance(t, dict) and t.get("case_id")
    ]
    assert "case-task-1" not in task_case_refs


def test_operational_feed_excludes_manual_operations_from_cases_and_desk() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case(
        {
            "case_id": "case-manual-ops-1",
            "case_family": "operations",
            "subject": "Firmowe auto",
            "metadata": {
                "source_kind": "manual",
                "task_title": "Firmowe auto",
                "requires_action": True,
                "task_status": "confirmed",
            },
        }
    )
    feed = build_operational_feed_from_mailbox_store(store, case_limit=50)
    case_ids = [c.get("case_id") for c in (feed.get("cases") or []) if isinstance(c, dict)]
    desk_ids = [c.get("case_id") for c in (feed.get("desk") or []) if isinstance(c, dict)]
    assert "case-manual-ops-1" not in case_ids
    assert "case-manual-ops-1" not in desk_ids


def test_tasks_endpoint_returns_manual_operations_only() -> None:
    client = _make_client()
    meta = {"task_title": "ZUS", "task_status": "pending", "priority": "normalny", "source_kind": "manual"}
    manual_row = (
        "case-task-1",
        "operations",
        json.dumps(meta),
        "2026-07-07T10:00:00",
        "2026-07-07T10:00:00",
        "ZUS",
        None,
    )
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = [manual_row]
    mock_conn.cursor.return_value = mock_cur
    executed_sql = ""
    executed_params: tuple = ()

    def _capture_execute(sql: str, params: tuple | None = None) -> None:
        nonlocal executed_sql, executed_params
        executed_sql = sql
        executed_params = tuple(params or ())

    mock_cur.execute.side_effect = _capture_execute

    with patch("api_app.load_settings", return_value=_mock_settings()):
        with patch("psycopg.connect", return_value=mock_conn):
            response = client.get("/tasks")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["case_id"] == "case-task-1"
    assert body["tasks"][0].get("case_family") == "operations"
    assert body.get("deprecated") is True
    assert all(t.get("case_id") != "case-lead-1" for t in body["tasks"])

    assert "case_family" in executed_sql
    assert executed_params and executed_params[0] == "operations"
    assert executed_params[1] == "manual"
