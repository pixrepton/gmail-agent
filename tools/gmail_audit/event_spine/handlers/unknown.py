"""Fallback handler for unknown event types."""

from __future__ import annotations

from event_spine.handlers.base import EventContext
from event_spine.models import HandlerResult, OsEvent


class UnknownEventHandler:
    event_types = frozenset()

    def handle(self, event: OsEvent, *, ctx: EventContext) -> HandlerResult:
        if ctx.mode == "shadow":
            ctx.logger.info(
                "event_spine unknown type skipped (shadow) event_id=%s type=%s",
                event.event_id,
                event.event_type,
            )
            return HandlerResult(outcome="skipped", message=f"unknown_type:{event.event_type}")
        return HandlerResult(
            outcome="failed",
            message=f"unknown_event_type:{event.event_type}",
            detail={"event_type": event.event_type},
        )
