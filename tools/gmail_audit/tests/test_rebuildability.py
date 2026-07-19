from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_snapshot_manager import CaseSnapshotManager
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_contract import build_canonical_signal
from signal_journal import SignalJournal


def _build_signal(*, suffix: str, summary: str, fact_key: str, fact_value: str, observed_at: str):
    return build_canonical_signal(
        signal_kind="drive_document_added",
        source_kind="drive",
        source_ref={
            "file_id": f"drv-{suffix}",
            "change_id": f"chg-{suffix}",
            "revision_id": f"rev-{suffix}",
            "modified_time": observed_at,
        },
        observed_at=observed_at,
        effective_at=observed_at,
        case_key_hint="case-key-2",
        thread_key_hint="case-key-2",
        business_lane="operations",
        signal_summary_pl=summary,
        payload={
            "case_id": "case-2",
            "case_key": "case-key-2",
            "fact_rows": [
                {
                    "fact_id": f"fact-{suffix}",
                    "case_id": "case-2",
                    "message_id": "",
                    "document_id": f"doc-{suffix}",
                    "entity_scope": "document",
                    "fact_key": fact_key,
                    "normalized_value": fact_value,
                    "raw_value": fact_value,
                    "confidence": 0.9,
                    "observed_at": observed_at,
                    "source_type": "drive_signal",
                    "source_ref": f"https://drive.google.com/file/d/drv-{suffix}",
                    "status": "active",
                    "metadata": {},
                }
            ],
        },
        artifacts={"raw_observation_id": f"obs-{suffix}"},
        revision_marker=f"rev-{suffix}",
        created_by_runtime="test",
    )


class RebuildabilityTests(unittest.TestCase):
    def test_case_snapshot_can_be_rebuilt_from_signal_journal_only(self) -> None:
        store = InMemoryMailboxMemoryStore()
        store.bootstrap()
        store.upsert_case(
            {
                "case_id": "case-2",
                "case_key": "case-key-2",
                "thread_id": "",
                "case_family": "operations",
                "mailbox": "drive",
                "subject": "bundle",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": {},
                "created_at": "2026-04-15T11:00:00+02:00",
                "updated_at": "2026-04-15T11:00:00+02:00",
            }
        )
        manager = CaseSnapshotManager(store=store)
        journal = SignalJournal(store)
        first_signal = _build_signal(
            suffix="a",
            summary="Media update",
            fact_key="city",
            fact_value="Jaworzno",
            observed_at="2026-04-15T11:01:00+02:00",
        )
        second_signal = _build_signal(
            suffix="b",
            summary="Final protocol added",
            fact_key="installation_stage",
            fact_value="completed",
            observed_at="2026-04-15T11:05:00+02:00",
        )

        journal.append(first_signal)
        journal.append(second_signal)
        manager.apply_signal(first_signal)
        latest = manager.apply_signal(second_signal)

        rebuilt = manager.rebuild_from_signal_journal(
            journal=journal,
            case_id="case-2",
            case_key_hint="case-key-2",
        )

        self.assertEqual(rebuilt["case_id"], "case-2")
        self.assertEqual(rebuilt["summary"], latest["summary"])
        self.assertEqual(rebuilt["status"], latest["status"])
        self.assertEqual(rebuilt["open_loops"], latest["open_loops"])
        self.assertEqual(rebuilt["last_facts"], latest["last_facts"])
        self.assertEqual(rebuilt["cold_evidence_pointers"], latest["cold_evidence_pointers"])
        self.assertEqual(rebuilt["version"], latest["version"])


if __name__ == "__main__":
    unittest.main()
