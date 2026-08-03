"""Minimal business outcome capture substrate (DQ-05 / RP-31).

Persists operator-visible win/loss on case metadata via mutate_case. World-model
and behavior steering remain out of scope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

VALID_OUTCOMES = frozenset({"won", "lost", "cancelled", "unknown"})

WON_CASE_STATUSES = frozenset({"completed", "closed", "resolved", "archived"})
LOST_CASE_STATUSES = frozenset({"lost", "cancelled"})
WON_OUTCOME_VALUES = frozenset({"won", "completed", "accepted"})
LOST_OUTCOME_VALUES = frozenset({"lost", "cancelled", "rejected"})


def read_resolution_outcome(case_row: dict[str, Any]) -> str:
    meta = case_row.get("metadata") if isinstance(case_row.get("metadata"), dict) else {}
    outcome = str(
        meta.get("resolution_outcome") or meta.get("business_outcome") or ""
    ).strip().lower()
    if outcome:
        return outcome
    snap = case_row.get("snapshot_json") if isinstance(case_row.get("snapshot_json"), dict) else {}
    return str(snap.get("resolution_outcome") or snap.get("outcome") or "").strip().lower()


def classify_case_outcome(*, status: str, resolution_outcome: str = "") -> str:
    """Return won|lost|open using lifecycle status + optional metadata outcome."""
    outcome = str(resolution_outcome or "").strip().lower()
    status_l = str(status or "").strip().lower()
    if outcome in WON_OUTCOME_VALUES:
        return "won"
    if outcome in LOST_OUTCOME_VALUES:
        return "lost"
    if status_l in LOST_CASE_STATUSES:
        return "lost"
    if status_l in WON_CASE_STATUSES:
        return "won"
    return "open"


def record_business_outcome(
    store: Any,
    *,
    case_id: str,
    outcome: str,
    source: str = "operator",
    note: str = "",
) -> dict[str, Any]:
    normalized = str(outcome or "").strip().lower()
    if normalized not in VALID_OUTCOMES:
        return {"ok": False, "error": f"invalid_outcome:{normalized or 'empty'}"}
    mutate = getattr(store, "mutate_case", None)
    if not callable(mutate):
        return {"ok": False, "error": "mutate_case_unavailable"}

    def _mutator(row: dict[str, Any]) -> dict[str, Any]:
        meta = dict(row.get("metadata") or {})
        meta["resolution_outcome"] = normalized
        meta["business_outcome"] = normalized
        meta["business_outcome_captured_at"] = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        meta["business_outcome_source"] = str(source or "operator")
        if note:
            meta["business_outcome_note"] = str(note)[:500]
        row["metadata"] = meta
        status = str(row.get("status") or "").strip().lower()
        if normalized == "won" and status not in WON_CASE_STATUSES:
            row["status"] = "completed"
        elif normalized == "lost" and status not in LOST_CASE_STATUSES:
            row["status"] = "lost"
        return row

    try:
        updated = mutate(case_id, _mutator, create_if_missing=False)
    except LookupError:
        return {"ok": False, "error": "case_not_found"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "case_id": case_id,
        "resolution_outcome": normalized,
        "case_status": str((updated or {}).get("status") or ""),
    }


__all__ = [
    "VALID_OUTCOMES",
    "WON_CASE_STATUSES",
    "LOST_CASE_STATUSES",
    "WON_OUTCOME_VALUES",
    "LOST_OUTCOME_VALUES",
    "read_resolution_outcome",
    "classify_case_outcome",
    "record_business_outcome",
]
