"""Business-level document intelligence V1.

This layer consumes parser-first text/metadata and produces typed document
understanding with provenance. It does not replace attachment extraction.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from document_intelligence_contract import (
    DocumentConflict,
    DocumentIntelligenceResult,
    EvidenceRef,
    ExtractedField,
    now_iso,
)

DATE_PATTERN = r"(?:[0-9]{4}[.\-/][0-9]{1,2}[.\-/][0-9]{1,2}|[0-9]{1,2}[.\-/][0-9]{1,2}[.\-/][0-9]{2,4})"

PROMOTABLE_FIELDS_BY_DOCUMENT_TYPE = {
    "invoice": {"invoice_number", "amount_total", "due_date", "issue_date", "nip"},
    "offer": {"vendor", "price", "product_model", "validity_date"},
    "protocol": {"address", "device_model", "service_date"},
    "service_document": {"address", "device_model", "service_date"},
}


def build_document_intelligence_result(
    *,
    source_type: str,
    source_id: str,
    case_id: str = "",
    filename: str = "",
    mime_type: str = "",
    text: str = "",
    parser: str = "fallback",
    parser_confidence: float = 0.0,
    pre_extracted_fields: list[dict[str, Any]] | None = None,
) -> DocumentIntelligenceResult:
    document_id = stable_document_id(source_type, source_id, filename)
    doc_type, type_conf = classify_document(filename=filename, mime_type=mime_type, text=text)
    if pre_extracted_fields:
        fields = [dict(item) for item in pre_extracted_fields if isinstance(item, dict)]
    else:
        fields = extract_fields_for_type(
            document_type=doc_type,
            source_id=source_id or document_id,
            filename=filename,
            mime_type=mime_type,
            text=text,
        )
    requires_review = doc_type in {"unknown", "installation_photo"} or not fields
    not_proven_multimodal = doc_type == "installation_photo" and not text.strip()
    summary = summarize_document(doc_type, filename=filename, fields=fields, text=text)
    evidence_refs = [field["evidence_ref"] for field in fields if isinstance(field.get("evidence_ref"), dict)]
    return DocumentIntelligenceResult(
        document_id=document_id,
        source_type=source_type,
        source_id=source_id,
        case_id=case_id,
        filename=filename,
        mime_type=mime_type,
        document_type=doc_type,
        document_type_confidence=type_conf,
        summary=summary,
        extracted_fields=fields,
        evidence_refs=evidence_refs,
        conflicts=[],
        parser=parser,
        parser_confidence=parser_confidence,
        created_at=now_iso(),
        requires_human_review=requires_review,
        not_proven_multimodal=not_proven_multimodal,
    )


def stable_document_id(source_type: str, source_id: str, filename: str) -> str:
    raw = f"{source_type}:{source_id}:{filename}".encode("utf-8")
    return "docintel_" + hashlib.sha256(raw).hexdigest()[:24]


def classify_document(*, filename: str, mime_type: str, text: str) -> tuple[str, float]:
    name = filename.lower()
    body = text.lower()
    if any(tok in name or tok in body for tok in ("faktura", "invoice", "fv/", "vat")):
        return "invoice", 0.82
    if any(tok in name or tok in body for tok in ("oferta", "offer", "wycena", "quotation")):
        return "offer", 0.78
    if any(tok in name or tok in body for tok in ("protokol", "protokół", "protocol", "odbioru", "serwis")):
        return "protocol", 0.74
    if any(tok in name or tok in body for tok in ("zamowienie", "zamówienie", "order")):
        return "order", 0.72
    if any(tok in name or tok in body for tok in ("datasheet", "karta katalogowa", "specyfikacja", "manual")):
        return "datasheet", 0.74
    if any(tok in name or tok in body for tok in ("umowa", "contract")):
        return "contract", 0.76
    if any(tok in name or tok in body for tok in ("serwis", "service", "naprawa", "przeglad", "przegląd")):
        return "service_document", 0.7
    if str(mime_type or "").startswith("image/") or any(tok in name for tok in ("montaz", "montaż", "instalacja", "photo", "zdjecie", "zdjęcie")):
        return "installation_photo", 0.55
    if any(tok in name for tok in ("logo", "stopka", "signature")):
        return "irrelevant", 0.65
    return "unknown", 0.35


def extract_fields_for_type(*, document_type: str, source_id: str, filename: str, mime_type: str, text: str) -> list[dict[str, Any]]:
    if document_type == "invoice":
        specs = [
            ("invoice_number", r"(?:faktura|invoice|fv)\s*(?:nr|no\.?)?\s*[:#]?\s*([A-Z0-9\/\-]{3,})", "generic", 0.75),
            ("nip", r"\bNIP[:\s-]*([0-9\-\s]{10,})", "company", 0.75),
            ("amount_total", r"(?:razem|total|do zaplaty|do zapłaty)\s*[:\s]*([0-9\s,.]+)\s*(?:PLN|zł|zl)?", "amount", 0.7),
            ("due_date", r"(?:termin platnosci|termin płatności|due date)\s*[:\s]*([0-9]{1,2}[.\-/][0-9]{1,2}[.\-/][0-9]{2,4})", "date", 0.68),
            ("issue_date", r"(?:data wystawienia|issue date)\s*[:\s]*([0-9]{1,2}[.\-/][0-9]{1,2}[.\-/][0-9]{2,4})", "date", 0.68),
        ]
    elif document_type == "offer":
        specs = [
            ("price", r"(?:cena|wartosc|wartość|price)\s*[:\s]*([0-9\s,.]+)\s*(?:PLN|zł|zl)?", "amount", 0.65),
            ("product_model", r"(?:model|typ)\s*[:\s]*([A-Z0-9][A-Z0-9 .\/\-]{2,40})", "product", 0.62),
            ("validity_date", r"(?:wazna do|ważna do|valid until)\s*[:\s]*([0-9]{1,2}[.\-/][0-9]{1,2}[.\-/][0-9]{2,4})", "date", 0.62),
            ("scope_of_work", r"(?:zakres prac|scope)\s*[:\s]*(.{10,180})", "generic", 0.55),
            ("terms", r"(?:warunki|terms)\s*[:\s]*(.{10,180})", "generic", 0.55),
        ]
    elif document_type in {"protocol", "service_document"}:
        specs = [
            ("address", r"(?:adres|lokalizacja)\s*[:\s]*(.{8,120})", "address", 0.65),
            ("device_model", r"(?:model|urzadzenie|urządzenie)\s*[:\s]*([A-Z0-9][A-Z0-9 .\/\-]{2,50})", "product", 0.62),
            ("service_date", r"(?:data|service date)\s*[:\s]*([0-9]{1,2}[.\-/][0-9]{1,2}[.\-/][0-9]{2,4})", "date", 0.6),
            ("work_description", r"(?:opis prac|wykonano|work description)\s*[:\s]*(.{10,200})", "generic", 0.55),
            ("recommendations", r"(?:zalecenia|recommendations)\s*[:\s]*(.{10,200})", "generic", 0.55),
        ]
    elif document_type == "installation_photo":
        value = "true" if str(mime_type or "").startswith("image/") else "unknown"
        return [
            ExtractedField(
                field_name="is_installation_photo",
                field_value=value,
                field_type="generic",
                confidence=0.45,
                evidence_ref=EvidenceRef(source_id=source_id, excerpt=filename[:180]).to_dict(),
            ).to_dict(),
            ExtractedField(
                field_name="requires_human_review",
                field_value="true",
                field_type="generic",
                confidence=1.0,
                evidence_ref=EvidenceRef(source_id=source_id, excerpt="metadata/filename fallback; multimodal not proven").to_dict(),
            ).to_dict(),
        ]
    else:
        specs = []
    return [_field_from_regex(name, pattern, field_type, confidence, source_id, text) for name, pattern, field_type, confidence in specs if _field_from_regex(name, pattern, field_type, confidence, source_id, text)]


def extract_fields_for_type(*, document_type: str, source_id: str, filename: str, mime_type: str, text: str) -> list[dict[str, Any]]:
    # V1 quality override for business fields. The legacy extractor above is kept
    # untouched to avoid broad churn in a mojibake-heavy block.
    if document_type == "invoice":
        specs = [
            (
                "invoice_number",
                r"(?:faktura(?:\s+vat)?|invoice)\s*(?:nr|no\.?|number)?\s*[:#]?\s*((?:FV|FA|FS|INV)[A-Z0-9\/\-.]*|[0-9][A-Z0-9\/\-.]*\/[A-Z0-9\/\-.]+)",
                "generic",
                0.78,
            ),
            ("contractor", r"(?:sprzedawca|seller|contractor|wykonawca)\s*[:\s]*([^\n\r]{3,120})", "company", 0.68),
            ("nip", r"\bNIP[:\s-]*([0-9\-\s]{10,})", "company", 0.75),
            ("amount_total", r"(?:razem(?:\s+do\s+zaplaty|\s+do\s+zapłaty)?|total|do zaplaty|do zapłaty)\s*[:\s]*([0-9\s,.]+)\s*(?:PLN|zł|zl)?", "amount", 0.7),
            ("due_date", rf"(?:termin platnosci|termin płatności|due date)\s*[:\s]*({DATE_PATTERN})", "date", 0.68),
            ("issue_date", rf"(?:data wystawienia|issue date)\s*[:\s]*({DATE_PATTERN})", "date", 0.68),
        ]
    elif document_type == "offer":
        specs = [
            ("vendor", r"(?:vendor|sprzedawca|wykonawca|oferent)\s*[:\s]*([^\n\r]{3,120})", "company", 0.62),
            ("price", r"(?:cena|wartosc|wartość|price)\s*[:\s]*([0-9\s,.]+)\s*(?:PLN|zł|zl)?", "amount", 0.65),
            ("product_model", r"(?:model|typ)\s*[:\s]*([A-Z0-9][A-Z0-9 .\/\-]{2,40})", "product", 0.62),
            ("validity_date", rf"(?:wazna do|ważna do|valid until)\s*[:\s]*({DATE_PATTERN})", "date", 0.62),
            ("scope_of_work", r"(?:zakres prac|scope)\s*[:\s]*([^\n\r]{10,180})", "generic", 0.55),
            ("terms", r"(?:warunki|terms)\s*[:\s]*([^\n\r]{10,180})", "generic", 0.55),
        ]
    elif document_type in {"protocol", "service_document"}:
        specs = [
            ("address", r"(?:adres|lokalizacja)\s*[:\s]*([^\n\r]{8,120})", "address", 0.65),
            ("device_model", r"(?:model|urzadzenie|urządzenie)\s*[:\s]*([A-Z0-9][A-Z0-9 .\/\-]{2,50})", "product", 0.62),
            ("service_date", rf"(?:data serwisu|data|service date)\s*[:\s]*({DATE_PATTERN})", "date", 0.6),
            ("work_description", r"(?:opis prac|wykonano|work description)\s*[:\s]*([^\n\r]{10,200})", "generic", 0.55),
            ("recommendations", r"(?:zalecenia|recommendations)\s*[:\s]*([^\n\r]{10,200})", "generic", 0.55),
        ]
    elif document_type == "installation_photo":
        value = "true" if str(mime_type or "").startswith("image/") else "unknown"
        return [
            ExtractedField(
                field_name="is_installation_photo",
                field_value=value,
                field_type="generic",
                confidence=0.45,
                evidence_ref=EvidenceRef(source_id=source_id, excerpt=filename[:180]).to_dict(),
            ).to_dict(),
            ExtractedField(
                field_name="requires_human_review",
                field_value="true",
                field_type="generic",
                confidence=1.0,
                evidence_ref=EvidenceRef(source_id=source_id, excerpt="metadata/filename fallback; multimodal not proven").to_dict(),
            ).to_dict(),
        ]
    else:
        specs = []
    fields: list[dict[str, Any]] = []
    for name, pattern, field_type, confidence in specs:
        field = _field_from_regex(name, pattern, field_type, confidence, source_id, text)
        if field:
            fields.append(field)
    return fields


def detect_document_conflicts(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_field: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        for field in result.get("extracted_fields") or []:
            key = str(field.get("field_name") or "")
            value = str(field.get("field_value") or "").strip().lower()
            if not key or not value:
                continue
            by_field.setdefault(key, []).append({"document_id": result.get("document_id"), "value": value, "evidence_ref": field.get("evidence_ref")})
    conflicts: list[dict[str, Any]] = []
    for field_name, values in by_field.items():
        unique = {item["value"] for item in values}
        if len(unique) <= 1:
            continue
        conflicts.append(
            DocumentConflict(
                conflict_type="attachment_vs_attachment",
                field_name=field_name,
                values=values,
                severity="medium",
                evidence_refs=[dict(item.get("evidence_ref") or {}) for item in values],
            ).to_dict()
        )
    return conflicts


def document_fields_to_fact_rows(result: dict[str, Any], *, min_confidence: float = 0.78) -> list[dict[str, Any]]:
    """Promote high-confidence document fields to tentative mailbox facts with provenance."""
    if not isinstance(result, dict):
        return []
    case_id = str(result.get("case_id") or "").strip()
    document_id = str(result.get("document_id") or "").strip()
    document_type = str(result.get("document_type") or "").strip()
    if not case_id or not document_id:
        return []
    allowed = PROMOTABLE_FIELDS_BY_DOCUMENT_TYPE.get(document_type, set())
    conflicts = result.get("conflicts") if isinstance(result.get("conflicts"), list) else []
    conflict_fields = {
        str(item.get("field_name") or "")
        for item in conflicts
        if isinstance(item, dict) and str(item.get("field_name") or "")
    }
    rows: list[dict[str, Any]] = []
    for field in result.get("extracted_fields") or []:
        if not isinstance(field, dict):
            continue
        field_name = str(field.get("field_name") or "").strip()
        if field_name not in allowed:
            continue
        evidence_ref = field.get("evidence_ref") if isinstance(field.get("evidence_ref"), dict) else {}
        if not evidence_ref:
            continue
        try:
            confidence = float(field.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < min_confidence:
            continue
        value = str(field.get("field_value") or "").strip()
        if not value:
            continue
        source_ref = _evidence_source_ref(evidence_ref)
        fact_id = _document_fact_id(document_id=document_id, field_name=field_name, value=value, source_ref=source_ref)
        rows.append(
            {
                "fact_id": fact_id,
                "case_id": case_id,
                "message_id": "",
                "document_id": document_id,
                "entity_scope": "document",
                "fact_key": field_name,
                "normalized_value": value.lower(),
                "raw_value": value,
                "confidence": max(0.0, min(1.0, confidence)),
                "observed_at": str(result.get("created_at") or now_iso()),
                "source_type": "document_intelligence",
                "source_ref": source_ref,
                "status": "active",
                "metadata": {
                    "document_type": document_type,
                    "tentative": confidence < 0.9 or field_name in conflict_fields or bool(conflicts),
                    "evidence_ref": evidence_ref,
                },
            }
        )
    return rows


def superseded_facts_audit(facts: list[dict[str, Any]], *, limit: int = 24) -> list[dict[str, Any]]:
    """Read-only operator audit trail for superseded mailbox facts (not a second SoT)."""
    rows = [dict(item) for item in facts if isinstance(item, dict) and str(item.get("status") or "") == "superseded"]
    rows.sort(key=lambda item: str(item.get("observed_at") or ""), reverse=True)
    out: list[dict[str, Any]] = []
    for item in rows[: max(0, int(limit))]:
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        out.append(
            {
                "fact_id": str(item.get("fact_id") or ""),
                "case_id": str(item.get("case_id") or ""),
                "document_id": str(item.get("document_id") or ""),
                "entity_scope": str(item.get("entity_scope") or ""),
                "fact_key": str(item.get("fact_key") or ""),
                "normalized_value": str(item.get("normalized_value") or ""),
                "raw_value": str(item.get("raw_value") or ""),
                "observed_at": str(item.get("observed_at") or ""),
                "source_type": str(item.get("source_type") or ""),
                "source_ref": str(item.get("source_ref") or ""),
                "status": "superseded",
                "metadata": {
                    "evidence_ref": meta.get("evidence_ref") if isinstance(meta.get("evidence_ref"), dict) else {},
                    "superseded_at": meta.get("superseded_at"),
                    "superseded_by_fact_id": meta.get("superseded_by_fact_id"),
                },
            }
        )
    return out


def promote_document_fact_rows(store: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist document-derived fact rows via supersession API; return write stats + projection."""
    empty = {
        "rows": [],
        "write_stats": {"inserted": 0, "superseded": 0, "unchanged": 0},
        "active_facts": [],
        "conflicting_facts": [],
        "superseded_facts": [],
    }
    prepared = [dict(item) for item in rows if isinstance(item, dict)]
    if not prepared:
        return empty

    write_stats = {"inserted": 0, "superseded": 0, "unchanged": 0}
    append_super = getattr(store, "append_facts_with_supersession", None)
    if callable(append_super):
        raw_stats = append_super(prepared) or {}
        write_stats = {
            "inserted": int(raw_stats.get("inserted") or 0),
            "superseded": int(raw_stats.get("superseded") or 0),
            "unchanged": int(raw_stats.get("unchanged") or 0),
        }
    elif hasattr(store, "append_fact_rows"):
        store.append_fact_rows(prepared)
        write_stats = {"inserted": len(prepared), "superseded": 0, "unchanged": 0}
    else:
        raise AttributeError("store lacks append_facts_with_supersession / append_fact_rows")

    case_ids = {str(item.get("case_id") or "").strip() for item in prepared if str(item.get("case_id") or "").strip()}
    case_id = next(iter(sorted(case_ids)), "")
    all_facts: list[dict[str, Any]] = []
    if case_id and hasattr(store, "fetch_facts_for_case"):
        all_facts = list(store.fetch_facts_for_case(case_id) or [])

    # Lazy import avoids circular import with mailbox_memory_runtime consumers.
    from mailbox_memory_runtime import split_conflicting_facts

    active_facts, conflicting_facts = split_conflicting_facts(all_facts)
    return {
        "rows": prepared,
        "write_stats": write_stats,
        "active_facts": active_facts,
        "conflicting_facts": conflicting_facts,
        "superseded_facts": superseded_facts_audit(all_facts),
    }


