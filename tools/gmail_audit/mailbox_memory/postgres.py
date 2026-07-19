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
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._acquire_owner_lock(cur, scope="message_facts", owner_id=message_id)
                cur.execute("DELETE FROM mailbox_memory_facts WHERE message_id = %(message_id)s", {"message_id": message_id})
                if rows:
                    prepared = [self._prep(item, json_fields={"metadata"}, time_fields={"observed_at"}) for item in rows]
                    cur.executemany(
                        """
                        INSERT INTO mailbox_memory_facts (
                            fact_id, case_id, message_id, document_id, entity_scope, fact_key, normalized_value, raw_value,
                            confidence, observed_at, source_type, source_ref, status, metadata
                        ) VALUES (
                            %(fact_id)s, %(case_id)s, %(message_id)s, %(document_id)s, %(entity_scope)s, %(fact_key)s, %(normalized_value)s, %(raw_value)s,
                            %(confidence)s, %(observed_at)s, %(source_type)s, %(source_ref)s, %(status)s, %(metadata)s::jsonb
                        )
                        ON CONFLICT (fact_id) DO UPDATE SET
                            case_id = EXCLUDED.case_id,
                            message_id = EXCLUDED.message_id,
                            document_id = EXCLUDED.document_id,
                            entity_scope = EXCLUDED.entity_scope,
                            fact_key = EXCLUDED.fact_key,
                            normalized_value = EXCLUDED.normalized_value,
                            raw_value = EXCLUDED.raw_value,
                            confidence = EXCLUDED.confidence,
                            observed_at = EXCLUDED.observed_at,
                            source_type = EXCLUDED.source_type,
                            source_ref = EXCLUDED.source_ref,
                            status = EXCLUDED.status,
                            metadata = EXCLUDED.metadata
                        """,
                        prepared,
                    )
            conn.commit()

    def append_fact_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                prepared = [self._prep(item, json_fields={"metadata"}, time_fields={"observed_at"}) for item in rows]
                cur.executemany(
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
                    prepared,
                )
            conn.commit()
        log.debug("append_fact_rows rows=%s case_id=%s", len(rows), rows[0].get("case_id", "unknown") if rows else "none")

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
