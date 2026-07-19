"""Canonical mailbox-memory schema DDL and helper utilities."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any

__all__ = [
    "MAILBOX_MEMORY_SCHEMA_SQL",
    "build_mailbox_memory_vector_schema_sql",
    "_coerce_iso",
    "_json_dump",
    "_case_payload_with_defaults",
    "_vector_literal",
    "_parse_vector_literal_coords",
    "_cosine_similarity",
    "_stable_advisory_lock_key",
]


MAILBOX_MEMORY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mailbox_memory_cases (
    case_id TEXT PRIMARY KEY,
    case_key TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    case_family TEXT NOT NULL DEFAULT 'unknown',
    mailbox TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    customer_name TEXT NOT NULL DEFAULT '',
    customer_email TEXT NOT NULL DEFAULT '',
    latest_signal_id TEXT NOT NULL DEFAULT '',
    latest_signal_at TIMESTAMPTZ,
    last_rebuild_at TIMESTAMPTZ,
    last_projection_refresh_at TIMESTAMPTZ,
    last_source_kinds_seen JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE mailbox_memory_cases ADD COLUMN IF NOT EXISTS latest_signal_id TEXT NOT NULL DEFAULT '';
ALTER TABLE mailbox_memory_cases ADD COLUMN IF NOT EXISTS latest_signal_at TIMESTAMPTZ;
ALTER TABLE mailbox_memory_cases ADD COLUMN IF NOT EXISTS last_rebuild_at TIMESTAMPTZ;
ALTER TABLE mailbox_memory_cases ADD COLUMN IF NOT EXISTS last_projection_refresh_at TIMESTAMPTZ;
ALTER TABLE mailbox_memory_cases ADD COLUMN IF NOT EXISTS last_source_kinds_seen JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS mailbox_memory_messages (
    message_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    mailbox TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    sender_email TEXT NOT NULL DEFAULT '',
    recipients JSONB NOT NULL DEFAULT '[]'::jsonb,
    subject TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    labels JSONB NOT NULL DEFAULT '[]'::jsonb,
    received_at TIMESTAMPTZ,
    raw_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_messages_case_id ON mailbox_memory_messages(case_id);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_messages_thread_id ON mailbox_memory_messages(thread_id);

CREATE TABLE IF NOT EXISTS mailbox_memory_attachments (
    attachment_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    size_bytes BIGINT NOT NULL DEFAULT 0,
    gmail_attachment_id TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL DEFAULT '',
    blob_path TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_attachments_message_id ON mailbox_memory_attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_attachments_case_id ON mailbox_memory_attachments(case_id);

CREATE TABLE IF NOT EXISTS mailbox_memory_documents (
    document_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    attachment_id TEXT NOT NULL DEFAULT '',
    parent_document_id TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'attachment',
    document_kind TEXT NOT NULL DEFAULT 'generic',
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    parser_name TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL DEFAULT '',
    blob_path TEXT NOT NULL DEFAULT '',
    text_content TEXT NOT NULL DEFAULT '',
    summary_text TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_documents_case_id ON mailbox_memory_documents(case_id);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_documents_attachment_id ON mailbox_memory_documents(attachment_id);

CREATE TABLE IF NOT EXISTS mailbox_memory_document_chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0,
    chunk_text TEXT NOT NULL DEFAULT '',
    token_estimate INTEGER NOT NULL DEFAULT 0,
    embedding_model TEXT NOT NULL DEFAULT '',
    embedding_status TEXT NOT NULL DEFAULT 'missing',
    embedding_updated_at TIMESTAMPTZ,
    embedding_error TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_document_chunks_case_id ON mailbox_memory_document_chunks(case_id);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_document_chunks_document_id ON mailbox_memory_document_chunks(document_id);
ALTER TABLE mailbox_memory_document_chunks ADD COLUMN IF NOT EXISTS embedding_model TEXT NOT NULL DEFAULT '';
ALTER TABLE mailbox_memory_document_chunks ADD COLUMN IF NOT EXISTS embedding_status TEXT NOT NULL DEFAULT 'missing';
ALTER TABLE mailbox_memory_document_chunks ADD COLUMN IF NOT EXISTS embedding_updated_at TIMESTAMPTZ;
ALTER TABLE mailbox_memory_document_chunks ADD COLUMN IF NOT EXISTS embedding_error TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_document_chunks_fts
    ON mailbox_memory_document_chunks USING GIN (to_tsvector('simple', chunk_text));

CREATE TABLE IF NOT EXISTS mailbox_memory_events (
    event_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    occurred_at TIMESTAMPTZ,
    summary_text TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_events_case_id ON mailbox_memory_events(case_id);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_events_occurred_at ON mailbox_memory_events(occurred_at DESC);

CREATE TABLE IF NOT EXISTS mailbox_memory_facts (
    fact_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    document_id TEXT NOT NULL DEFAULT '',
    entity_scope TEXT NOT NULL DEFAULT 'case',
    fact_key TEXT NOT NULL,
    normalized_value TEXT NOT NULL DEFAULT '',
    raw_value TEXT NOT NULL DEFAULT '',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    observed_at TIMESTAMPTZ,
    source_type TEXT NOT NULL DEFAULT 'message',
    source_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_facts_case_id ON mailbox_memory_facts(case_id);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_facts_lookup ON mailbox_memory_facts(case_id, entity_scope, fact_key);

CREATE TABLE IF NOT EXISTS mailbox_memory_snapshots (
    case_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'open',
    customer_name TEXT NOT NULL DEFAULT '',
    customer_email TEXT NOT NULL DEFAULT '',
    recommended_next_action TEXT NOT NULL DEFAULT '',
    snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mailbox_memory_next_actions (
    case_id TEXT PRIMARY KEY,
    next_action TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    source_stage TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mailbox_memory_case_snapshot_versions (
    snapshot_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_signal_id TEXT NOT NULL DEFAULT '',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_case_snapshot_versions_case_id
    ON mailbox_memory_case_snapshot_versions(case_id, version DESC);

CREATE TABLE IF NOT EXISTS company_drive_documents (
    document_id TEXT PRIMARY KEY,
    drive_item_id TEXT NOT NULL UNIQUE,
    parent_drive_item_id TEXT NOT NULL DEFAULT '',
    parent_document_id TEXT NOT NULL DEFAULT '',
    case_id TEXT NOT NULL DEFAULT '',
    probable_case_key TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    folder_path TEXT NOT NULL DEFAULT '',
    lane TEXT NOT NULL DEFAULT 'cleanup_unknown',
    document_kind TEXT NOT NULL DEFAULT 'generic_document',
    scope TEXT NOT NULL DEFAULT 'company_reference',
    source_ref TEXT NOT NULL DEFAULT '',
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    linkage_status TEXT NOT NULL DEFAULT 'unresolved_candidate',
    classification_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    extraction_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    link_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    download_mime_type TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL DEFAULT '',
    blob_path TEXT NOT NULL DEFAULT '',
    text_content TEXT NOT NULL DEFAULT '',
    summary_text TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_company_drive_documents_case_id ON company_drive_documents(case_id);
CREATE INDEX IF NOT EXISTS idx_company_drive_documents_scope ON company_drive_documents(scope);
CREATE INDEX IF NOT EXISTS idx_company_drive_documents_lane ON company_drive_documents(lane);

CREATE TABLE IF NOT EXISTS company_drive_document_chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    case_id TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL DEFAULT 0,
    chunk_text TEXT NOT NULL DEFAULT '',
    token_estimate INTEGER NOT NULL DEFAULT 0,
    embedding_model TEXT NOT NULL DEFAULT '',
    embedding_status TEXT NOT NULL DEFAULT 'missing',
    embedding_updated_at TIMESTAMPTZ,
    embedding_error TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_company_drive_document_chunks_case_id ON company_drive_document_chunks(case_id);
CREATE INDEX IF NOT EXISTS idx_company_drive_document_chunks_document_id ON company_drive_document_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_company_drive_document_chunks_fts
    ON company_drive_document_chunks USING GIN (to_tsvector('simple', chunk_text));

CREATE TABLE IF NOT EXISTS company_drive_facts (
    fact_id TEXT PRIMARY KEY,
    drive_document_id TEXT NOT NULL,
    case_id TEXT NOT NULL DEFAULT '',
    probable_case_key TEXT NOT NULL DEFAULT '',
    fact_family TEXT NOT NULL DEFAULT '',
    entity_scope TEXT NOT NULL DEFAULT 'document',
    fact_key TEXT NOT NULL,
    normalized_value TEXT NOT NULL DEFAULT '',
    raw_value TEXT NOT NULL DEFAULT '',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    observed_at TIMESTAMPTZ,
    source_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_company_drive_facts_case_id ON company_drive_facts(case_id);
CREATE INDEX IF NOT EXISTS idx_company_drive_facts_document_id ON company_drive_facts(drive_document_id);

CREATE TABLE IF NOT EXISTS drive_ingest_runs (
    run_id TEXT PRIMARY KEY,
    root_folder_id TEXT NOT NULL DEFAULT '',
    cursor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    stats JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mailbox_memory_raw_observations (
    observation_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL DEFAULT '1.0',
    observation_kind TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_ref_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ,
    source_fingerprint TEXT NOT NULL,
    payload_hash TEXT NOT NULL DEFAULT '',
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by_runtime TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mailbox_memory_raw_observations_source_fingerprint
    ON mailbox_memory_raw_observations(source_fingerprint);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_raw_observations_source_kind
    ON mailbox_memory_raw_observations(source_kind, observed_at DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS mailbox_memory_signals (
    signal_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL DEFAULT '1.0',
    signal_kind TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_ref_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    idempotency_key TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    case_key_hint TEXT NOT NULL DEFAULT '',
    thread_key_hint TEXT NOT NULL DEFAULT '',
    business_lane TEXT NOT NULL DEFAULT '',
    signal_summary_pl TEXT NOT NULL DEFAULT '',
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    artifacts_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    processing_state TEXT NOT NULL DEFAULT 'pending',
    replayable BOOLEAN NOT NULL DEFAULT TRUE,
    engagement_id TEXT NOT NULL DEFAULT '',
    created_by_runtime TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mailbox_memory_signals_idempotency_key
    ON mailbox_memory_signals(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_signals_case_key_hint
    ON mailbox_memory_signals(case_key_hint);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_signals_observed_at
    ON mailbox_memory_signals(observed_at DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS mailbox_memory_signal_processing_attempts (
    attempt_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error_text TEXT NOT NULL DEFAULT '',
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_signal_attempts_signal_id
    ON mailbox_memory_signal_processing_attempts(signal_id, created_at DESC);

CREATE TABLE IF NOT EXISTS mailbox_memory_source_cursors (
    cursor_key TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL DEFAULT '',
    cursor_scope TEXT NOT NULL DEFAULT '',
    last_cursor TEXT NOT NULL DEFAULT '',
    last_success_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idle',
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_source_cursors_source_kind
    ON mailbox_memory_source_cursors(source_kind, cursor_scope);

CREATE TABLE IF NOT EXISTS mailbox_memory_action_proposals (
    proposal_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    source_signal_id TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    proposed_by TEXT NOT NULL DEFAULT 'ai',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    risk_class TEXT NOT NULL DEFAULT 'R1',
    requires_review BOOLEAN NOT NULL DEFAULT TRUE,
    policy_basis JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'proposed',
    decision_reason TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TIMESTAMPTZ,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_action_proposals_case_id
    ON mailbox_memory_action_proposals(case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_action_proposals_status
    ON mailbox_memory_action_proposals(status, created_at DESC);

CREATE TABLE IF NOT EXISTS mailbox_memory_execution_results (
    execution_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    action_type TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TIMESTAMPTZ,
    executed_by TEXT NOT NULL DEFAULT '',
    executed_at TIMESTAMPTZ,
    execution_status TEXT NOT NULL DEFAULT 'skipped',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    result_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    audit_trace_id TEXT NOT NULL DEFAULT '',
    policy_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_execution_results_case_id
    ON mailbox_memory_execution_results(case_id, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_execution_results_proposal_id
    ON mailbox_memory_execution_results(proposal_id);

CREATE TABLE IF NOT EXISTS mailbox_memory_calendar_events (
    calendar_event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'google_calendar',
    summary TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    attendees JSONB NOT NULL DEFAULT '[]'::jsonb,
    organizer TEXT NOT NULL DEFAULT '',
    html_link TEXT NOT NULL DEFAULT '',
    recurring BOOLEAN NOT NULL DEFAULT FALSE,
    ingested_at TIMESTAMPTZ,
    visibility_scope TEXT NOT NULL DEFAULT '',
    case_id TEXT NOT NULL DEFAULT '',
    link_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_calendar_events_case_id
    ON mailbox_memory_calendar_events(case_id, start_at ASC);

CREATE TABLE IF NOT EXISTS mailbox_memory_calendar_case_links (
    calendar_event_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    link_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    match_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ,
    PRIMARY KEY (calendar_event_id, case_id)
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_calendar_case_links_case_id
    ON mailbox_memory_calendar_case_links(case_id);

CREATE TABLE IF NOT EXISTS mailbox_memory_document_intelligence_results (
    document_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    case_id TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    document_type TEXT NOT NULL DEFAULT 'unknown',
    document_type_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    summary TEXT NOT NULL DEFAULT '',
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflicts JSONB NOT NULL DEFAULT '[]'::jsonb,
    parser TEXT NOT NULL DEFAULT 'fallback',
    parser_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at TIMESTAMPTZ,
    requires_human_review BOOLEAN NOT NULL DEFAULT FALSE,
    not_proven_multimodal BOOLEAN NOT NULL DEFAULT FALSE,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_document_intelligence_results_case_id
    ON mailbox_memory_document_intelligence_results(case_id, created_at DESC);

CREATE TABLE IF NOT EXISTS mailbox_memory_document_extracted_fields (
    document_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    field_value TEXT NOT NULL DEFAULT '',
    field_type TEXT NOT NULL DEFAULT 'generic',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    evidence_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (document_id, field_name, field_value)
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_document_extracted_fields_document_id
    ON mailbox_memory_document_extracted_fields(document_id);

CREATE TABLE IF NOT EXISTS mailbox_memory_document_conflicts (
    conflict_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL DEFAULT '',
    document_id TEXT NOT NULL DEFAULT '',
    conflict_type TEXT NOT NULL DEFAULT 'field_conflict',
    field_name TEXT NOT NULL DEFAULT '',
    values_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    severity TEXT NOT NULL DEFAULT 'medium',
    requires_human_review BOOLEAN NOT NULL DEFAULT TRUE,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mailbox_memory_document_conflicts_case_id
    ON mailbox_memory_document_conflicts(case_id, created_at DESC);
"""


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
