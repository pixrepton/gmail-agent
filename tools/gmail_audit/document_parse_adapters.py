"""Parser backends: Docling, Unstructured, legacy stdlib fallbacks."""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path
from typing import Any

from document_field_extractor import enrich_elements_from_plain_text
from document_parse_contract import DocumentElement, DocumentParseResult

_MARKDOWN_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s\-:|]+\|?\s*$")


def _digest_prefix(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16] if data else ""


def _finalize_result(
    *,
    parser_id: str,
    plain_text: str,
    elements: list[DocumentElement],
    extraction_method: str,
    extraction_confidence: float,
    extraction_status: str,
    data: bytes,
    metadata: dict[str, Any] | None = None,
    source_id: str = "",
    parser_provenance_override: str = "",
) -> DocumentParseResult:
    text = str(plain_text or "").strip()
    enriched = enrich_elements_from_plain_text(list(elements), text, source_id=source_id) if text else list(elements)
    structured = any(
        el.element_type in {"key_value", "table_row", "table"} and (el.label or el.value)
        for el in enriched
    )
    pid = parser_id
    if structured and parser_id == "legacy":
        pid = "legacy_structured"
    provenance = str(parser_provenance_override or parser_id)
    return DocumentParseResult(
        parser_id=pid,
        plain_text=text,
        elements=enriched,
        extraction_method=extraction_method,
        extraction_confidence=float(extraction_confidence or 0.0),
        extraction_status=extraction_status,
        content_sha256_prefix=_digest_prefix(data),
        parser_provenance=provenance,
        structured=structured,
        metadata=dict(metadata or {}),
    )


def _elements_from_docling_document(document: Any, *, source_id: str = "") -> list[DocumentElement]:
    elements: list[DocumentElement] = []
    tables = getattr(document, "tables", None)
    if tables:
        for table in list(tables)[:40]:
            grid = getattr(table, "data", None) or getattr(table, "grid", None)
            if not grid:
                continue
            for row in list(grid)[:80]:
                cells = [str(c or "").strip() for c in list(row)]
                cells = [c for c in cells if c]
                if len(cells) >= 2:
                    elements.append(
                        DocumentElement(
                            element_type="table_row",
                            text=" | ".join(cells),
                            label=cells[0],
                            value=cells[1],
                            metadata={"source_id": source_id, "origin": "docling_table"},
                        )
                    )
    if hasattr(document, "iterate_items"):
        try:
            for item, _level in document.iterate_items():
                text = str(getattr(item, "text", "") or "").strip()
                if not text:
                    continue
                label = str(getattr(item, "label", "") or "").strip()
                elements.append(
                    DocumentElement(
                        element_type=str(getattr(item, "label", "") or "narrative")[:32].lower() or "narrative",
                        text=text[:2000],
                        label=label,
                        value=text if label else "",
                        metadata={"source_id": source_id, "origin": "docling_item"},
                    )
                )
        except Exception as exc:
            logger.warning("docling_document_iterate_failed source_id=%s exc=%s", source_id, exc)
    return elements


def parse_with_docling(
    data: bytes,
    *,
    mime_type: str,
    file_name: str,
    options: dict[str, Any] | None = None,
) -> DocumentParseResult | None:
    if not data:
        return None
    try:
        from docling.document_converter import DocumentConverter  # type: ignore[import-untyped]
    except ImportError:
        return None

    suffix = Path(str(file_name or "document.pdf")).suffix or (".pdf" if mime_type == "application/pdf" else ".bin")
    temp_dir = Path(tempfile.gettempdir())
    temp_path = temp_dir / f"docling-{hashlib.sha256(data).hexdigest()[:12]}{suffix}"
    try:
        temp_path.write_bytes(data)
        converter = DocumentConverter()
        conv_result = converter.convert(str(temp_path))
        document = getattr(conv_result, "document", conv_result)
        text = ""
        if hasattr(document, "export_to_markdown"):
            text = str(document.export_to_markdown() or "")
        elif hasattr(document, "text"):
            text = str(getattr(document, "text") or "")
        else:
            text = str(document or "")
        elements = _elements_from_docling_document(document)
        page_count = int(getattr(document, "page_count", None) or 0)
        table_count = int(getattr(document, "table_count", None) or len(getattr(document, "tables", []) or []))
        method = "docling_pdf" if mime_type == "application/pdf" or str(file_name).lower().endswith(".pdf") else "docling_image"
        status = "ok" if text.strip() else "empty"
        return _finalize_result(
            parser_id="docling",
            plain_text=text[:12000],
            elements=elements,
            extraction_method=method,
            extraction_confidence=0.9 if text.strip() else 0.15,
            extraction_status=status,
            data=data,
            metadata={"page_count": page_count, "table_count": table_count, "warnings": []},
        )
    except Exception as exc:  # noqa: BLE001
        return _finalize_result(
            parser_id="docling",
            plain_text="",
            elements=[],
            extraction_method="docling_error",
            extraction_confidence=0.0,
            extraction_status="failed",
            data=data,
            metadata={"warnings": [str(exc)[:300]]},
        )
    finally:
        temp_path.unlink(missing_ok=True)


