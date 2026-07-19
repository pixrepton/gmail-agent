"""Env-gated Google Calendar client using existing Google OAuth settings."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests

from config import GOOGLE_CALENDAR_EVENTS_SCOPE, GOOGLE_CALENDAR_READONLY_SCOPE, Settings
from gmail_auth import resolve_google_access_token
from log_config import get_logger
from redaction import sanitize_text


CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"

log = get_logger(__name__)


def build_google_calendar_check(settings: Settings, *, check_access: bool = False) -> dict[str, Any]:
    if not getattr(settings, "google_calendar_enabled", False):
        return {"status": "disabled", "detail": "GOOGLE_CALENDAR_ENABLED is false"}
    calendar_id = str(getattr(settings, "google_calendar_id", "") or "").strip()
    if not calendar_id:
        return {"status": "not_configured", "missing": ["GOOGLE_CALENDAR_ID"]}
    scopes = set(getattr(settings, "google_oauth_scopes", ()) or ())
    missing_scopes = [scope for scope in (GOOGLE_CALENDAR_READONLY_SCOPE,) if scope not in scopes]
    if missing_scopes:
        return {
            "status": "missing_scope",
            "missing_scopes": missing_scopes,
            "detail": "Regenerate Google OAuth token with Calendar scope.",
        }
    if not check_access:
        return {"status": "ok", "calendar_id": calendar_id, "checked_live": False}
    try:
        client = GoogleCalendarClient(settings)
        events = client.list_events(max_results=1)
        return {"status": "ok", "calendar_id": calendar_id, "checked_live": True, "sample_count": len(events)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "fail_env", "calendar_id": calendar_id, "error": sanitize_text(str(exc))}


class GoogleCalendarClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calendar_id = str(getattr(settings, "google_calendar_id", "primary") or "primary")
        self.timeout = int(getattr(settings, "http_timeout", 60) or 60)

    def list_events(
        self,
        *,
        time_min: str = "",
        time_max: str = "",
        max_results: int = 50,
        single_events: bool = True,
    ) -> list[dict[str, Any]]:
        token = resolve_google_access_token(self.settings)
        params: dict[str, Any] = {"maxResults": max(1, min(2500, int(max_results or 50))), "singleEvents": single_events}
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        url = f"{CALENDAR_API_BASE}/calendars/{quote(self.calendar_id, safe='')}/events"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=self.timeout)
        if resp.status_code in {401, 403}:
            raise RuntimeError("missing_scope_or_auth_failed for Google Calendar read")
        resp.raise_for_status()
        data = resp.json()
        return list(data.get("items") or [])

    def create_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        scopes = set(getattr(self.settings, "google_oauth_scopes", ()) or ())
        if GOOGLE_CALENDAR_EVENTS_SCOPE not in scopes:
            raise RuntimeError("missing_scope: GOOGLE_OAUTH_SCOPES lacks calendar.events")
        start_at = str(payload.get("start_at") or "").strip()
        if not start_at:
            raise RuntimeError("create_event requires a non-empty 'start_at' field (RFC 3339 timestamp).")
        end_at = str(payload.get("end_at") or "").strip()
        if not end_at:
            raise RuntimeError("create_event requires a non-empty 'end_at' field (RFC 3339 timestamp).")
        token = resolve_google_access_token(self.settings)
        body = {
            "summary": str(payload.get("title") or payload.get("summary") or ""),
            "description": str(payload.get("description") or ""),
            "location": str(payload.get("location") or ""),
            "start": {"dateTime": start_at},
            "end": {"dateTime": end_at},
            "attendees": [{"email": str(item.get("email") or item)} for item in payload.get("attendees") or []],
        }
        url = f"{CALENDAR_API_BASE}/calendars/{quote(self.calendar_id, safe='')}/events"
        resp = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=self.timeout)
        if resp.status_code in {401, 403}:
            raise RuntimeError("missing_scope_or_auth_failed for Google Calendar create")
        resp.raise_for_status()
        return dict(resp.json())


__all__ = ["GOOGLE_CALENDAR_EVENTS_SCOPE", "GOOGLE_CALENDAR_READONLY_SCOPE", "GoogleCalendarClient", "build_google_calendar_check"]
