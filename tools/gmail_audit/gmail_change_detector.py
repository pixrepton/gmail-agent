"""Durable Gmail History API polling for the unified signal worker."""

from __future__ import annotations
from log_config import get_logger

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from google_gmail_api import GoogleGmailApiError, get_profile, list_history
from config import Settings


DEFAULT_GMAIL_HISTORY_TYPES = ("messageAdded", "labelAdded", "labelRemoved")
logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class GmailSourceEvent:
    event_id: str
    history_id: str
    message_id: str
    thread_id: str
    change_type: str
    mailbox: str
    observed_at: str
    raw_entry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GmailChangeDetector:
    """Bounded Gmail history polling with restart-safe cursor persistence."""

    def __init__(self, settings: Settings, *, store: Any) -> None:
        self.settings = settings
        self.store = store

    def poll_changes(
        self,
        *,
        cursor_scope: str = "default",
        max_results: int = 100,
        max_pages: int = 3,
        history_types: tuple[str, ...] = DEFAULT_GMAIL_HISTORY_TYPES,
        verbose: bool = False,
        bootstrap_if_missing: bool = True,
    ) -> dict[str, Any]:
        cursor_row = self.store.fetch_source_cursor("gmail", cursor_scope)
        profile = get_profile(self.settings, verbose=verbose)
        mailbox = str(profile.get("email") or profile.get("mailbox") or "unknown").strip() or "unknown"
        now_iso = datetime.now().astimezone().isoformat()

        if cursor_row is None or not str(cursor_row.get("last_cursor") or "").strip():
            if bootstrap_if_missing:
                return self._bootstrap_from_profile(
                    cursor_scope=cursor_scope,
                    profile=profile,
                    mailbox=mailbox,
                    now_iso=now_iso,
                    reason="missing_cursor",
                )
            history_id = str(profile.get("historyId") or "").strip()
            if not history_id:
                raise RuntimeError("Gmail profile did not return historyId for change detection bootstrap.")
            cursor_row = {"last_cursor": history_id, "metadata_json": {"mailbox": mailbox}}

        cursor_metadata = dict(cursor_row.get("metadata_json") or {})
        start_history_id = str(cursor_row.get("last_cursor") or "").strip()
        resume_page_token = str(cursor_metadata.get("resume_page_token") or "").strip()
        resume_start_history_id = str(cursor_metadata.get("resume_start_history_id") or "").strip()
        if resume_page_token and resume_start_history_id:
            start_history_id = resume_start_history_id
        self._upsert_cursor(
            cursor_scope=cursor_scope,
            last_cursor=start_history_id,
            last_success_at=str(cursor_row.get("last_success_at") or ""),
            last_error="",
            status="running",
            metadata={
                "mailbox": mailbox,
                "resume_page_token": resume_page_token,
                "resume_start_history_id": start_history_id if resume_page_token else "",
            },
        )
        try:
            events, latest_history_id, next_page_token, page_count = self._list_history_pages(
                start_history_id=start_history_id,
                resume_page_token=resume_page_token,
                max_results=max_results,
                max_pages=max_pages,
                history_types=history_types,
                mailbox=mailbox,
                verbose=verbose,
            )
        except Exception as exc:
            if _is_gmail_history_list_http_404(exc):
                return self._bootstrap_from_profile(
                    cursor_scope=cursor_scope,
                    profile=profile,
                    mailbox=mailbox,
                    now_iso=now_iso,
                    reason="stale_history_http_404",
                    metadata_extra={
                        "stale_cursor_recovered": True,
                        "replaced_cursor": start_history_id,
                    },
                )
            self._upsert_cursor(
                cursor_scope=cursor_scope,
                last_cursor=start_history_id,
                last_success_at=str(cursor_row.get("last_success_at") or ""),
                last_error=str(exc),
                status=_cursor_error_status(exc),
                metadata={
                    "mailbox": mailbox,
                    "resume_page_token": resume_page_token,
                    "resume_start_history_id": start_history_id if resume_page_token else "",
                },
            )
            raise

        has_more = bool(next_page_token)
        new_history_id = latest_history_id if not has_more else start_history_id
        self._upsert_cursor(
            cursor_scope=cursor_scope,
            last_cursor=new_history_id,
            last_success_at=now_iso,
            last_error="",
            status="ok",
            metadata={
                "mailbox": mailbox,
                "history_types": list(history_types),
                "event_count": len(events),
                "page_count": page_count,
                "has_more": has_more,
                "next_page_token": next_page_token,
                "resume_page_token": next_page_token,
                "resume_start_history_id": start_history_id if has_more else "",
                "last_seen_history_id": latest_history_id,
            },
        )
        return {
            "status": "ok",
            "cursor_scope": cursor_scope,
            "mailbox": mailbox,
            "last_cursor": new_history_id,
            "events": [event.to_dict() for event in events],
            "event_count": len(events),
            "page_count": page_count,
            "has_more": has_more,
            "next_page_token": next_page_token,
        }

    def _list_history_pages(
        self,
        *,
        start_history_id: str,
        resume_page_token: str,
        max_results: int,
        max_pages: int,
        history_types: tuple[str, ...],
        mailbox: str,
        verbose: bool,
    ) -> tuple[list[GmailSourceEvent], str, str, int]:
        events_by_id: dict[str, GmailSourceEvent] = {}
        current_page_token = resume_page_token or None
        latest_history_id = start_history_id
        page_count = 0
        while True:
            payload = list_history(
                self.settings,
                start_history_id=start_history_id,
                page_token=current_page_token,
                max_results=max_results,
                history_types=list(history_types),
                verbose=verbose,
            )
            for event in extract_history_events(payload, mailbox=mailbox):
                events_by_id.setdefault(event.event_id, event)
            latest_history_id = str(payload.get("historyId") or latest_history_id).strip() or latest_history_id
            current_page_token = str(payload.get("nextPageToken") or "").strip() or None
            page_count += 1
            if current_page_token is None or page_count >= max(1, int(max_pages)):
                break
        events = sorted(events_by_id.values(), key=lambda item: (item.history_id, item.message_id, item.change_type))
        return events, latest_history_id, str(current_page_token or ""), page_count

    def _bootstrap_from_profile(
        self,
        *,
        cursor_scope: str,
        profile: dict[str, Any],
        mailbox: str,
        now_iso: str,
        reason: str,
        metadata_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        history_id = str(profile.get("historyId") or "").strip()
        if not history_id:
            raise RuntimeError("Gmail profile did not return historyId for change detection bootstrap.")
        metadata: dict[str, Any] = {"bootstrapped": True, "mailbox": mailbox, "bootstrap_reason": reason}
        if metadata_extra:
            metadata.update(metadata_extra)
        self._upsert_cursor(
            cursor_scope=cursor_scope,
            last_cursor=history_id,
            last_success_at=now_iso,
            last_error="",
            status="ok",
            metadata=metadata,
        )
        logger.warning(
            "Gmail cursor auto-bootstrap (%s): scope=%s historyId=%s mailbox=%s",
            reason,
            cursor_scope,
            history_id,
            mailbox,
        )
        return {
            "status": "bootstrapped",
            "cursor_scope": cursor_scope,
            "mailbox": mailbox,
            "last_cursor": history_id,
            "events": [],
            "event_count": 0,
            "bootstrap_reason": reason,
        }

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
                "cursor_key": f"gmail:{cursor_scope}",
                "source_kind": "gmail",
                "cursor_scope": cursor_scope,
                "last_cursor": str(last_cursor or ""),
                "last_success_at": str(last_success_at or "") or None,
                "last_error": str(last_error or ""),
                "status": str(status or "idle"),
                "metadata_json": dict(metadata or {}),
                "updated_at": datetime.now().astimezone().isoformat(),
            }
        )