def promote_document_intelligence_facts(
    store: Any,
    result: dict[str, Any],
    *,
    min_confidence: float = 0.78,
    fact_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract (or accept) document fact rows and append them via supersession."""
    rows = list(fact_rows) if fact_rows is not None else document_fields_to_fact_rows(result, min_confidence=min_confidence)
    return promote_document_fact_rows(store, rows)


def _evidence_source_ref(evidence_ref: dict[str, Any]) -> str:
    parts = [
        str(evidence_ref.get("source_id") or ""),
        str(evidence_ref.get("page") or ""),
        str(evidence_ref.get("chunk_id") or ""),
    ]
    return "document_intelligence:" + ":".join(part for part in parts if part)


def _document_fact_id(*, document_id: str, field_name: str, value: str, source_ref: str) -> str:
    raw = f"{document_id}:{field_name}:{value}:{source_ref}".encode("utf-8")
    return "docfact_" + hashlib.sha256(raw).hexdigest()[:24]


def summarize_document(document_type: str, *, filename: str, fields: list[dict[str, Any]], text: str) -> str:
    names = ", ".join(str(field.get("field_name") or "") for field in fields[:4] if field.get("field_name"))
    if names:
        return f"{filename}: {document_type}, extracted: {names}"
    snippet = " ".join(str(text or "").split())[:180]
    return f"{filename}: {document_type}" + (f" - {snippet}" if snippet else "")


def _field_from_regex(name: str, pattern: str, field_type: str, confidence: float, source_id: str, text: str) -> dict[str, Any] | None:
    match = re.search(pattern, text or "", flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    value = " ".join(str(match.group(1) or "").split())
    if not value:
        return None
    start = max(0, match.start() - 60)
    end = min(len(text), match.end() + 60)
    return ExtractedField(
        field_name=name,
        field_value=value[:300],
        field_type=field_type,
        confidence=confidence,
        evidence_ref=EvidenceRef(source_id=source_id, page=1, excerpt=(text[start:end] or value)[:500]).to_dict(),
    ).to_dict()


__all__ = [
    "build_document_intelligence_result",
    "classify_document",
    "detect_document_conflicts",
    "document_fields_to_fact_rows",
    "extract_fields_for_type",
    "promote_document_fact_rows",
    "promote_document_intelligence_facts",
    "stable_document_id",
    "superseded_facts_audit",
]
