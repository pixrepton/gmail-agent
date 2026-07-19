"""Deterministic Drive lane/kind/scope classification for bounded ingest."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from drive_ingest_models import DriveDocumentKind, DriveIngestCandidate, DriveLane, DriveScope


LANE_KEYWORDS: dict[DriveLane, tuple[str, ...]] = {
    "formal_contracts": ("umowa", "umowy", "contract", "skany", "camscanner"),
    "commercial_transactions": ("zam", "zamowienie", "zamówienie", "faktura", "invoice", "zal", "zaliczk"),
    "service_warranty": ("gwaranc", "warranty", "serwis", "service", "protok", "przeglad", "przegląd"),
    "offer_library": ("kit-", "oferta", "split 1f", "split 3f", "aio", "all in one", "t-cap", "t cap"),
    "commercial_pricing": ("cennik", "pricing", "price list", "date-base33", "koszt", "haier", "panasonic"),
    "technical_reference": ("diagnost", "manual", "instruk", "reference", "technical", "panasonic service"),
    "case_folder": ("zdjec", "zdjęc", "video", "wideo", "case", "media bundle", "montaz", "montaż"),
    "media_marketing": ("fb", "facebook", "www", "pere", "portfolio", "kampania", "marketing"),
    "scans_intake": ("scan", "skany", "camscanner", "folder bez nazwy"),
    "cleanup_unknown": (),
}

KIND_PATTERNS: list[tuple[re.Pattern[str], DriveDocumentKind]] = [
    (re.compile(r"\bumowa\b|\bcontract\b", re.IGNORECASE), "contract"),
    (re.compile(r"\bszablon\b|template", re.IGNORECASE), "contract_template"),
    (re.compile(r"\bzam[-\s]?\d+\b|\bzamowienie\b|\bzamówienie\b", re.IGNORECASE), "order"),
    (re.compile(r"\bzal[-\s]?\d+\b|zaliczk", re.IGNORECASE), "deposit_invoice"),
    (re.compile(r"\bfv\b|\bfaktura\b|invoice", re.IGNORECASE), "invoice"),
    (re.compile(r"gwaranc|warranty", re.IGNORECASE), "warranty_card"),
    (re.compile(r"serwis|protok|przeglad|przegląd", re.IGNORECASE), "service_protocol"),
    (re.compile(r"\bkit-[a-z0-9-]+\b", re.IGNORECASE), "offer_template"),
    (re.compile(r"split 1f|split 3f|all[- ]in[- ]one|aio|t-cap|t cap", re.IGNORECASE), "offer_family_reference"),
    (re.compile(r"cennik|price list", re.IGNORECASE), "price_list"),
    (re.compile(r"date-base33|workbook|koszt", re.IGNORECASE), "pricing_workbook"),
    (re.compile(r"diagnost|manual|instruk|technical", re.IGNORECASE), "technical_reference"),
]

CASE_HINT_MAP = {
    "siedlec 9kw": "siedlec_9kw_panasonic_adc0309k3e5",
    "psary hp 12 kw": "psary_wisniowa_panasonic_12kw",
    "sosnowiec lg na panasia": "sosnowiec_dojazdowa_panasonic_9kw",
    "gleboka": "myslachowice_gleboka_panasonic_12kw",
    "głęboka": "myslachowice_gleboka_panasonic_12kw",
    "zubadan": "wojcik_regulice_zubadan_10kw",
    "zam-3": "skwarczynski_zator_panasonic_12kw",
    "zal-3": "skwarczynski_zator_panasonic_12kw",
}

MEDIA_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".mp4", ".mov", ".avi", ".mkv")


def classify_candidate(
    *,
    title: str,
    mime_type: str,
    folder_path: str,
    is_folder: bool,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lane, lane_confidence = classify_lane(folder_path=folder_path, title=title, is_folder=is_folder)
    document_kind, kind_confidence = classify_document_kind(
        title=title,
        mime_type=mime_type,
        folder_path=folder_path,
        lane=lane,
        is_folder=is_folder,
    )
    scope = classify_scope(lane=lane, document_kind=document_kind)
    probable_case_key = infer_probable_case_key(
        title=title,
        folder_path=folder_path,
        metadata=metadata or {},
    )
    return {
        "lane": lane,
        "document_kind": document_kind,
        "scope": scope,
        "probable_case_key": probable_case_key,
        "classification_confidence": round(max(lane_confidence, kind_confidence), 4),
    }


def classify_lane(*, folder_path: str, title: str, is_folder: bool) -> tuple[DriveLane, float]:
    normalized = _normalize_blob(" ".join(part for part in (folder_path, title) if part))
    if not normalized:
        return "cleanup_unknown", 0.2
    for lane, keywords in LANE_KEYWORDS.items():
        if lane == "cleanup_unknown":
            continue
        if any(keyword in normalized for keyword in keywords):
            confidence = 0.94 if any(keyword in _normalize_blob(folder_path) for keyword in keywords) else 0.76
            if lane == "case_folder" and not is_folder and any(normalized.endswith(ext) for ext in MEDIA_EXTENSIONS):
                confidence = 0.96
            return lane, confidence
    return "cleanup_unknown", 0.35


def classify_document_kind(
    *,
    title: str,
    mime_type: str,
    folder_path: str,
    lane: DriveLane,
    is_folder: bool,
) -> tuple[DriveDocumentKind, float]:
    normalized_title = _normalize_blob(title)
    normalized_path = _normalize_blob(folder_path)
    mime = str(mime_type or "").lower()
    if is_folder:
        if lane == "case_folder":
            return "media_bundle", 0.95
        return "generic_document", 0.45
    if normalized_title.endswith(MEDIA_EXTENSIONS) or mime.startswith("image/") or mime.startswith("video/"):
        return "media_asset", 0.97 if lane == "case_folder" else 0.74
    if lane == "commercial_pricing" and (normalized_title.endswith(".xlsx") or normalized_title.endswith(".xls")):
        return "pricing_workbook", 0.86 if "date-base33" in normalized_title or "koszt" in normalized_title else 0.72
    for pattern, document_kind in KIND_PATTERNS:
        if pattern.search(title) or pattern.search(folder_path):
            return document_kind, 0.92 if pattern.search(title) else 0.78
    if lane == "scans_intake":
        return "scan_backlog", 0.88
    if lane == "technical_reference":
        return "technical_reference", 0.74
    if lane == "offer_library":
        return "offer_template" if "kit-" in normalized_title else "offer_family_reference", 0.72
    if lane == "formal_contracts":
        return "contract", 0.62
    if lane == "commercial_transactions":
        return "invoice" if "faktura" in normalized_path else "order", 0.58
    if normalized_title.endswith(".pdf") or mime == "application/pdf":
        return "generic_document", 0.45
    return "generic_document", 0.3


def classify_scope(*, lane: DriveLane, document_kind: DriveDocumentKind) -> DriveScope:
    if document_kind in {"contract", "order", "deposit_invoice", "invoice", "warranty_card", "service_protocol", "media_bundle", "media_asset", "scan_backlog"}:
        return "case_specific"
    if document_kind in {"contract_template", "offer_template"}:
        return "reference_template"
    if lane in {"commercial_pricing", "technical_reference", "media_marketing", "offer_library"}:
        return "company_reference"
    return "company_reference"


def infer_probable_case_key(*, title: str, folder_path: str, metadata: dict[str, Any]) -> str:
    explicit = str(metadata.get("probable_case_key") or "").strip()
    if explicit:
        return explicit
    normalized = _normalize_blob(" ".join(part for part in (folder_path, title) if part))
    for hint, case_key in CASE_HINT_MAP.items():
        if hint in normalized:
            return case_key
    order_match = re.search(r"\b(zam[-\s]?\d+|zal[-\s]?\d+)\b", normalized, re.IGNORECASE)
    if order_match:
        token = order_match.group(1).replace(" ", "").lower()
        return token.replace("-", "_")
    if any(city in normalized for city in ("siedlec", "psary", "zator", "regulice", "sosnowiec", "myslachowice", "myślachowice")):
        tokens = [token for token in re.split(r"[^a-z0-9]+", normalized) if token][:6]
        return "_".join(tokens)
    return ""


def apply_classification(candidate: DriveIngestCandidate) -> DriveIngestCandidate:
    classified = classify_candidate(
        title=candidate.title,
        mime_type=candidate.mime_type,
        folder_path=candidate.folder_path,
        is_folder=candidate.is_folder,
        metadata=candidate.metadata,
    )
    candidate.lane = classified["lane"]
    candidate.document_kind = classified["document_kind"]
    candidate.scope = classified["scope"]
    candidate.probable_case_key = classified["probable_case_key"]
    candidate.classification_confidence = float(classified["classification_confidence"])
    return candidate


def _normalize_blob(value: str) -> str:
    cleaned = unicodedata.normalize("NFKD", str(value or ""))
    cleaned = "".join(character for character in cleaned if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", cleaned).strip().lower()


__all__ = [
    "apply_classification",
    "classify_candidate",
    "classify_document_kind",
    "classify_lane",
    "classify_scope",
    "infer_probable_case_key",
]
