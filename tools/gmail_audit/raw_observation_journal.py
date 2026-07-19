"""Append-only durable raw-observation journal."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from artifact_io import append_jsonl
from raw_observation_contract import RawObservation


@dataclass(slots=True, frozen=True)
class RawObservationJournalAppendResult:
    observation: RawObservation
    inserted: bool
    duplicate_of_observation_id: str = ""


class RawObservationJournal:
    """Canonical append-only raw-observation journal with durable dedupe."""

    def __init__(
        self,
        store: Any,
        *,
        jsonl_mirror_enabled: bool = False,
        jsonl_mirror_path: Path | None = None,
    ) -> None:
        self.store = store
        self.jsonl_mirror_enabled = bool(jsonl_mirror_enabled)
        self.jsonl_mirror_path = jsonl_mirror_path

    def append(self, observation: RawObservation) -> RawObservationJournalAppendResult:
        existing = self.store.fetch_raw_observation_by_source_fingerprint(observation.source_fingerprint)
        if existing:
            existing_observation = RawObservation.from_dict(existing)
            return RawObservationJournalAppendResult(
                observation=existing_observation,
                inserted=False,
                duplicate_of_observation_id=existing_observation.observation_id,
            )

        inserted = bool(
            self.store.append_raw_observation(
                {
                    "observation_id": observation.observation_id,
                    "schema_version": observation.schema_version,
                    "observation_kind": observation.observation_kind,
                    "source_kind": observation.source_kind,
                    "source_ref_json": observation.source_ref,
                    "occurred_at": observation.occurred_at,
                    "observed_at": observation.observed_at,
                    "source_fingerprint": observation.source_fingerprint,
                    "payload_hash": observation.payload_hash,
                    "payload_json": observation.payload,
                    "created_by_runtime": observation.created_by_runtime,
                    "created_at": observation.created_at,
                }
            )
        )

        duplicate_of = ""
        if not inserted:
            duplicate = self.store.fetch_raw_observation_by_source_fingerprint(observation.source_fingerprint)
            duplicate_of = str((duplicate or {}).get("observation_id") or "")
        elif self.jsonl_mirror_enabled and self.jsonl_mirror_path is not None:
            append_jsonl(self.jsonl_mirror_path, observation.to_dict())

        return RawObservationJournalAppendResult(
            observation=observation,
            inserted=inserted,
            duplicate_of_observation_id=duplicate_of,
        )

    def fetch_observation(self, observation_id: str) -> RawObservation | None:
        row = self.store.fetch_raw_observation(observation_id)
        return RawObservation.from_dict(row) if row else None

    def fetch_observations_for_source(self, source_kind: str, *, limit: int = 200) -> list[RawObservation]:
        rows = self.store.fetch_raw_observations_for_source(source_kind, limit=limit)
        return [RawObservation.from_dict(row) for row in rows]


__all__ = [
    "RawObservationJournal",
    "RawObservationJournalAppendResult",
]
