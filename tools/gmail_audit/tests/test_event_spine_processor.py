from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from event_spine.emitter import publish_os_event
from event_spine.handlers.registry import build_default_registry
from event_spine.models import OsEvent
from event_spine.processor import EventProcessor
from event_spine.store import InMemoryEventSpineStore


def _sample_event(
    event_id: str = "osevt_test001",
    *,
    event_type: str = "correlation_links_registered",
    status: str = "pending",
) -> OsEvent:
    return OsEvent(
        event_id=event_id,
        event_type=event_type,
        engagement_id="eng-1",
        source_repo="gmail-agent",
        occurred_at=datetime.now(timezone.utc),
        payload={"links_count": 2},
        correlation={"identity_id": "id-1"},
        processing_status=status,  # type: ignore[arg-type]
        attempt_count=0,
    )


def test_process_once_marks_correlation_event_processed() -> None:
    store = InMemoryEventSpineStore()
    store.insert_pending(_sample_event())
    processor = EventProcessor(store, build_default_registry(), mode="shadow", batch_size=10)
    result = processor.process_once()
    assert result.claimed == 1
    assert result.processed == 1
    row = store.get_by_id("osevt_test001")
    assert row is not None
    assert row.processing_status == "processed"


def test_duplicate_process_is_idempotent() -> None:
    store = InMemoryEventSpineStore()
    store.insert_pending(_sample_event())
    processor = EventProcessor(store, build_default_registry(), mode="shadow", batch_size=10)
    first = processor.process_once()
    second = processor.process_once()
    assert first.processed == 1
    assert second.claimed == 0
    assert second.processed == 0


def test_unknown_event_type_skipped_in_shadow() -> None:
    store = InMemoryEventSpineStore()
    store.insert_pending(_sample_event(event_id="osevt_unknown1", event_type="not_registered_yet"))
    processor = EventProcessor(store, build_default_registry(), mode="shadow", batch_size=10)
    result = processor.process_once()
    assert result.skipped == 1
    row = store.get_by_id("osevt_unknown1")
    assert row is not None
    assert row.processing_status == "skipped"


def test_handler_exception_marks_failed_without_crashing_batch() -> None:
    store = InMemoryEventSpineStore()
    store.insert_pending(_sample_event(event_id="osevt_fail1"))

    class BoomHandler:
        event_types = frozenset({"correlation_links_registered"})

        def handle(self, event: OsEvent, *, ctx):  # noqa: ANN001
            raise RuntimeError("handler boom")

    registry = build_default_registry()
    registry._by_type["correlation_links_registered"] = BoomHandler()  # noqa: SLF001
    processor = EventProcessor(store, registry, mode="shadow", batch_size=10)
    result = processor.process_once()
    assert result.failed == 1
    row = store.get_by_id("osevt_fail1")
    assert row is not None
    assert row.processing_status == "failed"
    assert "handler boom" in str(row.last_error or "")


def test_active_mode_records_bounded_handler_effect() -> None:
    store = InMemoryEventSpineStore()
    store.insert_pending(_sample_event(event_id="osevt_active1"))
    processor = EventProcessor(store, build_default_registry(), mode="active", batch_size=10)
    result = processor.process_once()
    assert result.processed == 1
    assert ("osevt_active1", "correlation_links_registered") in store.handler_effects
    second = processor.process_once()
    assert second.claimed == 0
    assert len(store.handler_effects) == 1


def test_claim_respects_batch_limit() -> None:
    store = InMemoryEventSpineStore()
    for idx in range(5):
        store.insert_pending(_sample_event(event_id=f"osevt_batch{idx}"))
    processor = EventProcessor(store, build_default_registry(), mode="shadow", batch_size=2)
    result = processor.process_once()
    assert result.claimed == 2
    pending = sum(
        1
        for row in store.events.values()
        if str(row.get("processing_status")) == "pending"
    )
    assert pending == 3


def test_malformed_payload_still_processes_with_empty_dict() -> None:
    store = InMemoryEventSpineStore()
    event = _sample_event(event_id="osevt_badjson")
    store.insert_pending(event)
    store.events["osevt_badjson"]["payload"] = "{not-json"
    processor = EventProcessor(store, build_default_registry(), mode="shadow", batch_size=5)
    result = processor.process_once()
    assert result.processed == 1


def test_cieplo_workflow_persisted_shadow_processed() -> None:
    store = InMemoryEventSpineStore()
    store.insert_pending(
        _sample_event(
            event_id="osevt_cieplo1",
            event_type="cieplo_workflow_persisted",
        )
    )
    processor = EventProcessor(store, build_default_registry(), mode="shadow", batch_size=10)
    result = processor.process_once()
    assert result.processed == 1
    row = store.get_by_id("osevt_cieplo1")
    assert row is not None
    assert row.processing_status == "processed"


def test_publish_os_event_requires_database_url() -> None:
    assert publish_os_event(database_url="", event_type="test") is None
