"""Orchestrates document parser chain (Docling → Unstructured → legacy)."""

from __future__ import annotations

from typing import Any

import os

from document_parse_adapters import (
    parse_with_docling,
    parse_with_hard_pdf,
    parse_with_legacy,
    parse_with_unstructured,
)
from document_parse_contract import DocumentParseConfig, DocumentParseResult, PARSER_CHAIN_DEFAULT
from config import Settings


def resolve_parser_chain_from_env(raw: str) -> tuple[str, ...]:
    text = str(raw or "").strip().lower()
    if not text:
        return PARSER_CHAIN_DEFAULT
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    return tuple(parts) if parts else PARSER_CHAIN_DEFAULT


def build_parse_config_from_settings(settings: Settings) -> DocumentParseConfig:
    chain_raw = str(getattr(settings, "attachment_parser_chain_raw", "") or "").strip()
    chain = getattr(settings, "attachment_parser_chain", None)
    if not chain:
        chain = resolve_parser_chain_from_env(chain_raw)
    return DocumentParseConfig(
        parser_chain=tuple(chain),
        docling_enabled=bool(getattr(settings, "docling_enabled", False)),
        unstructured_enabled=bool(getattr(settings, "unstructured_enabled", False)),
        docling_options={
            "max_pages": int(getattr(settings, "docling_max_pages", 0) or 0),
            "timeout_sec": int(getattr(settings, "docling_timeout_sec", 0) or 0),
        },
        structured_facts_enabled=bool(getattr(settings, "document_structured_facts_enabled", True)),
    )


def build_parse_config_from_runtime(runtime: Any) -> DocumentParseConfig:
    chain = getattr(runtime, "attachment_parser_chain", None) or PARSER_CHAIN_DEFAULT
    return DocumentParseConfig(
        parser_chain=tuple(chain),
        docling_enabled=bool(getattr(runtime, "docling_enabled", False)),
        unstructured_enabled=bool(getattr(runtime, "unstructured_enabled", False)),
        docling_options=dict(getattr(runtime, "docling_options", {}) or {}),
        structured_facts_enabled=bool(getattr(runtime, "document_structured_facts_enabled", True)),
    )


def _run_parser(
    parser_id: str,
    data: bytes,
    *,
    mime_type: str,
    file_name: str,
    config: DocumentParseConfig,
) -> DocumentParseResult | None:
    if parser_id == "docling":
        return parse_with_docling(data, mime_type=mime_type, file_name=file_name, options=config.docling_options)
    if parser_id == "unstructured":
        return parse_with_unstructured(data, mime_type=mime_type, file_name=file_name, options=config.docling_options)
    if parser_id == "legacy":
        return parse_with_legacy(data, mime_type=mime_type, file_name=file_name, options=config.docling_options)
    if parser_id == "hard_pdf":
        return parse_with_hard_pdf(data, mime_type=mime_type, file_name=file_name, options=config.docling_options)
    return None


def _hard_pdf_lane_enabled() -> bool:
    return os.getenv("HARD_PDF_LANE_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


def _hard_pdf_coverage_threshold() -> float:
    try:
        return float(os.getenv("HARD_PDF_LANE_COVERAGE_THRESHOLD", "0.35"))
    except ValueError:
        return 0.35


def _should_trigger_hard_pdf(result: DocumentParseResult | None) -> bool:
    if result is None:
        return True
    text = str(result.plain_text or "").strip()
    if not text:
        return True
    conf = float(result.extraction_confidence or 0.0)
    return conf < _hard_pdf_coverage_threshold()


def parse_document(
    data: bytes,
    *,
    mime_type: str,
    file_name: str = "",
    config: DocumentParseConfig | None = None,
    force_hard_lane: bool = False,
) -> DocumentParseResult:
    """Try parsers in chain; prefer first structured result with text, else best plain text."""
    cfg = config or DocumentParseConfig()
    chain = cfg.resolved_chain()
    best: DocumentParseResult | None = None

    for parser_id in chain:
        result = _run_parser(parser_id, data, mime_type=mime_type, file_name=file_name, config=cfg)
        if result is None:
            continue
        if result.plain_text.strip() and result.structured:
            return result
        if best is None:
            best = result
        elif result.plain_text.strip() and not best.plain_text.strip():
            best = result
        elif result.structured and not best.structured:
            best = result
        elif float(result.extraction_confidence) > float(best.extraction_confidence):
            best = result

    if best is not None:
        if force_hard_lane or (
            _hard_pdf_lane_enabled()
            and _should_trigger_hard_pdf(best)
            and (mime_type == "application/pdf" or str(file_name).lower().endswith(".pdf"))
        ):
            hard = parse_with_hard_pdf(
                data,
                mime_type=mime_type,
                file_name=file_name,
                options=cfg.docling_options,
            )
            if hard is not None:
                if force_hard_lane:
                    return hard
                if str(hard.plain_text or "").strip() and len(hard.plain_text) > len(best.plain_text):
                    return hard
        return best

    if force_hard_lane or _hard_pdf_lane_enabled():
        hard = parse_with_hard_pdf(
            data,
            mime_type=mime_type,
            file_name=file_name,
            options=cfg.docling_options,
        )
        if hard is not None:
            return hard

    return DocumentParseResult(
        parser_id="none",
        plain_text="",
        extraction_method="unsupported",
        extraction_status="unsupported_mime",
        content_sha256_prefix="",
        structured=False,
        metadata={"warnings": ["no_parser_matched"]},
    )


__all__ = [
    "build_parse_config_from_runtime",
    "build_parse_config_from_settings",
    "parse_document",
    "resolve_parser_chain_from_env",
]
