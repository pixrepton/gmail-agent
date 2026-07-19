"""Best-effort text extraction from common attachment formats."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def inspect_ocr_runtime() -> dict[str, Any]:
    """Return a redaction-safe readiness summary for local OCR support."""
    try:
        import PIL  # type: ignore[import-untyped]
    except ImportError as exc:
        return {
            "status": "deps_missing",
            "reason": f"Pillow is not installed: {exc}",
        }
    try:
        import pytesseract  # type: ignore[import-untyped]
    except ImportError as exc:
        return {
            "status": "deps_missing",
            "reason": f"pytesseract is not installed: {exc}",
        }

    dependencies = {
        "pillow": getattr(PIL, "__version__", "unknown"),
        "pytesseract": getattr(pytesseract, "__version__", "unknown"),
    }
    binary_hint = str(getattr(pytesseract.pytesseract, "tesseract_cmd", "") or "").strip() or "tesseract"
    resolved_binary = shutil.which(binary_hint) or shutil.which("tesseract") or ""
    try:
        version = str(pytesseract.get_tesseract_version())
    except pytesseract.TesseractNotFoundError:
        return {
            "status": "binary_missing",
            "reason": f"Tesseract binary is not available via `{binary_hint}`.",
            "binary_hint": binary_hint,
            "binary_path": resolved_binary or None,
            "dependencies": dependencies,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "reason": str(exc),
            "binary_hint": binary_hint,
            "binary_path": resolved_binary or None,
            "dependencies": dependencies,
        }

    return {
        "status": "ok",
        "binary_hint": binary_hint,
        "binary_path": resolved_binary or binary_hint,
        "version": version,
        "dependencies": dependencies,
    }


def inspect_docling_runtime() -> dict[str, Any]:
    """Return a redaction-safe readiness summary for Docling support."""
    try:
        import docling  # type: ignore[import-untyped]
    except ImportError as exc:
        return {
            "status": "deps_missing",
            "reason": f"Docling is not installed: {exc}",
        }

    return {
        "status": "ok",
        "version": getattr(docling, "__version__", "unknown"),
    }


def extract_text_from_pdf_scanned_bytes(data: bytes, *, max_pages: int = 20) -> tuple[str, str, float]:
    """OCR for image-only PDFs using pdftoppm + Tesseract when poppler is available."""
    if not data:
        return "", "skipped_empty", 0.0
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return "", "ocr_binary_missing", 0.0
    try:
        from PIL import Image  # type: ignore[import-untyped]
        import pytesseract  # type: ignore[import-untyped]
    except ImportError:
        return "", "ocr_deps_missing", 0.0

    chunks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pdf-ocr-") as tmp_dir:
        pdf_path = Path(tmp_dir) / "scan.pdf"
        pdf_path.write_bytes(data)
        prefix = str(Path(tmp_dir) / "page")
        proc = subprocess.run(
            [
                pdftoppm,
                "-png",
                "-f",
                "1",
                "-l",
                str(max(1, int(max_pages))),
                str(pdf_path),
                prefix,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return "", "pdf_ocr_render_failed", 0.0
        for image_path in sorted(Path(tmp_dir).glob("page-*.png")):
            image = Image.open(image_path)
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            page_text = (pytesseract.image_to_string(image, lang="pol+eng") or "").strip()
            if page_text:
                chunks.append(page_text)
    text = "\n\n".join(chunks).strip()
    if not text:
        return "", "ocr_empty", 0.1
    conf = _clamp01(0.35 + min(0.5, len(text) / 6000.0))
    return _truncate(text, 12000), "pdf_tesseract_ocr", conf


def extract_text_from_pdf_bytes(data: bytes) -> tuple[str, str, float]:
    """Return (text, method, confidence). Uses pypdf when available."""
    if not data:
        return "", "skipped_empty", 0.0
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped]
    except ImportError:
        return "", "pypdf_missing", 0.0
    try:
        reader = PdfReader(BytesIO(data))
        chunks: list[str] = []
        for page in reader.pages[:30]:
            extracted = page.extract_text() or ""
            if extracted.strip():
                chunks.append(extracted)
        text = "\n\n".join(chunks).strip()
        if not text:
            return "", "pdf_no_text_layer", 0.15
        conf = _clamp01(0.55 + min(0.4, len(text) / 8000.0))
        return _truncate(text, 12000), "pdf_text_layer", conf
    except Exception:
        return "", "pdf_read_error", 0.0


def extract_text_from_image_bytes(data: bytes) -> tuple[str, str, float]:
    """OCR for images when Pillow + pytesseract (+ Tesseract binary) are available."""
    if not data:
        return "", "skipped_empty", 0.0
    try:
        from PIL import Image  # type: ignore[import-untyped]
        import pytesseract  # type: ignore[import-untyped]
    except ImportError:
        return "", "ocr_deps_missing", 0.0
    try:
        image = Image.open(BytesIO(data))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        text = pytesseract.image_to_string(image, lang="pol+eng") or ""
        text = text.strip()
        if not text:
            return "", "ocr_empty", 0.1
        conf = _clamp01(0.4 + min(0.45, len(text) / 4000.0))
        return _truncate(text, 8000), "tesseract_ocr", conf
    except pytesseract.TesseractNotFoundError:
        return "", "ocr_binary_missing", 0.0
    except Exception:
        return "", "ocr_failed", 0.0


def extract_text_from_docx_bytes(data: bytes) -> tuple[str, str, float]:
    """Extract text from DOCX XML payloads using stdlib only."""
    if not data:
        return "", "skipped_empty", 0.0
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            xml_bytes = archive.read("word/document.xml")
    except KeyError:
        return "", "docx_missing_document_xml", 0.0
    except Exception:
        return "", "docx_read_error", 0.0
    try:
        root = ET.fromstring(xml_bytes)
        texts = [node.text or "" for node in root.iter() if node.tag.endswith("}t") and (node.text or "").strip()]
        text = " ".join(texts).strip()
        if not text:
            return "", "docx_no_text", 0.15
        conf = _clamp01(0.62 + min(0.28, len(text) / 12000.0))
        return _truncate(text, 12000), "docx_xml_text", conf
    except Exception:
        return "", "docx_parse_error", 0.0


def extract_text_from_xlsx_bytes(data: bytes) -> tuple[str, str, float]:
    """Extract text from XLSX shared strings and worksheet cells using stdlib only."""
    if not data:
        return "", "skipped_empty", 0.0
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            shared_strings = _read_xlsx_shared_strings(archive)
            sheet_names = sorted(
                name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            )
            rows: list[str] = []
            for sheet_name in sheet_names[:6]:
                rows.extend(_read_xlsx_sheet_rows(archive.read(sheet_name), shared_strings))
    except Exception:
        return "", "xlsx_read_error", 0.0
    text = "\n".join(row for row in rows if row.strip()).strip()
    if not text:
        return "", "xlsx_no_text", 0.15
    conf = _clamp01(0.58 + min(0.28, len(text) / 12000.0))
    return _truncate(text, 12000), "xlsx_xml_cells", conf


def extract_text_from_xls_bytes(data: bytes) -> tuple[str, str, float]:
    """Low-confidence binary XLS fallback based on visible strings."""
    if not data:
        return "", "skipped_empty", 0.0
    ascii_matches = re.findall(rb"[ -~]{4,}", data)
    utf16_matches = re.findall(rb"(?:[ -~]\x00){4,}", data)
    parts: list[str] = []
    for match in ascii_matches[:40]:
        try:
            parts.append(match.decode("latin1"))
        except Exception:
            continue
    for match in utf16_matches[:20]:
        try:
            parts.append(match.decode("utf-16le"))
        except Exception:
            continue
    text = " ".join(part.strip() for part in parts if part.strip())
    if not text:
        return "", "xls_binary_no_strings", 0.05
    return _truncate(text, 8000), "xls_binary_strings", 0.22


def extract_attachment_text_legacy(
    data: bytes,
    *,
    mime_type: str,
    file_name: str = "",
    docling_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stdlib/pypdf/tesseract paths only — no parse stack (used by legacy adapter)."""
    mime = (mime_type or "").strip().lower()
    name = (file_name or "").lower()
    digest = hashlib.sha256(data).hexdigest()[:16] if data else ""
    extraction_status = "ok"
    parser_provenance = "stdlib"
    metadata: dict[str, Any] = {"warnings": []}

    if mime == "application/pdf" or name.endswith(".pdf"):
        text, method, conf = extract_text_from_pdf_bytes(data)
        parser_provenance = "pypdf"
        if not text.strip():
            ocr_text, ocr_method, ocr_conf = extract_text_from_pdf_scanned_bytes(
                data,
                max_pages=int((docling_options or {}).get("max_pages") or 20),
            )
            if ocr_text.strip():
                text, method, conf = ocr_text, ocr_method, ocr_conf
                parser_provenance = "pdftoppm_tesseract"
    elif mime.startswith("image/") or any(name.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".heic")):
        text, method, conf = extract_text_from_image_bytes(data)
        parser_provenance = "pillow_tesseract"
    elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or name.endswith(".docx"):
        text, method, conf = extract_text_from_docx_bytes(data)
    elif mime in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
    } or name.endswith((".xlsx", ".xlsm")):
        text, method, conf = extract_text_from_xlsx_bytes(data)
    elif mime == "application/vnd.ms-excel" or name.endswith(".xls"):
        text, method, conf = extract_text_from_xls_bytes(data)
    elif mime in {"application/zip", "application/x-zip-compressed"} or name.endswith(".zip"):
        return {
            "extracted_text": "",
            "extraction_method": "zip_container",
            "extraction_confidence": 0.0,
            "content_sha256_prefix": digest,
            "extraction_status": "archive_container",
            "parser_provenance": "zipfile",
            "metadata": metadata,
        }
    else:
        return {
            "extracted_text": "",
            "extraction_method": "unsupported_mime",
            "extraction_confidence": 0.0,
            "content_sha256_prefix": digest,
            "extraction_status": "unsupported_mime",
            "parser_provenance": "none",
            "metadata": metadata,
        }

    cleaned = _cleanup_extracted_text(text)
    if not cleaned.strip() and conf <= 0.0:
        extraction_status = "failed"
    elif not cleaned.strip():
        extraction_status = "empty"
    return {
        "extracted_text": cleaned,
        "extraction_method": method,
        "extraction_confidence": float(conf),
        "content_sha256_prefix": digest,
        "extraction_status": extraction_status,
        "parser_provenance": parser_provenance,
        "metadata": metadata,
    }


