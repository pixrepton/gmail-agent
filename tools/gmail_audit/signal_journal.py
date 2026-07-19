"""Append-only durable signal journal backed by mailbox-memory persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import time
from typing import Any

from artifact_io import append_jsonl
from signal_contract import CanonicalSignal


@dataclass(slots=True, frozen=True)
class SignalJournalAppendResult:
    signal: CanonicalSignal
    inserted: bool
    duplicate_of_signal_id: str = ""
    duration_ms: float = 0.0  # Faza 3a: metryka latency append


class SignalJournal:
    """Canonical append-only signal journal with durable idempotency checks."""

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

    def append(self, signal: CanonicalSignal) -> SignalJournalAppendResult:
        _t = time.monotonic()
        existing = self.store.fetch_signal_by_idempotency_key(signal.idempotency_key)
        if existing:
            existing_signal = CanonicalSignal.from_dict(existing)
            if self.jsonl_mirror_enabled and self.jsonl_mirror_path is not None:
                append_jsonl(
                    self.jsonl_mirror_path,
                    {"record_type": "signal", "action": "duplicate_skip", "signal_kind": signal.signal_kind,
                     "idempotency_key": signal.idempotency_key, "existing_signal_id": existing_signal.signal_id,
                     "duration_ms": round((time.monotonic() - _t) * 1000, 1)},
                )
            return SignalJournalAppendResult(
                signal=existing_signal,
                inserted=False,
                duplicate_of_signal_id=existing_signal.signal_id,
                duration_ms=round((time.monotonic() - _t) * 1000, 1),
            )

        inserted = bool(
            self.store.append_signal(
                {
                    "signal_id": signal.signal_id,
                    "schema_version": signal.schema_version,
                    "signal_kind": signal.signal_kind,
                    "source_kind": signal.source_kind,
                    "source_ref_json": signal.source_ref,
                    "observed_at": signal.observed_at,
                    "effective_at": signal.effective_at,
                    "idempotency_key": signal.idempotency_key,
                    "content_hash": signal.content_hash or "",
                    "case_key_hint": signal.case_key_hint or "",
                    "thread_key_hint": signal.thread_key_hint or "",
                    "business_lane": signal.business_lane or "",
                    "signal_summary_pl": signal.signal_summary_pl,
                    "payload_json": signal.payload,
                    "artifacts_json": signal.artifacts,
                    "processing_state": signal.processing_state,
                    "replayable": signal.replayable,
                    "engagement_id": str(getattr(signal, "engagement_id", "") or ""),
                    "created_by_runtime": signal.created_by_runtime,
                    "created_at": signal.created_at,
                }
            )
        )

        duplicate_of = ""
        duration_ms = round((time.monotonic() - _t) * 1000, 1)
        if not inserted:
            duplicate = self.store.fetch_signal_by_idempotency_key(signal.idempotency_key)
            duplicate_of = str((duplicate or {}).get("signal_id") or "")
        elif self.jsonl_mirror_enabled and self.jsonl_mirror_path is not None:
            append_jsonl(self.jsonl_mirror_path, signal.to_dict())

        return SignalJournalAppendResult(
            signal=signal,
            inserted=inserted,
            duplicate_of_signal_id=duplicate_of,
            duration_ms=duration_ms,
        )

    def fetch_signal(self, signal_id: str) -> CanonicalSignal | None:
        row = self.store.fetch_signal(signal_id)
        return CanonicalSignal.from_dict(row) if row else None

    def fetch_signals_for_case(self, case_id: str = "", *, case_key_hint: str = "", limit: int = 200) -> list[CanonicalSignal]:
        rows = self.store.fetch_signals_for_case(case_id, case_key_hint=case_key_hint, limit=limit)
        return [CanonicalSignal.from_dict(row) for row in rows]

    def fetch_signals_for_source(self, source_kind: str, *, limit: int = 200) -> list[CanonicalSignal]:
        rows = self.store.fetch_signals_for_source(source_kind, limit=limit)
        return [CanonicalSignal.from_dict(row) for row in rows]

    def record_processing_attempt(
        self,
        *,
        signal: CanonicalSignal,
        status: str,
        error_text: str = "",
        details: dict[str, Any] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        timestamp = datetime.now().astimezone().isoformat()
        self.store.append_signal_processing_attempt(
            {
                "attempt_id": f"sigatt_{signal.signal_id}_{timestamp}",
                "signal_id": signal.signal_id,
                "status": status,
                "started_at": started_at or timestamp,
                "finished_at": finished_at or timestamp,
                "error_text": error_text,
                "details_json": details or {},
                "created_at": timestamp,
            }
        )


__all__ = [
    "SignalJournal",
    "SignalJournalAppendResult",
]
