"""Unified Document Intelligence V1 contract for Gmail attachments and Drive files."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


DOCUMENT_TYPES = (
    "offer",
    "invoice",
    "protocol",
    "order",
    "datasheet",
    "installation_photo",
    "contract",
    "service_document",
    "irrelevant",
    "unknown",
)
FIELD_TYPES = ("date", "amount", "address", "company", "product", "serial", "person", "phone", "email", "generic")
CONFLICT_TYPES = ("mail_vs_attachment", "attachment_vs_attachment", "old_vs_new", "field_conflict")


@dataclass(slots=True)
class EvidenceRef:
    source_id: str
    page: int = 1
    chunk_id: str = ""
    excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExtractedField:
    field_name: str
    field_value: str
    field_type: str = "generic"
    confidence: float = 0.0
    evidence_ref: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DocumentConflict:
    conflict_type: str
    field_name: str
    values: list[dict[str, Any]] = field(default_factory=list)
    severity: str = "medium"
    requires_human_review: bool = True
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DocumentIntelligenceResult:
    document_id: str
    source_type: str
    source_id: str
    case_id: str = ""
    filename: str = ""
    mime_type: str = ""
    document_type: str = "unknown"
    document_type_confidence: float = 0.0
    summary: str = ""
    extracted_fields: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    parser: str = "fallback"
    parser_confidence: float = 0.0
    created_at: str = ""
    requires_human_review: bool = False
    not_proven_multimodal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


__all__ = [
    "CONFLICT_TYPES",
    "DOCUMENT_TYPES",
    "FIELD_TYPES",
    "DocumentConflict",
    "DocumentIntelligenceResult",
    "EvidenceRef",
    "ExtractedField",
    "now_iso",
]
