"""Fact extraction from text and HVAC signals for mailbox memory."""
from __future__ import annotations
import re
from typing import Any

# Regex patterns for fact extraction
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d[\d\s\-\(\)]{7,}\d)")
AREA_RE = re.compile(r"(\d{2,4})\s*m[2\^]?", re.IGNORECASE)
CITY_HINT_RE = re.compile(r"\b(miasto|miejscowość|miejscowosc|gmina|dzielnica|osiedle)\s+([a-ząęćłńóśźż]{3,})", re.IGNORECASE)
BUILDING_TYPE_WORDS = ("dom", "blok", "mieszkanie", "kamienica", "szeregowiec", "bliźniak", "wolnostojący")
CASE_TOKEN_RE = re.compile(r"\b(?:sprawa|case|zlecenie|nr)\s*(?::|nr|\.)?\s*#?([A-Z0-9][A-Z0-9/_\-]{3,})", re.IGNORECASE)

# Additional patterns for text extraction
_KW_RE = re.compile(r"(\d{1,3}(?:\.\d)?)\s*(?:kw|kilowat(?:ów|y|a)?)", re.IGNORECASE)
_NIP_RE = re.compile(r"\bNIP\s*\d{10}\b", re.IGNORECASE)
_KRS_RE = re.compile(r"\bKRS\s*\d{10}\b", re.IGNORECASE)
_REGON_RE = re.compile(r"\bREGON\s*\d{9}\b", re.IGNORECASE)
_OFFER_REF_RE = re.compile(r"[A-Z]+\s*\d{5,}-\d{3,}")
_SINGLE_FAMILY_RE = re.compile(r"\bdom\s+jednorodzinny\b", re.IGNORECASE)
_BLIZNIAK_RE = re.compile(r"\bbliźniak", re.IGNORECASE)
_SZEREGOWIEC_RE = re.compile(r"\bszeregowiec", re.IGNORECASE)
_MIESZKANIE_RE = re.compile(r"\bmieszkanie\b", re.IGNORECASE)


def infer_document_kind(file_name: str, mime_type: str) -> str:
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    mime = mime_type.lower()
    if ext in ("pdf",) or "pdf" in mime:
        return "pdf"
    if ext in ("doc", "docx") or "word" in mime:
        return "docx"
    if ext in ("xls", "xlsx") or "spreadsheet" in mime or "excel" in mime:
        return "spreadsheet"
    if ext in ("zip", "rar", "7z", "tar", "gz"):
        return "archive"
    if ext in ("jpg", "jpeg", "png", "gif", "bmp", "tiff") or mime.startswith("image/"):
        return "image"
    if ext in ("dwg", "dxf", "ifc") or "drawing" in mime:
        return "cad_drawing"
    if ext in ("eml", "msg") or "email" in mime:
        return "email_file"
    return "other"


def summarize_document_text(text: str, *, file_name: str) -> str:
    cleaned = str(text or "").strip()[:220]
    if not cleaned:
        return f"[{file_name}] — brak wyodrębnionego tekstu."
    return cleaned.rstrip() + ("..." if len(text or "") > 220 else "")


def _is_real_email(email: str) -> bool:
    e = str(email or "").strip().lower()
    if not e or "@" not in e:
        return False
    local, domain = e.split("@", 1)
    if len(local) < 2 or "." not in domain:
        return False
    if domain in {"example.com", "test.com", "localhost"}:
        return False
    return True


def _build_fact(
    *,
    scope: str,
    fact_key: str,
    value: str,
    source_type: str,
    source_ref: str,
    message_id: str = "",
    confidence: float = 0.8,
    observed_at: str = "",
) -> dict[str, Any]:
    return {
        "scope": scope, "fact_key": fact_key, "normalized_value": str(value or "").strip(),
        "source_type": source_type, "source_ref": str(source_ref or "").strip(),
        "message_id": str(message_id or "").strip(), "confidence": max(0.0, min(1.0, confidence)),
        "observed_at": str(observed_at or "").strip(),
    }


