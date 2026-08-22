"""Structure-first field extraction from DocumentParseResult (no full-text regex scan)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from document_intelligence_contract import EvidenceRef, ExtractedField, now_iso
from document_parse_contract import DocumentElement, DocumentParseResult
from evidence_authority import provenance_defaults

# Normalize Polish labels to mailbox fact keys.
_LABEL_TO_FACT_KEY: dict[str, str] = {
    "nip": "nip",
    "regon": "regon",
    "krs": "krs",
    "tel": "customer_phone",
    "tel.": "customer_phone",
    "telefon": "customer_phone",
    "phone": "customer_phone",
    "e-mail": "customer_email",
    "email": "customer_email",
    "mail": "customer_email",
    "miasto": "city",
    "miejscowosc": "city",
    "miejscowość": "city",
    "lokalizacja": "city",
    "adres": "address",
    "ulica": "address",
    "ul": "address",
    "ul.": "address",
    "m": "city",
    "m.": "city",
    "kod pocztowy": "postal_code",
    "faktura": "invoice_number",
    "nr faktury": "invoice_number",
    "numer faktury": "invoice_number",
    "wartość": "amount_total",
    "wartosc": "amount_total",
    "razem": "amount_total",
    "do zapłaty": "amount_total",
    "do zaplaty": "amount_total",
    "cena": "price",
    "model": "product_model",
    "typ": "product_model",
    "powierzchnia": "heated_area_m2",
    "powierzchni": "heated_area_m2",
    "metraż": "heated_area_m2",
    "metraz": "heated_area_m2",
}

_PHONE_LABELS = frozenset({"tel", "tel.", "telefon", "phone", "komórka", "komorka", "mobile", "gsm"})
_CITY_LABELS = frozenset({"miasto", "miejscowosc", "miejscowość", "lokalizacja", "m", "m."})
_ADDRESS_LABEL_PREFIXES = ("ul", "ul.", "ulica", "adres")
_ID_LABELS = frozenset({"nip", "regon", "krs"})
_CITY_NOISE_VALUES = frozenset({"vat", "netto", "brutto", "pln", "zl", "zł", "kwota", "razem", "suma", "wartość", "wartosc"})

# Role faktury — pozwalają rozróżnić sprzedaż (my wystawiamy) vs zakup (faktura kosztowa).
_SELLER_HEADERS = ("sprzedawca", "wystawca", "sprzedający", "sprzedajacy")
_BUYER_HEADERS = ("nabywca", "kupujący", "kupujacy", "odbiorca", "płatnik", "platnik")

_KEY_VALUE_LINE_RE = re.compile(
    r"^\s*([A-Za-zÀ-ÿąćęłńóśżźŁŚŻŹĆŃÓ0-9][A-Za-zÀ-ÿąćęłńóśżźŁŚŻŹĆŃÓ0-9 .\/\-]{1,48}?)\s*[:;]\s*(.+?)\s*$"
)
_LOOSE_KV_LINE_RE = re.compile(
    r"^\s*([A-Za-zÀ-ÿąćęłńóśżźŁŚŻŹĆŃÓ0-9][A-Za-zÀ-ÿąćęłńóśżźŁŚŻŹĆŃÓ0-9 .\/\-]{0,48}?)\s*[:;.]+\s*(.+?)\s*$",
    re.IGNORECASE,
)
_TEL_LABEL_ONLY_RE = re.compile(r"^(?:cds-)?tel\s*\.?\s*:?\s*$", re.IGNORECASE)
_EMBEDDED_LABEL_VALUE_RE = re.compile(
    r"^(tel\.?|telefon|phone|e-mail\.?|email)\s*[:.]?\s*(.+)$",
    re.IGNORECASE,
)
_NIP_INLINE_RE = re.compile(r"\bnip\s*[:\s.\-]*([0-9][0-9\s\-]{8,14}[0-9])", re.IGNORECASE)
_REGON_INLINE_RE = re.compile(r"\bregon\s*[:\s.\-]*([0-9]{9,14})", re.IGNORECASE)
_EMAIL_INLINE_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_POSTAL_CITY_RE = re.compile(
    r"(\d{2}-\d{3})\s*([A-Za-zÀ-ÿąćęłńóśżźĄĆĘŁŃÓŚŻŹ][A-Za-zÀ-ÿąćęłńóśżźĄĆĘŁŃÓŚŻŹ0-9][A-Za-zÀ-ÿąćęłńóśżźĄĆĘŁŃÓŚŻŹ0-9\s-]*)"
)
_PIPE_FOOTER_HINT_RE = re.compile(r"\b(?:ul\.?|tel\.?|www\.|@)\b", re.IGNORECASE)
_MARKDOWN_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s\-:|]+\|?\s*$")


def normalize_label(raw: str) -> str:
    text = str(raw or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .:-—")


def _split_embedded_label_value(raw: str) -> tuple[str, str] | None:
    m = _EMBEDDED_LABEL_VALUE_RE.match(str(raw or "").strip())
    if not m:
        return None
    return normalize_label(m.group(1)), m.group(2).strip()


def _enrich_kv(add, *, label: str, value: str, text: str, source_id: str, origin: str) -> None:
    if not label or not value:
        return
    add(
        DocumentElement(
            element_type="key_value",
            text=text,
            label=label,
            value=value,
            metadata={"source_id": source_id, "origin": origin},
        )
    )


def _enrich_address_segments(add, segment: str, *, source_id: str, origin: str) -> None:
    seg = segment.strip()
    if not seg or not re.search(r"\bul\.?\b", seg, re.IGNORECASE):
        return
    _enrich_kv(add, label="ulica", value=seg, text=seg, source_id=source_id, origin=origin)
    postal_city = _POSTAL_CITY_RE.search(seg)
    if not postal_city:
        return
    postal = postal_city.group(1).strip()
    city = postal_city.group(2).strip(" ,")
    if city.lower() not in _CITY_NOISE_VALUES:
        _enrich_kv(add, label="miasto", value=city, text=seg, source_id=source_id, origin=origin)
    _enrich_kv(add, label="kod pocztowy", value=postal, text=seg, source_id=source_id, origin=origin)


def _enrich_pipe_footer_line(add, stripped: str, *, source_id: str) -> bool:
    if "|" not in stripped or not _PIPE_FOOTER_HINT_RE.search(stripped):
        return False
    handled = False
    for segment in stripped.split("|"):
        seg = segment.strip()
        if not seg:
            continue
        embedded = _split_embedded_label_value(seg)
        if embedded:
            label, value = embedded
            _enrich_kv(add, label=label, value=value, text=stripped, source_id=source_id, origin="pipe_footer")
            handled = True
            continue
        if re.search(r"\bul\.?\b", seg, re.IGNORECASE):
            _enrich_address_segments(add, seg, source_id=source_id, origin="pipe_footer")
            handled = True
            continue
        email = _EMAIL_INLINE_RE.search(seg)
        if email:
            _enrich_kv(
                add,
                label="e-mail",
                value=email.group(0),
                text=stripped,
                source_id=source_id,
                origin="pipe_footer",
            )
            handled = True
    return handled


def _enrich_inline_ids(add, stripped: str, *, source_id: str) -> bool:
    handled = False
    for match in _NIP_INLINE_RE.finditer(stripped):
        _enrich_kv(
            add,
            label="nip",
            value=match.group(1).strip(),
            text=stripped,
            source_id=source_id,
            origin="inline_id",
        )
        handled = True
    for match in _REGON_INLINE_RE.finditer(stripped):
        _enrich_kv(
            add,
            label="regon",
            value=match.group(1).strip(),
            text=stripped,
            source_id=source_id,
            origin="inline_id",
        )
        handled = True
    return handled


def enrich_elements_from_plain_text(elements: list[DocumentElement], text: str, *, source_id: str = "") -> list[DocumentElement]:
    """Derive key_value and table_row elements from markdown/plain text (Docling export)."""
    out = list(elements)
    seen: set[tuple[str, str, str]] = set()

    def add(el: DocumentElement) -> None:
        key = (el.element_type, str(el.label or ""), str(el.value or ""))
        if key in seen:
            return
        seen.add(key)
        out.append(el)

    lines = str(text or "").splitlines()
    idx = 0
    while idx < len(lines):
        stripped = lines[idx].strip()
        idx += 1
        if not stripped:
            continue

        if _TEL_LABEL_ONLY_RE.match(stripped):
            next_idx = idx
            while next_idx < len(lines) and not lines[next_idx].strip():
                next_idx += 1
            if next_idx < len(lines):
                nxt = lines[next_idx].strip()
                digits = _digits_only(nxt)
                if 7 <= len(digits) <= 11:
                    _enrich_kv(
                        add,
                        label="tel",
                        value=nxt,
                        text=f"{stripped} {nxt}",
                        source_id=source_id,
                        origin="tel_next_line",
                    )
            continue

        if _enrich_pipe_footer_line(add, stripped, source_id=source_id):
            _enrich_inline_ids(add, stripped, source_id=source_id)
            continue

        kv = _KEY_VALUE_LINE_RE.match(stripped) or _LOOSE_KV_LINE_RE.match(stripped)
        if kv:
            label = normalize_label(kv.group(1))
            value = kv.group(2).strip()
            if label and value:
                _enrich_kv(
                    add,
                    label=label,
                    value=value,
                    text=f"{kv.group(1)}: {value}",
                    source_id=source_id,
                    origin="plain_text_kv",
                )
            continue

        if _enrich_inline_ids(add, stripped, source_id=source_id):
            continue

        if "|" in stripped and not _MARKDOWN_TABLE_SEP_RE.match(stripped):
            if _PIPE_FOOTER_HINT_RE.search(stripped):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|") if c.strip()]
            if len(cells) >= 2 and not re.search(r"\bul\.?\b|\btel\.?\b", cells[0], re.IGNORECASE):
                label = normalize_label(cells[0])
                value = cells[1]
                add(
                    DocumentElement(
                        element_type="table_row",
                        text=stripped,
                        label=label,
                        value=value,
                        metadata={"source_id": source_id, "origin": "markdown_table", "cells": cells},
                    )
                )

    return out


def _digits_only(value: str) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _is_plausible_phone(*, label: str, value: str, digits: str) -> bool:
    if label in _ID_LABELS:
        return False
    if normalize_label(label) in _CITY_NOISE_VALUES:
        return False
    if "vat" in normalize_label(label):
        return False
    if len(digits) == 10 and label not in _PHONE_LABELS:
        # REGON/KRS/NIP-style identifiers without explicit phone label
        return False
    if len(digits) == 9:
        return label in _PHONE_LABELS or "tel" in label
    if len(digits) == 11 and digits.startswith("48"):
        return label in _PHONE_LABELS or "tel" in label
    return label in _PHONE_LABELS and 7 <= len(digits) <= 11


def _is_plausible_city(label: str, value: str) -> bool:
    if label not in _CITY_LABELS:
        return False
    norm = value.strip().lower()
    if norm in _CITY_NOISE_VALUES:
        return False
    if len(norm) < 3:
        return False
    if any(ch.isdigit() for ch in value):
        return False
    if "vat" in norm:
        return False
    return True


def _field_type_for_fact_key(fact_key: str) -> str:
    if fact_key in {"customer_phone"}:
        return "phone"
    if fact_key in {"customer_email"}:
        return "email"
    if fact_key in {"city", "address", "postal_code"}:
        return "address"
    if fact_key in {"nip", "regon", "krs"}:
        return "company"
    if fact_key in {"amount_total", "price"}:
        return "amount"
    if fact_key in {"product_model"}:
        return "product"
    return "generic"


def _confidence_for_labeled_field(fact_key: str, label: str) -> float:
    if fact_key in _ID_LABELS:
        return 0.88
    if fact_key == "customer_phone" and label in _PHONE_LABELS:
        return 0.9
    if fact_key == "city" and label in _CITY_LABELS:
        return 0.82
    if fact_key == "customer_email":
        return 0.9
    return 0.8


def extract_invoice_parties(text: str) -> dict[str, str]:
    """Z treści faktury wyłuskaj NIP Sprzedawcy i Nabywcy (same cyfry).

    Sekcje wykrywane po nagłówkach; NIP brany z okna od nagłówka do następnego
    nagłówka strony (maks. 600 znaków). Dla dokumentów nie-faktur zwraca {}.
    """
    raw = str(text or "")
    low = raw.lower()
    if not low:
        return {}
    positions = sorted(
        low.find(h) for h in (_SELLER_HEADERS + _BUYER_HEADERS) if low.find(h) != -1
    )

    def _region_nip(headers: tuple[str, ...]) -> str:
        for header in headers:
            idx = low.find(header)
            if idx == -1:
                continue
            later = [p for p in positions if p > idx]
            end = min(later) if later else idx + 600
            window = raw[idx:min(end, idx + 600)]
            match = _NIP_INLINE_RE.search(window)
            if match:
                return re.sub(r"\D+", "", match.group(1))
        return ""

    out: dict[str, str] = {}
    seller = _region_nip(_SELLER_HEADERS)
    buyer = _region_nip(_BUYER_HEADERS)
    if seller:
        out["seller_nip"] = seller
    if buyer and buyer != seller:
        out["buyer_nip"] = buyer
    return out


def extract_structured_fields(
    result: DocumentParseResult,
    *,
    source_id: str = "",
) -> list[dict[str, Any]]:
    """Map labeled document elements to ExtractedField dicts."""
    sid = str(source_id or result.metadata.get("source_id") or "").strip()
    elements = list(result.elements)
    if result.plain_text.strip():
        elements = enrich_elements_from_plain_text(elements, result.plain_text, source_id=sid)

    fields: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for element in elements:
        label = normalize_label(element.label or "")
        value = str(element.value or "").strip()
        if not label and element.element_type == "key_value" and ":" in element.text:
            parts = element.text.split(":", 1)
            label = normalize_label(parts[0])
            value = parts[1].strip()
        if not value:
            continue
        embedded = _split_embedded_label_value(value)
        if embedded:
            emb_label, emb_value = embedded
            if not _LABEL_TO_FACT_KEY.get(label) or emb_label in _PHONE_LABELS | {"e-mail", "email"}:
                label, value = emb_label, emb_value
        if not label or not value:
            continue

        fact_key = _LABEL_TO_FACT_KEY.get(label)
        if not fact_key:
            continue

        if fact_key == "customer_phone":
            digits = _digits_only(value)
            if not _is_plausible_phone(label=label, value=value, digits=digits):
                continue
            value = digits[-9:] if len(digits) >= 9 else digits
        elif fact_key == "city":
            if not _is_plausible_city(label, value):
                continue
        elif fact_key in _ID_LABELS:
            digits = _digits_only(value)
            if not digits:
                continue
            value = digits
        elif fact_key == "customer_email":
            if "@" not in value:
                continue
        elif fact_key == "heated_area_m2":
            area_match = re.search(r"(\d{2,4}(?:[.,]\d{1,2})?)", value)
            if not area_match:
                continue
            value = area_match.group(1).replace(",", ".")

        dedupe = (fact_key, value.lower())
        if dedupe in seen:
            continue
        seen.add(dedupe)

        confidence = _confidence_for_labeled_field(fact_key, label)
        excerpt = element.text[:500] if element.text else f"{label}: {value}"
        fields.append(
            ExtractedField(
                field_name=fact_key,
                field_value=value[:300],
                field_type=_field_type_for_fact_key(fact_key),
                confidence=confidence,
                evidence_ref=EvidenceRef(
                    source_id=sid,
                    page=int(element.page or 1),
                    excerpt=excerpt[:500],
                ).to_dict(),
            ).to_dict()
        )

    # Faktura: przypisz NIP do roli Sprzedawca/Nabywca (kierunek sprzedaż/zakup ustala agent).
    if result.plain_text.strip():
        existing_keys = {str(f.get("field_name") or "") for f in fields}
        for role_key, role_nip in extract_invoice_parties(result.plain_text).items():
            if role_key in existing_keys or not role_nip:
                continue
            fields.append(
                ExtractedField(
                    field_name=role_key,
                    field_value=role_nip,
                    field_type="company",
                    confidence=0.9,
                    evidence_ref=EvidenceRef(source_id=sid, page=1, excerpt=f"{role_key}: {role_nip}").to_dict(),
                ).to_dict()
            )

    return fields


def structured_fields_to_fact_rows(
    fields: list[dict[str, Any]],
    *,
    case_id: str,
    document_id: str,
    message_id: str = "",
    observed_at: str | None = None,
    parser_id: str = "",
) -> list[dict[str, Any]]:
    """Promote structured document fields to mailbox_memory_facts rows."""
    if not case_id or not document_id or not fields:
        return []
    ts = observed_at or now_iso()
    rows: list[dict[str, Any]] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        fact_key = str(field.get("field_name") or "").strip()
        value = str(field.get("field_value") or "").strip()
        if not fact_key or not value:
            continue
        try:
            confidence = float(field.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        evidence_ref = field.get("evidence_ref") if isinstance(field.get("evidence_ref"), dict) else {}
        source_ref = "structured_parse:" + ":".join(
            part
            for part in (
                parser_id,
                str(evidence_ref.get("source_id") or document_id),
                str(evidence_ref.get("page") or ""),
            )
            if part
        )
        fact_id = "sfact_" + hashlib.sha256(f"{document_id}:{fact_key}:{value}:{source_ref}".encode()).hexdigest()[:24]
        rows.append(
            {
                "fact_id": fact_id,
                "case_id": case_id,
                "message_id": message_id,
                "document_id": document_id,
                "entity_scope": "document",
                "fact_key": fact_key,
                "normalized_value": value.lower() if fact_key not in {"customer_phone", "nip", "regon", "krs"} else value,
                "raw_value": value,
                "confidence": max(0.0, min(1.0, confidence)),
                "observed_at": ts,
                "source_type": "structured_document_parse",
                "source_ref": source_ref,
                "status": "active",
                "metadata": {
                    "parser_id": parser_id,
                    "field_type": str(field.get("field_type") or "generic"),
                    "evidence_ref": evidence_ref,
                    "extraction": "structure_first",
                    # P1.5: provenance trio stamped at creation so the resolved
                    # fact view never degrades document evidence to DERIVED.
                    **provenance_defaults(origin="ATTACHMENT"),
                },
            }
        )
    return rows


__all__ = [
    "enrich_elements_from_plain_text",
    "extract_invoice_parties",
    "extract_structured_fields",
    "normalize_label",
    "structured_fields_to_fact_rows",
]
