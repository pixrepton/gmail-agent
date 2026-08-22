"""Postgres implementation of MailboxMemoryStore."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .protocol import MailboxMemoryStore

log = logging.getLogger(__name__)


POSTGRES_CONNECT_TIMEOUT_SEC = 15


from .schema import MAILBOX_MEMORY_SCHEMA_SQL


def build_mailbox_memory_vector_schema_sql(*, dimensions: int) -> str:
    dim = max(1, int(dimensions or 1))
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE mailbox_memory_document_chunks
    ADD COLUMN IF NOT EXISTS embedding vector({dim});
ALTER TABLE company_drive_document_chunks
    ADD COLUMN IF NOT EXISTS embedding vector({dim});
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_document_chunks_embedding_hnsw
    ON mailbox_memory_document_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_company_drive_document_chunks_embedding_hnsw
    ON company_drive_document_chunks USING hnsw (embedding vector_cosine_ops);
"""


def _coerce_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _json_dump(value: Any) -> str:
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        payload = value
    else:
        payload = str(value)
    return json.dumps(payload, ensure_ascii=False)


def _case_payload_with_defaults(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    for field, default in {
        "case_key": "",
        "thread_id": "",
        "case_family": "unknown",
        "mailbox": "",
        "subject": "",
        "status": "open",
        "customer_name": "",
        "customer_email": "",
        "latest_signal_id": "",
        "latest_signal_at": "",
        "last_rebuild_at": "",
        "last_projection_refresh_at": "",
        "last_source_kinds_seen": [],
        "metadata": {},
    }.items():
        if payload.get(field) is None:
            payload[field] = default
        else:
            payload.setdefault(field, default)
    return payload


def _vector_literal(value: Any) -> str | None:
    if value in (None, "", []):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        numeric_parts: list[str] = []
        for item in value:
            try:
                numeric_parts.append(str(float(item)))
            except (TypeError, ValueError):
                return None
        return "[" + ",".join(numeric_parts) + "]"
    return None


def _parse_vector_literal_coords(literal: str) -> list[float] | None:
    raw = str(literal or "").strip()
    if not raw.startswith("[") or not raw.endswith("]"):
        return None
    inner = raw[1:-1].strip()
    if inner == "":
        return []
    coords: list[float] = []
    for part in inner.split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            coords.append(float(piece))
        except ValueError:
            return None
    return coords


def _cosine_similarity(query: list[float], vector: list[float]) -> float:
    if len(query) != len(vector) or not query:
        return 0.0
    dot = sum(q * v for q, v in zip(query, vector))
    nq = math.sqrt(sum(q * q for q in query))
    nv = math.sqrt(sum(v * v for v in vector))
    if nq <= 0.0 or nv <= 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (nq * nv)))


