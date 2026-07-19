"""Connectivity checks for canonical mailbox-memory Postgres."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from intake_policy import CHECK_STATUS_FAILED, CHECK_STATUS_OK, CHECK_STATUS_SKIPPED
from redaction import sanitize_text

# Canonical vector retrieval path labels (doctor, CaseContextPack, chunk retrieval_signals).
VECTOR_PATH_DISABLED = "vector_path_disabled"
VECTOR_PATH_UNAVAILABLE = "vector_path_unavailable"
VECTOR_PATH_FAILED = "vector_path_failed"
VECTOR_PATH_USED = "vector_path_used"


def check_mailbox_memory_database(database_url: str) -> dict[str, Any]:
    """Return a doctor-style check dict for ``MAILBOX_MEMORY_DATABASE_URL``."""
    url = str(database_url or "").strip()
    connection_target = _build_connection_target(url)
    if not url:
        return {
            "status": CHECK_STATUS_SKIPPED,
            "reason": "MAILBOX_MEMORY_DATABASE_URL is not set.",
            "failure_kind": "missing_url",
            "connection_target": connection_target,
        }
    try:
        import psycopg  # type: ignore[import-not-found]
        from psycopg import sql  # type: ignore[import-not-found]
    except ImportError as exc:
        return {
            "status": CHECK_STATUS_FAILED,
            "failure_kind": "missing_driver",
            "connection_target": connection_target,
            "error": sanitize_text(
                "Python runtime is missing psycopg. Install tools/gmail_audit/requirements.txt in the canonical "
                f"interpreter before using Mailbox Memory / Postgres. ({exc})"
            ),
        }

    try:
        with psycopg.connect(url, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name LIKE 'mailbox_memory_%'
                    ORDER BY table_name
                    """
                )
                tables = [str(row[0]) for row in cur.fetchall() if row and row[0]]
                row_counts: dict[str, int] = {}
                for table in tables:
                    cur.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))
                    )
                    row = cur.fetchone()
                    row_counts[table] = int(row[0]) if row else 0
        return {
            "status": CHECK_STATUS_OK,
            "failure_kind": "",
            "connection_target": connection_target,
            "table_count": len(tables),
            "tables": tables,
            "row_counts": row_counts,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": CHECK_STATUS_FAILED,
            "failure_kind": _classify_database_failure(exc),
            "connection_target": connection_target,
            "error": sanitize_text(str(exc)),
        }


