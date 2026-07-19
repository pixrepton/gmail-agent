"""Staging engagement TTL cleanup (MAX-STACK slice E)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def _parse_ts(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def cleanup_stale_staging_engagements(
    store: Any,
    *,
    max_age_hours: int = 72,
) -> int:
    """Remove staging rows older than TTL without case_id. Returns count removed."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(max_age_hours)))
    removed = 0
    list_fn = getattr(store, "list_staging_engagement_ids", None)
    load_fn = getattr(store, "load_snapshot", None)
    delete_fn = getattr(store, "soft_delete_snapshot", None) or getattr(store, "delete_snapshot", None)
    if not callable(list_fn) or not callable(load_fn) or not callable(delete_fn):
        return 0
    for eid in list_fn() or []:
        if not str(eid).startswith("stg_"):
            continue
        snap = load_fn(eid)
        if snap is None or str(getattr(snap, "case_id", "") or "").strip():
            continue
        updated = _parse_ts(str(getattr(store, "_rows", {}).get(eid, {}).get("updated_at") or ""))
        if updated is None:
            updated = _parse_ts(str(getattr(snap, "trace_id", "") or ""))
        if updated is None:
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated < cutoff:
            delete_fn(eid)
            removed += 1
    return removed


__all__ = ["cleanup_stale_staging_engagements"]
