"""Best-effort unified OS event publisher (P1 MVP)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from log_config import get_logger

log = get_logger(__name__)


def _new_event_id() -> str:
    return f"osevt_{uuid.uuid4().hex[:16]}"


def publish_os_event(
    *,
    database_url: str,
    event_type: str,
    source_repo: str = "gmail-agent",
    engagement_id: str = "",
    payload: dict[str, Any] | None = None,
    correlation: dict[str, Any] | None = None,
    occurred_at: str | None = None,
    # P7 Observability fields
    trace_id: str | None = None,
    span_id: str | None = None,
    parent_event_id: str | None = None,
    case_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    severity: str = "info",
    duration_ms: int | None = None,
    token_usage: dict[str, Any] | None = None,
    cost: float | None = None,
    success: bool | None = None,
    error_message: str | None = None,
    # #35: Współdzielone połączenie dla transakcyjności
    _connection_override: Any | None = None,
) -> str | None:
    """
    Insert one row into unified_os_events. Returns event_id or None on failure (best-effort).

    Args:
        _connection_override: Współdzielone połączenie psycopg (do użytku w transakcjach).
            Gdy podane, nie tworzy własnego połączenia i nie robi commit.
            Caller odpowiada za commit/rollback.
    """
    if not str(database_url or "").strip():
        return None
    event_id = _new_event_id()
    # Faza 0c: fallback trace_id z ContextVar jesli nie podany przez callera
    if not trace_id:
        from log_config import get_trace_id
        trace_id = get_trace_id() or None
    ts = occurred_at or datetime.now(timezone.utc).isoformat()
    sev = str(severity or "").strip().lower()
    if sev not in {"debug", "info", "warn", "error", "critical"}:
        sev = "info"
    try:
        import psycopg
    except ImportError:
        log.warning("event_spine: psycopg not available")
        return None

    try:
        if _connection_override is not None:
            # Użyj współdzielonego połączenia (bez commit — caller robi commit)
            conn = _connection_override
            cur = conn.cursor()
            _execute_os_event_insert(cur, event_id, event_type, engagement_id, source_repo,
                                     ts, payload, correlation, trace_id, span_id,
                                     parent_event_id, case_id, user_id, session_id,
                                     sev, duration_ms, token_usage, cost, success, error_message)
        else:
            with psycopg.connect(database_url) as conn:
                with conn.cursor() as cur:
                    _execute_os_event_insert(cur, event_id, event_type, engagement_id, source_repo,
                                             ts, payload, correlation, trace_id, span_id,
                                             parent_event_id, case_id, user_id, session_id,
                                             sev, duration_ms, token_usage, cost, success, error_message)
                conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("event_spine publish failed type=%s err=%s", event_type, exc)
        return None
    return event_id


def _execute_os_event_insert(
    cur: Any,
    event_id: str,
    event_type: str,
    engagement_id: str,
    source_repo: str,
    ts: str,
    payload: dict[str, Any] | None,
    correlation: dict[str, Any] | None,
    trace_id: str | None,
    span_id: str | None,
    parent_event_id: str | None,
    case_id: str | None,
    user_id: str | None,
    session_id: str | None,
    sev: str,
    duration_ms: int | None,
    token_usage: dict[str, Any] | None,
    cost: float | None,
    success: bool | None,
    error_message: str | None,
) -> None:
    """Wykonuje INSERT do unified_os_events na podanym cursorze."""
    cur.execute(
        """
        INSERT INTO unified_os_events (
            event_id, event_type, engagement_id, source_repo,
            occurred_at, payload, correlation,
            trace_id, span_id, parent_event_id,
            case_id, user_id, session_id,
            severity, duration_ms, token_usage, cost,
            success, error_message
        ) VALUES (
            %s, %s, NULLIF(%s, ''), %s, %s::timestamptz, %s::jsonb, %s::jsonb,
            NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''),
            NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''),
            %s, %s, %s::jsonb, %s,
            %s, %s
        )
        """,
        (
            event_id,
            str(event_type or "").strip() or "unknown",
            str(engagement_id or "").strip(),
            str(source_repo or "gmail-agent").strip() or "gmail-agent",
            ts,
            json.dumps(dict(payload or {})),
            json.dumps(dict(correlation or {})),
            str(trace_id or "").strip(),
            str(span_id or "").strip(),
            str(parent_event_id or "").strip(),
            str(case_id or "").strip(),
            str(user_id or "").strip(),
            str(session_id or "").strip(),
            sev,
            int(duration_ms) if duration_ms is not None else None,
            json.dumps(dict(token_usage or {})) if token_usage else None,
            float(cost) if cost is not None else None,
            bool(success) if success is not None else None,
            str(error_message or "").strip() or None,
        ),
    )
