"""Event handler protocol."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from event_spine.models import HandlerResult, OsEvent, ProcessorMode


@dataclass(slots=True)
class EventContext:
    processor_id: str
    mode: ProcessorMode
    logger: logging.Logger
    settings: Any = None
    store: Any = None


class EventHandler(Protocol):
    @property
    def event_types(self) -> frozenset[str]:
        """Empty set means this handler is not selected by type (use as fallback only)."""
        ...

    def handle(self, event: OsEvent, *, ctx: EventContext) -> HandlerResult: ...
