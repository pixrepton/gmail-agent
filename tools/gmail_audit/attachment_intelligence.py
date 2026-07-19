"""Attachment Intelligence: first-class operational evidence from message attachments."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

from attachment_content_extraction import extract_attachment_text, summarize_extracted_text_for_operator
from document_intelligence_runtime import build_document_intelligence_result, detect_document_conflicts


ATTACHMENT_BUSINESS_TYPES = (
    "technical_photo",
    "technical_pdf",
    "product_datasheet",
    "competitor_offer",
    "invoice",
    "delivery_confirmation",
    "project_document",
    "screenshot",
    "reference_document",
    "unknown",
)

MIME_TYPE_HINTS: dict[str, str] = {
    "application/pdf": "technical_pdf",
    "application/zip": "project_document",
    "application/x-zip-compressed": "project_document",
    "image/jpeg": "technical_photo",
    "image/png": "screenshot",
    "image/heic": "technical_photo",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "project_document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "project_document",
    "application/vnd.ms-excel": "project_document",
    "application/msword": "project_document",
}

FILENAME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"faktur|invoice|fv[_\-\s]", re.IGNORECASE), "invoice"),
    (re.compile(r"oferta|offer|propozycj", re.IGNORECASE), "competitor_offer"),
    (re.compile(r"dostaw|delivery|potwierdz.*odbioru|confirmation", re.IGNORECASE), "delivery_confirmation"),
    (re.compile(r"karta.*katalog|datasheet|specyfikacj|technical", re.IGNORECASE), "product_datasheet"),
    (re.compile(r"projekt|obliczen|calculation|design", re.IGNORECASE), "project_document"),
    (re.compile(r"zdj[eę]ci|photo|img_|dsc_|kotłown|instalacj", re.IGNORECASE), "technical_photo"),
    (re.compile(r"screen|zrzut|screenshot|snap", re.IGNORECASE), "screenshot"),
]


def build_attachment_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract canonical attachment records from a source snapshot."""
    source_message = snapshot.get("source_message") or {}
    message_id = str(source_message.get("message_id") or "").strip()
    thread_id = str(source_message.get("thread_id") or "").strip()
    raw_obj = source_message.get("raw") or {}
    raw_attachments = raw_obj.get("attachments") if isinstance(raw_obj, dict) else []
    if not isinstance(raw_attachments, list):
        raw_attachments = []
    attachment_names = list(source_message.get("attachment_names") or [])
    has_attachments = bool(source_message.get("has_attachments"))

    raw_source = snapshot.get("source_message") or {}
    raw_raw = raw_source.get("raw") or {}
    if isinstance(raw_raw, dict):
        raw_raw_attachments = raw_raw.get("attachments") or []
    else:
        raw_raw_attachments = []
    all_raw_attachments = raw_attachments or raw_raw_attachments
    if not isinstance(all_raw_attachments, list):
        all_raw_attachments = []

    part_list = raw_source.get("attachment_parts")
    structured_parts: list[dict[str, Any]] = []
    if isinstance(part_list, list):
        for item in part_list:
            if isinstance(item, dict):
                structured_parts.append(item)

    if not has_attachments and not attachment_names and not all_raw_attachments and not structured_parts:
        return []

    records: list[dict[str, Any]] = []

    if structured_parts:
        for item in structured_parts:
            records.append(_build_record_from_raw(item, message_id=message_id, thread_id=thread_id))
    else:
        for item in all_raw_attachments:
            if isinstance(item, dict):
                records.append(_build_record_from_raw(item, message_id=message_id, thread_id=thread_id))

    if not records and attachment_names:
        for name in attachment_names:
            records.append(_build_record_from_name(str(name), message_id=message_id, thread_id=thread_id))

    return records


def classify_attachment(record: dict[str, Any]) -> dict[str, Any]:
    """Assign business type and initial classification to an attachment record."""
    file_name = str(record.get("file_name") or "").strip()
    mime_type = str(record.get("mime_type") or "").strip().lower()

    business_type = MIME_TYPE_HINTS.get(mime_type, "unknown")
    for pattern, candidate_type in FILENAME_PATTERNS:
        if pattern.search(file_name):
            business_type = candidate_type
            break

    record["attachment_business_type"] = business_type
    return record


