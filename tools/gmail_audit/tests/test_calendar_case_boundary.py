from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from calendar_runtime import CalendarRuntime
from mailbox_memory_store import InMemoryMailboxMemoryStore


def test_calendar_ingest_does_not_link_internal_task_case() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case(
        {
            "case_id": "case-task-cal",
            "case_key": "task-cal",
            "subject": "Faktura firmowa",
            "status": "open",
            "case_family": "internal_task",
            "customer_name": "Shared Klient",
            "updated_at": "2026-07-07T10:00:00+00:00",
        }
    )
    store.upsert_case(
        {
            "case_id": "case-lead-cal",
            "case_key": "lead-cal",
            "subject": "Serwis pompy",
            "status": "open",
            "case_family": "lead_opportunity",
            "customer_name": "Shared Klient",
            "customer_email": "shared.klient@example.com",
            "updated_at": "2026-07-07T11:00:00+00:00",
        }
    )

    client = MagicMock()
    client.list_events.return_value = [
        {
            "id": "cal-evt-1",
            "summary": "Wizyta Shared Klient serwis",
            "attendees": [{"email": "shared.klient@example.com"}],
            "start": {"dateTime": "2026-07-08T09:00:00+02:00"},
            "end": {"dateTime": "2026-07-08T10:00:00+02:00"},
        }
    ]
    settings = MagicMock()
    settings.google_calendar_id = "primary"

    result = CalendarRuntime(settings=settings, store=store, client=client).ingest_events(dry_run=True)

    assert result["ok"] is True
    assert len(result["events"]) == 1
    linked_case_id = str(result["events"][0].get("case_id") or "")
    assert linked_case_id == "case-lead-cal"
    assert linked_case_id != "case-task-cal"
