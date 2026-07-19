from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_contract import build_canonical_signal
from signal_journal import SignalJournal


def _build_signal(*, message_id: str, revision_marker: str, observed_at: str, case_id: str = "case-1"):
    return build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"mailbox": "biuro.topinstal@gmail.com", "message_id": message_id, "thread_id": "thr-1"},
        observed_at=observed_at,
        signal_summary_pl=f"Wiadomosc {message_id}",
        payload={"case_id": case_id, "snapshot": {"message_id": message_id}},
        artifacts={"run_id": "run-1"},
        case_key_hint="case-key-1",
        thread_key_hint="thr-1",
        business_lane="intake_llm",
        revision_marker=revision_marker,
        created_by_runtime="gmail_signal_adapter",
    )


class SignalJournalTests(unittest.TestCase):
    def test_append_only_duplicate_suppression_uses_idempotency_key(self) -> None:
        store = InMemoryMailboxMemoryStore()
        journal = SignalJournal(store)

        signal = _build_signal(message_id="msg-1", revision_marker="history:10", observed_at="2026-04-13T10:00:00+02:00")
        duplicate = _build_signal(message_id="msg-1", revision_marker="history:10", observed_at="2026-04-13T10:00:00+02:00")

        first = journal.append(signal)
        second = journal.append(duplicate)

        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(second.duplicate_of_signal_id, signal.signal_id)
        self.assertEqual(len(store.signals), 1)

    def test_replay_order_uses_observed_time_then_insert_order(self) -> None:
        store = InMemoryMailboxMemoryStore()
        journal = SignalJournal(store)

        journal.append(_build_signal(message_id="msg-2", revision_marker="history:12", observed_at="2026-04-13T10:02:00+02:00"))
        journal.append(_build_signal(message_id="msg-1", revision_marker="history:10", observed_at="2026-04-13T10:00:00+02:00"))
        journal.append(_build_signal(message_id="msg-3", revision_marker="history:15", observed_at="2026-04-13T10:05:00+02:00"))

        rows = journal.fetch_signals_for_case(case_id="case-1")

        self.assertEqual([row.payload["snapshot"]["message_id"] for row in rows], ["msg-1", "msg-2", "msg-3"])

    def test_source_cursor_round_trip_is_durable_in_store(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.upsert_source_cursor(
            {
                "cursor_key": "gmail:biuro.topinstal@gmail.com",
                "source_kind": "gmail",
                "cursor_scope": "biuro.topinstal@gmail.com",
                "last_cursor": "123456",
                "last_success_at": "2026-04-13T10:00:00+02:00",
                "last_error": "",
                "status": "ok",
                "metadata": {"history_types": ["messageAdded"]},
                "updated_at": "2026-04-13T10:00:00+02:00",
            }
        )

        fetched = store.fetch_source_cursor("gmail", "biuro.topinstal@gmail.com")

        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["last_cursor"], "123456")
        self.assertEqual(fetched["status"], "ok")


if __name__ == "__main__":
    unittest.main()
