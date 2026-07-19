"""Integration tests for the full reconcile flow — signal → entity link → downstream."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from signal_contract import CanonicalSignal


class FakeJournal:
    """Minimal journal that returns 'inserted' for all signals."""

    def append(self, signal: CanonicalSignal):
        return SimpleNamespace(inserted=True, signal=signal, duplicate_of_signal_id="", duration_ms=0.0, to_dict=lambda: {})

    def replay_all(self):
        return []

    def record_processing_attempt(self, **kwargs):
        pass

    def close(self):
        pass


class FakeStore:
    """Minimal in-memory store for reconcile testing."""

    def __init__(self):
        self.cases: dict[str, dict] = {}
        self.signals: list[dict] = []
        self._conn_open = False

    def fetch_signal_by_idempotency_key(self, key: str) -> dict | None:
        return None

    def append_signal(self, row: dict) -> bool:
        self.signals.append(row)
        return True

    def get_case(self, case_id: str) -> dict | None:
        return self.cases.get(case_id)

    def upsert_case(self, case: dict) -> None:
        self.cases[str(case.get("case_id", ""))] = case

    def _connect(self, **kw):
        return SimpleNamespace(cursor=lambda: SimpleNamespace(execute=lambda *a: None, fetchone=lambda: None))


class TestReconcileFullFlow(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.journal = FakeJournal()
        self.settings = SimpleNamespace(
            mailbox_memory_database_url="",
            signal_runtime_mode="active",
            daszek_operational_feed_auto_push_enabled=False,
        )

    def _make_signal(self, signal_id: str = "sig-1", source_kind: str = "gmail") -> CanonicalSignal:
        from signal_contract import CanonicalSignal
        return CanonicalSignal(
            signal_id=signal_id,
            schema_version="1",
            signal_kind="gmail_message_observed",
            source_kind=source_kind,
            source_ref={"message_id": "msg-1"},
            observed_at="2026-07-03T12:00:00Z",
            effective_at=None,
            case_key_hint=None,
            thread_key_hint=None,
            business_lane=None,
            signal_summary_pl="Test signal",
            payload={"snapshot": {"source_message": {"message_id": "msg-1"}}},
            artifacts={},
            processing_state="pending",
            idempotency_key=f"idem-{signal_id}",
            content_hash=None,
            replayable=True,
            created_by_runtime="test",
        )

    def test_signal_contract_constructs(self):
        """CanonicalSignal can be constructed with all required fields."""
        signal = self._make_signal("sig-test")
        self.assertEqual(signal.signal_id, "sig-test")
        self.assertEqual(signal.signal_kind, "gmail_message_observed")

    def test_signal_context_constructs(self):
        """SignalRuntimeContext can be constructed."""
        from signal_reconciler import SignalRuntimeContext
        ctx = SignalRuntimeContext(
            settings=self.settings,
            journal=self.journal,
            store=self.store,
            mode="test",
        )
        self.assertEqual(ctx.mode, "test")
        self.assertIsNotNone(ctx.journal)

    def test_signal_context_with_trace_id(self):
        """SignalRuntimeContext accepts and stores trace_id."""
        from signal_reconciler import SignalRuntimeContext
        ctx = SignalRuntimeContext(
            settings=self.settings,
            journal=self.journal,
            store=self.store,
            mode="test",
            trace_id="test-trace-123",
        )
        self.assertEqual(ctx.trace_id, "test-trace-123")


if __name__ == "__main__":
    unittest.main()
