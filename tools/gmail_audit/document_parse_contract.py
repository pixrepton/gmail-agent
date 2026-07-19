"""Canonical contract for attachment/document parsing (structure-first, not regex-first)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Parsers that produce layout/table/key-value structure (regex document facts are skipped).
STRUCTURED_PARSER_IDS = frozenset({"docling", "unstructured", "legacy_structured"})

PARSER_CHAIN_DEFAULT = ("docling", "unstructured", "legacy")


@dataclass(slots=True)
class DocumentElement:
    """One logical unit from a professional parser (table row, labeled field, section)."""

    element_type: str
    text: str
    page: int = 1
    label: str = ""
    value: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DocumentParseResult:
    """Unified output of the document parse stack."""

    parser_id: str
    plain_text: str
    elements: list[DocumentElement] = field(default_factory=list)
    extraction_method: str = ""
    extraction_confidence: float = 0.0
    extraction_status: str = "pending"
    content_sha256_prefix: str = ""
    parser_provenance: str = ""
    structured: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_extraction_dict(self) -> dict[str, Any]:
        """Backward-compatible shape for callers expecting extract_attachment_text()."""
        return {
            "extracted_text": self.plain_text,
            "extraction_method": self.extraction_method or self.parser_id,
            "extraction_confidence": float(self.extraction_confidence or 0.0),
            "content_sha256_prefix": self.content_sha256_prefix,
            "extraction_status": self.extraction_status,
            "parser_provenance": self.parser_provenance or self.parser_id,
            "metadata": {
                **dict(self.metadata or {}),
                "parser_id": self.parser_id,
                "structured": bool(self.structured),
                "element_count": len(self.elements),
            },
        }


@dataclass(slots=True)
class DocumentParseConfig:
    """Runtime configuration for parse_document()."""

    parser_chain: tuple[str, ...] = PARSER_CHAIN_DEFAULT
    docling_enabled: bool = False
    unstructured_enabled: bool = False
    docling_options: dict[str, Any] = field(default_factory=dict)
    structured_facts_enabled: bool = True

    def resolved_chain(self) -> tuple[str, ...]:
        chain: list[str] = []
        for item in self.parser_chain:
            pid = str(item or "").strip().lower()
            if not pid or pid in chain:
                continue
            if pid == "docling" and not self.docling_enabled:
                continue
            if pid == "unstructured" and not self.unstructured_enabled:
                continue
            chain.append(pid)
        if "legacy" not in chain:
            chain.append("legacy")
        return tuple(chain)


def should_skip_regex_document_facts(
    result: DocumentParseResult,
    *,
    structured_facts_enabled: bool = True,
) -> bool:
    """When True, do not run PHONE_RE/CITY_HINT regex on document text_content."""
    if not structured_facts_enabled:
        return False
    if not result.plain_text.strip():
        return False
    if result.structured and result.parser_id in STRUCTURED_PARSER_IDS:
        return True
    if result.structured and result.parser_id == "legacy":
        return True
    return False


DOCUMENT_ELEMENT_TYPES = ("title", "table", "table_row", "key_value", "narrative", "list_item", "header", "footer")

__all__ = [
    "DOCUMENT_ELEMENT_TYPES",
    "DocumentElement",
    "DocumentParseConfig",
    "DocumentParseResult",
    "PARSER_CHAIN_DEFAULT",
    "STRUCTURED_PARSER_IDS",
    "should_skip_regex_document_facts",
]
