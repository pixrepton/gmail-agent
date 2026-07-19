from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from drive_change_detector import DriveChangeDetector
from gmail_change_detector import GmailChangeDetector
from google_gmail_api import GoogleGmailApiError
from mailbox_memory_store import InMemoryMailboxMemoryStore


def test_gmail_change_detector_bootstraps_and_persists_cursor() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    detector = GmailChangeDetector(SimpleNamespace(), store=store)
    with patch("gmail_change_detector.get_profile", return_value={"email": "ops@example.com", "historyId": "9001"}):
        result = detector.poll_changes(cursor_scope="mailbox", bootstrap_if_missing=True)

    cursor = store.fetch_source_cursor("gmail", "mailbox")
    assert result["status"] == "bootstrapped"
    assert result["event_count"] == 0
    assert cursor["last_cursor"] == "9001"
    assert cursor["metadata_json"]["mailbox"] == "ops@example.com"


def test_gmail_change_detector_extracts_events_and_advances_cursor() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_source_cursor(
        {
            "cursor_key": "gmail:mailbox",
            "source_kind": "gmail",
            "cursor_scope": "mailbox",
            "last_cursor": "9001",
            "last_success_at": None,
            "last_error": "",
            "status": "ok",
            "metadata_json": {"mailbox": "ops@example.com"},
            "updated_at": "2026-04-13T10:00:00+02:00",
        }
    )
    detector = GmailChangeDetector(SimpleNamespace(), store=store)
    with patch("gmail_change_detector.get_profile", return_value={"email": "ops@example.com", "historyId": "9002"}), patch(
        "gmail_change_detector.list_history",
        return_value={
            "historyId": "9002",
            "history": [
                {
                    "id": "9002",
                    "messagesAdded": [{"message": {"id": "msg-1", "threadId": "thr-1"}}],
                    "labelsAdded": [{"message": {"id": "msg-1", "threadId": "thr-1"}, "labelIds": ["STARRED"]}],
                }
            ],
        },
    ):
        result = detector.poll_changes(cursor_scope="mailbox", bootstrap_if_missing=False)

    assert result["status"] == "ok"
    assert result["event_count"] == 2
    assert store.fetch_source_cursor("gmail", "mailbox")["last_cursor"] == "9002"


def test_gmail_change_detector_persists_resume_page_token_for_bounded_pagination() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_source_cursor(
        {
            "cursor_key": "gmail:mailbox",
            "source_kind": "gmail",
            "cursor_scope": "mailbox",
            "last_cursor": "9001",
            "last_success_at": None,
            "last_error": "",
            "status": "ok",
            "metadata_json": {"mailbox": "ops@example.com"},
            "updated_at": "2026-04-13T10:00:00+02:00",
        }
    )
    detector = GmailChangeDetector(SimpleNamespace(), store=store)
    with patch("gmail_change_detector.get_profile", return_value={"email": "ops@example.com", "historyId": "9003"}), patch(
        "gmail_change_detector.list_history",
        side_effect=[
            {
                "historyId": "9002",
                "nextPageToken": "page-2",
                "history": [{"id": "9002", "messagesAdded": [{"message": {"id": "msg-1", "threadId": "thr-1"}}]}],
            },
            {
                "historyId": "9003",
                "history": [{"id": "9003", "messagesAdded": [{"message": {"id": "msg-2", "threadId": "thr-2"}}]}],
            },
        ],
    ) as list_history_mock:
        first = detector.poll_changes(cursor_scope="mailbox", bootstrap_if_missing=False, max_pages=1)
        first_cursor = store.fetch_source_cursor("gmail", "mailbox")
        second = detector.poll_changes(cursor_scope="mailbox", bootstrap_if_missing=False, max_pages=2)
        second_cursor = store.fetch_source_cursor("gmail", "mailbox")

    assert first["has_more"] is True
    assert first["next_page_token"] == "page-2"
    assert first_cursor["last_cursor"] == "9001"
    assert first_cursor["metadata_json"]["resume_page_token"] == "page-2"
    assert list_history_mock.call_args_list[0].kwargs["page_token"] is None
    assert list_history_mock.call_args_list[1].kwargs["page_token"] == "page-2"
    assert second["event_count"] == 1
    assert second["has_more"] is False
    assert second_cursor["last_cursor"] == "9003"
    assert second_cursor["metadata_json"]["resume_page_token"] == ""


def test_gmail_change_detector_recovers_stale_cursor_on_http_404(caplog) -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_source_cursor(
        {
            "cursor_key": "gmail:mailbox",
            "source_kind": "gmail",
            "cursor_scope": "mailbox",
            "last_cursor": "1526698",
            "last_success_at": None,
            "last_error": "",
            "status": "ok",
            "metadata_json": {"mailbox": "ops@example.com"},
            "updated_at": "2026-04-13T10:00:00+02:00",
        }
    )
    detector = GmailChangeDetector(SimpleNamespace(), store=store)
    stale_error = GoogleGmailApiError("Requested Gmail resource was not found (HTTP 404).")
    with patch("gmail_change_detector.get_profile", return_value={"email": "ops@example.com", "historyId": "1580177"}), patch(
        "gmail_change_detector.list_history",
        side_effect=stale_error,
    ):
        with caplog.at_level("WARNING"):
            result = detector.poll_changes(cursor_scope="mailbox", bootstrap_if_missing=False)

    cursor = store.fetch_source_cursor("gmail", "mailbox")
    assert result["status"] == "bootstrapped"
    assert result["bootstrap_reason"] == "stale_history_http_404"
    assert result["last_cursor"] == "1580177"
    assert cursor["last_cursor"] == "1580177"
    assert cursor["status"] == "ok"
    assert cursor["metadata_json"]["stale_cursor_recovered"] is True
    assert cursor["metadata_json"]["replaced_cursor"] == "1526698"
    assert any("auto-bootstrap" in record.message for record in caplog.records)


