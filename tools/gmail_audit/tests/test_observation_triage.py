from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from gmail_signal_adapter import build_gmail_raw_observation
from drive_signal_adapter import build_drive_raw_observation
from observation_triage import triage_drive_observation, triage_gmail_observation


class ObservationTriageTests(unittest.TestCase):
    def test_gmail_triage_returns_explicit_skip_for_obvious_noise(self) -> None:
        observation = build_gmail_raw_observation(
            snapshot={
                "mailbox": "ops@example.com",
                "observed_at": "2026-04-15T10:00:00+02:00",
                "source_message": {
                    "message_id": "msg-noise",
                    "thread_id": "thr-noise",
                    "history_id": "1001",
                    "date": "2026-04-15T09:59:00+02:00",
                    "subject": "Newsletter security alert",
                    "sender": "No Reply",
                    "sender_email": "noreply@example.com",
                    "body": "unsubscribe newsletter update",
                },
            },
            created_by_runtime="test",
        )

        triage = triage_gmail_observation(observation)

        self.assertEqual(triage["triage_class"], "ignore")
        self.assertEqual(triage["routing_decision"], "skip_heavy_reasoning")
        self.assertEqual(triage["reasoning_budget"]["reasoning_mode"], "skip")
        self.assertEqual(triage["preclassification"]["lane"], "skip")

    def test_gmail_triage_returns_business_signal_for_operator_relevant_message(self) -> None:
        observation = build_gmail_raw_observation(
            snapshot={
                "mailbox": "ops@example.com",
                "observed_at": "2026-04-15T10:00:00+02:00",
                "source_message": {
                    "message_id": "msg-real",
                    "thread_id": "thr-real",
                    "history_id": "1002",
                    "date": "2026-04-15T09:58:00+02:00",
                    "subject": "Prośba o termin montażu",
                    "sender": "Jan Kowalski",
                    "sender_email": "jan@example.com",
                    "body": "Czy możecie potwierdzić termin montażu?",
                },
            },
            created_by_runtime="test",
        )

        triage = triage_gmail_observation(observation)

        self.assertEqual(triage["triage_class"], "business_signal")
        self.assertEqual(triage["routing_decision"], "promote_to_reasoning")
        self.assertEqual(triage["reasoning_budget"]["reasoning_mode"], "standard")
        self.assertEqual(triage["preclassification"]["lane"], "intake_llm")

    def test_drive_triage_enables_batching_for_case_media_burst(self) -> None:
        observation = build_drive_raw_observation(
            source_ref={
                "file_id": "drv-1",
                "revision_id": "rev-1",
                "modified_time": "2026-04-15T10:01:05+02:00",
                "parent_drive_item_id": "folder-1",
                "source_ref": "gdrive://drv-1",
            },
            observed_at="2026-04-15T10:01:05+02:00",
            payload={
                "candidate": {
                    "drive_item_id": "drv-1",
                    "title": "photo-1.jpg",
                    "mime_type": "image/jpeg",
                    "folder_path": "case-folder/photos",
                    "parent_drive_item_id": "folder-1",
                    "source_ref": "gdrive://drv-1",
                    "is_folder": False,
                    "size_bytes": 1024,
                    "modified_time": "2026-04-15T10:01:05+02:00",
                    "lane": "case_folder",
                    "document_kind": "media_asset",
                    "scope": "case_specific",
                    "probable_case_key": "case-key-1",
                    "classification_confidence": 0.97,
                }
            },
            created_by_runtime="test",
        )

        triage = triage_drive_observation(observation)

        self.assertEqual(triage["triage_class"], "business_signal")
        self.assertEqual(triage["routing_decision"], "promote_to_reasoning")
        self.assertTrue(triage["batching"]["enabled"])
        self.assertEqual(triage["batching"]["signal_kind"], "drive_media_batch_observed")
        self.assertEqual(triage["reasoning_budget"]["reasoning_mode"], "thin")


if __name__ == "__main__":
    unittest.main()
