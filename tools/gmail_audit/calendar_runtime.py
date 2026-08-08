"""Google Calendar V1 runtime: read-only ingest, link, context and proposals."""

from __future__ import annotations

from typing import Any

from calendar_case_linker import link_calendar_event_to_case
from case_family_boundary import filter_operational_feed_case_rows
from calendar_client import GoogleCalendarClient
from calendar_models import (
    CalendarCaseLink,
    active_calendar_events,
    infer_calendar_risk,
    normalize_google_calendar_event,
    now_iso,
)
from calendar_signal_adapter import build_calendar_raw_observation, build_calendar_signal
from config import Settings
from log_config import get_logger
from mailbox_memory.active_facts import fetch_current_facts_for_case

log = get_logger(__name__)


class CalendarRuntime:
    def __init__(self, *, settings: Settings, store: Any, client: GoogleCalendarClient | None = None) -> None:
        self.settings = settings
        self.store = store
        self.client = client or GoogleCalendarClient(settings)

    def ingest_events(self, *, time_min: str = "", time_max: str = "", limit: int = 50, dry_run: bool = True) -> dict[str, Any]:
        raw_items = self.client.list_events(time_min=time_min, time_max=time_max, max_results=limit)
        observed_at = now_iso()
        calendar_id = str(getattr(self.settings, "google_calendar_id", "") or "primary")
        cases = filter_operational_feed_case_rows(
            list(self.store.fetch_cases(limit=500) or [])
        )
        normalized: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for idx, raw in enumerate(raw_items):
            # Error isolation per event: wrap each event in try/except with log.warning
            try:
                event = normalize_google_calendar_event(raw, calendar_id=calendar_id, ingested_at=observed_at)
                if not event.calendar_event_id:
                    raise ValueError("calendar_event_id is required for read-only visit lifecycle")
                link = link_calendar_event_to_case(event.to_dict(), cases)
                if link.get("link_status") == "linked" and link.get("case_id"):
                    event.case_id = str(link.get("case_id") or "")
                    event.link_confidence = float(link.get("link_confidence") or 0.0)
                event_row = event.to_dict()
                event_row["case_link"] = {
                    "link_status": str(link.get("link_status") or "no_link"),
                    "candidates": list(link.get("candidates") or []),
                    "top_score_delta": float(link.get("top_score_delta") or 0.0),
                    "match_reasons": list(link.get("match_reasons") or []),
                }
                normalized.append(event_row)
                if dry_run:
                    continue
                self.store.upsert_calendar_event(event_row)
                if event.case_id:
                    self.store.upsert_calendar_case_link(
                        CalendarCaseLink(
                            calendar_event_id=event.calendar_event_id,
                            case_id=event.case_id,
                            link_confidence=event.link_confidence,
                            match_reasons=list(link.get("match_reasons") or []),
                            created_at=observed_at,
                        ).to_dict()
                    )
                source_ref = {"calendar_id": calendar_id, "calendar_event_id": event.calendar_event_id, "provider": event.source}
                raw_obs = build_calendar_raw_observation(source_ref=source_ref, observed_at=observed_at, payload=event_row)
                self.store.append_raw_observation(raw_obs.to_dict())
                signal = build_calendar_signal(source_ref=source_ref, observed_at=observed_at, payload=event_row, raw_observation=raw_obs)
                self.store.append_signal(signal.to_dict())
            except Exception as exc:
                log.warning(
                    "CALENDAR_EVENT_INGEST_FAILED",
                    extra={
                        "x": {
                            "calendar_id": calendar_id,
                            "event_index": idx,
                            "event_summary": str(raw.get("summary", "")),
                            "event_id": str(raw.get("id", "")),
                            "error": str(exc),
                        }
                    },
                )
                errors.append({"index": idx, "event_id": str(raw.get("id", "")), "error": str(exc)})
        summary = {
            "ok": True,
            "dry_run": dry_run,
            "count": len(normalized),
            "errors": len(errors),
            "events": normalized,
        }
        if errors:
            summary["error_details"] = errors
        if errors:
            log.info(
                "CALENDAR_EVENT_INGEST_PARTIAL",
                extra={"x": {"total": len(raw_items), "succeeded": len(normalized), "failed": len(errors)}},
            )
        else:
            log.info(
                "CALENDAR_EVENT_INGEST_COMPLETE",
                extra={"x": {"total": len(normalized)}},
            )
        return summary

    def context_for_case(self, case_id: str) -> dict[str, Any]:
        all_events = self.store.fetch_calendar_events_for_case(case_id, limit=10)
        events = active_calendar_events(all_events)
        facts = fetch_current_facts_for_case(self.store, case_id)
        risk = infer_calendar_risk(events=all_events, facts=facts)
        next_event = events[0] if events else {}
        return {
            "case_id": case_id,
            "events": events,
            "observed_events": all_events,
            "next_event": next_event,
            "has_calendar_event": bool(events),
            "calendar_risk": risk,
            "possible_conflict": risk == "possible_conflict",
            "visit_lifecycle": "scheduled_visit" if events else ("proposed_visit" if risk == "customer_proposed_date" else "no_calendar_event"),
        }


def build_calendar_event_action_proposal(*, store: Any, case_id: str, payload: dict[str, Any], proposed_by: str = "ai") -> dict[str, Any]:
    raise RuntimeError(
        "calendar_event_action_proposal_disabled_read_only: Node B keeps proposed_visit text only; "
        "scheduled_visit requires an ingested real calendar_event_id."
    )


__all__ = ["CalendarRuntime", "build_calendar_event_action_proposal", "infer_calendar_risk"]
