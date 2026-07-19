"""Durable Google Drive Changes API polling for the unified signal worker."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from drive_client import GoogleDriveClient
from config import Settings


@dataclass(slots=True, frozen=True)
class DriveSourceEvent:
    event_id: str
    change_id: str
    file_id: str
    removed: bool
    observed_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DriveChangeDetector:
    """Bounded Drive change polling with durable page-token persistence."""

    def __init__(self, settings: Settings, *, store: Any, client: GoogleDriveClient | None = None) -> None:
        self.settings = settings
        self.store = store
        self.client = client or GoogleDriveClient(settings)

    def poll_changes(
        self,
        *,
        cursor_scope: str = "default",
        max_results: int = 100,
        max_pages: int = 3,
        bootstrap_if_missing: bool = True,
    ) -> dict[str, Any]:
        cursor_row = self.store.fetch_source_cursor("drive", cursor_scope)
        now_iso = datetime.now().astimezone().isoformat()

        if cursor_row is None or not str(cursor_row.get("last_cursor") or "").strip():
            start_page_token = self.client.get_start_page_token()
            if bootstrap_if_missing:
                self._upsert_cursor(
                    cursor_scope=cursor_scope,
                    last_cursor=start_page_token,
                    last_success_at=now_iso,
                    last_error="",
                    status="ok",
                    metadata={"bootstrapped": True},
                )
                return {
                    "status": "bootstrapped",
                    "cursor_scope": cursor_scope,
                    "last_cursor": start_page_token,
                    "events": [],
                    "event_count": 0,
                }
            cursor_row = {"last_cursor": start_page_token}

        page_token = str(cursor_row.get("last_cursor") or "").strip()
        self._upsert_cursor(
            cursor_scope=cursor_scope,
            last_cursor=page_token,
            last_success_at=str(cursor_row.get("last_success_at") or ""),
            last_error="",
            status="running",
            metadata={"page_token": page_token},
        )
        try:
            events, durable_cursor, next_page_token, new_start_page_token, page_count = self._list_change_pages(
                page_token=page_token,
                max_results=max_results,
                max_pages=max_pages,
            )
        except Exception as exc:
            self._upsert_cursor(
                cursor_scope=cursor_scope,
                last_cursor=page_token,
                last_success_at=str(cursor_row.get("last_success_at") or ""),
                last_error=str(exc),
                status="error",
                metadata={"page_token": page_token},
            )
            raise

        has_more = bool(next_page_token)
        self._upsert_cursor(
            cursor_scope=cursor_scope,
            last_cursor=durable_cursor,
            last_success_at=now_iso,
            last_error="",
            status="ok",
            metadata={
                "event_count": len(events),
                "page_count": page_count,
                "has_more": has_more,
                "next_page_token": next_page_token,
                "new_start_page_token": new_start_page_token,
            },
        )
        return {
            "status": "ok",
            "cursor_scope": cursor_scope,
            "last_cursor": durable_cursor,
            "events": [event.to_dict() for event in events],
            "event_count": len(events),
            "page_count": page_count,
            "has_more": has_more,
            "next_page_token": next_page_token,
            "new_start_page_token": new_start_page_token,
        }

    def _list_change_pages(
        self,
        *,
        page_token: str,
        max_results: int,
        max_pages: int,
    ) -> tuple[list[DriveSourceEvent], str, str, str, int]:
        events_by_id: dict[str, DriveSourceEvent] = {}
        current_page_token = page_token
        last_consumed_token = page_token
        latest_new_start_page_token = ""
        page_count = 0
        while True:
            payload = self.client.list_changes(
                page_token=current_page_token,
                page_size=max_results,
                include_removed=True,
            )
            for event in extract_change_events(payload):
                events_by_id.setdefault(event.event_id, event)
            latest_new_start_page_token = str(payload.get("new_start_page_token") or latest_new_start_page_token).strip()
            next_page_token = str(payload.get("next_page_token") or "").strip()
            last_consumed_token = current_page_token
            page_count += 1
            if not next_page_token or page_count >= max(1, int(max_pages)):
                durable_cursor = next_page_token or latest_new_start_page_token or last_consumed_token
                events = sorted(events_by_id.values(), key=lambda item: (item.observed_at, item.file_id))
                return events, durable_cursor, next_page_token, latest_new_start_page_token, page_count
            current_page_token = next_page_token

    def _upsert_cursor(
        self,
        *,
        cursor_scope: str,
        last_cursor: str,
        last_success_at: str,
        last_error: str,
        status: str,
        metadata: dict[str, Any],
    ) -> None:
        self.store.upsert_source_cursor(
            {
                "cursor_key": f"drive:{cursor_scope}",
                "source_kind": "drive",
                "cursor_scope": cursor_scope,
                "last_cursor": str(last_cursor or ""),
                "last_success_at": str(last_success_at or "") or None,
                "last_error": str(last_error or ""),
                "status": str(status or "idle"),
                "metadata_json": dict(metadata or {}),
                "updated_at": datetime.now().astimezone().isoformat(),
            }
        )


def extract_change_events(payload: dict[str, Any]) -> list[DriveSourceEvent]:
    events: list[DriveSourceEvent] = []
    seen: set[str] = set()
    for row in payload.get("changes") or []:
        file_id = str(row.get("fileId") or "").strip()
        if not file_id:
            continue
        removed = bool(row.get("removed"))
        metadata = dict(row.get("file") or {})
        observed_at = str(row.get("time") or metadata.get("modifiedTime") or datetime.now().astimezone().isoformat())
        change_id = _stable_change_id(
            file_id=file_id,
            marker=str(metadata.get("modifiedTime") or row.get("time") or ""),
            removed=removed,
        )
        if change_id in seen:
            continue
        seen.add(change_id)
        events.append(
            DriveSourceEvent(
                event_id=f"drvevt_{change_id}",
                change_id=change_id,
                file_id=file_id,
                removed=removed,
                observed_at=observed_at,
                metadata=metadata,
            )
        )
    events.sort(key=lambda item: (item.observed_at, item.file_id))
    return events


def poll_drive_changes(
    settings: Settings,
    *,
    store: Any,
    client: GoogleDriveClient | None = None,
    cursor_scope: str = "default",
    max_results: int = 100,
    max_pages: int = 3,
    bootstrap_if_missing: bool = True,
) -> dict[str, Any]:
    detector = DriveChangeDetector(settings, store=store, client=client)
    return detector.poll_changes(
        cursor_scope=cursor_scope,
        max_results=max_results,
        max_pages=max_pages,
        bootstrap_if_missing=bootstrap_if_missing,
    )


def _stable_change_id(*, file_id: str, marker: str, removed: bool) -> str:
    digest = hashlib.sha256(f"{file_id}|{marker}|{int(removed)}".encode("utf-8")).hexdigest()
    return digest[:24]


__all__ = [
    "DriveChangeDetector",
    "DriveSourceEvent",
    "extract_change_events",
    "poll_drive_changes",
]