def _stable_advisory_lock_key(*, scope: str, owner_id: str) -> int:
    digest = hashlib.blake2b(f"{scope}:{owner_id}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


class PostgresMailboxMemoryStore:
    """Postgres-backed source of truth for mailbox memory."""

    def __init__(self, database_url: str, *, vector_enabled: bool = False, embedding_dimensions: int = 0) -> None:
        self.database_url = str(database_url or "").strip()
        if not self.database_url:
            raise ValueError("database_url is required for PostgresMailboxMemoryStore")
        self.vector_enabled = bool(vector_enabled)
        self.embedding_dimensions = max(0, int(embedding_dimensions or 0))

    def bootstrap(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(MAILBOX_MEMORY_SCHEMA_SQL)
                from correlation_registry.schema import CORRELATION_REGISTRY_SCHEMA_SQL

                cur.execute(CORRELATION_REGISTRY_SCHEMA_SQL)
                from agent_runtime.bootstrap import bootstrap_agent_runtime

                bootstrap_agent_runtime(conn)
                if self.vector_enabled:
                    cur.execute(
                        build_mailbox_memory_vector_schema_sql(
                            dimensions=self.embedding_dimensions or 1,
                        )
                    )
            conn.commit()

    def upsert_case(self, row: dict[str, Any]) -> None:
        case_id = str(row.get("case_id") or "").strip()
        if not case_id:
            return
        payload = _case_payload_with_defaults(row)
        payload["case_id"] = case_id
        timestamp = datetime.now().astimezone().isoformat()
        payload.setdefault("created_at", timestamp)
        payload.setdefault("updated_at", timestamp)
        self._upsert_case_payload(payload)

    def mutate_case(
        self,
        case_id: str,
        mutator,
        *,
        create_if_missing: bool = False,
    ) -> dict[str, Any]:
        case_id = str(case_id or "").strip()
        if not case_id:
            raise LookupError("case_id is required")
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                self._acquire_owner_lock(cur, scope="case_row", owner_id=case_id)
                cur.execute(
                    "SELECT * FROM mailbox_memory_cases WHERE case_id = %(case_id)s FOR UPDATE",
                    {"case_id": case_id},
                )
                row = cur.fetchone()
                if row is None and not create_if_missing:
                    raise LookupError(f"case not found: {case_id}")
                current = dict(row) if row else {"case_id": case_id, "metadata": {}}
                updated = mutator(current)
                if not isinstance(updated, dict):
                    raise RuntimeError("case mutator must return dict row")
                payload = _case_payload_with_defaults(updated)
                payload["case_id"] = case_id
                timestamp = datetime.now().astimezone().isoformat()
                if row is not None and not payload.get("created_at"):
                    payload["created_at"] = current.get("created_at")
                payload.setdefault("created_at", timestamp)
                payload.setdefault("updated_at", timestamp)
                self._upsert_case_payload(payload, cur=cur)
            conn.commit()
        return payload

    def upsert_message(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO mailbox_memory_messages (
                message_id, case_id, thread_id, mailbox, sender, sender_email, recipients,
                subject, snippet, body_text, labels, received_at, raw_snapshot, created_at, updated_at
            ) VALUES (
                %(message_id)s, %(case_id)s, %(thread_id)s, %(mailbox)s, %(sender)s, %(sender_email)s, %(recipients)s::jsonb,
                %(subject)s, %(snippet)s, %(body_text)s, %(labels)s::jsonb, %(received_at)s, %(raw_snapshot)s::jsonb, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (message_id) DO UPDATE SET
                case_id = EXCLUDED.case_id,
                thread_id = EXCLUDED.thread_id,
                mailbox = EXCLUDED.mailbox,
                sender = EXCLUDED.sender,
                sender_email = EXCLUDED.sender_email,
                recipients = EXCLUDED.recipients,
                subject = EXCLUDED.subject,
                snippet = EXCLUDED.snippet,
                body_text = EXCLUDED.body_text,
                labels = EXCLUDED.labels,
                received_at = EXCLUDED.received_at,
                raw_snapshot = EXCLUDED.raw_snapshot,
                updated_at = EXCLUDED.updated_at
            """,
            self._prep(row, json_fields={"recipients", "labels", "raw_snapshot"}, time_fields={"received_at", "created_at", "updated_at"}),
        )

    def upsert_attachment(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO mailbox_memory_attachments (
                attachment_id, case_id, message_id, thread_id, file_name, mime_type, size_bytes,
                gmail_attachment_id, content_sha256, blob_path, metadata, created_at, updated_at
            ) VALUES (
                %(attachment_id)s, %(case_id)s, %(message_id)s, %(thread_id)s, %(file_name)s, %(mime_type)s, %(size_bytes)s,
                %(gmail_attachment_id)s, %(content_sha256)s, %(blob_path)s, %(metadata)s::jsonb, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (attachment_id) DO UPDATE SET
                case_id = EXCLUDED.case_id,
                message_id = EXCLUDED.message_id,
                thread_id = EXCLUDED.thread_id,
                file_name = EXCLUDED.file_name,
                mime_type = EXCLUDED.mime_type,
                size_bytes = EXCLUDED.size_bytes,
                gmail_attachment_id = EXCLUDED.gmail_attachment_id,
                content_sha256 = EXCLUDED.content_sha256,
                blob_path = EXCLUDED.blob_path,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at
            """,
            self._prep(row, json_fields={"metadata"}, time_fields={"created_at", "updated_at"}),
        )

    def upsert_document(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO mailbox_memory_documents (
                document_id, case_id, message_id, attachment_id, parent_document_id, file_name, mime_type,
                source_type, document_kind, extraction_status, parser_name, content_sha256, blob_path,
                text_content, summary_text, metadata, created_at, updated_at
            ) VALUES (
                %(document_id)s, %(case_id)s, %(message_id)s, %(attachment_id)s, %(parent_document_id)s, %(file_name)s, %(mime_type)s,
                %(source_type)s, %(document_kind)s, %(extraction_status)s, %(parser_name)s, %(content_sha256)s, %(blob_path)s,
                %(text_content)s, %(summary_text)s, %(metadata)s::jsonb, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (document_id) DO UPDATE SET
                case_id = EXCLUDED.case_id,
                message_id = EXCLUDED.message_id,
                attachment_id = EXCLUDED.attachment_id,
                parent_document_id = EXCLUDED.parent_document_id,
                file_name = EXCLUDED.file_name,
                mime_type = EXCLUDED.mime_type,
                source_type = EXCLUDED.source_type,
                document_kind = EXCLUDED.document_kind,
                extraction_status = EXCLUDED.extraction_status,
                parser_name = EXCLUDED.parser_name,
                content_sha256 = EXCLUDED.content_sha256,
                blob_path = EXCLUDED.blob_path,
                text_content = EXCLUDED.text_content,
                summary_text = EXCLUDED.summary_text,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at
            """,
            self._prep(row, json_fields={"metadata"}, time_fields={"created_at", "updated_at"}),
        )

    def replace_document_chunks(self, document_id: str, rows: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._acquire_owner_lock(cur, scope="document_chunks", owner_id=document_id)
                cur.execute("DELETE FROM mailbox_memory_document_chunks WHERE document_id = %(document_id)s", {"document_id": document_id})
                if rows:
                    prepared = [
                        self._prep(
                            item,
                            json_fields={"metadata"},
                            time_fields={"created_at", "embedding_updated_at"},
                            vector_fields={"embedding"},
                        )
                        for item in rows
                    ]
                    if self.vector_enabled:
                        cur.executemany(
                            """
                            INSERT INTO mailbox_memory_document_chunks (
                                chunk_id, document_id, case_id, ordinal, chunk_text, token_estimate,
                                embedding, embedding_model, embedding_status, embedding_updated_at, embedding_error,
                                metadata, created_at
                            ) VALUES (
                                %(chunk_id)s, %(document_id)s, %(case_id)s, %(ordinal)s, %(chunk_text)s, %(token_estimate)s,
                                %(embedding)s::vector, %(embedding_model)s, %(embedding_status)s, %(embedding_updated_at)s, %(embedding_error)s,
                                %(metadata)s::jsonb, %(created_at)s
                            )
                            ON CONFLICT (chunk_id) DO UPDATE SET
                                document_id = EXCLUDED.document_id,
                                case_id = EXCLUDED.case_id,
                                ordinal = EXCLUDED.ordinal,
                                chunk_text = EXCLUDED.chunk_text,
                                token_estimate = EXCLUDED.token_estimate,
                                embedding = EXCLUDED.embedding,
                                embedding_model = EXCLUDED.embedding_model,
                                embedding_status = EXCLUDED.embedding_status,
                                embedding_updated_at = EXCLUDED.embedding_updated_at,
                                embedding_error = EXCLUDED.embedding_error,
                                metadata = EXCLUDED.metadata,
                                created_at = EXCLUDED.created_at
                            """,
                            prepared,
                        )
                    else:
                        cur.executemany(
                            """
                            INSERT INTO mailbox_memory_document_chunks (
                                chunk_id, document_id, case_id, ordinal, chunk_text, token_estimate,
                                embedding_model, embedding_status, embedding_updated_at, embedding_error,
                                metadata, created_at
                            ) VALUES (
                                %(chunk_id)s, %(document_id)s, %(case_id)s, %(ordinal)s, %(chunk_text)s, %(token_estimate)s,
                                %(embedding_model)s, %(embedding_status)s, %(embedding_updated_at)s, %(embedding_error)s,
                                %(metadata)s::jsonb, %(created_at)s
                            )
                            ON CONFLICT (chunk_id) DO UPDATE SET
                                document_id = EXCLUDED.document_id,
                                case_id = EXCLUDED.case_id,
                                ordinal = EXCLUDED.ordinal,
                                chunk_text = EXCLUDED.chunk_text,
                                token_estimate = EXCLUDED.token_estimate,
                                embedding_model = EXCLUDED.embedding_model,
                                embedding_status = EXCLUDED.embedding_status,
                                embedding_updated_at = EXCLUDED.embedding_updated_at,
                                embedding_error = EXCLUDED.embedding_error,
                                metadata = EXCLUDED.metadata,
                                created_at = EXCLUDED.created_at
                            """,
                            prepared,
                        )
            conn.commit()

    def append_event(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO mailbox_memory_events (
                event_id, case_id, message_id, thread_id, event_type, occurred_at, summary_text, payload, source_refs
            ) VALUES (
                %(event_id)s, %(case_id)s, %(message_id)s, %(thread_id)s, %(event_type)s, %(occurred_at)s, %(summary_text)s, %(payload)s::jsonb, %(source_refs)s::jsonb
            )
            ON CONFLICT (event_id) DO NOTHING
            """,
            self._prep(row, json_fields={"payload", "source_refs"}, time_fields={"occurred_at"}),
        )

    def replace_message_facts(self, *, message_id: str, rows: list[dict[str, Any]]) -> None:
        """Replace one message's extract snapshot, then apply canonical supersession in one TX.

        Cross-message actives for the same logical identity are superseded.
        Distinct values *within* the same source snapshot remain dual-active.
        Separately promoted ``structured_document_parse`` rows for the same message_id
        are preserved and may legally conflict with extract rows.
        """
        mid = str(message_id or "").strip()
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    self._acquire_owner_lock(cur, scope="message_facts", owner_id=mid)
                    cur.execute(
                        """
                        DELETE FROM mailbox_memory_facts
                        WHERE message_id = %(message_id)s
                          AND COALESCE(source_type, '') <> 'structured_document_parse'
                        """,
                        {"message_id": mid},
                    )
                    if rows:
                        self._apply_replaced_message_fact_rows_on_cursor(cur, mid, rows)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _apply_replaced_message_fact_rows_on_cursor(
        self, cur: Any, message_id: str, rows: list[dict[str, Any]]
    ) -> None:
        mid = str(message_id or "").strip()
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for raw in rows:
            row = self._prep(raw, json_fields={"metadata"}, time_fields={"observed_at"})
            if not str(row.get("status") or "").strip():
                row["status"] = "active"
            case_id = str(row.get("case_id") or "").strip()
            entity_scope = str(row.get("entity_scope") or "case").strip() or "case"
            fact_key = str(row.get("fact_key") or "").strip()
            if not case_id or not fact_key:
                continue
            groups.setdefault((case_id, entity_scope, fact_key), []).append(row)

        for (case_id, entity_scope, fact_key), group_rows in groups.items():
            distinct_values = {
                str(item.get("normalized_value") or "").strip() for item in group_rows
            }
            winner_id = str(group_rows[0].get("fact_id") or "")
            observed_at = group_rows[0].get("observed_at")
            if hasattr(observed_at, "isoformat"):
                observed_at = observed_at.isoformat()
            cur.execute(
                """
                SELECT fact_id, normalized_value, metadata, message_id
                FROM mailbox_memory_facts
                WHERE case_id = %(case_id)s
                  AND entity_scope = %(entity_scope)s
                  AND fact_key = %(fact_key)s
                  AND status = 'active'
                """,
                {
                    "case_id": case_id,
                    "entity_scope": entity_scope,
                    "fact_key": fact_key,
                },
            )
            for active in cur.fetchall() or []:
                if isinstance(active, dict):
                    fact_id = str(active.get("fact_id") or "")
                    old_value = str(active.get("normalized_value") or "").strip()
                    old_meta = active.get("metadata") if isinstance(active.get("metadata"), dict) else {}
                    active_mid = str(active.get("message_id") or "")
                else:
                    fact_id = str(active[0] or "")
                    old_value = str(active[1] or "").strip()
                    old_meta = active[2] if len(active) > 2 and isinstance(active[2], dict) else {}
                    active_mid = str(active[3] or "") if len(active) > 3 else ""
                if active_mid == mid:
                    continue
                if len(distinct_values) == 1 and old_value in distinct_values:
                    continue
                meta = dict(old_meta)
                meta["superseded_at"] = observed_at
                meta["superseded_by_fact_id"] = winner_id
                meta["supersede_reason"] = "replace_message_facts"
                cur.execute(
                    """
                    UPDATE mailbox_memory_facts
                    SET status = 'superseded', metadata = %(metadata)s::jsonb
                    WHERE fact_id = %(fact_id)s
                    """,
                    {"fact_id": fact_id, "metadata": _json_dump(meta)},
                )
            for row in group_rows:
                cur.execute(
                    """
                    INSERT INTO mailbox_memory_facts (
                        fact_id, case_id, message_id, document_id, entity_scope, fact_key, normalized_value, raw_value,
                        confidence, observed_at, source_type, source_ref, status, metadata
                    ) VALUES (
                        %(fact_id)s, %(case_id)s, %(message_id)s, %(document_id)s, %(entity_scope)s, %(fact_key)s, %(normalized_value)s, %(raw_value)s,
                        %(confidence)s, %(observed_at)s, %(source_type)s, %(source_ref)s, %(status)s, %(metadata)s::jsonb
                    )
                    ON CONFLICT (fact_id) DO NOTHING
                    """,
                    row,
                )

    def append_fact_rows(self, rows: list[dict[str, Any]]) -> None:
        self.append_facts_with_supersession(rows)

    def append_facts_with_supersession(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """DQ-10: supersede prior active facts when value changes; idempotent on same value."""
        stats = {"inserted": 0, "superseded": 0, "unchanged": 0}
        if not rows:
            return stats
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    stats = self._append_facts_with_supersession_on_cursor(cur, rows)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        log.debug(
            "append_facts_with_supersession rows=%s inserted=%s superseded=%s unchanged=%s case_id=%s",
            len(rows),
            stats["inserted"],
            stats["superseded"],
            stats["unchanged"],
            rows[0].get("case_id", "unknown") if rows else "none",
        )
        return stats

    def reassign_case_facts(self, *, source_case_id: str, target_case_id: str) -> dict[str, int]:
        """Move facts from source case to target and reconcile dual-active identities."""
        source = str(source_case_id or "").strip()
        target = str(target_case_id or "").strip()
        if not source or not target or source == target:
            return {"moved": 0, "reconciled": 0}
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE mailbox_memory_facts
                        SET case_id = %(target)s
                        WHERE case_id = %(source)s
                        """,
                        {"source": source, "target": target},
                    )
                    moved = int(cur.rowcount or 0)
                    reconciled = self._reconcile_active_fact_identities_on_cursor(cur, target)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {"moved": moved, "reconciled": reconciled}

    def reconcile_active_fact_identities(self, case_id: str) -> int:
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    reconciled = self._reconcile_active_fact_identities_on_cursor(cur, case_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return reconciled

    def _reconcile_active_fact_identities_on_cursor(self, cur: Any, case_id: str) -> int:
        cid = str(case_id or "").strip()
        if not cid:
            return 0
        cur.execute(
            """
            SELECT fact_id, entity_scope, fact_key, normalized_value, observed_at, metadata
            FROM mailbox_memory_facts
            WHERE case_id = %(case_id)s
              AND status = 'active'
            ORDER BY entity_scope, fact_key, observed_at DESC NULLS LAST, fact_id DESC
            """,
            {"case_id": cid},
        )
        rows = cur.fetchall() or []
        winners: dict[tuple[str, str], str] = {}
        reconciled = 0
        for row in rows:
            if isinstance(row, dict):
                fact_id = str(row.get("fact_id") or "")
                entity_scope = str(row.get("entity_scope") or "case")
                fact_key = str(row.get("fact_key") or "")
                observed_at = row.get("observed_at")
                old_meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            else:
                fact_id = str(row[0] or "")
                entity_scope = str(row[1] or "case")
                fact_key = str(row[2] or "")
                observed_at = row[4] if len(row) > 4 else None
                old_meta = row[5] if len(row) > 5 and isinstance(row[5], dict) else {}
            identity = (entity_scope, fact_key)
            if not fact_key:
                continue
            if identity not in winners:
                winners[identity] = fact_id
                continue
            meta = dict(old_meta)
            if hasattr(observed_at, "isoformat"):
                observed_at = observed_at.isoformat()
            meta["superseded_at"] = observed_at
            meta["superseded_by_fact_id"] = winners[identity]
            meta["supersede_reason"] = "reconcile_active_fact_identities"
            cur.execute(
                """
                UPDATE mailbox_memory_facts
                SET status = 'superseded', metadata = %(metadata)s::jsonb
                WHERE fact_id = %(fact_id)s
                """,
                {"fact_id": fact_id, "metadata": _json_dump(meta)},
            )
            reconciled += 1
        return reconciled

    def _append_facts_with_supersession_on_cursor(self, cur: Any, rows: list[dict[str, Any]]) -> dict[str, int]:
        stats = {"inserted": 0, "superseded": 0, "unchanged": 0}
        for raw in rows:
            row = self._prep(raw, json_fields={"metadata"}, time_fields={"observed_at"})
            case_id = str(row.get("case_id") or "").strip()
            entity_scope = str(row.get("entity_scope") or "case").strip() or "case"
            fact_key = str(row.get("fact_key") or "").strip()
            new_value = str(row.get("normalized_value") or "").strip()
            if not case_id or not fact_key:
                continue
            cur.execute(
                """
                SELECT fact_id, normalized_value, metadata
                FROM mailbox_memory_facts
                WHERE case_id = %(case_id)s
                  AND entity_scope = %(entity_scope)s
                  AND fact_key = %(fact_key)s
                  AND status = 'active'
                """,
                {
                    "case_id": case_id,
                    "entity_scope": entity_scope,
                    "fact_key": fact_key,
                },
            )
            active_rows = cur.fetchall() or []
            skip_insert = False
            for active in active_rows:
                if isinstance(active, dict):
                    old_value = str(active.get("normalized_value") or "").strip()
                    fact_id = str(active.get("fact_id") or "")
                    old_meta = active.get("metadata") if isinstance(active.get("metadata"), dict) else {}
                else:
                    fact_id = str(active[0] or "")
                    old_value = str(active[1] or "").strip()
                    old_meta = active[2] if len(active) > 2 and isinstance(active[2], dict) else {}
                if old_value == new_value:
                    stats["unchanged"] += 1
                    skip_insert = True
                    break
                supersede_meta = dict(old_meta)
                superseded_at = row.get("observed_at")
                if hasattr(superseded_at, "isoformat"):
                    superseded_at = superseded_at.isoformat()
                supersede_meta["superseded_at"] = superseded_at
                supersede_meta["superseded_by_fact_id"] = str(row.get("fact_id") or "")
                cur.execute(
                    """
                    UPDATE mailbox_memory_facts
                    SET status = 'superseded', metadata = %(metadata)s::jsonb
                    WHERE fact_id = %(fact_id)s
                    """,
                    {"fact_id": fact_id, "metadata": _json_dump(supersede_meta)},
                )
                stats["superseded"] += 1
            if skip_insert:
                continue
            cur.execute(
                """
                INSERT INTO mailbox_memory_facts (
                    fact_id, case_id, message_id, document_id, entity_scope, fact_key, normalized_value, raw_value,
                    confidence, observed_at, source_type, source_ref, status, metadata
                ) VALUES (
                    %(fact_id)s, %(case_id)s, %(message_id)s, %(document_id)s, %(entity_scope)s, %(fact_key)s, %(normalized_value)s, %(raw_value)s,
                    %(confidence)s, %(observed_at)s, %(source_type)s, %(source_ref)s, %(status)s, %(metadata)s::jsonb
                )
                ON CONFLICT (fact_id) DO NOTHING
                """,
                row,
            )
            if int(cur.rowcount or 0) > 0:
                stats["inserted"] += 1
        return stats

    def upsert_snapshot(self, case_id: str, row: dict[str, Any]) -> None:
        log.warning("LEGACY mailbox_memory_snapshots write for case_id=%s — E4 gate should prevent this when v2 feed active", case_id)
        payload = dict(row)
        payload["case_id"] = case_id
        self._upsert(
            """
            INSERT INTO mailbox_memory_snapshots (
                case_id, status, customer_name, customer_email, recommended_next_action, snapshot_json, updated_at
            ) VALUES (
                %(case_id)s, %(status)s, %(customer_name)s, %(customer_email)s, %(recommended_next_action)s, %(snapshot_json)s::jsonb, %(updated_at)s
            )
            ON CONFLICT (case_id) DO UPDATE SET
                status = EXCLUDED.status,
                customer_name = EXCLUDED.customer_name,
                customer_email = EXCLUDED.customer_email,
                recommended_next_action = EXCLUDED.recommended_next_action,
                snapshot_json = EXCLUDED.snapshot_json,
                updated_at = EXCLUDED.updated_at
            """,
            self._prep(payload, json_fields={"snapshot_json"}, time_fields={"updated_at"}),
        )

    def append_case_snapshot_version(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO mailbox_memory_case_snapshot_versions (
                snapshot_id, case_id, version, source_signal_id, confidence, snapshot_json, created_at
            ) VALUES (
                %(snapshot_id)s, %(case_id)s, %(version)s, %(source_signal_id)s, %(confidence)s, %(snapshot_json)s::jsonb, %(created_at)s
            )
            ON CONFLICT (snapshot_id) DO NOTHING
            """,
            self._prep(row, json_fields={"snapshot_json"}, time_fields={"created_at"}),
        )

    def fetch_case_snapshot_versions(self, case_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_case_snapshot_versions
            WHERE case_id = %(case_id)s
            ORDER BY version ASC, created_at ASC
            LIMIT %(limit)s
            """,
            {"case_id": case_id, "limit": limit},
        )

    def fetch_latest_case_snapshot_version(self, case_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            """
            SELECT * FROM mailbox_memory_case_snapshot_versions
            WHERE case_id = %(case_id)s
            ORDER BY version DESC, created_at DESC
            LIMIT 1
            """,
            {"case_id": case_id},
        )

    def upsert_thread_memory(self, row: dict[str, Any], *, only_if_absent: bool = False) -> None:
        payload = dict(row)
        payload.setdefault("case_id", "")
        payload.setdefault("source_message_id", "")
        payload.setdefault("memory_json", {})
        payload.setdefault("memory_sha256", "")
        payload.setdefault("source_kind", "node_b_generated")
        payload.setdefault("version", 1)
        payload.setdefault("created_at", payload.get("updated_at"))
        prepared = self._prep(
            payload,
            json_fields={"memory_json"},
            time_fields={"created_at", "updated_at"},
        )
        if only_if_absent:
            self._upsert(
                """
                INSERT INTO mailbox_memory_thread_memory (
                    thread_id, case_id, source_message_id, memory_json, memory_sha256,
                    source_kind, version, created_at, updated_at
                ) VALUES (
                    %(thread_id)s, %(case_id)s, %(source_message_id)s, %(memory_json)s::jsonb,
                    %(memory_sha256)s, %(source_kind)s, %(version)s, %(created_at)s, %(updated_at)s
                )
                ON CONFLICT (thread_id) DO NOTHING
                """,
                prepared,
            )
            return
        self._upsert(
            """
            INSERT INTO mailbox_memory_thread_memory (
                thread_id, case_id, source_message_id, memory_json, memory_sha256,
                source_kind, version, created_at, updated_at
            ) VALUES (
                %(thread_id)s, %(case_id)s, %(source_message_id)s, %(memory_json)s::jsonb,
                %(memory_sha256)s, %(source_kind)s, %(version)s, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (thread_id) DO UPDATE SET
                case_id = CASE
                    WHEN EXCLUDED.case_id <> '' THEN EXCLUDED.case_id
                    ELSE mailbox_memory_thread_memory.case_id
                END,
                source_message_id = CASE
                    WHEN EXCLUDED.source_message_id <> '' THEN EXCLUDED.source_message_id
                    ELSE mailbox_memory_thread_memory.source_message_id
                END,
                memory_json = EXCLUDED.memory_json,
                memory_sha256 = EXCLUDED.memory_sha256,
                source_kind = EXCLUDED.source_kind,
                version = CASE
                    WHEN mailbox_memory_thread_memory.memory_sha256 = EXCLUDED.memory_sha256
                        THEN mailbox_memory_thread_memory.version
                    ELSE mailbox_memory_thread_memory.version + 1
                END,
                updated_at = CASE
                    WHEN mailbox_memory_thread_memory.memory_sha256 = EXCLUDED.memory_sha256
                        THEN mailbox_memory_thread_memory.updated_at
                    ELSE EXCLUDED.updated_at
                END
            """,
            prepared,
        )

    def fetch_thread_memory(self, thread_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM mailbox_memory_thread_memory WHERE thread_id = %(thread_id)s",
            {"thread_id": str(thread_id or "").strip()},
        )

    def upsert_next_action(self, case_id: str, row: dict[str, Any]) -> None:
        payload = dict(row)
        payload["case_id"] = case_id
        self._upsert(
            """
            INSERT INTO mailbox_memory_next_actions (
                case_id, next_action, rationale, source_stage, payload, updated_at
            ) VALUES (
                %(case_id)s, %(next_action)s, %(rationale)s, %(source_stage)s, %(payload)s::jsonb, %(updated_at)s
            )
            ON CONFLICT (case_id) DO UPDATE SET
                next_action = EXCLUDED.next_action,
                rationale = EXCLUDED.rationale,
                source_stage = EXCLUDED.source_stage,
                payload = EXCLUDED.payload,
                updated_at = EXCLUDED.updated_at
            """,
            self._prep(payload, json_fields={"payload"}, time_fields={"updated_at"}),
        )

    def fetch_case(self, case_id: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM mailbox_memory_cases WHERE case_id = %(case_id)s", {"case_id": case_id})

    def fetch_resolved_cases_by_family_and_fact_keys(
        self,
        *,
        case_family: str,
        fact_keys: list[str],
        exclude_case_id: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        family = str(case_family or "").strip()
        keys = [str(k or "").strip() for k in (fact_keys or []) if str(k or "").strip()]
        if not family or not keys:
            return []
        return self._fetch_all(
            """
            SELECT c.case_id, c.case_family, c.subject, c.status, c.metadata,
                   COUNT(DISTINCT f.fact_key) AS overlap_count
            FROM mailbox_memory_cases c
            JOIN mailbox_memory_facts f ON f.case_id = c.case_id
            WHERE c.case_family = %(case_family)s
              AND c.status = 'resolved'
              AND c.case_id <> %(exclude_case_id)s
              AND COALESCE(f.status, 'active') <> 'superseded'
              AND f.fact_key = ANY(%(fact_keys)s::text[])
            GROUP BY c.case_id, c.case_family, c.subject, c.status, c.metadata
            ORDER BY overlap_count DESC, c.updated_at DESC
            LIMIT %(limit)s
            """,
            {
                "case_family": family,
                "exclude_case_id": str(exclude_case_id or "").strip(),
                "fact_keys": keys,
                "limit": max(1, min(int(limit or 5), 20)),
            },
        )

    def fetch_case_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            """
            SELECT c.*
            FROM mailbox_memory_messages m
            JOIN mailbox_memory_cases c ON c.case_id = m.case_id
            WHERE m.message_id = %(message_id)s
            """,
            {"message_id": message_id},
        )

    def fetch_message(self, message_id: str) -> dict[str, Any] | None:
        message_id = str(message_id or "").strip()
        if not message_id:
            return None
        return self._fetch_one(
            "SELECT * FROM mailbox_memory_messages WHERE message_id = %(message_id)s",
            {"message_id": message_id},
        )

    def fetch_any_message(self, *, order: str = "oldest") -> dict[str, Any] | None:
        direction = str(order or "oldest").strip().lower()
        if direction in {"newest", "latest", "desc"}:
            sql = """
            SELECT * FROM mailbox_memory_messages
            ORDER BY received_at DESC NULLS LAST, updated_at DESC, created_at DESC
            LIMIT 1
            """
        else:
            sql = """
            SELECT * FROM mailbox_memory_messages
            ORDER BY received_at ASC NULLS LAST, created_at ASC
            LIMIT 1
            """
        return self._fetch_one(sql, {})

    def fetch_messages_for_case(self, case_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_messages
            WHERE case_id = %(case_id)s
            ORDER BY received_at DESC NULLS LAST, updated_at DESC
            LIMIT %(limit)s
            """,
            {"case_id": case_id, "limit": limit},
        )

    def fetch_cases(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_cases
            ORDER BY updated_at DESC, created_at DESC
            LIMIT %(limit)s
            """,
            {"limit": limit},
        )

    def fetch_events_for_case(self, case_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_events
            WHERE case_id = %(case_id)s
            ORDER BY occurred_at DESC NULLS LAST, created_at DESC
            LIMIT %(limit)s
            """,
            {"case_id": case_id, "limit": limit},
        )

    def fetch_events(self, *, event_types: tuple[str, ...] = (), limit: int = 1000) -> list[dict[str, Any]]:
        if event_types:
            return self._fetch_all(
                """
                SELECT * FROM mailbox_memory_events
                WHERE event_type = ANY(%(event_types)s)
                ORDER BY occurred_at DESC NULLS LAST, created_at DESC
                LIMIT %(limit)s
                """,
                {"event_types": list(event_types), "limit": limit},
            )
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_events
            ORDER BY occurred_at DESC NULLS LAST, created_at DESC
            LIMIT %(limit)s
            """,
            {"limit": limit},
        )

    def upsert_action_proposal(self, row: dict[str, Any]) -> None:
        payload = dict(row)
        payload["raw_json"] = dict(row)
        self._upsert(
            """
            INSERT INTO mailbox_memory_action_proposals (
                proposal_id, case_id, source_signal_id, action_type, payload, proposed_by, confidence,
                risk_class, requires_review, policy_basis, created_at, status, decision_reason,
                decided_by, decided_at, raw_json
            ) VALUES (
                %(proposal_id)s, %(case_id)s, %(source_signal_id)s, %(action_type)s, %(payload)s::jsonb, %(proposed_by)s, %(confidence)s,
                %(risk_class)s, %(requires_review)s, %(policy_basis)s::jsonb, %(created_at)s, %(status)s, %(decision_reason)s,
                %(decided_by)s, %(decided_at)s, %(raw_json)s::jsonb
            )
            ON CONFLICT (proposal_id) DO UPDATE SET
                payload = EXCLUDED.payload,
                confidence = EXCLUDED.confidence,
                risk_class = EXCLUDED.risk_class,
                requires_review = EXCLUDED.requires_review,
                policy_basis = EXCLUDED.policy_basis,
                status = EXCLUDED.status,
                decision_reason = EXCLUDED.decision_reason,
                decided_by = EXCLUDED.decided_by,
                decided_at = EXCLUDED.decided_at,
                raw_json = EXCLUDED.raw_json
            """,
            self._prep(payload, json_fields={"payload", "policy_basis", "raw_json"}, time_fields={"created_at", "decided_at"}),
        )

    def fetch_action_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM mailbox_memory_action_proposals WHERE proposal_id = %(proposal_id)s",
            {"proposal_id": proposal_id},
        )

    def fetch_action_proposals(self, *, case_id: str = "", status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if case_id and status:
            sql = "SELECT * FROM mailbox_memory_action_proposals WHERE case_id = %(case_id)s AND status = %(status)s ORDER BY created_at DESC NULLS LAST LIMIT %(limit)s"
        elif case_id:
            sql = "SELECT * FROM mailbox_memory_action_proposals WHERE case_id = %(case_id)s ORDER BY created_at DESC NULLS LAST LIMIT %(limit)s"
        elif status:
            sql = "SELECT * FROM mailbox_memory_action_proposals WHERE status = %(status)s ORDER BY created_at DESC NULLS LAST LIMIT %(limit)s"
        else:
            sql = "SELECT * FROM mailbox_memory_action_proposals ORDER BY created_at DESC NULLS LAST LIMIT %(limit)s"
        return self._fetch_all(sql, {"case_id": case_id, "status": status, "limit": limit})

    def append_policy_decision(self, row: dict[str, Any]) -> bool:
        payload = dict(row)
        payload["raw_json"] = dict(row.get("raw_json") or row)
        prepared = self._prep(
            payload,
            json_fields={
                "allowed_actions",
                "policy_basis",
                "failed_rules",
                "warnings",
                "evidence_refs",
                "raw_json",
            },
            time_fields={"generated_at"},
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mailbox_memory_policy_decisions (
                        policy_decision_id, decision_candidate_id, case_id,
                        source_signal_id, source_message_id, schema_version, status,
                        allowed_actions, requires_review, requires_human_approval,
                        policy_basis, failed_rules, warnings, evidence_refs,
                        generated_at, raw_json
                    ) VALUES (
                        %(policy_decision_id)s, %(decision_candidate_id)s, %(case_id)s,
                        %(source_signal_id)s, %(source_message_id)s, %(schema_version)s, %(status)s,
                        %(allowed_actions)s::jsonb, %(requires_review)s, %(requires_human_approval)s,
                        %(policy_basis)s::jsonb, %(failed_rules)s::jsonb, %(warnings)s::jsonb,
                        %(evidence_refs)s::jsonb, %(generated_at)s, %(raw_json)s::jsonb
                    )
                    ON CONFLICT (policy_decision_id) DO NOTHING
                    RETURNING policy_decision_id
                    """,
                    prepared,
                )
                inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def fetch_policy_decision(self, policy_decision_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            """
            SELECT * FROM mailbox_memory_policy_decisions
            WHERE policy_decision_id = %(policy_decision_id)s
            """,
            {"policy_decision_id": policy_decision_id},
        )

    def fetch_policy_decisions(
        self,
        *,
        case_id: str = "",
        source_signal_id: str = "",
        source_message_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = ["TRUE"]
        if case_id:
            where.append("case_id = %(case_id)s")
        if source_signal_id:
            where.append("source_signal_id = %(source_signal_id)s")
        if source_message_id:
            where.append("source_message_id = %(source_message_id)s")
        return self._fetch_all(
            f"""
            SELECT * FROM mailbox_memory_policy_decisions
            WHERE {' AND '.join(where)}
            ORDER BY generated_at DESC NULLS LAST, policy_decision_id DESC
            LIMIT %(limit)s
            """,
            {
                "case_id": case_id,
                "source_signal_id": source_signal_id,
                "source_message_id": source_message_id,
                "limit": limit,
            },
        )

    def append_action_proposal_v2(self, row: dict[str, Any]) -> bool:
        payload = dict(row)
        payload["raw_json"] = dict(row.get("raw_json") or row)
        prepared = self._prep(
            payload,
            json_fields={"evidence_refs", "raw_json"},
            time_fields={"generated_at", "expires_at"},
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mailbox_memory_action_proposals_v2 (
                        proposal_id, policy_decision_id, decision_candidate_id, case_id,
                        source_signal_id, source_message_id, schema_version, action_type,
                        allowed_by_policy, requires_operator_approval, status, action_mode,
                        blocked_reason, evidence_refs, generated_at, expires_at, raw_json
                    ) VALUES (
                        %(proposal_id)s, %(policy_decision_id)s, %(decision_candidate_id)s, %(case_id)s,
                        %(source_signal_id)s, %(source_message_id)s, %(schema_version)s, %(action_type)s,
                        %(allowed_by_policy)s, %(requires_operator_approval)s, %(status)s, %(action_mode)s,
                        %(blocked_reason)s, %(evidence_refs)s::jsonb, %(generated_at)s, %(expires_at)s,
                        %(raw_json)s::jsonb
                    )
                    ON CONFLICT (proposal_id) DO NOTHING
                    RETURNING proposal_id
                    """,
                    prepared,
                )
                inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def fetch_action_proposal_v2(self, proposal_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            """
            SELECT * FROM mailbox_memory_action_proposals_v2
            WHERE proposal_id = %(proposal_id)s
            """,
            {"proposal_id": proposal_id},
        )

    def fetch_action_proposals_v2(
        self,
        *,
        case_id: str = "",
        source_signal_id: str = "",
        source_message_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = ["TRUE"]
        if case_id:
            where.append("case_id = %(case_id)s")
        if source_signal_id:
            where.append("source_signal_id = %(source_signal_id)s")
        if source_message_id:
            where.append("source_message_id = %(source_message_id)s")
        return self._fetch_all(
            f"""
            SELECT * FROM mailbox_memory_action_proposals_v2
            WHERE {' AND '.join(where)}
            ORDER BY generated_at DESC NULLS LAST, proposal_id DESC
            LIMIT %(limit)s
            """,
            {
                "case_id": case_id,
                "source_signal_id": source_signal_id,
                "source_message_id": source_message_id,
                "limit": limit,
            },
        )

    def upsert_execution_result(self, row: dict[str, Any]) -> None:
        payload = dict(row)
        payload["raw_json"] = dict(row)
        self._upsert(
            """
            INSERT INTO mailbox_memory_execution_results (
                execution_id, proposal_id, case_id, action_type, approved_by, approved_at, executed_by,
                executed_at, execution_status, error_code, error_message, result_payload, audit_trace_id,
                policy_result, raw_json
            ) VALUES (
                %(execution_id)s, %(proposal_id)s, %(case_id)s, %(action_type)s, %(approved_by)s, %(approved_at)s, %(executed_by)s,
                %(executed_at)s, %(execution_status)s, %(error_code)s, %(error_message)s, %(result_payload)s::jsonb, %(audit_trace_id)s,
                %(policy_result)s::jsonb, %(raw_json)s::jsonb
            )
            ON CONFLICT (execution_id) DO UPDATE SET
                execution_status = EXCLUDED.execution_status,
                error_code = EXCLUDED.error_code,
                error_message = EXCLUDED.error_message,
                result_payload = EXCLUDED.result_payload,
                policy_result = EXCLUDED.policy_result,
                raw_json = EXCLUDED.raw_json
            """,
            self._prep(payload, json_fields={"result_payload", "policy_result", "raw_json"}, time_fields={"approved_at", "executed_at"}),
        )

    def fetch_execution_results(self, *, case_id: str = "", proposal_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if case_id and proposal_id:
            sql = "SELECT * FROM mailbox_memory_execution_results WHERE case_id = %(case_id)s AND proposal_id = %(proposal_id)s ORDER BY executed_at DESC NULLS LAST LIMIT %(limit)s"
        elif case_id:
            sql = "SELECT * FROM mailbox_memory_execution_results WHERE case_id = %(case_id)s ORDER BY executed_at DESC NULLS LAST LIMIT %(limit)s"
        elif proposal_id:
            sql = "SELECT * FROM mailbox_memory_execution_results WHERE proposal_id = %(proposal_id)s ORDER BY executed_at DESC NULLS LAST LIMIT %(limit)s"
        else:
            sql = "SELECT * FROM mailbox_memory_execution_results ORDER BY executed_at DESC NULLS LAST LIMIT %(limit)s"
        return self._fetch_all(sql, {"case_id": case_id, "proposal_id": proposal_id, "limit": limit})

    def upsert_calendar_event(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO mailbox_memory_calendar_events (
                calendar_event_id, source, summary, description, location, start_at, end_at, attendees,
                organizer, html_link, recurring, ingested_at, visibility_scope, case_id, link_confidence, raw_payload
            ) VALUES (
                %(calendar_event_id)s, %(source)s, %(summary)s, %(description)s, %(location)s, %(start_at)s, %(end_at)s, %(attendees)s::jsonb,
                %(organizer)s, %(html_link)s, %(recurring)s, %(ingested_at)s, %(visibility_scope)s, %(case_id)s, %(link_confidence)s, %(raw_payload)s::jsonb
            )
            ON CONFLICT (calendar_event_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                description = EXCLUDED.description,
                location = EXCLUDED.location,
                start_at = EXCLUDED.start_at,
                end_at = EXCLUDED.end_at,
                attendees = EXCLUDED.attendees,
                organizer = EXCLUDED.organizer,
                html_link = EXCLUDED.html_link,
                recurring = EXCLUDED.recurring,
                ingested_at = EXCLUDED.ingested_at,
                visibility_scope = EXCLUDED.visibility_scope,
                case_id = EXCLUDED.case_id,
                link_confidence = EXCLUDED.link_confidence,
                raw_payload = EXCLUDED.raw_payload
            """,
            self._prep(dict(row), json_fields={"attendees", "raw_payload"}, time_fields={"start_at", "end_at", "ingested_at"}),
        )

    def upsert_calendar_case_link(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO mailbox_memory_calendar_case_links (
                calendar_event_id, case_id, link_confidence, match_reasons, created_at
            ) VALUES (
                %(calendar_event_id)s, %(case_id)s, %(link_confidence)s, %(match_reasons)s::jsonb, %(created_at)s
            )
            ON CONFLICT (calendar_event_id, case_id) DO UPDATE SET
                link_confidence = EXCLUDED.link_confidence,
                match_reasons = EXCLUDED.match_reasons,
                created_at = EXCLUDED.created_at
            """,
            self._prep(dict(row), json_fields={"match_reasons"}, time_fields={"created_at"}),
        )

    def fetch_calendar_events_for_case(self, case_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_calendar_events
            WHERE case_id = %(case_id)s
            ORDER BY start_at ASC NULLS LAST, ingested_at DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {"case_id": case_id, "limit": limit},
        )

    def upsert_document_intelligence_result(self, row: dict[str, Any]) -> None:
        payload = dict(row)
        payload["raw_json"] = dict(row)
        self._upsert(
            """
            INSERT INTO mailbox_memory_document_intelligence_results (
                document_id, source_type, source_id, case_id, filename, mime_type, document_type,
                document_type_confidence, summary, evidence_refs, conflicts, parser, parser_confidence,
                created_at, requires_human_review, not_proven_multimodal, raw_json
            ) VALUES (
                %(document_id)s, %(source_type)s, %(source_id)s, %(case_id)s, %(filename)s, %(mime_type)s, %(document_type)s,
                %(document_type_confidence)s, %(summary)s, %(evidence_refs)s::jsonb, %(conflicts)s::jsonb, %(parser)s, %(parser_confidence)s,
                %(created_at)s, %(requires_human_review)s, %(not_proven_multimodal)s, %(raw_json)s::jsonb
            )
            ON CONFLICT (document_id) DO UPDATE SET
                case_id = EXCLUDED.case_id,
                document_type = EXCLUDED.document_type,
                document_type_confidence = EXCLUDED.document_type_confidence,
                summary = EXCLUDED.summary,
                evidence_refs = EXCLUDED.evidence_refs,
                conflicts = EXCLUDED.conflicts,
                parser = EXCLUDED.parser,
                parser_confidence = EXCLUDED.parser_confidence,
                requires_human_review = EXCLUDED.requires_human_review,
                not_proven_multimodal = EXCLUDED.not_proven_multimodal,
                raw_json = EXCLUDED.raw_json
            """,
            self._prep(payload, json_fields={"evidence_refs", "conflicts", "raw_json"}, time_fields={"created_at"}),
        )
        for field in list(row.get("extracted_fields") or []):
            fp = dict(field)
            fp["document_id"] = str(row.get("document_id") or "")
            self._upsert(
                """
                INSERT INTO mailbox_memory_document_extracted_fields (
                    document_id, field_name, field_value, field_type, confidence, evidence_ref
                ) VALUES (
                    %(document_id)s, %(field_name)s, %(field_value)s, %(field_type)s, %(confidence)s, %(evidence_ref)s::jsonb
                )
                ON CONFLICT (document_id, field_name, field_value) DO UPDATE SET
                    field_type = EXCLUDED.field_type,
                    confidence = EXCLUDED.confidence,
                    evidence_ref = EXCLUDED.evidence_ref
                """,
                self._prep(fp, json_fields={"evidence_ref"}),
            )

    def fetch_document_intelligence_for_case(self, case_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            """
            SELECT * FROM mailbox_memory_document_intelligence_results
            WHERE case_id = %(case_id)s
            ORDER BY created_at DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {"case_id": case_id, "limit": limit},
        )
        for row in rows:
            row["extracted_fields"] = self._fetch_all(
                """
                SELECT field_name, field_value, field_type, confidence, evidence_ref
                FROM mailbox_memory_document_extracted_fields
                WHERE document_id = %(document_id)s
                ORDER BY confidence DESC, field_name ASC
                """,
                {"document_id": row.get("document_id")},
            )
        return rows

    def fetch_latest_adjudication_link_override(self, signal_id: str) -> dict[str, Any] | None:
        sid = str(signal_id or "").strip()
        if not sid:
            return None
        row = self._fetch_one(
            """
            SELECT payload FROM mailbox_memory_events
            WHERE event_type = 'adjudication_link_override'
              AND payload->>'signal_id' = %(signal_id)s
            ORDER BY created_at DESC NULLS LAST
            LIMIT 1
            """,
            {"signal_id": sid},
        )
        if not row:
            return None
        payload = row.get("payload")
        return dict(payload) if isinstance(payload, dict) else None

    def fetch_facts_for_case(self, case_id: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_facts
            WHERE case_id = %(case_id)s
            ORDER BY fact_key ASC, confidence DESC, observed_at DESC NULLS LAST
            """,
            {"case_id": case_id},
        )

    def fetch_active_facts_for_case(self, case_id: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_facts
            WHERE case_id = %(case_id)s
              AND COALESCE(status, 'active') <> 'superseded'
            ORDER BY fact_key ASC, confidence DESC, observed_at DESC NULLS LAST
            """,
            {"case_id": case_id},
        )

    def fetch_documents_for_case(self, case_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_documents
            WHERE case_id = %(case_id)s
            ORDER BY updated_at DESC, created_at DESC
            LIMIT %(limit)s
            """,
            {"case_id": case_id, "limit": limit},
        )

    def fetch_chunks_for_case(self, case_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_document_chunks
            WHERE case_id = %(case_id)s
            ORDER BY created_at DESC, ordinal ASC
            LIMIT %(limit)s
            """,
            {"case_id": case_id, "limit": limit},
        )

    def fetch_snapshot(self, case_id: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM mailbox_memory_snapshots WHERE case_id = %(case_id)s", {"case_id": case_id})

    def fetch_next_action(self, case_id: str) -> dict[str, Any] | None:
        return self._fetch_one("SELECT * FROM mailbox_memory_next_actions WHERE case_id = %(case_id)s", {"case_id": case_id})

    def upsert_drive_document(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO company_drive_documents (
                document_id, drive_item_id, parent_drive_item_id, parent_document_id, case_id, probable_case_key,
                file_name, mime_type, folder_path, lane, document_kind, scope, source_ref, extraction_status, linkage_status,
                classification_confidence, extraction_confidence, link_confidence, download_mime_type, content_sha256, blob_path,
                text_content, summary_text, metadata, created_at, updated_at
            ) VALUES (
                %(document_id)s, %(drive_item_id)s, %(parent_drive_item_id)s, %(parent_document_id)s, %(case_id)s, %(probable_case_key)s,
                %(file_name)s, %(mime_type)s, %(folder_path)s, %(lane)s, %(document_kind)s, %(scope)s, %(source_ref)s, %(extraction_status)s, %(linkage_status)s,
                %(classification_confidence)s, %(extraction_confidence)s, %(link_confidence)s, %(download_mime_type)s, %(content_sha256)s, %(blob_path)s,
                %(text_content)s, %(summary_text)s, %(metadata)s::jsonb, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (document_id) DO UPDATE SET
                drive_item_id = EXCLUDED.drive_item_id,
                parent_drive_item_id = EXCLUDED.parent_drive_item_id,
                parent_document_id = EXCLUDED.parent_document_id,
                case_id = EXCLUDED.case_id,
                probable_case_key = EXCLUDED.probable_case_key,
                file_name = EXCLUDED.file_name,
                mime_type = EXCLUDED.mime_type,
                folder_path = EXCLUDED.folder_path,
                lane = EXCLUDED.lane,
                document_kind = EXCLUDED.document_kind,
                scope = EXCLUDED.scope,
                source_ref = EXCLUDED.source_ref,
                extraction_status = EXCLUDED.extraction_status,
                linkage_status = EXCLUDED.linkage_status,
                classification_confidence = EXCLUDED.classification_confidence,
                extraction_confidence = EXCLUDED.extraction_confidence,
                link_confidence = EXCLUDED.link_confidence,
                download_mime_type = EXCLUDED.download_mime_type,
                content_sha256 = EXCLUDED.content_sha256,
                blob_path = EXCLUDED.blob_path,
                text_content = EXCLUDED.text_content,
                summary_text = EXCLUDED.summary_text,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at
            """,
            self._prep(row, json_fields={"metadata"}, time_fields={"created_at", "updated_at"}),
        )
        document_id = str(row.get("document_id") or "").strip()
        case_id = str(row.get("case_id") or "").strip()
        if document_id and case_id:
            self._propagate_drive_document_case_id_to_chunks(document_id=document_id, case_id=case_id)

    def _propagate_drive_document_case_id_to_chunks(self, *, document_id: str, case_id: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE company_drive_document_chunks
                    SET case_id = %(case_id)s, updated_at = NOW()
                    WHERE document_id = %(document_id)s
                      AND (case_id IS DISTINCT FROM %(case_id)s)
                    """,
                    {"document_id": document_id, "case_id": case_id},
                )
            conn.commit()

    def replace_drive_document_chunks(self, *, document_id: str, rows: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._acquire_owner_lock(cur, scope="drive_document_chunks", owner_id=document_id)
                cur.execute(
                    "DELETE FROM company_drive_document_chunks WHERE document_id = %(document_id)s",
                    {"document_id": document_id},
                )
                if rows:
                    prepared = [
                        self._prep(
                            item,
                            json_fields={"metadata"},
                            time_fields={"created_at", "updated_at", "embedding_updated_at"},
                            vector_fields={"embedding"},
                        )
                        for item in rows
                    ]
                    if self.vector_enabled:
                        cur.executemany(
                            """
                            INSERT INTO company_drive_document_chunks (
                                chunk_id, document_id, case_id, ordinal, chunk_text, token_estimate,
                                embedding, embedding_model, embedding_status, embedding_updated_at, embedding_error,
                                metadata, created_at, updated_at
                            ) VALUES (
                                %(chunk_id)s, %(document_id)s, %(case_id)s, %(ordinal)s, %(chunk_text)s, %(token_estimate)s,
                                %(embedding)s::vector, %(embedding_model)s, %(embedding_status)s, %(embedding_updated_at)s, %(embedding_error)s,
                                %(metadata)s::jsonb, %(created_at)s, %(updated_at)s
                            )
                            ON CONFLICT (chunk_id) DO UPDATE SET
                                document_id = EXCLUDED.document_id,
                                case_id = EXCLUDED.case_id,
                                ordinal = EXCLUDED.ordinal,
                                chunk_text = EXCLUDED.chunk_text,
                                token_estimate = EXCLUDED.token_estimate,
                                embedding = EXCLUDED.embedding,
                                embedding_model = EXCLUDED.embedding_model,
                                embedding_status = EXCLUDED.embedding_status,
                                embedding_updated_at = EXCLUDED.embedding_updated_at,
                                embedding_error = EXCLUDED.embedding_error,
                                metadata = EXCLUDED.metadata,
                                created_at = EXCLUDED.created_at,
                                updated_at = EXCLUDED.updated_at
                            """,
                            prepared,
                        )
                    else:
                        cur.executemany(
                            """
                            INSERT INTO company_drive_document_chunks (
                                chunk_id, document_id, case_id, ordinal, chunk_text, token_estimate,
                                embedding_model, embedding_status, embedding_updated_at, embedding_error,
                                metadata, created_at, updated_at
                            ) VALUES (
                                %(chunk_id)s, %(document_id)s, %(case_id)s, %(ordinal)s, %(chunk_text)s, %(token_estimate)s,
                                %(embedding_model)s, %(embedding_status)s, %(embedding_updated_at)s, %(embedding_error)s,
                                %(metadata)s::jsonb, %(created_at)s, %(updated_at)s
                            )
                            ON CONFLICT (chunk_id) DO UPDATE SET
                                document_id = EXCLUDED.document_id,
                                case_id = EXCLUDED.case_id,
                                ordinal = EXCLUDED.ordinal,
                                chunk_text = EXCLUDED.chunk_text,
                                token_estimate = EXCLUDED.token_estimate,
                                embedding_model = EXCLUDED.embedding_model,
                                embedding_status = EXCLUDED.embedding_status,
                                embedding_updated_at = EXCLUDED.embedding_updated_at,
                                embedding_error = EXCLUDED.embedding_error,
                                metadata = EXCLUDED.metadata,
                                created_at = EXCLUDED.created_at,
                                updated_at = EXCLUDED.updated_at
                            """,
                            prepared,
                        )
            conn.commit()

    def replace_drive_document_facts(self, *, document_id: str, rows: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM company_drive_facts WHERE drive_document_id = %(document_id)s", {"document_id": document_id})
                if rows:
                    prepared = [self._prep(item, json_fields={"metadata"}, time_fields={"observed_at", "created_at"}) for item in rows]
                    cur.executemany(
                        """
                        INSERT INTO company_drive_facts (
                            fact_id, drive_document_id, case_id, probable_case_key, fact_family, entity_scope, fact_key,
                            normalized_value, raw_value, confidence, observed_at, source_ref, status, metadata, created_at
                        ) VALUES (
                            %(fact_id)s, %(drive_document_id)s, %(case_id)s, %(probable_case_key)s, %(fact_family)s, %(entity_scope)s, %(fact_key)s,
                            %(normalized_value)s, %(raw_value)s, %(confidence)s, %(observed_at)s, %(source_ref)s, %(status)s, %(metadata)s::jsonb, %(created_at)s
                        )
                        """,
                        prepared,
                    )
            conn.commit()

    def upsert_drive_ingest_run(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO drive_ingest_runs (
                run_id, root_folder_id, cursor, status, stats, created_at, updated_at
            ) VALUES (
                %(run_id)s, %(root_folder_id)s, %(cursor)s, %(status)s, %(stats)s::jsonb, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (run_id) DO UPDATE SET
                root_folder_id = EXCLUDED.root_folder_id,
                cursor = EXCLUDED.cursor,
                status = EXCLUDED.status,
                stats = EXCLUDED.stats,
                updated_at = EXCLUDED.updated_at
            """,
            self._prep(row, json_fields={"stats"}, time_fields={"created_at", "updated_at"}),
        )

    def fetch_drive_documents_for_case(self, case_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM company_drive_documents
            WHERE case_id = %(case_id)s
            ORDER BY updated_at DESC, created_at DESC
            LIMIT %(limit)s
            """,
            {"case_id": case_id, "limit": limit},
        )

    def fetch_drive_chunks_for_case(self, case_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT c.*
            FROM company_drive_document_chunks c
            INNER JOIN company_drive_documents d ON d.document_id = c.document_id
            WHERE d.case_id = %(case_id)s
            ORDER BY c.updated_at DESC, c.ordinal ASC
            LIMIT %(limit)s
            """,
            {"case_id": case_id, "limit": limit},
        )

    def fetch_semantic_chunk_candidates_for_case(
        self, case_id: str, query_vector_literal: str, *, limit_mailbox: int = 50, limit_drive: int = 50
    ) -> list[dict[str, Any]]:
        if not self.vector_enabled or not str(query_vector_literal or "").strip():
            return []
        lim_m = max(1, int(limit_mailbox))
        lim_d = max(1, int(limit_drive))
        params_base = {"case_id": case_id, "qv": str(query_vector_literal).strip()}
        sql_mailbox = """
            SELECT *,
                   GREATEST(
                       0.0::float8,
                       LEAST(1.0::float8, 1.0::float8 - (embedding <=> %(qv)s::vector))
                   ) AS vector_similarity
            FROM mailbox_memory_document_chunks
            WHERE case_id = %(case_id)s
              AND embedding IS NOT NULL
              AND embedding_status = 'ready'
            ORDER BY embedding <=> %(qv)s::vector ASC
            LIMIT %(lim_m)s
        """
        sql_drive = """
            SELECT c.*,
                   GREATEST(
                       0.0::float8,
                       LEAST(1.0::float8, 1.0::float8 - (c.embedding <=> %(qv)s::vector))
                   ) AS vector_similarity
            FROM company_drive_document_chunks c
            INNER JOIN company_drive_documents d ON d.document_id = c.document_id
            WHERE d.case_id = %(case_id)s
              AND c.embedding IS NOT NULL
              AND c.embedding_status = 'ready'
            ORDER BY c.embedding <=> %(qv)s::vector ASC
            LIMIT %(lim_d)s
        """
        rows_m = self._fetch_all(sql_mailbox, {**params_base, "lim_m": lim_m})
        rows_d = self._fetch_all(sql_drive, {**params_base, "lim_d": lim_d})
        return list(rows_m or []) + list(rows_d or [])

    def fetch_drive_facts_for_case(self, case_id: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM company_drive_facts
            WHERE case_id = %(case_id)s
            ORDER BY fact_family ASC, fact_key ASC, confidence DESC, observed_at DESC NULLS LAST
            """,
            {"case_id": case_id},
        )

    def fetch_drive_facts_for_document(self, document_id: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM company_drive_facts
            WHERE drive_document_id = %(document_id)s
            ORDER BY fact_family ASC, fact_key ASC, confidence DESC, observed_at DESC NULLS LAST
            """,
            {"document_id": document_id},
        )

    def fetch_drive_documents(self, *, limit: int = 100, scopes: tuple[str, ...] = (), lanes: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        where_clauses: list[str] = ["TRUE"]
        params: dict[str, Any] = {"limit": limit}
        if scopes:
            where_clauses.append("scope = ANY(%(scopes)s)")
            params["scopes"] = list(scopes)
        if lanes:
            where_clauses.append("lane = ANY(%(lanes)s)")
            params["lanes"] = list(lanes)
        return self._fetch_all(
            f"""
            SELECT * FROM company_drive_documents
            WHERE {' AND '.join(where_clauses)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT %(limit)s
            """,
            params,
        )

    def fetch_drive_document_by_item_id(self, drive_item_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            """
            SELECT * FROM company_drive_documents
            WHERE drive_item_id = %(drive_item_id)s
            LIMIT 1
            """,
            {"drive_item_id": drive_item_id},
        )

    def append_raw_observation(self, row: dict[str, Any]) -> bool:
        inserted = False
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mailbox_memory_raw_observations (
                        observation_id, schema_version, observation_kind, source_kind, source_ref_json,
                        occurred_at, observed_at, source_fingerprint, payload_hash, payload_json,
                        created_by_runtime, created_at
                    ) VALUES (
                        %(observation_id)s, %(schema_version)s, %(observation_kind)s, %(source_kind)s, %(source_ref_json)s::jsonb,
                        %(occurred_at)s, %(observed_at)s, %(source_fingerprint)s, %(payload_hash)s, %(payload_json)s::jsonb,
                        %(created_by_runtime)s, %(created_at)s
                    )
                    ON CONFLICT (source_fingerprint) DO NOTHING
                    RETURNING observation_id
                    """,
                    self._prep(
                        row,
                        json_fields={"source_ref_json", "payload_json"},
                        time_fields={"occurred_at", "observed_at", "created_at"},
                    ),
                )
                inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def fetch_raw_observation(self, observation_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM mailbox_memory_raw_observations WHERE observation_id = %(observation_id)s",
            {"observation_id": observation_id},
        )

    def fetch_raw_observation_by_source_fingerprint(self, source_fingerprint: str) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM mailbox_memory_raw_observations WHERE source_fingerprint = %(source_fingerprint)s",
            {"source_fingerprint": source_fingerprint},
        )

    def fetch_raw_observations_for_source(self, source_kind: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_raw_observations
            WHERE source_kind = %(source_kind)s
            ORDER BY observed_at ASC NULLS LAST, created_at ASC
            LIMIT %(limit)s
            """,
            {"source_kind": source_kind, "limit": limit},
        )

    def append_signal(self, row: dict[str, Any]) -> bool:
        inserted = False
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mailbox_memory_signals (
                        signal_id, schema_version, signal_kind, source_kind, source_ref_json, observed_at,
                        effective_at, idempotency_key, content_hash, case_key_hint, thread_key_hint, business_lane,
                        signal_summary_pl, payload_json, artifacts_json, processing_state, replayable,
                        engagement_id, created_by_runtime, created_at
                    ) VALUES (
                        %(signal_id)s, %(schema_version)s, %(signal_kind)s, %(source_kind)s, %(source_ref_json)s::jsonb, %(observed_at)s,
                        %(effective_at)s, %(idempotency_key)s, %(content_hash)s, %(case_key_hint)s, %(thread_key_hint)s, %(business_lane)s,
                        %(signal_summary_pl)s, %(payload_json)s::jsonb, %(artifacts_json)s::jsonb, %(processing_state)s, %(replayable)s,
                        %(engagement_id)s, %(created_by_runtime)s, %(created_at)s
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING signal_id
                    """,
                    self._prep(
                        row,
                        json_fields={"source_ref_json", "payload_json", "artifacts_json"},
                        time_fields={"observed_at", "effective_at", "created_at"},
                    ),
                )
                inserted = cur.fetchone() is not None
            conn.commit()
        return inserted

    def fetch_signal(self, signal_id: str) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM mailbox_memory_signals WHERE signal_id = %(signal_id)s",
            {"signal_id": signal_id},
        )

    def patch_signal_engagement_id(self, signal_id: str, engagement_id: str) -> bool:
        sid = str(signal_id or "").strip()
        eid = str(engagement_id or "").strip()
        if not sid or not eid:
            return False
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mailbox_memory_signals
                    SET engagement_id = %(engagement_id)s
                    WHERE signal_id = %(signal_id)s
                    """,
                    {"signal_id": sid, "engagement_id": eid},
                )
                updated = cur.rowcount == 1
            conn.commit()
        return updated

    def fetch_signal_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._fetch_one(
            "SELECT * FROM mailbox_memory_signals WHERE idempotency_key = %(idempotency_key)s",
            {"idempotency_key": idempotency_key},
        )

    def fetch_signals_for_case(self, case_id: str = "", *, case_key_hint: str = "", limit: int = 200) -> list[dict[str, Any]]:
        where_clauses: list[str] = ["TRUE"]
        params: dict[str, Any] = {"limit": limit}
        if case_id:
            where_clauses.append("(payload_json ->> 'case_id') = %(case_id)s")
            params["case_id"] = case_id
        if case_key_hint:
            where_clauses.append("case_key_hint = %(case_key_hint)s")
            params["case_key_hint"] = case_key_hint
        return self._fetch_all(
            f"""
            SELECT * FROM mailbox_memory_signals
            WHERE {' AND '.join(where_clauses)}
            ORDER BY observed_at ASC NULLS LAST, created_at ASC
            LIMIT %(limit)s
            """,
            params,
        )

    def fetch_signals_for_source(self, source_kind: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_signals
            WHERE source_kind = %(source_kind)s
            ORDER BY observed_at ASC NULLS LAST, created_at ASC
            LIMIT %(limit)s
            """,
            {"source_kind": source_kind, "limit": limit},
        )

    def append_signal_processing_attempt(self, row: dict[str, Any]) -> None:
        self._upsert(
            """
            INSERT INTO mailbox_memory_signal_processing_attempts (
                attempt_id, signal_id, status, started_at, finished_at, error_text, details_json, created_at
            ) VALUES (
                %(attempt_id)s, %(signal_id)s, %(status)s, %(started_at)s, %(finished_at)s, %(error_text)s, %(details_json)s::jsonb, %(created_at)s
            )
            ON CONFLICT (attempt_id) DO NOTHING
            """,
            self._prep(
                row,
                json_fields={"details_json"},
                time_fields={"started_at", "finished_at", "created_at"},
            ),
        )

    def fetch_signal_processing_attempts(self, signal_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_signal_processing_attempts
            WHERE signal_id = %(signal_id)s
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """,
            {"signal_id": signal_id, "limit": limit},
        )

    def upsert_source_cursor(self, row: dict[str, Any]) -> None:
        payload = dict(row)
        payload.setdefault("last_cursor", "")
        payload.setdefault("last_error", "")
        payload.setdefault("status", "idle")
        payload.setdefault("metadata_json", payload.get("metadata") or {})
        self._upsert(
            """
            INSERT INTO mailbox_memory_source_cursors (
                cursor_key, source_kind, cursor_scope, last_cursor, last_success_at, last_error, status, metadata_json, updated_at
            ) VALUES (
                %(cursor_key)s, %(source_kind)s, %(cursor_scope)s, %(last_cursor)s, %(last_success_at)s, %(last_error)s, %(status)s, %(metadata_json)s::jsonb, %(updated_at)s
            )
            ON CONFLICT (cursor_key) DO UPDATE SET
                source_kind = EXCLUDED.source_kind,
                cursor_scope = EXCLUDED.cursor_scope,
                last_cursor = EXCLUDED.last_cursor,
                last_success_at = EXCLUDED.last_success_at,
                last_error = EXCLUDED.last_error,
                status = EXCLUDED.status,
                metadata_json = EXCLUDED.metadata_json,
                updated_at = EXCLUDED.updated_at
            """,
            self._prep(payload, json_fields={"metadata_json"}, time_fields={"last_success_at", "updated_at"}),
        )

    def fetch_source_cursor(self, source_kind: str, cursor_scope: str) -> dict[str, Any] | None:
        return self._fetch_one(
            """
            SELECT * FROM mailbox_memory_source_cursors
            WHERE cursor_key = %(cursor_key)s
            """,
            {"cursor_key": f"{source_kind}:{cursor_scope}"},
        )

    def list_source_cursors(self) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT * FROM mailbox_memory_source_cursors
            ORDER BY updated_at DESC
            """,
            {},
        )

    # -- DecisionRevision lineage (P1.1P durable state) --------------------

    def append_decision_revision(self, row: dict[str, Any]) -> None:
        """Persist one CAD revision row (idempotent per decision_version_id)."""
        payload = dict(row)
        payload.setdefault("semantic_payload", dict(row))
        payload.setdefault("supersedes_version_id", "")
        payload.setdefault("superseded_by_version_id", "")
        payload.setdefault("created_at", datetime.now().astimezone().isoformat())
        self._upsert(
            """
            INSERT INTO mailbox_memory_decision_revisions (
                decision_version_id, decision_id, revision, semantic_hash, revision_status,
                case_id, situation_version, semantic_payload, supersedes_version_id,
                superseded_by_version_id, proposal_id, created_at
            ) VALUES (
                %(decision_version_id)s, %(decision_id)s, %(revision)s, %(semantic_hash)s,
                %(revision_status)s, %(case_id)s, %(situation_version)s, %(semantic_payload)s::jsonb,
                %(supersedes_version_id)s, %(superseded_by_version_id)s, %(proposal_id)s, %(created_at)s
            )
            ON CONFLICT (decision_version_id) DO NOTHING
            """,
            self._prep(payload, json_fields={"semantic_payload"}, time_fields={"created_at"}),
        )

    def append_decision_revision_request(self, row: dict[str, Any]) -> None:
        """Upsert one revision request row (idempotent per request_id)."""
        payload = dict(row)
        payload.setdefault("reject_reason", "")
        now = datetime.now().astimezone().isoformat()
        payload.setdefault("requested_at", now)
        payload.setdefault("created_at", now)
        payload.setdefault("updated_at", now)
        self._upsert(
            """
            INSERT INTO mailbox_memory_decision_revision_requests (
                request_id, decision_id, current_revision, current_decision_version_id,
                reason_code, failed_precondition, source_layer, source_event_id,
                evidence_refs, status, reject_reason, requested_at, created_at, updated_at
            ) VALUES (
                %(request_id)s, %(decision_id)s, %(current_revision)s, %(current_decision_version_id)s,
                %(reason_code)s, %(failed_precondition)s, %(source_layer)s, %(source_event_id)s,
                %(evidence_refs)s::jsonb, %(status)s, %(reject_reason)s, %(requested_at)s,
                %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (request_id) DO UPDATE SET
                status = EXCLUDED.status,
                reject_reason = EXCLUDED.reject_reason,
                updated_at = EXCLUDED.updated_at
            """,
            self._prep(
                payload,
                json_fields={"evidence_refs"},
                time_fields={"requested_at", "created_at", "updated_at"},
            ),
        )

    def accept_decision_revision_transition(
        self, *, old_cad: dict[str, Any], new_cad: dict[str, Any], request: dict[str, Any]
    ) -> None:
        """Atomically persist old -> SUPERSEDED, new -> CURRENT, request -> ACCEPTED.

        One transaction + advisory lock per decision lineage; a crash cannot
        leave two CURRENT revisions in durable state.
        """
        old_version = str(old_cad.get("decision_version_id") or "").strip()
        new_version = str(new_cad.get("decision_version_id") or "").strip()
        request_id = str(request.get("request_id") or "").strip()
        decision_id = str(old_cad.get("decision_id") or "").strip()
        if not decision_id or not old_version or not new_version or not request_id:
            raise ValueError(
                "accept_decision_revision_transition requires decision_id, old/new version ids and request_id"
            )
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    self._acquire_owner_lock(cur, scope="decision_revision", owner_id=decision_id)
                    cur.execute(
                        """
                        UPDATE mailbox_memory_decision_revisions
                        SET revision_status = 'SUPERSEDED',
                            superseded_by_version_id = %(new_version)s
                        WHERE decision_version_id = %(old_version)s
                          AND revision_status = 'CURRENT'
                        """,
                        {"old_version": old_version, "new_version": new_version},
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(
                            f"decision_revision_conflict: old CAD {old_version} is not CURRENT"
                        )
                    new_row = dict(new_cad)
                    new_row["revision_status"] = "CURRENT"
                    new_row["supersedes_version_id"] = old_version
                    self._insert_decision_revision_on_cursor(cur, new_row)
                    cur.execute(
                        """
                        UPDATE mailbox_memory_decision_revision_requests
                        SET status = 'ACCEPTED', updated_at = NOW()
                        WHERE request_id = %(request_id)s
                        """,
                        {"request_id": request_id},
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _insert_decision_revision_on_cursor(self, cur: Any, cad: dict[str, Any]) -> None:
        row = dict(cad)
        row.setdefault("semantic_payload", dict(cad))
        row.setdefault("superseded_by_version_id", "")
        row.setdefault("created_at", datetime.now().astimezone().isoformat())
        prepared = self._prep(row, json_fields={"semantic_payload"}, time_fields={"created_at"})
        cur.execute(
            """
            INSERT INTO mailbox_memory_decision_revisions (
                decision_version_id, decision_id, revision, semantic_hash, revision_status,
                case_id, situation_version, semantic_payload, supersedes_version_id,
                superseded_by_version_id, proposal_id, created_at
            ) VALUES (
                %(decision_version_id)s, %(decision_id)s, %(revision)s, %(semantic_hash)s,
                %(revision_status)s, %(case_id)s, %(situation_version)s, %(semantic_payload)s::jsonb,
                %(supersedes_version_id)s, %(superseded_by_version_id)s, %(proposal_id)s, %(created_at)s
            )
            ON CONFLICT (decision_version_id) DO NOTHING
            """,
            prepared,
        )
        if int(cur.rowcount or 0) != 1:
            raise RuntimeError(
                f"decision_revision_conflict: version already exists: {row.get('decision_version_id')}"
            )

    def fetch_decision_revisions(self, decision_id: str) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            """
            SELECT * FROM mailbox_memory_decision_revisions
            WHERE decision_id = %(decision_id)s
            ORDER BY revision ASC
            """,
            {"decision_id": str(decision_id or "").strip()},
        )
        return [self._decision_revision_from_row(row) for row in rows]

    def _decision_revision_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = row.get("semantic_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        elif not isinstance(payload, dict):
            payload = {}
        payload = dict(payload or {})
        # Typed columns are authoritative for lineage-critical fields so the
        # projection rebuilds the exact durable status/supersession graph.
        payload["decision_id"] = str(row.get("decision_id") or "")
        payload["revision"] = int(row.get("revision") or 0)
        payload["decision_version_id"] = str(row.get("decision_version_id") or "")
        payload["semantic_hash"] = str(row.get("semantic_hash") or "")
        payload["revision_status"] = str(row.get("revision_status") or "CURRENT")
        payload["supersedes_version_id"] = str(row.get("supersedes_version_id") or "")
        payload["superseded_by_version_id"] = str(row.get("superseded_by_version_id") or "")
        return payload

    def fetch_decision_revision_requests(self, decision_id: str) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            """
            SELECT * FROM mailbox_memory_decision_revision_requests
            WHERE decision_id = %(decision_id)s
            ORDER BY created_at ASC
            """,
            {"decision_id": str(decision_id or "").strip()},
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            evidence = item.get("evidence_refs")
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except json.JSONDecodeError:
                    evidence = []
            item["evidence_refs"] = evidence if isinstance(evidence, list) else []
            out.append(item)
        return out

    def list_decision_lineage_ids(self) -> list[str]:
        rows = self._fetch_all(
            "SELECT DISTINCT decision_id FROM mailbox_memory_decision_revisions",
            {},
        )
        return [
            str(row.get("decision_id") or "")
            for row in rows
            if str(row.get("decision_id") or "").strip()
        ]

    def _upsert(self, sql: str, params: dict[str, Any]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()

    def _upsert_case_payload(self, payload: dict[str, Any], *, cur: Any | None = None) -> None:
        sql = """
            INSERT INTO mailbox_memory_cases (
                case_id, case_key, thread_id, case_family, mailbox, subject, status,
                customer_name, customer_email, latest_signal_id, latest_signal_at,
                last_rebuild_at, last_projection_refresh_at, last_source_kinds_seen,
                metadata, created_at, updated_at
            ) VALUES (
                %(case_id)s, %(case_key)s, %(thread_id)s, %(case_family)s, %(mailbox)s, %(subject)s, %(status)s,
                %(customer_name)s, %(customer_email)s, %(latest_signal_id)s, %(latest_signal_at)s,
                %(last_rebuild_at)s, %(last_projection_refresh_at)s, %(last_source_kinds_seen)s::jsonb,
                %(metadata)s::jsonb, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (case_id) DO UPDATE SET
                case_key = EXCLUDED.case_key,
                thread_id = EXCLUDED.thread_id,
                case_family = EXCLUDED.case_family,
                mailbox = EXCLUDED.mailbox,
                subject = EXCLUDED.subject,
                status = EXCLUDED.status,
                customer_name = EXCLUDED.customer_name,
                customer_email = EXCLUDED.customer_email,
                latest_signal_id = EXCLUDED.latest_signal_id,
                latest_signal_at = EXCLUDED.latest_signal_at,
                last_rebuild_at = EXCLUDED.last_rebuild_at,
                last_projection_refresh_at = EXCLUDED.last_projection_refresh_at,
                last_source_kinds_seen = EXCLUDED.last_source_kinds_seen,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at
        """
        prepared = self._prep(
            payload,
            json_fields={"metadata", "last_source_kinds_seen"},
            time_fields={"created_at", "updated_at", "latest_signal_at", "last_rebuild_at", "last_projection_refresh_at"},
        )
        if cur is not None:
            cur.execute(sql, prepared)
            return
        self._upsert(sql, prepared)

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
        return dict(row) if row else None

    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [dict(item) for item in rows]

    def _acquire_owner_lock(self, cur: Any, *, scope: str, owner_id: str) -> None:
        cur.execute(
            "SELECT pg_advisory_xact_lock(%(lock_key)s)",
            {"lock_key": _stable_advisory_lock_key(scope=scope, owner_id=owner_id)},
        )

    def _prep(
        self,
        row: dict[str, Any],
        *,
        json_fields: set[str] | None = None,
        time_fields: set[str] | None = None,
        vector_fields: set[str] | None = None,
    ) -> dict[str, Any]:
        payload = dict(row)
        for field in json_fields or set():
            payload[field] = _json_dump(payload.get(field))
        for field in time_fields or set():
            payload[field] = _coerce_iso(payload.get(field))
        for field in vector_fields or set():
            payload[field] = _vector_literal(payload.get(field))
        return payload

    def _connect(self, *, row_factory: bool = False):
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for mailbox-memory Postgres access.") from exc
        kwargs: dict[str, Any] = {"connect_timeout": POSTGRES_CONNECT_TIMEOUT_SEC}
        if row_factory:
            from psycopg.rows import dict_row  # type: ignore[import-not-found]

            kwargs["row_factory"] = dict_row
        return psycopg.connect(self.database_url, **kwargs)
