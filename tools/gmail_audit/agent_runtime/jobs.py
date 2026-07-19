"""Agent runtime job audit trail (PR-D) — records completed reconcile runs."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from agent_runtime.database_url import resolve_mailbox_memory_database_url
from agent_runtime.validate import AgentRuntimeConfigError
from log_config import get_logger

logger = get_logger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_job_id(engagement_id: str, signal_id: str) -> str:
    seed = f"{engagement_id}:{signal_id}:{_utc_now_iso()}"
    return "job_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


class InMemoryAgentJobStore:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    def record_completed(
        self,
        *,
        engagement_id: str,
        signal_id: str,
        case_id: str,
    ) -> dict[str, Any]:
        job_id = new_job_id(engagement_id, signal_id)
        row = {
            "job_id": job_id,
            "engagement_id": engagement_id,
            "signal_id": signal_id,
            "case_id": case_id,
            "status": "completed",
            "error_text": "",
            "finished_at": _utc_now_iso(),
        }
        self._rows[job_id] = row
        return row


class PostgresAgentJobStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = str(database_url or "").strip()
        if not self.database_url:
            raise ValueError("database_url required for PostgresAgentJobStore")

    def record_completed(
        self,
        *,
        engagement_id: str,
        signal_id: str,
        case_id: str,
    ) -> dict[str, Any]:
        job_id = new_job_id(engagement_id, signal_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_runtime_jobs (
                        job_id, engagement_id, signal_id, case_id,
                        status, started_at, finished_at, created_at
                    ) VALUES (
                        %(job_id)s, %(engagement_id)s, %(signal_id)s, %(case_id)s,
                        'completed', NOW(), NOW(), NOW()
                    )
                    """,
                    {
                        "job_id": job_id,
                        "engagement_id": engagement_id,
                        "signal_id": signal_id,
                        "case_id": case_id,
                    },
                )
            conn.commit()
        return {
            "job_id": job_id,
            "engagement_id": engagement_id,
            "signal_id": signal_id,
            "case_id": case_id,
            "status": "completed",
        }

    def _connect(self):
        import psycopg  # type: ignore[import-not-found]

        return psycopg.connect(self.database_url, connect_timeout=10)


def build_agent_job_store(
    settings: Any,
    *,
    allow_in_memory: bool = False,
) -> InMemoryAgentJobStore | PostgresAgentJobStore:
    url, url_source = resolve_mailbox_memory_database_url(settings)
    if url:
        return PostgresAgentJobStore(url)
    reason = "settings_not_loaded_or_no_db_url"
    if not allow_in_memory:
        raise AgentRuntimeConfigError(
            "Postgres agent job store required; "
            "set MAILBOX_MEMORY_DATABASE_URL (call load_settings() so tools/gmail_audit/.env is loaded) "
            "or pass allow_in_memory=True for isolated dev/test only."
        )
    logger.warning(
        "AGENT_JOB_STORE_FALLBACK_TO_MEMORY",
        extra={"x": {"reason": reason, "url_source": url_source}},
    )
    return InMemoryAgentJobStore()
