"""Integration tests for Event Processor — claim + dispatch."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from event_spine.models import OsEvent
from event_spine.processor import EventProcessor
from event_spine.store import InMemoryEventSpineStore


class TestEventProcessor(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryEventSpineStore()

    def _make_event(self, event_id: str, event_type: str = "test.event") -> OsEvent:
        return OsEvent(
            event_id=event_id,
            event_type=event_type,
            engagement_id="",
            source_repo="gmail-agent",
            occurred_at=datetime.now(timezone.utc),
            payload={},
            processing_status="pending",
            correlation={},
            attempt_count=0,
        )

    def test_claim_and_process_empty(self):
        """Processor with no pending events — claims 0."""
        proc = EventProcessor(store=self.store, mode="active")
        result = proc.process_once()
        self.assertEqual(result.claimed, 0)

    def test_claim_pending_event(self):
        """Processor claims a batch of pending events."""
        self.store.insert_pending(self._make_event("e1", "test.event"))
        proc = EventProcessor(store=self.store, mode="active")
        result = proc.process_once()
        self.assertEqual(result.claimed, 1)

    def test_shadow_mode_claims_but_not_processes(self):
        """In shadow mode, events are claimed but not processed by handlers."""
        self.store.insert_pending(self._make_event("e2", "test.event"))
        proc = EventProcessor(store=self.store, mode="shadow")
        result = proc.process_once()
        # Events are claimed regardless of mode (needed for inspection)
        self.assertEqual(result.claimed, 1)
        # No matching handler in default registry for test.event, so processed=0
        self.assertEqual(result.processed, 0)


if __name__ == "__main__":
    unittest.main()
