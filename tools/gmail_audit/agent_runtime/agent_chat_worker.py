"""Background worker tick for async agent-chat jobs (AI-OS 6.2)."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def process_agent_chat_jobs_tick(settings: Any, *, max_jobs: int = 1) -> dict[str, Any]:
    """Claim and execute queued agent-chat jobs (best-effort)."""
    from agent_runtime.agent_chat_jobs import (
        claim_next_agent_chat_job,
        complete_agent_chat_job,
        ensure_agent_chat_jobs_table,
    )
    from agent_runtime.operator_command_spine import run_operator_command_spine

    db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "").strip()
    if not db_url:
        return {"ok": False, "processed": 0, "error": "database_not_configured"}

    import psycopg

    processed = 0
    errors: list[str] = []
    conn = psycopg.connect(db_url)
    try:
        ensure_agent_chat_jobs_table(conn)
        for _ in range(max(1, int(max_jobs))):
            job = claim_next_agent_chat_job(conn)
            if not job:
                break
            request = job.get("request_json") or {}
            if isinstance(request, str):
                try:
                    request = json.loads(request)
                except json.JSONDecodeError:
                    request = {}
            try:
                result = run_operator_command_spine(
                    user_input=str(request.get("user_input") or ""),
                    session_id=str(request.get("session_id") or job.get("session_id") or ""),
                    case_id=str(request.get("case_id") or job.get("case_id") or ""),
                    opmem_context=request.get("opmem_context") if isinstance(request.get("opmem_context"), dict) else {},
                    settings=settings,
                    command_id=str(job.get("command_id") or ""),
                )
                receipt = result.get("receipt") or {}
                complete_agent_chat_job(
                    conn,
                    job_id=str(job.get("job_id") or ""),
                    status=str(receipt.get("status") or "completed"),
                    receipt=receipt,
                    result={
                        "signal_id": result.get("signal_id", ""),
                        "engagement_id": result.get("engagement_id", ""),
                        "proposals": result.get("proposals", []),
                        "warnings": result.get("warnings", []),
                    },
                )
                processed += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                complete_agent_chat_job(
                    conn,
                    job_id=str(job.get("job_id") or ""),
                    status="failed",
                    receipt={"status": "failed", "error": str(exc)},
                    result={},
                    error_message=str(exc),
                )
    finally:
        conn.close()

    return {"ok": True, "processed": processed, "errors": errors}


__all__ = ["process_agent_chat_jobs_tick"]
