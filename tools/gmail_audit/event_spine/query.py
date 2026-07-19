"""Read-only queries over unified_os_events for operator projection (Daszek W0+)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from event_spine.store import _row_to_event

_SELECT_EVENT_COLUMNS = """
    event_id, event_type, engagement_id, source_repo, occurred_at,
    payload, correlation, processing_status, attempt_count,
    processor_id, last_error, failure_detail,
    trace_id, span_id, parent_event_id,
    case_id, user_id, session_id,
    severity, duration_ms, token_usage, cost,
    success, error_message
"""


def _iso_occurred_at(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return str(value or "")


def event_to_api_item(row: Any) -> dict[str, Any]:
    """Serialize DB row tuple or OsEvent to Daszek-facing JSON item."""
    if hasattr(row, "event_id"):
        event = row
        payload = dict(event.payload or {})
        correlation = dict(event.correlation or {})
        return {
            "event_id": str(event.event_id),
            "event_type": str(event.event_type or ""),
            "source_repo": str(event.source_repo or "gmail-agent"),
            "engagement_id": str(event.engagement_id or ""),
            "case_id": str(event.case_id or ""),
            "trace_id": str(event.trace_id or ""),
            "occurred_at": event.occurred_at.astimezone(timezone.utc).isoformat(),
            "summary_pl": str(payload.get("summary_pl") or "").strip(),
            "status": str(payload.get("status") or "ok").strip() or "ok",
            "payload": payload,
            "correlation": correlation,
        }
    event = _row_to_event(row)
    payload = dict(event.payload or {})
    correlation = dict(event.correlation or {})
    return {
        "event_id": str(event.event_id),
        "event_type": str(event.event_type or ""),
        "source_repo": str(event.source_repo or "gmail-agent"),
        "engagement_id": str(event.engagement_id or ""),
        "case_id": str(event.case_id or ""),
        "trace_id": str(event.trace_id or ""),
        "occurred_at": event.occurred_at.astimezone(timezone.utc).isoformat(),
        "summary_pl": str(payload.get("summary_pl") or "").strip(),
        "status": str(payload.get("status") or "ok").strip() or "ok",
        "payload": payload,
        "correlation": correlation,
    }


def fetch_os_events_for_engagement(
    database_url: str,
    engagement_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return newest-first os_event items for one engagement (read-only)."""
    eid = str(engagement_id or "").strip()
    if not eid or not str(database_url or "").strip():
        return []

    try:
        import psycopg
    except ImportError:
        return []

    capped = max(1, min(int(limit or 50), 200))
    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_SELECT_EVENT_COLUMNS}
                    FROM unified_os_events
                    WHERE engagement_id = %s
                    ORDER BY occurred_at DESC
                    LIMIT %s
                    """,
                    (eid, capped),
                )
                rows = cur.fetchall()
    except Exception:
        return []

    return [event_to_api_item(row) for row in rows]


def fetch_recent_os_events(
    database_url: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Global recent events (System view — W1+)."""
    if not str(database_url or "").strip():
        return []

    try:
        import psycopg
    except ImportError:
        return []

    capped = max(1, min(int(limit or 50), 200))
    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_SELECT_EVENT_COLUMNS}
                    FROM unified_os_events
                    ORDER BY occurred_at DESC
                    LIMIT %s
                    """,
                    (capped,),
                )
                rows = cur.fetchall()
    except Exception:
        return []

    return [event_to_api_item(row) for row in rows]

def cleanup_old_events(
    database_url: str,
    *,
    ttl_days: int = 30,
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete os_events older than ttl_days. Returns deleted/remaining counts."""
    if not str(database_url or "").strip():
        return {"deleted": 0, "remaining": 0}
    try:
        import psycopg
    except ImportError:
        return {"deleted": 0, "remaining": 0}
    ttl = max(1, int(ttl_days))
    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                if dry_run:
                    cur.execute("SELECT COUNT(*) FROM unified_os_events WHERE occurred_at < NOW() - INTERVAL %s days", (ttl,))
                    to_delete = (cur.fetchone() or [0])[0] or 0
                    cur.execute("SELECT COUNT(*) FROM unified_os_events")
                    remaining = (cur.fetchone() or [0])[0] or 0
                    return {"deleted": 0, "ready_to_delete": int(to_delete), "remaining": int(remaining)}
                cur.execute("DELETE FROM unified_os_events WHERE occurred_at < NOW() - INTERVAL %s days", (ttl,))
                deleted = cur.rowcount
                conn.commit()
                cur.execute("SELECT COUNT(*) FROM unified_os_events")
                remaining = (cur.fetchone() or [0])[0] or 0
                return {"deleted": int(deleted), "remaining": int(remaining)}
    except Exception:
        return {"deleted": 0, "remaining": 0}
