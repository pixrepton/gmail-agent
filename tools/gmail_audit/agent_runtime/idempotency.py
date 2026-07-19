
"""Idempotent Writes (PR-5B) — idempotency_log table helpers.

Każdy write executor może przyjąć opcjonalny idempotency_key.
Jeśli operacja z tym kluczem była już wykonana, zwracamy poprzedni wynik.
"""

from __future__ import annotations

from log_config import get_logger
import json
from datetime import datetime, timezone
from typing import Any

logger = get_logger(__name__)

IDEMPOTENCY_LOG_TABLE = "idempotency_log"


def _ensure_idempotency_table(db_url: str) -> bool:
    """Create idempotency_log table if it doesn't exist. Best-effort."""
    if not db_url:
        return False
    try:
        import psycopg

        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {IDEMPOTENCY_LOG_TABLE} (
                        key TEXT PRIMARY KEY,
                        operation TEXT NOT NULL,
                        result JSONB NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
            conn.commit()
        return True
    except Exception as exc:
        logger.warning("idempotency_table_create_failed: %s", exc)
        return False


def check_idempotency(db_url: str | None, key: str) -> dict[str, Any] | None:
    """Sprawdza czy operacja z danym kluczem była już wykonana.

    Returns:
        Poprzedni wynik operacji (dict) lub None.
    """
    if not db_url or not key:
        return None
    try:
        import psycopg

        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT operation, result FROM {IDEMPOTENCY_LOG_TABLE} WHERE key = %s",
                    (key,),
                )
                row = cur.fetchone()
                if row is not None:
                    return {
                        "operation": str(row[0] or ""),
                        "result": row[1] if isinstance(row[1], dict) else json.loads(row[1] or "{}"),
                        "from_idempotency_log": True,
                    }
        return None
    except Exception as exc:
        logger.warning("idempotency_check_failed key=%s: %s", key, exc)
        return None


def record_idempotency(
    db_url: str | None,
    key: str,
    operation: str,
    result: dict[str, Any],
) -> bool:
    """Zapisuje wynik operacji w idempotency_log."""
    if not db_url or not key:
        return False
    try:
        import psycopg
        from psycopg.errors import UniqueViolation

        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {IDEMPOTENCY_LOG_TABLE} (key, operation, result, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (key, operation, json.dumps(result), datetime.now(timezone.utc).isoformat()),
                )
            conn.commit()
        return True
    except UniqueViolation:
        # Konkurencyjny zapis — inny proces już zapisał, OK
        return True
    except Exception as exc:
        logger.warning("idempotency_record_failed key=%s: %s", key, exc)
        return False


def require_idempotency_key(args: dict[str, Any]) -> str | None:
    """Wyciąga idempotency_key z args executors (opcjonalny)."""
    return str(args.get("idempotency_key") or "").strip() or None


__all__ = [
    "check_idempotency",
    "record_idempotency",
    "require_idempotency_key",
    "_ensure_idempotency_table",
]
