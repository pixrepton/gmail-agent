"""Async agent-chat job queue (AI-OS 6.2)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from _protocols import DatabaseConnection

AGENT_CHAT_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS agent_chat_jobs (
    job_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    case_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    receipt_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_agent_chat_jobs_status ON agent_chat_jobs(status, created_at);
"""


def ensure_agent_chat_jobs_table(conn: DatabaseConnection) -> None:
    with conn.cursor() as cur:
        cur.execute(AGENT_CHAT_JOBS_DDL)
    conn.commit()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_agent_chat_job(
    conn: DatabaseConnection,
    *,
    command_id: str,
    session_id: str,
    case_id: str,
    request: dict[str, Any],
) -> str:
    job_id = f"chatjob_{uuid.uuid4().hex[:16]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_chat_jobs (
              job_id, command_id, session_id, case_id, status, request_json
            ) VALUES (%s, %s, %s, %s, 'queued', %s::jsonb)
            """,
            (
                job_id,
                str(command_id),
                str(session_id),
                str(case_id),
                json.dumps(request, ensure_ascii=False),
            ),
        )
    conn.commit()
    return job_id


def fetch_agent_chat_job(conn: DatabaseConnection, job_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id, command_id, session_id, case_id, status,
                   request_json, receipt_json, result_json, error_message,
                   created_at, updated_at, started_at, completed_at
            FROM agent_chat_jobs WHERE job_id = %s
            """,
            (str(job_id),),
        )
        row = cur.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return row
    return {
        "job_id": row[0],
        "command_id": row[1],
        "session_id": row[2],
        "case_id": row[3],
        "status": row[4],
        "request_json": row[5],
        "receipt_json": row[6],
        "result_json": row[7],
        "error_message": row[8],
        "created_at": row[9],
        "updated_at": row[10],
        "started_at": row[11],
        "completed_at": row[12],
    }


def claim_next_agent_chat_job(conn: DatabaseConnection) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id FROM agent_chat_jobs
            WHERE status = 'queued'
            ORDER BY created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
        row = cur.fetchone()
        if not row:
            return None
        job_id = row[0] if not isinstance(row, dict) else row.get("job_id")
        now = _utc_now()
        cur.execute(
            """
            UPDATE agent_chat_jobs
            SET status = 'running', started_at = %s, updated_at = %s
            WHERE job_id = %s AND status = 'queued'
            RETURNING job_id, command_id, session_id, case_id, request_json
            """,
            (now, now, job_id),
        )
        claimed = cur.fetchone()
    conn.commit()
    if not claimed:
        return None
    if isinstance(claimed, dict):
        return claimed
    return {
        "job_id": claimed[0],
        "command_id": claimed[1],
        "session_id": claimed[2],
        "case_id": claimed[3],
        "request_json": claimed[4],
    }


def complete_agent_chat_job(
    conn: DatabaseConnection,
    *,
    job_id: str,
    status: str,
    receipt: dict[str, Any],
    result: dict[str, Any],
    error_message: str = "",
) -> None:
    now = _utc_now()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_chat_jobs
            SET status = %s,
                receipt_json = %s::jsonb,
                result_json = %s::jsonb,
                error_message = %s,
                completed_at = %s,
                updated_at = %s
            WHERE job_id = %s
            """,
            (
                str(status),
                json.dumps(receipt, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                str(error_message or ""),
                now,
                now,
                str(job_id),
            ),
        )
    conn.commit()


__all__ = [
    "AGENT_CHAT_JOBS_DDL",
    "ensure_agent_chat_jobs_table",
    "enqueue_agent_chat_job",
    "fetch_agent_chat_job",
    "claim_next_agent_chat_job",
    "complete_agent_chat_job",
]