def check_pgvector_extension(database_url: str, *, vector_enabled: bool) -> dict[str, Any]:
    """Return a doctor-style check dict for pgvector readiness."""
    url = str(database_url or "").strip()
    connection_target = _build_connection_target(url)
    if not vector_enabled:
        return {
            "status": CHECK_STATUS_SKIPPED,
            "reason": "MAILBOX_MEMORY_VECTOR_ENABLED=0.",
            "connection_target": connection_target,
        }
    if not url:
        return {
            "status": CHECK_STATUS_FAILED,
            "reason": "MAILBOX_MEMORY_DATABASE_URL is required when MAILBOX_MEMORY_VECTOR_ENABLED=1.",
            "connection_target": connection_target,
        }
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:
        return {
            "status": CHECK_STATUS_FAILED,
            "reason": sanitize_text(f"Python runtime is missing psycopg. ({exc})"),
            "connection_target": connection_target,
        }
    try:
        with psycopg.connect(url, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT extname
                    FROM pg_extension
                    WHERE extname = 'vector'
                    """
                )
                row = cur.fetchone()
                extension_present = bool(row and row[0] == "vector")
        if not extension_present:
            return {
                "status": CHECK_STATUS_FAILED,
                "reason": "pgvector extension `vector` is not enabled in the target database.",
                "connection_target": connection_target,
                "extension": "vector",
            }
        return {
            "status": CHECK_STATUS_OK,
            "connection_target": connection_target,
            "extension": "vector",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": CHECK_STATUS_FAILED,
            "reason": sanitize_text(str(exc)),
            "failure_kind": _classify_database_failure(exc),
            "connection_target": connection_target,
        }


def _build_connection_target(database_url: str) -> dict[str, Any]:
    parsed = urlparse(str(database_url or "").strip())
    database = parsed.path.lstrip("/") if parsed.path else ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    return {
        "scheme": str(parsed.scheme or ""),
        "host": str(parsed.hostname or ""),
        "port": port,
        "database": database,
    }


def _classify_database_failure(exc: Exception) -> str:
    message = str(exc or "").lower()
    if any(token in message for token in ("connection refused", "timeout expired", "timed out", "connection reset")):
        return "connection_refused"
    if any(token in message for token in ("password authentication failed", "authentication failed", "role does not exist", "invalid password")):
        return "auth_failed"
    if any(token in message for token in ("could not translate host name", "name or service not known", "temporary failure in name resolution", "nodename nor servname")):
        return "dns_error"
    return "unknown"


def build_vector_retrieval_readiness_check(settings: Any) -> dict[str, Any]:
    """Doctor / preflight: bounded probe of embedding + pgvector prerequisites.

    ``vector_path_used`` here means the embedding provider returned a non-empty vector for a
    fixed probe string while pgvector is present — not per-query semantic ranking.
    """
    from embedding_runtime import build_embedding_runtime

    vector_enabled = bool(getattr(settings, "mailbox_memory_vector_enabled", False))
    db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "").strip()
    probe_text = "mailbox_memory.doctor.vector_probe"
    check: dict[str, Any] = {
        "status": CHECK_STATUS_SKIPPED,
        "vector_path_status": VECTOR_PATH_DISABLED,
        "probe_text": probe_text,
        "probe_embedding_dimensions": 0,
        "embedding_error": "",
        "pgvector": {},
        "config_sources": {
            "MAILBOX_MEMORY_VECTOR_ENABLED": getattr(settings, "config_sources", {}).get("MAILBOX_MEMORY_VECTOR_ENABLED", ""),
            "MAILBOX_MEMORY_DATABASE_URL": getattr(settings, "config_sources", {}).get("MAILBOX_MEMORY_DATABASE_URL", ""),
            "OPENAI_COMPAT_EMBEDDING_MODEL": getattr(settings, "config_sources", {}).get("OPENAI_COMPAT_EMBEDDING_MODEL", ""),
            "OPENAI_COMPAT_EMBEDDING_BASE_URL": getattr(settings, "config_sources", {}).get(
                "OPENAI_COMPAT_EMBEDDING_BASE_URL", ""
            ),
            "OPENAI_COMPAT_EMBEDDING_API_KEY": getattr(settings, "config_sources", {}).get(
                "OPENAI_COMPAT_EMBEDDING_API_KEY", ""
            ),
            "OPENAI_COMPAT_BASE_URL": getattr(settings, "config_sources", {}).get("OPENAI_COMPAT_BASE_URL", ""),
        },
    }
    if not vector_enabled:
        check["reason"] = "MAILBOX_MEMORY_VECTOR_ENABLED=0."
        return check

    pg = check_pgvector_extension(db_url, vector_enabled=True)
    check["pgvector"] = pg
    if pg.get("status") == CHECK_STATUS_FAILED:
        check["status"] = CHECK_STATUS_FAILED
        check["vector_path_status"] = VECTOR_PATH_FAILED
        check["reason"] = str(pg.get("reason") or "pgvector readiness check failed.")
        return check

    emb_runtime = build_embedding_runtime(settings)
    if emb_runtime is None:
        check["status"] = CHECK_STATUS_OK
        check["vector_path_status"] = VECTOR_PATH_UNAVAILABLE
        check["reason"] = "Embedding runtime is not configured (OPENAI_COMPAT_* / embedding model)."
        return check

    try:
        qvecs = list(emb_runtime.embed_texts([probe_text]))
    except Exception as exc:  # noqa: BLE001
        check["status"] = CHECK_STATUS_FAILED
        check["vector_path_status"] = VECTOR_PATH_FAILED
        check["embedding_error"] = sanitize_text(str(exc))
        check["reason"] = "Embedding probe raised an exception."
        return check

    qvec = qvecs[0] if qvecs else None
    if not isinstance(qvec, list) or not qvec:
        check["status"] = CHECK_STATUS_OK
        check["vector_path_status"] = VECTOR_PATH_UNAVAILABLE
        check["reason"] = "Embedding provider returned no vector for probe."
        return check

    check["status"] = CHECK_STATUS_OK
    check["vector_path_status"] = VECTOR_PATH_USED
    check["probe_embedding_dimensions"] = len(qvec)
    check["reason"] = "Bounded embedding probe succeeded."
    return check