def parse_attachment_document(
    data: bytes,
    *,
    mime_type: str,
    file_name: str = "",
    docling_enabled: bool = False,
    unstructured_enabled: bool = False,
    parser_chain: tuple[str, ...] | None = None,
    docling_options: dict[str, Any] | None = None,
    structured_facts_enabled: bool = True,
):
    """Structure-first parse: Docling → Unstructured → legacy (see document_parse_runtime)."""
    from document_parse_contract import DocumentParseConfig
    from document_parse_runtime import parse_document

    chain = tuple(parser_chain or ())
    if not chain:
        chain = ("docling", "unstructured", "legacy") if docling_enabled else ("legacy",)
    config = DocumentParseConfig(
        parser_chain=chain,
        docling_enabled=bool(docling_enabled),
        unstructured_enabled=bool(unstructured_enabled),
        docling_options=dict(docling_options or {}),
        structured_facts_enabled=bool(structured_facts_enabled),
    )
    return parse_document(data, mime_type=mime_type, file_name=file_name, config=config)


def extract_attachment_text(
    data: bytes,
    *,
    mime_type: str,
    file_name: str = "",
    docling_enabled: bool = False,
    docling_options: dict[str, Any] | None = None,
    unstructured_enabled: bool = False,
    parser_chain: tuple[str, ...] | None = None,
    structured_facts_enabled: bool = True,
) -> dict[str, Any]:
    """Route bytes through the document parse stack; returns legacy extraction dict."""
    from document_parse_runtime import resolve_parser_chain_from_env

    chain = tuple(parser_chain or ())
    if not chain:
        chain = resolve_parser_chain_from_env("docling,unstructured,legacy") if docling_enabled else ("legacy",)
    result = parse_attachment_document(
        data,
        mime_type=mime_type,
        file_name=file_name,
        docling_enabled=bool(docling_enabled),
        unstructured_enabled=bool(unstructured_enabled),
        parser_chain=chain,
        docling_options=docling_options,
        structured_facts_enabled=structured_facts_enabled,
    )
    legacy = result.to_extraction_dict()
    cleaned = _cleanup_extracted_text(str(legacy.get("extracted_text") or ""))
    legacy["extracted_text"] = cleaned
    return legacy


