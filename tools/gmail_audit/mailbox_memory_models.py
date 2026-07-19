"""Typed mailbox-memory and signal-runtime view models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class CaseContextPack:
    """Structured-first context bundle for one case."""

    case_id: str
    snapshot: dict[str, Any] = field(default_factory=dict)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    active_facts: list[dict[str, Any]] = field(default_factory=list)
    conflicting_facts: list[dict[str, Any]] = field(default_factory=list)
    latest_documents: list[dict[str, Any]] = field(default_factory=list)
    drive_documents_summary: list[dict[str, Any]] = field(default_factory=list)
    completeness_gaps: list[Any] = field(default_factory=list)
    graph_hints: list[dict[str, Any]] = field(default_factory=list)
    reference_documents: list[dict[str, Any]] = field(default_factory=list)
    relevant_chunks: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    next_action: dict[str, Any] = field(default_factory=dict)
    action_proposals: list[dict[str, Any]] = field(default_factory=list)
    execution_results: list[dict[str, Any]] = field(default_factory=list)
    calendar: dict[str, Any] = field(default_factory=dict)
    document_intelligence: dict[str, Any] = field(default_factory=dict)
    runtime_state: dict[str, Any] = field(default_factory=dict)
    neo4j_pilot: dict[str, Any] | None = None
    vector_retrieval: dict[str, Any] = field(
        default_factory=lambda: {
            "vector_path_status": "vector_path_disabled",
            "detail": "default",
            "semantic_candidate_count": 0,
            "embedding_error": "",
            "semantic_error": "",
        }
    )
    precedent_evidence_refs: list[dict[str, Any]] = field(default_factory=list)

    # I2: Case Coherence Validator — warnings o niespójnościach faktów
    coherence_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if payload.get("neo4j_pilot") is None:
            payload.pop("neo4j_pilot", None)
        return payload


@dataclass(slots=True)
class MailboxMemoryIngestResult:
    """Operational result returned after mailbox-memory ingest/finalize."""

    enabled: bool
    case_id: str = ""
    message_id: str = ""
    snapshot: dict[str, Any] = field(default_factory=dict)
    context_pack: CaseContextPack | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    next_action: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.context_pack is not None:
            payload["context_pack"] = self.context_pack.to_dict()
        return payload


@dataclass(slots=True)
class SignalJournalEntry:
    """Durable append-only signal row."""

    signal_id: str
    schema_version: str
    signal_kind: str
    source_kind: str
    source_ref_json: dict[str, Any] = field(default_factory=dict)
    observed_at: str = ""
    effective_at: str = ""
    idempotency_key: str = ""
    content_hash: str = ""
    case_key_hint: str = ""
    thread_key_hint: str = ""
    business_lane: str = ""
    signal_summary_pl: str = ""
    payload_json: dict[str, Any] = field(default_factory=dict)
    artifacts_json: dict[str, Any] = field(default_factory=dict)
    processing_state: str = "pending"
    replayable: bool = True
    created_by_runtime: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SourceCursorRecord:
    """Durable source cursor/checkpoint for polling detectors."""

    cursor_key: str
    source_kind: str
    cursor_scope: str
    last_cursor: str = ""
    last_success_at: str = ""
    last_error: str = ""
    status: str = "idle"
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "CaseContextPack",
    "MailboxMemoryIngestResult",
    "SignalJournalEntry",
    "SourceCursorRecord",
]
