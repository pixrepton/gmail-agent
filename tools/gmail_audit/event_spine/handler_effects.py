"""Bounded P1.4 side effects: idempotent audit rows per (event_id, handler_key)."""

from __future__ import annotations

import json
from typing import Any, Protocol

from event_spine.models import OsEvent
from log_config import get_logger

log = get_logger(__name__)

class HandlerEffectsRecorder(Protocol):
    def ensure_handler_effects_schema(self) -> None: ...

    def record_handler_effect(
        self,
        *,
        event: OsEvent,
        handler_key: str,
        processor_id: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def record_bounded_handler_effect(
    store: Any,
    event: OsEvent,
    *,
    handler_key: str,
    processor_id: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recorder = getattr(store, "record_handler_effect", None)
    if not callable(recorder):
        log.warning("event_spine store has no record_handler_effect; skipping P1.4 write")
        return {"recorded": False, "reason": "store_unsupported"}
    return recorder(
        event=event,
        handler_key=handler_key,
        processor_id=processor_id,
        detail=detail,
    )


def merge_active_handler_result(
    *,
    shadow_message: str,
    effect: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    if effect.get("recorded"):
        return "active_effect_recorded", effect
    if effect.get("idempotent"):
        return "active_effect_idempotent", effect
    return shadow_message, effect
