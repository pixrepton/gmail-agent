"""Event Memory: append-only operational event log and replay for case evolution."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any


EVENT_TYPES = (
    "signal_received",
    "signal_normalized",
    "attachment_processed",
    "thread_memory_updated",
    "case_linked",
    "case_created",
    "case_updated",
    "case_intelligence_generated",
    "desk_note_created",
    "desk_note_revised",
    "desk_note_moved_to_case_only",
    "desk_note_resolved",
    "feedback_recorded",
    "operator_action_recorded",
    "review_required_flagged",
)

ENTITY_TYPES = ("signal", "case", "desk_note", "attachment", "thread", "feedback")
ACTOR_TYPES = ("system", "operator", "ai")


def build_event(
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    case_id: str = "",
    thread_id: str = "",
    source_signal_id: str = "",
    payload: dict[str, Any] | None = None,
    confidence_domains: dict[str, float] | None = None,
    review_state: str = "",
    actor_type: str = "system",
    stage_name: str = "",
    correlation_id: str = "",
    stable_anchor: str = "",
    occurred_at: str = "",
) -> dict[str, Any]:
    """Build one canonical event for the operational event log."""
    normalized_payload = payload or {}
    normalized_occurred_at = str(occurred_at or "").strip() or datetime.now().astimezone().isoformat()
    event_id = _stable_event_id(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        case_id=case_id,
        thread_id=thread_id,
        source_signal_id=source_signal_id,
        payload=normalized_payload,
        stable_anchor=stable_anchor,
    )

    return {
        "event_id": event_id,
        "event_type": event_type if event_type in EVENT_TYPES else "signal_received",
        "entity_type": entity_type if entity_type in ENTITY_TYPES else "signal",
        "entity_id": str(entity_id or "").strip(),
        "case_id": str(case_id or "").strip(),
        "thread_id": str(thread_id or "").strip(),
        "source_signal_id": str(source_signal_id or "").strip(),
        "occurred_at": normalized_occurred_at,
        "payload": normalized_payload,
        "confidence_domains": confidence_domains or {},
        "review_state": str(review_state or "").strip(),
        "actor_type": actor_type if actor_type in ACTOR_TYPES else "system",
        "stage_name": str(stage_name or "").strip(),
        "correlation_id": str(correlation_id or "").strip() or event_id,
    }


class EventLog:
    """In-memory append-only event log for one pipeline run."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def append(self, event: dict[str, Any]) -> None:
        self._events.append(event)

    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def events_for_case(self, case_id: str) -> list[dict[str, Any]]:
        return [e for e in self._events if e.get("case_id") == case_id]

    def events_for_entity(self, entity_id: str) -> list[dict[str, Any]]:
        return [e for e in self._events if e.get("entity_id") == entity_id]

    def events_by_type(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self._events if e.get("event_type") == event_type]

    def replay_case(self, case_id: str) -> list[dict[str, Any]]:
        case_events = self.events_for_case(case_id)
        return sorted(case_events, key=lambda e: str(e.get("occurred_at") or ""))

    def __len__(self) -> int:
        return len(self._events)


def emit_signal_received(
    event_log: EventLog,
    *,
    snapshot: dict[str, Any],
    case_id: str = "",
    correlation_id: str = "",
) -> dict[str, Any]:
    source_message = snapshot.get("source_message") or {}
    message_id = str(source_message.get("message_id") or "").strip()
    thread_id = str(source_message.get("thread_id") or "").strip()
    message_date = str(source_message.get("date") or "").strip()
    event = build_event(
        event_type="signal_received",
        entity_type="signal",
        entity_id=message_id,
        case_id=case_id,
        thread_id=thread_id,
        source_signal_id=message_id,
        payload={"subject": str(source_message.get("subject") or ""), "sender": str(source_message.get("sender") or "")},
        actor_type="system",
        stage_name="snapshot",
        correlation_id=correlation_id,
        stable_anchor=message_id or thread_id,
        occurred_at=message_date,
    )
    event_log.append(event)
    return event


