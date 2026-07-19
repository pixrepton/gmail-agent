"""Day section composition (X1 v0) — honest "Karta dnia" from existing Node B data.

feed.day stays a pure projection: no persistence of its own, no new source of
truth. Every section here is derived directly from data that already exists
(decision queue, per-case calendar memory, case creation timestamps) — nothing
is invented and no new priority/lifecycle model is introduced.

Deliberately NOT included in v0: stagnation / at-risk cases. No single agreed
case-state representation exists yet in this repo (three incompatible ones
were found) — showing a section here would mean picking a favorite just to
fill the card, which is worse than showing nothing.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from daszek_v3_operational_feed_contract import strip_forbidden_nested
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

# Explicit, visible operator-day contract for X1 v0. This is the single place
# that defines "today" for the day card — not a hidden hardcode scattered
# across the codebase. No explicit per-deployment operator timezone config
# exists today, so this constant is the conscious v0 contract.
OPERATOR_TIMEZONE = ZoneInfo("Europe/Warsaw")

_DECISIONS_SECTION_KEY = "decyzje_czekajace"
_VISITS_SECTION_KEY = "wizyty_dzis"
_NEW_CASES_SECTION_KEY = "nowe_sprawy"

_DECISIONS_LIMIT = 20
_VISITS_PER_CASE_LIMIT = 5
_VISITS_TOTAL_LIMIT = 25
_NEW_CASES_LIMIT = 10


def _operator_today_bounds(now: datetime | None = None) -> tuple[datetime, datetime, date]:
    now_local = (now or datetime.now(timezone.utc)).astimezone(OPERATOR_TIMEZONE)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return day_start, day_end, day_start.date()


def _parse_calendar_moment(value: str) -> tuple[datetime | None, date | None]:
    """Timed events -> (aware datetime, None). All-day events -> (None, date)."""
    raw = str(value or "").strip()
    if not raw:
        return None, None
    if len(raw) == 10 and raw[4:5] == "-" and raw[7:8] == "-" and "T" not in raw:
        try:
            return None, date.fromisoformat(raw)
        except ValueError:
            return None, None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed, None


def _event_is_today(event: dict[str, Any], *, day_start: datetime, day_end: datetime, today: date) -> bool:
    start_raw = str(event.get("start_at") or event.get("start") or "")
    end_raw = str(event.get("end_at") or event.get("end") or "")
    start_dt, start_date = _parse_calendar_moment(start_raw)
    end_dt, end_date = _parse_calendar_moment(end_raw)

    if start_date is not None or end_date is not None:
        # All-day event. Google's `end.date` is exclusive; a single-day
        # all-day event with no end at all covers just its start date.
        span_start = start_date or end_date
        if span_start is None:
            return False
        span_end = end_date or (span_start + timedelta(days=1))
        return span_start <= today < span_end

    if start_dt is None:
        return False
    end_for_overlap = end_dt or (start_dt + timedelta(minutes=1))
    return start_dt < day_end and end_for_overlap > day_start


def _decision_items(mailbox_store: Any) -> list[dict[str, Any]]:
    connect = getattr(mailbox_store, "_connect", None)
    if not callable(connect):
        return []
    from divergence_loop import fetch_decision_queue

    try:
        with connect() as conn:
            rows = fetch_decision_queue(conn, limit=_DECISIONS_LIMIT)
    except Exception:  # noqa: BLE001 — day card is a soft projection, never blocks the feed build
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        proposal_id = str(row.get("proposal_id") or "").strip()
        if not proposal_id:
            continue
        proposal_type = str(row.get("proposal_type") or "").strip()
        hours_waiting = row.get("hours_waiting")
        wait_label = (
            f"Czeka {hours_waiting:g} godz. na reakcję operatora."
            if isinstance(hours_waiting, (int, float))
            else "Czeka na reakcję operatora."
        )
        items.append(
            strip_forbidden_nested(
                {
                    "note_id": f"decision-{proposal_id}",
                    "case_id": str(row.get("case_id") or "").strip(),
                    "title": f"Decyzja: {proposal_type}" if proposal_type else "Decyzja operatora",
                    "summary": str(row.get("summary_pl") or "").strip(),
                    "why_on_desk": wait_label,
                    "recommended_next_step": "Przejrzyj propozycję agenta i podejmij decyzję.",
                    "record_type_label": "Decyzja",
                    "status_label": wait_label,
                }
            )
        )
    return items


def _calendar_items_for_case(
    mailbox_store: Any,
    case_id: str,
    *,
    day_start: datetime,
    day_end: datetime,
    today: date,
) -> list[dict[str, Any]]:
    fetch = getattr(mailbox_store, "fetch_calendar_events_for_case", None)
    if not callable(fetch):
        return []
    try:
        events = fetch(case_id, limit=_VISITS_PER_CASE_LIMIT * 4) or []
    except Exception:  # noqa: BLE001 — one case's calendar must not break the whole card
        return []

    items: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if not _event_is_today(ev, day_start=day_start, day_end=day_end, today=today):
            continue
        start = str(ev.get("start_at") or ev.get("start") or "")
        title = str(ev.get("summary") or ev.get("title") or "Wydarzenie")[:300]
        event_key = str(ev.get("calendar_event_id") or start)
        items.append(
            strip_forbidden_nested(
                {
                    "note_id": f"day-visit-{case_id}-{hashlib.sha256(event_key.encode()).hexdigest()[:10]}",
                    "case_id": case_id,
                    "title": title,
                    "summary": start,
                    "why_on_desk": "Wizyta zaplanowana na dziś (kalendarz zapisany w pamięci sprawy).",
                    "recommended_next_step": "Sprawdź szczegóły terminu w kalendarzu.",
                    "record_type_label": "Wizyta",
                }
            )
        )
        if len(items) >= _VISITS_PER_CASE_LIMIT:
            break
    return items


def _today_visit_items(
    mailbox_store: Any,
    snapshots: list[EngagementSnapshotV2],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    day_start, day_end, today = _operator_today_bounds(now)
    items: list[dict[str, Any]] = []
    seen_cases: set[str] = set()
    for snapshot in snapshots:
        case_id = str(getattr(snapshot, "case_id", "") or "").strip()
        if not case_id or case_id in seen_cases:
            continue
        seen_cases.add(case_id)
        items.extend(
            _calendar_items_for_case(mailbox_store, case_id, day_start=day_start, day_end=day_end, today=today)
        )
        if len(items) >= _VISITS_TOTAL_LIMIT:
            break
    return items[:_VISITS_TOTAL_LIMIT]


def _new_case_items(mailbox_store: Any) -> list[dict[str, Any]]:
    from agent_runtime.business_pulse import get_daily_delta

    try:
        result = get_daily_delta(mailbox_store, None)
    except Exception:  # noqa: BLE001 — day card is a soft projection, never blocks the feed build
        return []
    if not isinstance(result, dict) or not result.get("ok"):
        return []
    delta = result.get("delta") or {}
    rows = delta.get("new_cases_list") or []

    items: list[dict[str, Any]] = []
    for row in rows[:_NEW_CASES_LIMIT]:
        if not isinstance(row, dict):
            continue
        case_id = str(row.get("case_id") or "").strip()
        if not case_id:
            continue
        client = str(row.get("client") or "").strip()
        items.append(
            strip_forbidden_nested(
                {
                    "note_id": f"day-newcase-{case_id}",
                    "case_id": case_id,
                    "title": client or "Nowa sprawa",
                    "summary": "Nowa sprawa Node B założona od wczoraj.",
                    "why_on_desk": "Sprawa nowa od wczoraj.",
                    "recommended_next_step": "Przejrzyj nową sprawę.",
                    "record_type_label": "Nowa sprawa",
                }
            )
        )
    return items


def compose_day_sections(
    mailbox_store: Any,
    snapshots: list[EngagementSnapshotV2],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compose feed.day.sections from existing, already-live Node B reads.

    Read-only, best-effort per section: a failure in one section (e.g. no DB
    connection available on this call path) never blocks the others or the
    surrounding feed build — it just means that section is absent.
    """
    sections: list[dict[str, Any]] = []

    decision_items = _decision_items(mailbox_store)
    if decision_items:
        sections.append(
            {
                "key": _DECISIONS_SECTION_KEY,
                "title": "Decyzje czekające",
                "subtitle": "Propozycje agenta czekające na reakcję operatora, od najdłużej czekającej.",
                "items": decision_items,
            }
        )

    visit_items = _today_visit_items(mailbox_store, snapshots, now=now)
    if visit_items:
        sections.append(
            {
                "key": _VISITS_SECTION_KEY,
                "title": "Dzisiejsze wizyty",
                "subtitle": "Z kalendarza zapisanego w pamięci sprawy (dzień operatora, Europe/Warsaw).",
                "items": visit_items,
            }
        )

    new_case_items = _new_case_items(mailbox_store)
    if new_case_items:
        sections.append(
            {
                "key": _NEW_CASES_SECTION_KEY,
                "title": "Nowe sprawy od wczoraj",
                "subtitle": "Nowe sprawy Node B założone od wczoraj (nie wszystkie nowe zdarzenia).",
                "items": new_case_items,
            }
        )

    return {"sections": sections}


__all__ = [
    "OPERATOR_TIMEZONE",
    "compose_day_sections",
]