def extract_history_events(payload: dict[str, Any], *, mailbox: str) -> list[GmailSourceEvent]:
    history_rows = list(payload.get("history") or [])
    events: list[GmailSourceEvent] = []
    seen: set[tuple[str, str, str]] = set()
    observed_at = datetime.now().astimezone().isoformat()
    for history_row in history_rows:
        history_id = str(history_row.get("id") or payload.get("historyId") or "").strip()
        candidates = (
            ("messagesAdded", "message_added"),
            ("labelsAdded", "label_added"),
            ("labelsRemoved", "label_removed"),
            ("messages", "message_observed"),
        )
        for key, change_type in candidates:
            for entry in history_row.get(key) or []:
                message = entry.get("message") if isinstance(entry, dict) else entry
                if not isinstance(message, dict):
                    continue
                message_id = str(message.get("id") or "").strip()
                thread_id = str(message.get("threadId") or "").strip()
                if not message_id:
                    continue
                dedupe_key = (history_id, message_id, change_type)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                events.append(
                    GmailSourceEvent(
                        event_id=_stable_event_id(history_id=history_id, message_id=message_id, change_type=change_type),
                        history_id=history_id,
                        message_id=message_id,
                        thread_id=thread_id,
                        change_type=change_type,
                        mailbox=mailbox,
                        observed_at=observed_at,
                        raw_entry=dict(entry if isinstance(entry, dict) else {"message": message}),
                    )
                )
    events.sort(key=lambda item: (item.history_id, item.message_id, item.change_type))
    return events


def poll_gmail_changes(
    settings: Settings,
    *,
    store: Any,
    cursor_scope: str = "default",
    max_results: int = 100,
    max_pages: int = 3,
    verbose: bool = False,
    bootstrap_if_missing: bool = True,
) -> dict[str, Any]:
    detector = GmailChangeDetector(settings, store=store)
    return detector.poll_changes(
        cursor_scope=cursor_scope,
        max_results=max_results,
        max_pages=max_pages,
        verbose=verbose,
        bootstrap_if_missing=bootstrap_if_missing,
    )


def _stable_event_id(*, history_id: str, message_id: str, change_type: str) -> str:
    digest = hashlib.sha256(f"{history_id}|{message_id}|{change_type}".encode("utf-8")).hexdigest()
    return f"ghevt_{digest[:24]}"


def _is_gmail_history_list_http_404(exc: Exception) -> bool:
    """True only for Gmail API history.list stale startHistoryId (HTTP 404)."""
    return isinstance(exc, GoogleGmailApiError) and "HTTP 404" in str(exc)


def _cursor_error_status(exc: Exception) -> str:
    if _is_gmail_history_list_http_404(exc):
        return "stale_cursor"
    return "error"


__all__ = [
    "DEFAULT_GMAIL_HISTORY_TYPES",
    "GmailChangeDetector",
    "GmailSourceEvent",
    "extract_history_events",
    "poll_gmail_changes",
]
