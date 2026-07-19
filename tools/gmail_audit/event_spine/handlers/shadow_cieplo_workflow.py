"""Handler for orchestrator workflow persist events (shadow log; P1.4 audit in active)."""

from __future__ import annotations

from event_spine.handler_effects import merge_active_handler_result, record_bounded_handler_effect
from event_spine.handlers.base import EventContext
from event_spine.models import HandlerResult, OsEvent

_HANDLER_KEY = "cieplo_workflow_persisted"


class ShadowCieploWorkflowPersistedHandler:
    event_types = frozenset({_HANDLER_KEY})

    def handle(self, event: OsEvent, *, ctx: EventContext) -> HandlerResult:
        payload = event.payload if isinstance(event.payload, dict) else {}
        correlation = event.correlation if isinstance(event.correlation, dict) else {}
        ctx.logger.info(
            "event_spine cieplo_workflow_persisted mode=%s workflow_id=%s message_id=%s trace_id=%s",
            ctx.mode,
            payload.get("workflow_id") or "",
            payload.get("message_id") or "",
            correlation.get("trace_id") or "",
        )
        if ctx.mode != "active":
            return HandlerResult(outcome="processed", message="shadow_log_only")

        effect = record_bounded_handler_effect(
            ctx.store,
            event,
            handler_key=_HANDLER_KEY,
            processor_id=ctx.processor_id,
            detail={
                "workflow_id": payload.get("workflow_id"),
                "message_id": payload.get("message_id"),
                "trace_id": correlation.get("trace_id"),
            },
        )
        message, detail = merge_active_handler_result(shadow_message="shadow_log_only", effect=effect)
        return HandlerResult(outcome="processed", message=message, detail=detail)