def _read_xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        xml_bytes = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []
    return [node.text or "" for node in root.iter() if node.tag.endswith("}t")]


def _read_xlsx_sheet_rows(xml_bytes: bytes, shared_strings: list[str]) -> list[str]:
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []
    rows: list[str] = []
    for row in root.iter():
        if not str(row.tag).endswith("}row"):
            continue
        values: list[str] = []
        for cell in row:
            if not str(cell.tag).endswith("}c"):
                continue
            cell_type = cell.attrib.get("t", "")
            value_text = ""
            for sub in cell:
                if str(sub.tag).endswith("}v") and (sub.text or "").strip():
                    value_text = sub.text or ""
                    break
            if cell_type == "s":
                try:
                    idx = int(value_text)
                    value_text = shared_strings[idx] if 0 <= idx < len(shared_strings) else ""
                except Exception:
                    value_text = ""
            if value_text.strip():
                values.append(value_text.strip())
        if values:
            rows.append(" | ".join(values))
    return rows


def _cleanup_extracted_text(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def summarize_extracted_text_for_operator(text: str, *, max_sentence: int = 240) -> str:
    """Polish-first one-line hint from extracted body (not an LLM)."""
    if not text.strip():
        return ""
    sentence = text.replace("\n", " ").strip()
    if len(sentence) > max_sentence:
        sentence = sentence[: max_sentence - 3].rstrip() + "..."
    return f"Treść odczytana z pliku: {sentence}"
