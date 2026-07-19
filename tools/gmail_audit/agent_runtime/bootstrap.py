
"""Bootstrap agent runtime DDL + signal spine migrations (PR-A)."""

from __future__ import annotations

import sys as _sys
from log_config import get_logger
from pathlib import Path

try:
    from .._protocols import DatabaseConnection
except ImportError:
    # Fallback when imported from test context (relative import fails)
    from _protocols import DatabaseConnection  # type: ignore[no-redef]

log = get_logger(__name__)

AGENT_RUNTIME_SCHEMA_PATH = Path(__file__).resolve().parent / "AGENT_RUNTIME_SCHEMA.sql"
AGENT_RUNTIME_MIGRATIONS_PATH = Path(__file__).resolve().parent / "AGENT_RUNTIME_MIGRATIONS.sql"
AGENT_RUNTIME_JOBS_PATH = Path(__file__).resolve().parent / "AGENT_RUNTIME_JOBS.sql"


def agent_runtime_bootstrap_sql() -> str:
    jobs_sql = AGENT_RUNTIME_JOBS_PATH.read_text(encoding="utf-8") if AGENT_RUNTIME_JOBS_PATH.is_file() else ""
    learning_sql = ""
    try:
        from learning_loops_bootstrap import learning_loops_bootstrap_sql

        learning_sql = learning_loops_bootstrap_sql()
    except ImportError:
        log.warning("bootstrap: learning_loops_bootstrap module not found — learning loops migration skipped")
    return (
        AGENT_RUNTIME_SCHEMA_PATH.read_text(encoding="utf-8")
        + "\n"
        + AGENT_RUNTIME_MIGRATIONS_PATH.read_text(encoding="utf-8")
        + "\n"
        + jobs_sql
        + "\n"
        + learning_sql
    )


def bootstrap_agent_runtime(conn: DatabaseConnection) -> None:
    """Execute agent runtime schema + migrations on an open psycopg connection."""
    with conn.cursor() as cur:
        cur.execute(agent_runtime_bootstrap_sql())
    conn.commit()
