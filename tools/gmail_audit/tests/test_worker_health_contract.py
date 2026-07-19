from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app, _worker_health_stale_threshold_seconds


class _FakeCursor:
    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    def fetchone(self) -> tuple[str, datetime, int, str]:
        return (
            "gmail-worker",
            datetime.now(timezone.utc) - timedelta(seconds=120),
            16,
            "continuous_poll",
        )


class _FakeConnection:
    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()


def test_worker_health_threshold_covers_predictive_scheduler_quiet_cadence() -> None:
    settings = SimpleNamespace(
        gmail_change_detection_enabled=True,
        drive_change_detection_enabled=False,
        gmail_history_poll_interval_sec=120,
        drive_changes_poll_interval_sec=180,
        http_timeout=60,
    )

    assert _worker_health_stale_threshold_seconds(settings) == 660


def test_system_worker_health_uses_cadence_threshold(monkeypatch) -> None:
    settings = SimpleNamespace(
        mailbox_memory_database_url="postgresql://example.invalid/db",
        gmail_change_detection_enabled=True,
        drive_change_detection_enabled=False,
        gmail_history_poll_interval_sec=120,
        drive_changes_poll_interval_sec=180,
        http_timeout=60,
    )
    fake_psycopg = SimpleNamespace(connect=lambda _db_url: _FakeConnection())
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setattr("config.load_settings", lambda **_kwargs: settings)

    client = TestClient(
        create_app(
            runtime_provider=lambda: None,
            cohort_reader=lambda _run_id: None,
            registry_provider=lambda: None,
        )
    )

    response = client.get("/system/worker/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "alive"
    assert body["stale_threshold_sec"] == 660
