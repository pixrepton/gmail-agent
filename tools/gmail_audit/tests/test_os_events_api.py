from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app
from event_spine.query import fetch_os_events_for_engagement


def test_engagement_os_events_route_returns_read_only_list() -> None:
    sample = [
        {
            "event_id": "osevt_abc",
            "event_type": "gmail.hitl.approved",
            "source_repo": "gmail-agent",
            "engagement_id": "eng_1",
            "occurred_at": "2026-06-17T10:00:00+00:00",
            "summary_pl": "Operator zatwierdził szkic",
            "status": "ok",
            "payload": {"summary_pl": "Operator zatwierdził szkic", "status": "ok"},
            "correlation": {"case_id": "case_1"},
        }
    ]

    with patch("api_app.load_settings") as load_settings:
        load_settings.return_value = __import__("types").SimpleNamespace(
            mailbox_memory_database_url="postgresql://test"
        )
        with patch("api_app.fetch_os_events_for_engagement", return_value=sample):
            app = create_app(runtime_provider=lambda: None, registry_provider=lambda: None)
            client = TestClient(app)
            response = client.get("/engagements/eng_1/os-events")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["read_only"] is True
    assert body["engagement_id"] == "eng_1"
    assert body["items"][0]["event_type"] == "gmail.hitl.approved"
    assert body["items"][0]["summary_pl"] == "Operator zatwierdził szkic"


def test_system_os_events_recent_route_returns_read_only_list() -> None:
    sample = [
        {
            "event_id": "osevt_cieplo",
            "event_type": "cieplo.workflow.pdf_ready",
            "source_repo": "cieplo-orchestrator",
            "engagement_id": "",
            "occurred_at": "2026-06-17T12:00:00+00:00",
            "summary_pl": "PDF oferty Cieplo gotowy",
            "status": "ok",
            "payload": {"summary_pl": "PDF oferty Cieplo gotowy", "status": "ok"},
            "correlation": {"workflow_id": "wf-1"},
        }
    ]

    with patch("api_app.load_settings") as load_settings:
        load_settings.return_value = __import__("types").SimpleNamespace(
            mailbox_memory_database_url="postgresql://test"
        )
        with patch("api_app.fetch_recent_os_events", return_value=sample):
            app = create_app(runtime_provider=lambda: None, registry_provider=lambda: None)
            client = TestClient(app)
            response = client.get("/system/os-events/recent")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["read_only"] is True
    assert body["items"][0]["event_type"] == "cieplo.workflow.pdf_ready"
