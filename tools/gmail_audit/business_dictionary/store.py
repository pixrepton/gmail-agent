"""PostgreSQL store for Business Dictionary terms."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

try:
    from .._protocols import DatabaseConnection
except ImportError:
    from _protocols import DatabaseConnection  # type: ignore[no-redef]

from business_dictionary.model import BusinessTerm, BusinessDictionaryStats

BUSINESS_DICTIONARY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS business_dictionary_terms (
    term_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    definition TEXT NOT NULL DEFAULT '',
    source_document TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'manual',
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    related_terms JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence REAL NOT NULL DEFAULT 0.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_business_dict_category ON business_dictionary_terms(category);
CREATE INDEX IF NOT EXISTS idx_business_dict_name ON business_dictionary_terms(name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_business_dict_name_unique ON business_dictionary_terms(LOWER(name));
"""

SYNC_OUTBOX_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sync_outbox (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sync_outbox_unprocessed ON sync_outbox(processed_at) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sync_outbox_created ON sync_outbox(created_at);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return f"bizterm_{uuid.uuid4().hex[:16]}"


def ensure_dictionary_table(conn: DatabaseConnection) -> None:
    """Create the business_dictionary_terms table if not exists."""
    with conn.cursor() as cur:
        cur.execute(BUSINESS_DICTIONARY_TABLE_SQL)
    conn.commit()


def ensure_sync_outbox_table(conn: DatabaseConnection) -> None:
    """Create the sync_outbox table if not exists."""
    with conn.cursor() as cur:
        cur.execute(SYNC_OUTBOX_TABLE_SQL)
    conn.commit()


def write_outbox_entry(conn: DatabaseConnection, *, entity_type: str, entity_id: str, operation: str, payload: dict[str, Any]) -> str:
    """Write an entry to the sync outbox within the current transaction."""
    import uuid
    oid = f"outbox_{uuid.uuid4().hex[:16]}"
    now = _utc_now()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_outbox (id, entity_type, entity_id, operation, payload, created_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            """,
            (oid, entity_type, entity_id, operation, json.dumps(payload, ensure_ascii=False), now),
        )
    return oid


def upsert_term(conn: DatabaseConnection, term: BusinessTerm) -> str:
    """Insert or update a business term. Writes to sync_outbox in same transaction."""
    tid = term.term_id or _new_id()
    now = _utc_now()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO business_dictionary_terms
                (term_id, name, category, definition, source_document, source_kind,
                 aliases, related_terms, confidence, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
            ON CONFLICT (LOWER(name)) DO UPDATE SET
                category = EXCLUDED.category,
                definition = EXCLUDED.definition,
                source_document = EXCLUDED.source_document,
                aliases = EXCLUDED.aliases,
                related_terms = EXCLUDED.related_terms,
                confidence = GREATEST(business_dictionary_terms.confidence, EXCLUDED.confidence),
                updated_at = EXCLUDED.updated_at
            """,
            (
                tid,
                term.name.strip(),
                term.category.strip(),
                term.definition.strip(),
                term.source_document.strip(),
                term.source_kind.strip(),
                json.dumps(term.aliases, ensure_ascii=False),
                json.dumps(term.related_terms, ensure_ascii=False),
                float(max(0.0, min(1.0, term.confidence))),
                now,
                now,
            ),
        )
        # Write outbox entry in same transaction
        outbox_oid = f"outbox_{uuid.uuid4().hex[:16]}"
        outbox_payload = {
            "term_id": tid,
            "name": term.name,
            "category": term.category,
            "definition": term.definition,
            "source_document": term.source_document,
            "source_kind": term.source_kind,
            "aliases": term.aliases,
            "related_terms": term.related_terms,
            "confidence": term.confidence,
        }
        cur.execute(
            "INSERT INTO sync_outbox (id, entity_type, entity_id, operation, payload, created_at) VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
            (outbox_oid, "business_term", tid, "upsert", json.dumps(outbox_payload, ensure_ascii=False), now),
        )
    conn.commit()
    return tid


def search_terms(conn: DatabaseConnection, *, query: str = "", category: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """Search business terms by name, category or aliases."""
    with conn.cursor() as cur:
        if query:
            pattern = f"%{query.strip()}%"
            cur.execute(
                """
                SELECT term_id, name, category, definition, source_document, source_kind,
                       aliases, related_terms, confidence, created_at
                FROM business_dictionary_terms
                WHERE name ILIKE %s
                   OR definition ILIKE %s
                   OR aliases::text ILIKE %s
                ORDER BY confidence DESC, name ASC
                LIMIT %s
                """,
                (pattern, pattern, pattern, max(1, int(limit))),
            )
        elif category:
            cur.execute(
                """
                SELECT term_id, name, category, definition, source_document, source_kind,
                       aliases, related_terms, confidence, created_at
                FROM business_dictionary_terms
                WHERE category = %s
                ORDER BY name ASC
                LIMIT %s
                """,
                (category.strip(), max(1, int(limit))),
            )
        else:
            cur.execute(
                """
                SELECT term_id, name, category, definition, source_document, source_kind,
                       aliases, related_terms, confidence, created_at
                FROM business_dictionary_terms
                ORDER BY confidence DESC, name ASC
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
        rows = cur.fetchall() or []
    return [
        {
            "term_id": r[0] if not isinstance(r, dict) else r.get("term_id"),
            "name": r[1] if not isinstance(r, dict) else r.get("name"),
            "category": r[2] if not isinstance(r, dict) else r.get("category"),
            "definition": r[3] if not isinstance(r, dict) else r.get("definition"),
            "source_document": r[4] if not isinstance(r, dict) else r.get("source_document"),
            "source_kind": r[5] if not isinstance(r, dict) else r.get("source_kind"),
            "aliases": r[6] if not isinstance(r, dict) else r.get("aliases"),
            "related_terms": r[7] if not isinstance(r, dict) else r.get("related_terms"),
            "confidence": r[8] if not isinstance(r, dict) else r.get("confidence"),
            "created_at": str(r[9]) if not isinstance(r, dict) else r.get("created_at"),
        }
        for r in rows
    ]


def get_stats(conn: DatabaseConnection) -> BusinessDictionaryStats:
    """Get aggregated statistics about the business dictionary."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM business_dictionary_terms")
        total = (cur.fetchone() or [0])[0] or 0

        cur.execute(
            "SELECT category, COUNT(*) as cnt FROM business_dictionary_terms GROUP BY category ORDER BY cnt DESC"
        )
        by_category = dict(cur.fetchall() or [])

        cur.execute(
            "SELECT source_kind, COUNT(*) as cnt FROM business_dictionary_terms GROUP BY source_kind ORDER BY cnt DESC"
        )
        by_source = dict(cur.fetchall() or [])

        cur.execute("SELECT MAX(updated_at) FROM business_dictionary_terms")
        last = cur.fetchone()
        last_str = str(last[0]) if last and last[0] else ""

    return BusinessDictionaryStats(
        total_terms=int(total),
        by_category=by_category,
        by_source=by_source,
        last_extracted_at=last_str,
    )


def delete_term(conn: DatabaseConnection, term_id: str) -> bool:
    """Delete a business term by ID."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM business_dictionary_terms WHERE term_id = %s", (term_id,))
        deleted = cur.rowcount
    conn.commit()
    return deleted > 0
