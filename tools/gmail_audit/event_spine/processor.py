"""Active/shadow processor for unified_os_events."""

from __future__ import annotations

import time
import traceback
import uuid
from typing import Any

from event_spine.handlers.base import EventContext
from event_spine.handlers.registry import HandlerRegistry, build_default_registry
from event_spine.models import ProcessBatchResult
from event_spine.store import EventSpineStore
from log_config import get_logger

log = get_logger(__name__)


def _new_processor_id() -> str:
    return f"evtproc_{uuid.uuid4().hex[:12]}"


class EventProcessor:
    def __init__(
        self,
        store: EventSpineStore,
        registry: HandlerRegistry | None = None,
        *,
        mode: str,
        batch_size: int = 25,
        settings: Any = None,
        processor_id: str = "",
    ) -> None:
        self.store = store
        self.registry = registry or build_default_registry()
        self.mode = str(mode or "shadow").strip().lower()
        self.batch_size = max(1, int(batch_size))
        self.settings = settings
        self.processor_id = str(processor_id or "").strip() or _new_processor_id()

    def process_once(self) -> ProcessBatchResult:
        result = ProcessBatchResult()
        try:
            self.store.ensure_schema()
            events = self.store.claim_batch(limit=self.batch_size, processor_id=self.processor_id)
        except Exception as exc:
            log.exception("event_spine claim_batch failed", extra={"x": {"error": str(exc)[:200]}})
            result.errors.append(str(exc))
            return result

        result.claimed = len(events)
        ctx = EventContext(
            processor_id=self.processor_id,
            mode=self.mode,  # type: ignore[arg-type]
            logger=log,
            settings=self.settings,
            store=self.store,
        )

        for event in events:
            try:
                handler = self.registry.resolve(event.event_type)
                handler_result = handler.handle(event, ctx=ctx)
                ok = self.store.mark_terminal(
                    event.event_id,
                    status=handler_result.outcome,
                    processor_id=self.processor_id,
                    message=handler_result.message,
                    detail=handler_result.detail,
                )
                if not ok:
                    log.warning(
                        "event_spine mark_terminal no-op event_id=%s outcome=%s",
                        event.event_id,
                        handler_result.outcome,
                    )
                if handler_result.outcome == "processed":
                    result.processed += 1
                elif handler_result.outcome == "skipped":
                    result.skipped += 1
                else:
                    result.failed += 1
            except Exception as exc:
                log.exception(
                    "event_spine handler failed",
                    extra={"x": {"event_id": event.event_id, "event_type": event.event_type, "error": str(exc)[:200]}},
                )
                self.store.mark_terminal(
                    event.event_id,
                    status="failed",
                    processor_id=self.processor_id,
                    message=str(exc),
                    detail={
                        "error_type": type(exc).__name__,
                        "traceback": traceback.format_exc(),
                    },
                )
                result.failed += 1
                result.errors.append(f"{event.event_id}:{exc}")

        return result

    def run_loop(self, *, max_iterations: int = 0, poll_interval_sec: int = 15) -> ProcessBatchResult:
        aggregate = ProcessBatchResult()
        iterations = 0
        while True:
            iterations += 1
            batch = self.process_once()
            aggregate.claimed += batch.claimed
            aggregate.processed += batch.processed
            aggregate.failed += batch.failed
            aggregate.skipped += batch.skipped
            aggregate.errors.extend(batch.errors)
            if max_iterations > 0 and iterations >= max_iterations:
                break
            if max_iterations == 0 or iterations < max_iterations:
                time.sleep(max(1, int(poll_interval_sec)))
        return aggregate


def build_event_processor(
    settings: Any,
    *,
    store: EventSpineStore | None = None,
    registry: HandlerRegistry | None = None,
) -> EventProcessor:
    from event_spine.store import build_event_spine_store

    effective_store = store or build_event_spine_store(str(getattr(settings, "mailbox_memory_database_url", "") or ""))
    if effective_store is None:
        raise RuntimeError(
            "Event spine processor requires MAILBOX_MEMORY_DATABASE_URL. "
            "Next check: python tools/gmail_audit/gmail_intake.py doctor --skip-gmail --verbose"
        )
    mode = str(getattr(settings, "event_spine_processor_mode", "off") or "off").strip().lower()
    if mode == "off" and bool(getattr(settings, "event_spine_processor_enabled", False)):
        mode = "shadow"
    return EventProcessor(
        effective_store,
        registry or build_default_registry(),
        mode=mode,
        batch_size=int(getattr(settings, "event_spine_processor_batch_size", 25) or 25),
        settings=settings,
    )
