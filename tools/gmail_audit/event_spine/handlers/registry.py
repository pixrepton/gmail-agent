"""Handler registry for event spine processor."""

from __future__ import annotations

from event_spine.handlers.base import EventHandler
from event_spine.handlers.shadow_cieplo_workflow import ShadowCieploWorkflowPersistedHandler
from event_spine.handlers.shadow_correlation import ShadowCorrelationLinksHandler
from event_spine.handlers.unknown import UnknownEventHandler


class HandlerRegistry:
    def __init__(self, handlers: list[EventHandler] | None = None) -> None:
        typed: list[EventHandler] = list(handlers or [])
        self._by_type: dict[str, EventHandler] = {}
        self._fallback = UnknownEventHandler()
        for handler in typed:
            for event_type in handler.event_types:
                self._by_type[event_type] = handler

    def resolve(self, event_type: str) -> EventHandler:
        key = str(event_type or "").strip()
        return self._by_type.get(key, self._fallback)


def build_default_registry() -> HandlerRegistry:
    return HandlerRegistry(
        handlers=[
            ShadowCorrelationLinksHandler(),
            ShadowCieploWorkflowPersistedHandler(),
            UnknownEventHandler(),
        ]
    )
