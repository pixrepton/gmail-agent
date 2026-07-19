"""World model bootstrap (Mechanism B) — offline corpus + insights."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any
from _protocols import DatabaseConnection

INSIGHT_PENDING = "pending_operator"
INSIGHT_APPROVED = "approved"
INSIGHT_REJECTED = "rejected"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def ingest_corpus_message(
    conn: DatabaseConnection,
    *,
    subject: str,
    body_text: str,
    sender_email: str = "",
    source_type: str = "export",
    source_ref: str = "",
    corpus_message_id: str = "",
) -> str:
    mid = str(corpus_message_id or _new_id("corp_msg")).strip() or _new_id("corp_msg")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO historical_corpus_messages (
                corpus_message_id, source_type, source_ref, subject, body_text,
                sender_email, ingested_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (corpus_message_id) DO NOTHING
            """,
            (mid, source_type, source_ref, subject, body_text, sender_email, _utc_now()),
        )
    return mid


def ingest_corpus_fact(
    conn: DatabaseConnection,
    *,
    corpus_message_id: str,
    fact_key: str,
    normalized_value: str,
    confidence: float = 0.8,
) -> str:
    fid = _new_id("corp_fact")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO historical_corpus_facts (
                corpus_fact_id, corpus_message_id, fact_key, normalized_value, confidence
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (fid, corpus_message_id, fact_key, normalized_value, confidence),
        )
    return fid


def distill_insights_from_corpus(
    conn: DatabaseConnection,
    *,
    categories: dict[str, list[str]] | None = None,
) -> list[str]:
    """Deterministic v1 distillation (no LLM) — category counts from fact keys."""
    cats = categories or {
        "customer_type": ["customer_type", "client_segment"],
        "seasonality": ["season", "heating_season"],
        "language": ["communication_tone", "formality"],
        "situation": ["case_family", "service_type", "installation_type"],
    }
    created: list[str] = []
    with conn.cursor() as cur:
        for category, keys in cats.items():
            cur.execute(
                """
                SELECT fact_key, normalized_value, count(*) AS cnt
                FROM historical_corpus_facts
                WHERE fact_key = ANY(%s::text[])
                GROUP BY fact_key, normalized_value
                ORDER BY cnt DESC
                LIMIT 3
                """,
                (list(keys),),
            )
            rows = cur.fetchall() or []
            if not rows:
                continue
            parts = []
            total = 0
            for row in rows:
                fk = row[0] if not isinstance(row, dict) else row.get("fact_key")
                val = row[1] if not isinstance(row, dict) else row.get("normalized_value")
                cnt = int((row[2] if not isinstance(row, dict) else row.get("cnt")) or 0)
                total += cnt
                parts.append(f"{fk}={val} ({cnt})")
            text = f"[{category}] " + "; ".join(parts)
            iid = _new_id("insight")
            cur.execute(
                """
                INSERT INTO world_model_insights (
                    insight_id, category, insight_text_pl, supporting_count,
                    source_refs, status, created_at
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    iid,
                    category,
                    text,
                    total,
                    json.dumps([{"type": "corpus_distill_v1"}], ensure_ascii=False),
                    INSIGHT_PENDING,
                    _utc_now(),
                ),
            )
            created.append(iid)
    return created


def fetch_insights(conn: DatabaseConnection, *, status: str = INSIGHT_PENDING, limit: int = 50) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT insight_id, category, insight_text_pl, supporting_count, status, created_at
            FROM world_model_insights
            WHERE status = %s
            ORDER BY supporting_count DESC, created_at DESC
            LIMIT %s
            """,
            (status, max(1, int(limit))),
        )
        rows = cur.fetchall() or []
    return [
        {
            "insight_id": r[0] if not isinstance(r, dict) else r.get("insight_id"),
            "category": r[1] if not isinstance(r, dict) else r.get("category"),
            "insight_text_pl": r[2] if not isinstance(r, dict) else r.get("insight_text_pl"),
            "supporting_count": r[3] if not isinstance(r, dict) else r.get("supporting_count"),
            "status": r[4] if not isinstance(r, dict) else r.get("status"),
        }
        for r in rows
    ]


def update_insight_status(
    conn: DatabaseConnection,
    *,
    insight_id: str,
    status: str,
    approved_by: str = "operator",
) -> bool:
    if status not in {INSIGHT_APPROVED, INSIGHT_REJECTED}:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE world_model_insights
            SET status = %s, approved_at = %s, approved_by = %s
            WHERE insight_id = %s
            """,
            (status, _utc_now(), str(approved_by), str(insight_id)),
        )
        return cur.rowcount > 0


def fetch_approved_insights_for_category(conn: DatabaseConnection, *, category: str, limit: int = 5) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT insight_id, category, insight_text_pl, supporting_count
            FROM world_model_insights
            WHERE status = %s AND category = %s
            ORDER BY supporting_count DESC
            LIMIT %s
            """,
            (INSIGHT_APPROVED, str(category), max(1, int(limit))),
        )
        rows = cur.fetchall() or []
    return [
        {
            "insight_id": r[0] if not isinstance(r, dict) else r.get("insight_id"),
            "category": r[1] if not isinstance(r, dict) else r.get("category"),
            "insight_text_pl": r[2] if not isinstance(r, dict) else r.get("insight_text_pl"),
        }
        for r in rows
    ]


def approved_insights_for_planner(conn: DatabaseConnection, *, case_family: str = "") -> list[str]:
    """Return approved insight texts for agent planner context."""
    _ = case_family
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT insight_text_pl FROM world_model_insights
            WHERE status = %s
            ORDER BY supporting_count DESC
            LIMIT 20
            """,
            (INSIGHT_APPROVED,),
        )
        rows = cur.fetchall() or []
    return [str(r[0] if not isinstance(r, dict) else r.get("insight_text_pl") or "") for r in rows if r]
