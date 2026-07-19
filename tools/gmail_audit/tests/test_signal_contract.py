from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from signal_contract import CanonicalSignal, build_canonical_signal, build_idempotency_key
from signal_types import SIGNAL_SCHEMA_VERSION


class SignalContractTests(unittest.TestCase):
    def test_signal_serialization_round_trip_preserves_fields(self) -> None:
        signal = build_canonical_signal(
            signal_kind="gmail_message_observed",
            source_kind="gmail",
            source_ref={"mailbox": "biuro.topinstal@gmail.com", "message_id": "msg-1", "thread_id": "thr-1"},
            observed_at="2026-04-13T10:00:00+02:00",
            signal_summary_pl="Nowa wiadomosc testowa",
            payload={"snapshot": {"message_id": "msg-1"}},
            artifacts={"run_id": "run-1"},
            case_key_hint="case:1",
            thread_key_hint="thr-1",
            business_lane="intake_llm",
            revision_marker="history:10",
            created_by_runtime="gmail_intake.process_snapshot",
        )

        clone = CanonicalSignal.from_dict(signal.to_dict())

        self.assertEqual(clone.signal_id, signal.signal_id)
        self.assertEqual(clone.schema_version, SIGNAL_SCHEMA_VERSION)
        self.assertEqual(clone.source_ref["message_id"], "msg-1")
        self.assertEqual(clone.payload["snapshot"]["message_id"], "msg-1")
        self.assertEqual(clone.idempotency_key, signal.idempotency_key)

    def test_idempotency_key_is_stable_for_same_semantics(self) -> None:
        source_ref = {"mailbox": "biuro.topinstal@gmail.com", "message_id": "msg-1", "thread_id": "thr-1"}
        key_a = build_idempotency_key(
            source_kind="gmail",
            signal_kind="gmail_message_observed",
            source_ref=source_ref,
            revision_marker="history:10",
        )
        key_b = build_idempotency_key(
            source_kind="gmail",
            signal_kind="gmail_message_observed",
            source_ref={"thread_id": "thr-1", "message_id": "msg-1", "mailbox": "biuro.topinstal@gmail.com"},
            revision_marker="history:10",
        )

        self.assertEqual(key_a, key_b)

    def test_idempotency_key_changes_when_revision_changes(self) -> None:
        source_ref = {"file_id": "drv-1", "change_id": "ch-10"}
        key_a = build_idempotency_key(
            source_kind="drive",
            signal_kind="drive_document_updated",
            source_ref=source_ref,
            revision_marker="rev-1",
        )
        key_b = build_idempotency_key(
            source_kind="drive",
            signal_kind="drive_document_updated",
            source_ref=source_ref,
            revision_marker="rev-2",
        )

        self.assertNotEqual(key_a, key_b)

    def test_source_ref_integrity_survives_sorting(self) -> None:
        signal = build_canonical_signal(
            signal_kind="drive_document_updated",
            source_kind="drive",
            source_ref={"revision_id": "rev-2", "file_id": "drv-1", "change_id": "ch-10"},
            observed_at="2026-04-13T10:10:00+02:00",
            signal_summary_pl="Drive update",
            payload={"file": {"id": "drv-1"}},
            artifacts={},
            revision_marker="rev-2",
            created_by_runtime="drive_signal_adapter",
        )

        self.assertEqual(signal.source_ref, {"change_id": "ch-10", "file_id": "drv-1", "revision_id": "rev-2"})


if __name__ == "__main__":
    unittest.main()