def build_attachment_summary(record: dict[str, Any], *, intake_result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a short Polish operator summary for one attachment."""
    business_type = str(record.get("attachment_business_type") or "unknown")
    file_name = str(record.get("file_name") or "załącznik")
    extracted = str(record.get("extracted_text_preview") or "").strip()
    if extracted:
        hint = summarize_extracted_text_for_operator(extracted)
        if hint:
            record["attachment_summary_pl"] = hint
            return record

    summary_templates: dict[str, str] = {
        "technical_photo": f"Zdjęcie techniczne ({file_name}) może zawierać istotne dane wizualne dla sprawy.",
        "technical_pdf": f"Dokument PDF ({file_name}) może zawierać szczegóły techniczne lub projektowe.",
        "product_datasheet": f"Karta katalogowa ({file_name}) opisuje parametry produktu lub systemu.",
        "competitor_offer": f"Oferta ({file_name}) może być punktem odniesienia do wyceny lub negocjacji.",
        "invoice": f"Dokument finansowy ({file_name}) może wymagać weryfikacji kwot lub terminów płatności.",
        "delivery_confirmation": f"Potwierdzenie dostawy ({file_name}) może zamykać otwartą pętlę logistyczną.",
        "project_document": f"Dokument projektowy ({file_name}) może zawierać obliczenia lub plan prac.",
        "screenshot": f"Zrzut ekranu ({file_name}) może ilustrować konkretny problem lub stan.",
        "reference_document": f"Dokument referencyjny ({file_name}) – do zachowania w tle sprawy.",
        "unknown": f"Załącznik ({file_name}) – typ nierozpoznany, warto sprawdzić ręcznie.",
    }

    record["attachment_summary_pl"] = summary_templates.get(business_type, summary_templates["unknown"])
    return record


def assess_attachment_relevance(
    record: dict[str, Any],
    *,
    intake_result: dict[str, Any] | None = None,
    case_link_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assess how relevant an attachment is to the current case and whether it changes understanding."""
    business_type = str(record.get("attachment_business_type") or "unknown")
    extraction_confidence = float(record.get("extraction_confidence") or 0.0)

    risk_flags: list[str] = []
    if business_type == "invoice":
        risk_flags.append("financial_document_present")
    if business_type == "delivery_confirmation":
        risk_flags.append("logistics_evidence_present")
    if business_type == "unknown":
        risk_flags.append("unrecognized_attachment")
    if extraction_confidence < 0.4 and business_type != "reference_document":
        risk_flags.append("low_confidence_extraction")

    case_relevance = "background"
    if business_type in {"invoice", "delivery_confirmation", "project_document", "product_datasheet"}:
        case_relevance = "significant"
    elif business_type in {"technical_photo", "technical_pdf", "competitor_offer"}:
        case_relevance = "supportive"
    elif business_type == "unknown":
        case_relevance = "review_needed"

    operator_attention_hint = "none"
    if case_relevance in {"significant", "review_needed"}:
        operator_attention_hint = "check_attachment"
    elif risk_flags:
        operator_attention_hint = "note_risk"

    record["attachment_risk_flags"] = risk_flags
    record["case_relevance"] = case_relevance
    record["operator_attention_hint"] = operator_attention_hint
    return record


def build_attachment_intelligence(
    snapshot: dict[str, Any],
    *,
    intake_result: dict[str, Any] | None = None,
    case_link_result: dict[str, Any] | None = None,
    attachment_fetcher: Callable[[str, str], bytes] | None = None,
    attachment_max_bytes: int = 8_000_000,
    docling_enabled: bool = False,
    docling_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full attachment intelligence pass: ingest -> classify -> summarize -> assess."""
    records = build_attachment_records(snapshot)
    if attachment_fetcher:
        _apply_binary_extraction(
            records,
            snapshot=snapshot,
            attachment_fetcher=attachment_fetcher,
            attachment_max_bytes=attachment_max_bytes,
            docling_enabled=docling_enabled,
            docling_options=docling_options,
        )
    processed: list[dict[str, Any]] = []
    for record in records:
        classify_attachment(record)
        build_attachment_summary(record, intake_result=intake_result)
        _apply_document_intelligence(record, case_id=str((case_link_result or {}).get("case_id") or ""))
        assess_attachment_relevance(record, intake_result=intake_result, case_link_result=case_link_result)
        processed.append(record)

    document_intelligence = [r.get("document_intelligence") for r in processed if isinstance(r.get("document_intelligence"), dict)]
    document_conflicts = detect_document_conflicts(document_intelligence)
    if document_conflicts:
        for doc in document_intelligence:
            doc["conflicts"] = document_conflicts

    significant = [r for r in processed if r.get("case_relevance") in {"significant", "review_needed"}]
    all_risk_flags: list[str] = []
    for r in processed:
        all_risk_flags.extend(r.get("attachment_risk_flags") or [])

    summary_parts: list[str] = []
    if significant:
        summary_parts.append(f"Znaleziono {len(significant)} istotnych załączników.")
    if all_risk_flags:
        summary_parts.append(f"Uwaga: {', '.join(sorted(set(all_risk_flags)))}.")

    return {
        "attachments": processed,
        "attachment_count": len(processed),
        "significant_count": len(significant),
        "combined_risk_flags": sorted(set(all_risk_flags)),
        "summary_pl": " ".join(summary_parts).strip() or "Brak istotnych załączników.",
        "has_significant_attachments": bool(significant),
        "document_intelligence": {
            "important_documents": document_intelligence,
            "document_conflicts": document_conflicts,
        },
    }


def refresh_attachment_intelligence_with_intake_context(
    attachment_intelligence: dict[str, Any],
    *,
    intake_result: dict[str, Any] | None,
    case_link_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Re-run summary + relevance on cached attachment records without re-extracting bytes."""
    raw = attachment_intelligence.get("attachments") or []
    if not raw:
        return attachment_intelligence
    processed: list[dict[str, Any]] = []
    for record in raw:
        if not isinstance(record, dict):
            continue
        rec = dict(record)
        build_attachment_summary(rec, intake_result=intake_result)
        assess_attachment_relevance(rec, intake_result=intake_result, case_link_result=case_link_result)
        processed.append(rec)
    significant = [r for r in processed if r.get("case_relevance") in {"significant", "review_needed"}]
    all_risk_flags: list[str] = []
    for r in processed:
        all_risk_flags.extend(r.get("attachment_risk_flags") or [])
    summary_parts: list[str] = []
    if significant:
        summary_parts.append(f"Znaleziono {len(significant)} istotnych załączników.")
    if all_risk_flags:
        summary_parts.append(f"Uwaga: {', '.join(sorted(set(all_risk_flags)))}.")
    out = dict(attachment_intelligence)
    out["attachments"] = processed
    out["attachment_count"] = len(processed)
    out["significant_count"] = len(significant)
    out["combined_risk_flags"] = sorted(set(all_risk_flags))
    out["summary_pl"] = " ".join(summary_parts).strip() or "Brak istotnych załączników."
    out["has_significant_attachments"] = bool(significant)
    return out


def _apply_binary_extraction(
    records: list[dict[str, Any]],
    *,
    snapshot: dict[str, Any],
    attachment_fetcher: Callable[[str, str], bytes],
    attachment_max_bytes: int,
    docling_enabled: bool = False,
    docling_options: dict[str, Any] | None = None,
) -> None:
    source_message = snapshot.get("source_message") or {}
    message_id = str(source_message.get("message_id") or "").strip()
    if not message_id:
        return
    for record in records:
        gid = str(record.get("storage_ref") or "").strip()
        if not gid:
            continue
        size_b = int(record.get("size_bytes") or 0)
        if size_b and size_b > attachment_max_bytes:
            record["extraction_warnings"] = list(record.get("extraction_warnings") or []) + ["attachment_too_large_for_extraction"]
            continue
        try:
            data = attachment_fetcher(message_id, gid)
        except Exception as exc:  # noqa: BLE001
            record["extraction_warnings"] = list(record.get("extraction_warnings") or []) + [f"fetch_failed:{type(exc).__name__}"]
            continue
        if not data:
            continue
        mime = str(record.get("mime_type") or "")
        name = str(record.get("file_name") or "")
        result = extract_attachment_text(
            data,
            mime_type=mime,
            file_name=name,
            docling_enabled=docling_enabled,
            docling_options=docling_options,
        )
        text = str(result.get("extracted_text") or "").strip()
        method = str(result.get("extraction_method") or "")
        conf = float(result.get("extraction_confidence") or 0.0)
        record["extracted_text_preview"] = text[:4000]
        record["extraction_method"] = method
        record["extraction_confidence"] = conf
        record["extraction_status"] = str(result.get("extraction_status") or "")
        record["parser_provenance"] = str(result.get("parser_provenance") or "")
        record["extracted_fields"] = {"content_sha256_prefix": result.get("content_sha256_prefix") or ""}
        if text:
            record["extracted_fields"]["char_count"] = len(text)


def _apply_document_intelligence(record: dict[str, Any], *, case_id: str = "") -> None:
    result = build_document_intelligence_result(
        source_type="gmail_attachment",
        source_id=str(record.get("attachment_id") or record.get("storage_ref") or ""),
        case_id=case_id,
        filename=str(record.get("file_name") or ""),
        mime_type=str(record.get("mime_type") or ""),
        text=str(record.get("extracted_text_preview") or ""),
        parser=str(record.get("extraction_method") or "fallback"),
        parser_confidence=float(record.get("extraction_confidence") or 0.0),
    )
    record["document_intelligence"] = result.to_dict()


def _build_record_from_raw(item: dict[str, Any], *, message_id: str, thread_id: str) -> dict[str, Any]:
    file_name = str(item.get("name") or item.get("filename") or "").strip()
    mime_type = str(item.get("mime_type") or item.get("mimeType") or item.get("content_type") or "").strip()
    size_bytes = _coerce_int(item.get("size") or item.get("size_bytes"), default=0)
    gmail_att_id = str(item.get("attachment_id") or item.get("attachmentId") or "").strip()
    attachment_id = _stable_id("att", message_id, file_name or mime_type or gmail_att_id)

    return {
        "attachment_id": attachment_id,
        "source_signal_id": "",
        "source_message_id": message_id,
        "thread_id": thread_id,
        "file_name": file_name,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "storage_ref": gmail_att_id or str(item.get("body", {}).get("attachmentId") or ""),
        "attachment_business_type": "unknown",
        "attachment_summary_pl": "",
        "extracted_fields": {},
        "extraction_confidence": 0.0,
        "extraction_warnings": [],
        "attachment_risk_flags": [],
        "case_relevance": "background",
        "linked_case_id": "",
        "operator_attention_hint": "none",
        "created_at": "",
    }


def _build_record_from_name(name: str, *, message_id: str, thread_id: str) -> dict[str, Any]:
    attachment_id = _stable_id("att", message_id, name)
    mime_type = _guess_mime_from_name(name)

    return {
        "attachment_id": attachment_id,
        "source_signal_id": "",
        "source_message_id": message_id,
        "thread_id": thread_id,
        "file_name": name,
        "mime_type": mime_type,
        "size_bytes": 0,
        "storage_ref": "",
        "attachment_business_type": "unknown",
        "attachment_summary_pl": "",
        "extracted_fields": {},
        "extraction_confidence": 0.0,
        "extraction_warnings": [],
        "attachment_risk_flags": [],
        "case_relevance": "background",
        "linked_case_id": "",
        "operator_attention_hint": "none",
        "created_at": "",
    }


def _guess_mime_from_name(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith((".xlsx", ".xls")):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if lower.endswith((".docx", ".doc")):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return ""


def _stable_id(prefix: str, *parts: str) -> str:
    seed = "::".join(str(part or "").strip() for part in parts if str(part or "").strip())
    if not seed:
        seed = prefix
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


__all__ = [
    "build_attachment_intelligence",
    "build_attachment_records",
    "classify_attachment",
    "build_attachment_summary",
    "assess_attachment_relevance",
    "refresh_attachment_intelligence_with_intake_context",
]