def parse_with_unstructured(
    data: bytes,
    *,
    mime_type: str,
    file_name: str,
    options: dict[str, Any] | None = None,
) -> DocumentParseResult | None:
    if not data:
        return None
    try:
        from unstructured.partition.auto import partition  # type: ignore[import-untyped]
    except ImportError:
        return None

    suffix = Path(str(file_name or "document.bin")).suffix or ".bin"
    temp_path = Path(tempfile.gettempdir()) / f"unstructured-{hashlib.sha256(data).hexdigest()[:12]}{suffix}"
    try:
        temp_path.write_bytes(data)
        raw_elements = partition(filename=str(temp_path))
    except Exception as exc:  # noqa: BLE001
        return _finalize_result(
            parser_id="unstructured",
            plain_text="",
            elements=[],
            extraction_method="unstructured_error",
            extraction_confidence=0.0,
            extraction_status="failed",
            data=data,
            metadata={"warnings": [str(exc)[:300]]},
        )
    finally:
        temp_path.unlink(missing_ok=True)

    elements: list[DocumentElement] = []
    text_parts: list[str] = []
    for el in list(raw_elements or [])[:500]:
        category = str(getattr(el, "category", "") or "Text").strip().lower()
        text = str(el).strip()
        if not text:
            continue
        text_parts.append(text)
        element_type = category.replace(" ", "_")
        label = ""
        value = ""
        if category == "table":
            element_type = "table"
        elif ":" in text and len(text) < 240:
            parts = text.split(":", 1)
            label = parts[0].strip()
            value = parts[1].strip()
            element_type = "key_value"
        elements.append(
            DocumentElement(
                element_type=element_type,
                text=text[:2000],
                label=label,
                value=value,
                metadata={"origin": "unstructured", "category": category},
            )
        )
    plain = "\n\n".join(text_parts).strip()[:12000]
    return _finalize_result(
        parser_id="unstructured",
        plain_text=plain,
        elements=elements,
        extraction_method="unstructured_partition",
        extraction_confidence=0.85 if plain else 0.1,
        extraction_status="ok" if plain else "empty",
        data=data,
        metadata={"element_count": len(elements), "warnings": []},
    )


def parse_with_legacy(
    data: bytes,
    *,
    mime_type: str,
    file_name: str,
    options: dict[str, Any] | None = None,
) -> DocumentParseResult:
    """Delegate to existing stdlib/pypdf/tesseract paths in attachment_content_extraction."""
    from attachment_content_extraction import extract_attachment_text_legacy

    legacy = extract_attachment_text_legacy(
        data,
        mime_type=mime_type,
        file_name=file_name,
        docling_options=options,
    )
    text = str(legacy.get("extracted_text") or "")
    meta = dict(legacy.get("metadata") or {})
    return _finalize_result(
        parser_id="legacy",
        plain_text=text,
        elements=[],
        extraction_method=str(legacy.get("extraction_method") or "legacy"),
        extraction_confidence=float(legacy.get("extraction_confidence") or 0.0),
        extraction_status=str(legacy.get("extraction_status") or "ok"),
        data=data,
        metadata=meta,
        parser_provenance_override=str(legacy.get("parser_provenance") or "legacy"),
    )


def _ocr_pdf_bytes(data: bytes, *, dpi: int = 300) -> str:
    """OCR PDF pages via pdf2image + tesseract when available."""
    if not data:
        return ""
    try:
        import pdf2image  # type: ignore[import-untyped]
        import pytesseract  # type: ignore[import-untyped]
    except ImportError:
        return ""
    try:
        images = pdf2image.convert_from_bytes(data, dpi=max(150, int(dpi or 300)))
    except Exception:
        return ""
    parts: list[str] = []
    for img in list(images or [])[:20]:
        try:
            text = pytesseract.image_to_string(img, lang="pol+eng")
            if str(text or "").strip():
                parts.append(str(text).strip())
        except Exception:
            continue
    return "\n\n".join(parts).strip()[:12000]


def parse_with_hard_pdf(
    data: bytes,
    *,
    mime_type: str,
    file_name: str,
    options: dict[str, Any] | None = None,
) -> DocumentParseResult | None:
    """Hard PDF lane: OCR 300 DPI + optional pymupdf text recovery."""
    if not data:
        return None
    opts = dict(options or {})
    dpi = int(opts.get("ocr_dpi") or 300)
    text = ""
    method = "hard_pdf_ocr"
    try:
        import fitz  # type: ignore[import-untyped]

        doc = fitz.open(stream=data, filetype="pdf")
        parts = [page.get_text("text") for page in doc][:30]
        text = "\n".join(p for p in parts if str(p or "").strip()).strip()
        if text:
            method = "hard_pdf_pymupdf"
    except Exception:
        text = ""
    if len(text) < int(opts.get("min_chars") or 40):
        ocr_text = _ocr_pdf_bytes(data, dpi=dpi)
        if len(ocr_text) > len(text):
            text = ocr_text
            method = "hard_pdf_ocr"
    coverage = min(1.0, len(text) / max(1, int(opts.get("target_chars") or 200)))
    status = "ok" if text.strip() else "empty"
    return _finalize_result(
        parser_id="hard_pdf",
        plain_text=text[:12000],
        elements=[],
        extraction_method=method,
        extraction_confidence=0.55 + 0.35 * coverage if text.strip() else 0.1,
        extraction_status=status,
        data=data,
        metadata={"hard_pdf_lane": True, "ocr_dpi": dpi},
        parser_provenance_override="hard_pdf",
    )


def inspect_unstructured_runtime() -> dict[str, Any]:
    try:
        import unstructured  # type: ignore[import-untyped]

        return {
            "status": "ok",
            "version": getattr(unstructured, "__version__", "unknown"),
        }
    except ImportError as exc:
        return {
            "status": "deps_missing",
            "reason": f"unstructured is not installed: {exc}",
        }


__all__ = [
    "inspect_unstructured_runtime",
    "parse_with_docling",
    "parse_with_hard_pdf",
    "parse_with_legacy",
    "parse_with_unstructured",
]
