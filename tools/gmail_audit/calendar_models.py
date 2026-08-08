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
CALENDAR_SIGNAL_SOURCE_KIND = "calendar"
GOOGLE_CALENDAR_PROVIDER = "google_calendar"


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


def calendar_event_status(event: dict[str, Any]) -> str:
    raw_payload = event.get("raw_payload") if isinstance(event.get("raw_payload"), dict) else {}
    return str(event.get("event_status") or event.get("status") or raw_payload.get("status") or "confirmed").strip().lower()


def calendar_event_is_cancelled(event: dict[str, Any]) -> bool:
    return calendar_event_status(event) == "cancelled"


def active_calendar_events(events: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [dict(event) for event in (events or []) if isinstance(event, dict) and not calendar_event_is_cancelled(event)]


def scheduled_visit_fact_has_calendar_event(row: dict[str, Any]) -> bool:
    """A scheduled_visit fact is authoritative only when linked to a real Calendar event."""
    if str(row.get("fact_key") or row.get("predicate") or "").strip() != "scheduled_visit":
        return False
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    raw_value = str(row.get("raw_value") or row.get("normalized_value") or row.get("value") or "")
    candidates = [
        row.get("calendar_event_id"),
        metadata.get("calendar_event_id"),
        metadata.get("google_calendar_event_id"),
    ]
    if any(str(value or "").strip() for value in candidates):
        return True
    return "calendar_event_id=" in raw_value or "google_calendar:" in raw_value


def _has_customer_proposed_date_fact(facts: list[dict[str, Any]] | None) -> bool:
    from mailbox_memory.active_facts import is_live_fact

    for item in facts or []:
        if not isinstance(item, dict) or not is_live_fact(item):
            continue
        key = str(item.get("field_type") or item.get("fact_key") or item.get("predicate") or "").strip().lower()
        if key in {"date", "service_date", "proposed_date", "proposed_visit"}:
            return True
    return False


def infer_calendar_risk(*, events: list[dict[str, Any]], facts: list[dict[str, Any]] | None = None) -> str:
    if active_calendar_events(events):
        return "calendar_event_exists"
    if _has_customer_proposed_date_fact(facts):
        return "customer_proposed_date"
    return "calendar_event_missing"


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
    "CALENDAR_SIGNAL_SOURCE_KIND",
    "CalendarCaseLink",
    "CalendarEvent",
    "GOOGLE_CALENDAR_PROVIDER",
    "active_calendar_events",
    "calendar_event_is_cancelled",
    "calendar_event_status",
    "infer_calendar_risk",
    "normalize_google_calendar_event",
    "now_iso",
    "proposed_calendar_event_payload",
    "scheduled_visit_fact_has_calendar_event",
]
