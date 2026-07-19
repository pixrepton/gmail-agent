"""Handler for correlation registry events (shadow log; P1.4 audit row in active)."""

from __future__ import annotations

from event_spine.handler_effects import merge_active_handler_result, record_bounded_handler_effect
from event_spine.handlers.base import EventContext
from event_spine.models import HandlerResult, OsEvent

_HANDLER_KEY = "correlation_links_registered"


class ShadowCorrelationLinksHandler:
    event_types = frozenset({_HANDLER_KEY})

    def handle(self, event: OsEvent, *, ctx: EventContext) -> HandlerResult:
        links_count = (event.payload or {}).get("links_count")
        ctx.logger.info(
            "event_spine processed type=%s mode=%s event_id=%s engagement_id=%s links=%s",
            event.event_type,
            ctx.mode,
            event.event_id,
            event.engagement_id or "",
            links_count,
        )
        if ctx.mode != "active":
            return HandlerResult(outcome="processed", message="shadow_log_only")

        effect = record_bounded_handler_effect(
            ctx.store,
            event,
            handler_key=_HANDLER_KEY,
            processor_id=ctx.processor_id,
            detail={"links_count": links_count},
        )
        message, detail = merge_active_handler_result(shadow_message="shadow_log_only", effect=effect)
        return HandlerResult(outcome="processed", message=message, detail=detail)
