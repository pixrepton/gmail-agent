"""Google Calendar V1 contracts for mailbox-memory context."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


CALENDAR_RISKS = (
    "no_calendar_action_needed",
    "calendar_event_exists",
    "calendar_event_missing",
    "customer_proposed_date",
    "possible_conflict",
)


@dataclass(slots=True)
class CalendarEvent:
    calendar_event_id: str
    source: str = "google_calendar"
    summary: str = ""
    description: str = ""
    location: str = ""
    start_at: str = ""
    end_at: str = ""
    attendees: list[dict[str, Any]] = field(default_factory=list)
    organizer: str = ""
    html_link: str = ""
    recurring: bool = False
    ingested_at: str = ""
    visibility_scope: str = ""
    case_id: str = ""
    link_confidence: float = 0.0
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CalendarCaseLink:
    calendar_event_id: str
    case_id: str
    link_confidence: float
    match_reasons: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def normalize_google_calendar_event(raw: dict[str, Any], *, calendar_id: str = "", ingested_at: str = "") -> CalendarEvent:
    start_obj = raw.get("start") if isinstance(raw.get("start"), dict) else {}
    end_obj = raw.get("end") if isinstance(raw.get("end"), dict) else {}
    organizer = raw.get("organizer") if isinstance(raw.get("organizer"), dict) else {}
    event_id = str(raw.get("id") or raw.get("calendar_event_id") or "").strip()
    return CalendarEvent(
        calendar_event_id=event_id,
        summary=str(raw.get("summary") or ""),
        description=str(raw.get("description") or ""),
        location=str(raw.get("location") or ""),
        start_at=str(start_obj.get("dateTime") or start_obj.get("date") or raw.get("start_at") or ""),
        end_at=str(end_obj.get("dateTime") or end_obj.get("date") or raw.get("end_at") or ""),
        attendees=list(raw.get("attendees") or []),
        organizer=str(organizer.get("email") or raw.get("organizer") or ""),
        html_link=str(raw.get("htmlLink") or raw.get("html_link") or ""),
        recurring=bool(raw.get("recurringEventId") or raw.get("recurrence")),
        ingested_at=ingested_at or now_iso(),
        visibility_scope=str(calendar_id or raw.get("visibility_scope") or ""),
        raw_payload=dict(raw),
    )


def proposed_calendar_event_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(raw.get("title") or raw.get("summary") or ""),
        "start_at": str(raw.get("start_at") or ""),
        "end_at": str(raw.get("end_at") or ""),
        "location": str(raw.get("location") or ""),
        "description": str(raw.get("description") or ""),
        "attendees": list(raw.get("attendees") or []),
        "case_id": str(raw.get("case_id") or ""),
        "source_evidence": list(raw.get("source_evidence") or []),
        "confidence": float(raw.get("confidence") or 0.0),
    }


__all__ = [
    "CALENDAR_RISKS",
    "CalendarCaseLink",
    "CalendarEvent",
    "normalize_google_calendar_event",
    "now_iso",
    "proposed_calendar_event_payload",
]