def test_gmail_change_detector_does_not_recover_non_http_404_errors() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    store.upsert_source_cursor(
        {
            "cursor_key": "gmail:mailbox",
            "source_kind": "gmail",
            "cursor_scope": "mailbox",
            "last_cursor": "9001",
            "last_success_at": None,
            "last_error": "",
            "status": "ok",
            "metadata_json": {"mailbox": "ops@example.com"},
            "updated_at": "2026-04-13T10:00:00+02:00",
        }
    )
    detector = GmailChangeDetector(SimpleNamespace(), store=store)
    with patch("gmail_change_detector.get_profile", return_value={"email": "ops@example.com", "historyId": "9002"}), patch(
        "gmail_change_detector.list_history",
        side_effect=GoogleGmailApiError("Gmail API rejected the request (HTTP 400)."),
    ):
        try:
            detector.poll_changes(cursor_scope="mailbox", bootstrap_if_missing=False)
        except GoogleGmailApiError:
            pass
        else:
            raise AssertionError("expected GoogleGmailApiError")

    cursor = store.fetch_source_cursor("gmail", "mailbox")
    assert cursor["last_cursor"] == "9001"
    assert cursor["status"] == "error"


class _FakeDriveClient:
    def get_start_page_token(self) -> str:
        return "start-1"

    def list_changes(self, *, page_token: str, page_size: int | None = None, include_removed: bool = True) -> dict[str, object]:
        assert page_token == "start-1"
        return {
            "changes": [
                {
                    "fileId": "drv-1",
                    "removed": False,
                    "time": "2026-04-13T11:00:00Z",
                    "file": {"id": "drv-1", "name": "invoice.pdf", "modifiedTime": "2026-04-13T11:00:00Z"},
                },
                {
                    "fileId": "drv-2",
                    "removed": True,
                    "time": "2026-04-13T11:05:00Z",
                    "file": {},
                },
            ],
            "next_page_token": "",
            "new_start_page_token": "start-2",
        }


class _PagedDriveClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_start_page_token(self) -> str:
        return "start-1"

    def list_changes(self, *, page_token: str, page_size: int | None = None, include_removed: bool = True) -> dict[str, object]:
        self.calls.append(page_token)
        if page_token == "start-1":
            return {
                "changes": [
                    {
                        "fileId": "drv-1",
                        "removed": False,
                        "time": "2026-04-13T11:00:00Z",
                        "file": {"id": "drv-1", "name": "invoice.pdf", "modifiedTime": "2026-04-13T11:00:00Z"},
                    }
                ],
                "next_page_token": "page-2",
                "new_start_page_token": "",
            }
        return {
            "changes": [
                {
                    "fileId": "drv-2",
                    "removed": True,
                    "time": "2026-04-13T11:05:00Z",
                    "file": {},
                }
            ],
            "next_page_token": "",
            "new_start_page_token": "start-2",
        }


def test_drive_change_detector_bootstraps_then_emits_events() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    detector = DriveChangeDetector(SimpleNamespace(), store=store, client=_FakeDriveClient())

    boot = detector.poll_changes(cursor_scope="drive", bootstrap_if_missing=True)
    poll = detector.poll_changes(cursor_scope="drive", bootstrap_if_missing=False)

    cursor = store.fetch_source_cursor("drive", "drive")
    assert boot["status"] == "bootstrapped"
    assert poll["status"] == "ok"
    assert poll["event_count"] == 2
    assert cursor["last_cursor"] == "start-2"


def test_drive_change_detector_drains_multiple_pages_and_advances_cursor() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    detector = DriveChangeDetector(SimpleNamespace(), store=store, client=_PagedDriveClient())

    detector.poll_changes(cursor_scope="drive", bootstrap_if_missing=True)
    poll = detector.poll_changes(cursor_scope="drive", bootstrap_if_missing=False, max_pages=2)

    cursor = store.fetch_source_cursor("drive", "drive")
    assert poll["event_count"] == 2
    assert poll["page_count"] == 2
    assert poll["has_more"] is False
    assert cursor["last_cursor"] == "start-2"


def test_drive_change_detector_persists_next_page_token_when_bounded() -> None:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    detector = DriveChangeDetector(SimpleNamespace(), store=store, client=_PagedDriveClient())

    detector.poll_changes(cursor_scope="drive", bootstrap_if_missing=True)
    poll = detector.poll_changes(cursor_scope="drive", bootstrap_if_missing=False, max_pages=1)

    cursor = store.fetch_source_cursor("drive", "drive")
    assert poll["event_count"] == 1
    assert poll["has_more"] is True
    assert poll["next_page_token"] == "page-2"
    assert cursor["last_cursor"] == "page-2"
    assert cursor["metadata_json"]["has_more"] is True
