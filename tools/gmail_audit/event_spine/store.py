"""Persistence for unified_os_events processor."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import psycopg

from correlation_registry.schema import CORRELATION_REGISTRY_SCHEMA_SQL
from event_spine.models import OsEvent, ProcessingStatus, TerminalStatus
from log_config import get_logger

log = get_logger(__name__)

POSTGRES_CONNECT_TIMEOUT_SEC = int(os.getenv("EVENT_SPINE_CONNECT_TIMEOUT", "15"))

_SELECT_EVENT_COLUMNS = """
    event_id, event_type, engagement_id, source_repo, occurred_at,
    payload, correlation, processing_status, attempt_count,
    processor_id, last_error, failure_detail,
    trace_id, span_id, parent_event_id,
    case_id, user_id, session_id,
    severity, duration_ms, token_usage, cost,
    success, error_message
"""


def _parse_json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _row_to_event(row: tuple[Any, ...]) -> OsEvent:
    occurred = row[4]
    if not isinstance(occurred, datetime):
        occurred = datetime.now(timezone.utc)
    elif occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    return OsEvent(
        event_id=str(row[0]),
        event_type=str(row[1] or ""),
        engagement_id=str(row[2] or "") or None,
        source_repo=str(row[3] or "gmail-agent"),
        occurred_at=occurred,
        payload=_parse_json_mapping(row[5]),
        correlation=_parse_json_mapping(row[6]),
        processing_status=str(row[7] or "pending"),  # type: ignore[arg-type]
        attempt_count=int(row[8] or 0),
        processor_id=str(row[9]) if row[9] else None,
        last_error=str(row[10]) if row[10] else None,
        failure_detail=_parse_json_mapping(row[11]),
        # P7 Observability
        trace_id=str(row[12] or "").strip() or None,
        span_id=str(row[13] or "").strip() or None,
        parent_event_id=str(row[14] or "").strip() or None,
        case_id=str(row[15] or "").strip() or None,
        user_id=str(row[16] or "").strip() or None,
        session_id=str(row[17] or "").strip() or None,
        severity=str(row[18] or "info"),
        duration_ms=int(row[19]) if row[19] else None,
        token_usage=_parse_json_mapping(row[20]) if row[20] else None,
        cost=float(row[21]) if row[21] else None,
        success=bool(row[22]) if row[22] is not None else None,
        error_message=str(row[23]) if row[23] else None,
    )


class EventSpineStore(Protocol):
    def ensure_schema(self) -> None: ...

    def claim_batch(self, *, limit: int, processor_id: str) -> list[OsEvent]: ...

    def mark_terminal(
        self,
        event_id: str,
        *,
        status: TerminalStatus,
        processor_id: str,
        message: str = "",
        detail: dict[str, Any] | None = None,
        expected_status: ProcessingStatus = "processing",
    ) -> bool: ...

    def get_by_id(self, event_id: str) -> OsEvent | None: ...


@dataclass
class InMemoryEventSpineStore:
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    handler_effects: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def ensure_schema(self) -> None:
        return None

    def claim_batch(self, *, limit: int, processor_id: str) -> list[OsEvent]:
        out: list[OsEvent] = []
        for event_id, row in sorted(self.events.items(), key=lambda item: item[1].get("occurred_at", "")):
            if len(out) >= limit:
                break
            if str(row.get("processing_status") or "") not in {"pending", "failed"}:
                continue
            row["processing_status"] = "processing"
            row["processor_id"] = processor_id
            row["attempt_count"] = int(row.get("attempt_count") or 0) + 1
            out.append(_row_to_event(self._tuple_from_row(event_id, row)))
        return out

    def mark_terminal(
        self,
        event_id: str,
        *,
        status: TerminalStatus,
        processor_id: str,
        message: str = "",
        detail: dict[str, Any] | None = None,
        expected_status: ProcessingStatus = "processing",
    ) -> bool:
        row = self.events.get(event_id)
        if not row:
            return False
        current = str(row.get("processing_status") or "")
        if current == status:
            return True
        if current != expected_status and current not in {expected_status, "processing"}:
            if current in {"processed", "skipped"}:
                return True
            return False
        row["processing_status"] = status
        row["processor_id"] = processor_id
        row["processed_at"] = datetime.now(timezone.utc).isoformat()
        if message:
            row["last_error"] = message
        if detail:
            row["failure_detail"] = dict(detail)
        return True

    def get_by_id(self, event_id: str) -> OsEvent | None:
        row = self.events.get(event_id)
        if not row:
            return None
        return _row_to_event(self._tuple_from_row(event_id, row))

    def record_handler_effect(
        self,
        *,
        event: OsEvent,
        handler_key: str,
        processor_id: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = (str(event.event_id), str(handler_key))
        if key in self.handler_effects:
            return {"recorded": False, "idempotent": True, "handler_key": handler_key}
        row = {
            "event_id": event.event_id,
            "handler_key": handler_key,
            "event_type": event.event_type,
            "engagement_id": event.engagement_id,
            "processor_id": processor_id,
            "detail": dict(detail or {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.handler_effects[key] = row
        return {"recorded": True, "idempotent": False, "handler_key": handler_key}

    def insert_pending(self, event: OsEvent) -> None:
        self.events[event.event_id] = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "engagement_id": event.engagement_id,
            "source_repo": event.source_repo,
            "occurred_at": event.occurred_at.isoformat(),
            "payload": dict(event.payload),
            "correlation": dict(event.correlation),
            "processing_status": "pending",
            "attempt_count": 0,
            "processor_id": None,
            "last_error": None,
            "failure_detail": {},
            "trace_id": event.trace_id,
            "span_id": event.span_id,
            "parent_event_id": event.parent_event_id,
            "case_id": event.case_id,
            "user_id": event.user_id,
            "session_id": event.session_id,
            "severity": event.severity,
            "duration_ms": event.duration_ms,
            "token_usage": event.token_usage,
            "cost": event.cost,
            "success": event.success,
            "error_message": event.error_message,
        }

    @staticmethod
    def _tuple_from_row(event_id: str, row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            event_id,
            row.get("event_type"),
            row.get("engagement_id"),
            row.get("source_repo"),
            datetime.fromisoformat(str(row.get("occurred_at")).replace("Z", "+00:00")),
            row.get("payload"),
            row.get("correlation"),
            row.get("processing_status"),
            row.get("attempt_count"),
            row.get("processor_id"),
            row.get("last_error"),
            row.get("failure_detail"),
            row.get("trace_id"),
            row.get("span_id"),
            row.get("parent_event_id"),
            row.get("case_id"),
            row.get("user_id"),
            row.get("session_id"),
            row.get("severity"),
            row.get("duration_ms"),
            row.get("token_usage"),
            row.get("cost"),
            row.get("success"),
            row.get("error_message"),
        )


@dataclass(slots=True)
class PostgresEventSpineStore:
    database_url: str

    def ensure_schema(self) -> None:
        with psycopg.connect(self.database_url, connect_timeout=POSTGRES_CONNECT_TIMEOUT_SEC) as conn:
            with conn.cursor() as cur:
                cur.execute(CORRELATION_REGISTRY_SCHEMA_SQL)
            conn.commit()

    def claim_batch(self, *, limit: int, processor_id: str) -> list[OsEvent]:
        claimed: list[OsEvent] = []
        with psycopg.connect(self.database_url, connect_timeout=POSTGRES_CONNECT_TIMEOUT_SEC) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH batch AS (
                        SELECT event_id FROM unified_os_events
                        WHERE processing_status IN ('pending', 'failed')
                        ORDER BY occurred_at ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE unified_os_events u
                    SET processing_status = 'processing',
                        processor_id = %s,
                        attempt_count = attempt_count + 1
                    FROM batch
                    WHERE u.event_id = batch.event_id
                    RETURNING {_SELECT_EVENT_COLUMNS}
                    """,
                    (max(1, int(limit)), processor_id),
                )
                for row in cur.fetchall():
                    claimed.append(_row_to_event(row))
            conn.commit()
        return claimed

    def mark_terminal(
        self,
        event_id: str,
        *,
        status: TerminalStatus,
        processor_id: str,
        message: str = "",
        detail: dict[str, Any] | None = None,
        expected_status: ProcessingStatus = "processing",
    ) -> bool:
        with psycopg.connect(self.database_url, connect_timeout=POSTGRES_CONNECT_TIMEOUT_SEC) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE unified_os_events
                    SET processing_status = %s,
                        processed_at = NOW(),
                        processor_id = %s,
                        last_error = NULLIF(%s, ''),
                        failure_detail = %s::jsonb
                    WHERE event_id = %s
                      AND processing_status = %s
                    """,
                    (
                        status,
                        processor_id,
                        message,
                        json.dumps(dict(detail or {})),
                        event_id,
                        expected_status,
                    ),
                )
                updated = cur.rowcount
                if updated == 0:
                    cur.execute(
                        "SELECT processing_status FROM unified_os_events WHERE event_id = %s",
                        (event_id,),
                    )
                    row = cur.fetchone()
                    if row and str(row[0]) in {"processed", "skipped", "failed"}:
                        conn.commit()
                        return True
            conn.commit()
        return updated > 0

    def get_by_id(self, event_id: str) -> OsEvent | None:
        with psycopg.connect(self.database_url, connect_timeout=POSTGRES_CONNECT_TIMEOUT_SEC) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SELECT_EVENT_COLUMNS} FROM unified_os_events WHERE event_id = %s",
                    (event_id,),
                )
                row = cur.fetchone()
        return _row_to_event(row) if row else None

    def record_handler_effect(
        self,
        *,
        event: OsEvent,
        handler_key: str,
        processor_id: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with psycopg.connect(self.database_url, connect_timeout=POSTGRES_CONNECT_TIMEOUT_SEC) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO event_spine_handler_effects (
                        event_id, handler_key, event_type, engagement_id, processor_id, detail
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (event_id, handler_key) DO NOTHING
                    """,
                    (
                        event.event_id,
                        handler_key,
                        event.event_type,
                        event.engagement_id,
                        processor_id,
                        json.dumps(dict(detail or {})),
                    ),
                )
                inserted = cur.rowcount > 0
            conn.commit()
        if inserted:
            return {"recorded": True, "idempotent": False, "handler_key": handler_key}
        return {"recorded": False, "idempotent": True, "handler_key": handler_key}


def build_event_spine_store(database_url: str = "", *, in_memory: bool = False) -> EventSpineStore | None:
    if in_memory:
        return InMemoryEventSpineStore()
    url = str(database_url or "").strip()
    if not url:
        return None
    return PostgresEventSpineStore(database_url=url)
