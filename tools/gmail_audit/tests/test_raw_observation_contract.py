from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from raw_observation_contract import (
    RawObservation,
    RAW_OBSERVATION_SCHEMA_VERSION,
    build_raw_observation,
    build_source_fingerprint,
)


class RawObservationContractTests(unittest.TestCase):
    def test_raw_observation_round_trip_preserves_payload_and_fingerprint(self) -> None:
        observation = build_raw_observation(
            observation_kind="gmail_source_snapshot",
            source_kind="gmail",
            source_ref={
                "mailbox": "biuro.topinstal@gmail.com",
                "message_id": "msg-1",
                "thread_id": "thr-1",
                "history_id": "777",
            },
            occurred_at="2026-04-13T08:59:00+02:00",
            observed_at="2026-04-13T09:00:00+02:00",
            payload={
                "snapshot": {
                    "mailbox": "biuro.topinstal@gmail.com",
                    "source_message": {"message_id": "msg-1", "thread_id": "thr-1"},
                }
            },
            source_marker="history:777",
            created_by_runtime="gmail_intake.process_snapshot",
        )

        clone = RawObservation.from_dict(observation.to_dict())

        self.assertEqual(clone.observation_id, observation.observation_id)
        self.assertEqual(clone.schema_version, RAW_OBSERVATION_SCHEMA_VERSION)
        self.assertEqual(clone.source_ref["message_id"], "msg-1")
        self.assertEqual(clone.payload["snapshot"]["source_message"]["thread_id"], "thr-1")
        self.assertEqual(clone.source_fingerprint, observation.source_fingerprint)

    def test_source_fingerprint_is_stable_for_same_semantics(self) -> None:
        key_a = build_source_fingerprint(
            source_kind="drive",
            observation_kind="drive_candidate_observed",
            source_ref={"file_id": "drv-1", "revision_id": "rev-2", "source_ref": "gdrive://drv-1"},
            source_marker="rev-2",
        )
        key_b = build_source_fingerprint(
            source_kind="drive",
            observation_kind="drive_candidate_observed",
            source_ref={"source_ref": "gdrive://drv-1", "revision_id": "rev-2", "file_id": "drv-1"},
            source_marker="rev-2",
        )

        self.assertEqual(key_a, key_b)


if __name__ == "__main__":
    unittest.main()
