from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory_store import InMemoryMailboxMemoryStore
from raw_observation_contract import build_raw_observation
from raw_observation_journal import RawObservationJournal


def _build_observation(*, message_id: str, source_marker: str, observed_at: str):
    return build_raw_observation(
        observation_kind="gmail_source_snapshot",
        source_kind="gmail",
        source_ref={
            "mailbox": "biuro.topinstal@gmail.com",
            "message_id": message_id,
            "thread_id": "thr-1",
            "history_id": source_marker,
        },
        occurred_at=observed_at,
        observed_at=observed_at,
        payload={"snapshot": {"source_message": {"message_id": message_id}}},
        source_marker=source_marker,
        created_by_runtime="test",
    )


class RawObservationJournalTests(unittest.TestCase):
    def test_append_only_duplicate_suppression_uses_source_fingerprint(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        journal = RawObservationJournal(store)

        observation = _build_observation(message_id="msg-1", source_marker="777", observed_at="2026-04-13T10:00:00+02:00")
        duplicate = _build_observation(message_id="msg-1", source_marker="777", observed_at="2026-04-13T10:00:00+02:00")

        first = journal.append(observation)
        second = journal.append(duplicate)

        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(second.duplicate_of_observation_id, observation.observation_id)
        self.assertEqual(len(store.raw_observations), 1)

    def test_fetch_observations_for_source_orders_by_observed_time(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        journal = RawObservationJournal(store)

        journal.append(_build_observation(message_id="msg-2", source_marker="779", observed_at="2026-04-13T10:02:00+02:00"))
        journal.append(_build_observation(message_id="msg-1", source_marker="777", observed_at="2026-04-13T10:00:00+02:00"))
        journal.append(_build_observation(message_id="msg-3", source_marker="780", observed_at="2026-04-13T10:05:00+02:00"))

        rows = journal.fetch_observations_for_source("gmail")

        self.assertEqual([row.source_ref["message_id"] for row in rows], ["msg-1", "msg-2", "msg-3"])


if __name__ == "__main__":
    unittest.main()
