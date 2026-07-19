"""Typed Drive-ingest contracts for bounded Google Drive v1 runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


DriveLane = Literal[
    "formal_contracts",
    "commercial_transactions",
    "service_warranty",
    "offer_library",
    "commercial_pricing",
    "technical_reference",
    "case_folder",
    "media_marketing",
    "scans_intake",
    "cleanup_unknown",
]

DriveDocumentKind = Literal[
    "contract",
    "contract_template",
    "order",
    "deposit_invoice",
    "invoice",
    "warranty_card",
    "service_protocol",
    "offer_template",
    "offer_family_reference",
    "price_list",
    "pricing_workbook",
    "technical_reference",
    "media_bundle",
    "media_asset",
    "scan_backlog",
    "generic_document",
]

DriveScope = Literal["case_specific", "reference_template", "company_reference"]

DriveExtractionStatus = Literal[
    "pending",
    "extracted",
    "empty",
    "blocked",
    "failed",
    "skipped_folder",
    "skipped_binary",
]

DriveLinkageStatus = Literal[
    "deterministic",
    "inferred_high",
    "inferred_medium",
    "unresolved_candidate",
]


@dataclass(slots=True)
class DriveDocumentRecord:
    document_id: str
    drive_item_id: str
    title: str
    mime_type: str
    folder_path: str
    source_ref: str
    lane: DriveLane
    document_kind: DriveDocumentKind
    scope: DriveScope
    extraction_status: DriveExtractionStatus
    linkage_status: DriveLinkageStatus
    case_id: str = ""
    probable_case_key: str = ""
    classification_confidence: float = 0.0
    extraction_confidence: float = 0.0
    link_confidence: float = 0.0
    download_mime_type: str = ""
    content_sha256: str = ""
    blob_path: str = ""
    text_content: str = ""
    summary_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DriveIngestCandidate:
    drive_item_id: str
    title: str
    mime_type: str
    folder_path: str
    parent_drive_item_id: str = ""
    source_ref: str = ""
    is_folder: bool = False
    size_bytes: int = 0
    modified_time: str = ""
    lane: DriveLane = "cleanup_unknown"
    document_kind: DriveDocumentKind = "generic_document"
    scope: DriveScope = "company_reference"
    probable_case_key: str = ""
    classification_confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DriveCaseLinkCandidate:
    case_id: str
    case_key: str
    linkage_status: DriveLinkageStatus
    confidence: float
    reasons: list[str] = field(default_factory=list)
    matched_facts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DriveGraphUpsert:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DriveIngestResult:
    enabled: bool
    run_id: str = ""
    cursor: str = ""
    processed_count: int = 0
    stored_document_count: int = 0
    linked_case_count: int = 0
    graph_node_count: int = 0
    graph_edge_count: int = 0
    documents: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "DriveCaseLinkCandidate",
    "DriveDocumentKind",
    "DriveDocumentRecord",
    "DriveExtractionStatus",
    "DriveGraphUpsert",
    "DriveIngestCandidate",
    "DriveIngestResult",
    "DriveLane",
    "DriveLinkageStatus",
    "DriveScope",
]