def _guess_customer_name(sender: str) -> str:
    name = str(sender or "").strip()
    if not name or "@" not in name:
        return ""
    return name.split("@")[0].replace(".", " ").replace("_", " ").strip()


def _extract_first_email(text: str) -> str:
    match = EMAIL_RE.search(text or "")
    return match.group(0) if match else ""


def stable_id(prefix: str, *parts: str) -> str:
    import hashlib
    seed = "::".join(str(part or "").strip() for part in parts if str(part or "").strip())
    if not seed:
        seed = prefix
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def extract_facts_from_text(
    text: str,
    *,
    file_name: str = "",
    case_id: str = "",
    message_id: str = "",
    document_id: str = "",
    source_type: str = "message",
    source_ref: str = "",
    observed_at: str = "",
    entity_scope: str = "customer",
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract facts from raw text using regex patterns.

    Returns a list of dicts suitable for append_fact_rows.
    Uses the shared regex patterns defined at module level.
    """
    facts: list[dict[str, Any]] = []
    meta = dict(metadata or {})
    body = str(text or "")

    # Building type
    bt: str | None = None
    if _SINGLE_FAMILY_RE.search(body):
        bt = "single_family_house"
    elif _BLIZNIAK_RE.search(body):
        bt = "semi_detached"
    elif _SZEREGOWIEC_RE.search(body):
        bt = "terraced"
    elif _MIESZKANIE_RE.search(body):
        bt = "apartment"
    if bt:
        facts.append(_build_fact(
            scope=entity_scope,
            fact_key="building_type",
            value=bt,
            source_type=source_type,
            source_ref=source_ref,
            message_id=message_id,
            confidence=0.85,
            observed_at=observed_at,
        ))

    # Power kW
    kw_match = _KW_RE.search(body)
    if kw_match:
        val = float(kw_match.group(1))
        if val <= 100:
            facts.append(_build_fact(
                scope=entity_scope,
                fact_key="power_kw",
                value=str(val),
                source_type=source_type,
                source_ref=source_ref,
                message_id=message_id,
                confidence=0.9,
                observed_at=observed_at,
            ))

    # Heated area (m2)
    area_match = AREA_RE.search(body)
    if area_match:
        facts.append(_build_fact(
            scope=entity_scope,
            fact_key="heated_area_m2",
            value=area_match.group(1),
            source_type=source_type,
            source_ref=source_ref,
            message_id=message_id,
            confidence=0.8,
            observed_at=observed_at,
        ))

    # Phone — exclude NIP/KRS/REGON numbers and offer references
    has_business_ids = bool(_NIP_RE.search(body) or _KRS_RE.search(body) or _REGON_RE.search(body))
    phone_match = PHONE_RE.search(body)
    if phone_match and not _OFFER_REF_RE.search(body):
        phone = phone_match.group(0).strip()
        digits = re.sub(r"\D", "", phone)
        # Skip if NIP/KRS/REGON present and phone looks like a business ID (9-10 digits)
        is_business_id = has_business_ids and len(digits) in (9, 10)
        if not is_business_id:
            facts.append(_build_fact(
                scope=entity_scope,
                fact_key="customer_phone",
                value=phone,
                source_type=source_type,
                source_ref=source_ref,
                message_id=message_id,
                confidence=0.75,
                observed_at=observed_at,
            ))

    # Email
    email_match = EMAIL_RE.search(body)
    if email_match:
        email = email_match.group(0)
        if _is_real_email(email):
            facts.append(_build_fact(
                scope=entity_scope,
                fact_key="customer_email",
                value=email,
                source_type=source_type,
                source_ref=source_ref,
                message_id=message_id,
                confidence=0.95,
                observed_at=observed_at,
            ))

    # City hint
    city_match = CITY_HINT_RE.search(body)
    if city_match:
        facts.append(_build_fact(
                scope=entity_scope,
                fact_key="city",
                value=city_match.group(2).strip().capitalize(),
                source_type=source_type,
                source_ref=source_ref,
                message_id=message_id,
                confidence=0.7,
                observed_at=observed_at,
            ))

    return facts