def emit_case_intelligence(
    event_log: EventLog,
    *,
    case_id: str,
    intelligence_result: dict[str, Any],
    correlation_id: str = "",
    source_signal_id: str = "",
    thread_id: str = "",
) -> dict[str, Any]:
    case_understanding = intelligence_result.get("case_understanding") or {}
    lifecycle_revision = intelligence_result.get("lifecycle_revision") or {}
    lifecycle_intent = str(lifecycle_revision.get("lifecycle_intent") or "").strip()
    target_zone = str(lifecycle_revision.get("target_surface_zone") or "").strip()
    event = build_event(
        event_type="case_intelligence_generated",
        entity_type="case",
        entity_id=case_id,
        case_id=case_id,
        thread_id=thread_id,
        source_signal_id=source_signal_id,
        payload={
            "business_priority": str(case_understanding.get("business_priority") or ""),
            "presence_mode": str((intelligence_result.get("desk_composition") or {}).get("presence_mode") or ""),
            "lifecycle_intent": lifecycle_intent,
            "target_surface_zone": target_zone,
        },
        confidence_domains=_extract_confidence_domains(intelligence_result),
        actor_type="ai",
        stage_name="case_intelligence",
        correlation_id=correlation_id,
        stable_anchor="::".join(part for part in (source_signal_id, thread_id, lifecycle_intent, target_zone) if part),
    )
    event_log.append(event)
    return event


def emit_desk_note_event(
    event_log: EventLog,
    *,
    event_type: str,
    note_id: str,
    case_id: str,
    payload: dict[str, Any] | None = None,
    actor_type: str = "ai",
    correlation_id: str = "",
    source_signal_id: str = "",
) -> dict[str, Any]:
    event = build_event(
        event_type=event_type,
        entity_type="desk_note",
        entity_id=note_id,
        case_id=case_id,
        payload=payload or {},
        actor_type=actor_type,
        stage_name="desk_note",
        correlation_id=correlation_id,
        source_signal_id=source_signal_id,
        stable_anchor="::".join(part for part in (note_id, case_id, source_signal_id, event_type) if part),
    )
    event_log.append(event)
    return event


def emit_feedback_event(
    event_log: EventLog,
    *,
    note_id: str,
    case_id: str,
    feedback_type: str,
    actor: str = "operator",
    correlation_id: str = "",
) -> dict[str, Any]:
    event = build_event(
        event_type="feedback_recorded",
        entity_type="feedback",
        entity_id=note_id,
        case_id=case_id,
        payload={"feedback_type": feedback_type, "actor": actor},
        actor_type="operator",
        stage_name="feedback",
        correlation_id=correlation_id,
        stable_anchor="::".join(part for part in (note_id, case_id, feedback_type) if part),
    )
    event_log.append(event)
    return event


def _extract_confidence_domains(intelligence_result: dict[str, Any]) -> dict[str, float]:
    case_understanding = intelligence_result.get("case_understanding") or {}
    return {
        "confidence_overall": float(case_understanding.get("confidence_overall") or 0.0),
    }


def _stable_id(prefix: str, *parts: str) -> str:
    seed = "::".join(str(part or "").strip() for part in parts if str(part or "").strip())
    if not seed:
        seed = prefix
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _stable_event_id(
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    case_id: str,
    thread_id: str,
    source_signal_id: str,
    payload: dict[str, Any],
    stable_anchor: str,
) -> str:
    payload_anchor = ""
    if isinstance(payload, dict):
        payload_anchor = str(
            payload.get("decision_type")
            or payload.get("feedback_type")
            or payload.get("lifecycle_intent")
            or payload.get("target_surface_zone")
            or ""
        ).strip()
    return _stable_id(
        "evt",
        stable_anchor,
        event_type,
        entity_type,
        entity_id,
        case_id,
        thread_id,
        source_signal_id,
        payload_anchor,
    )


__all__ = [
    "EventLog",
    "build_event",
    "emit_signal_received",
    "emit_case_intelligence",
    "emit_desk_note_event",
    "emit_feedback_event",
]
