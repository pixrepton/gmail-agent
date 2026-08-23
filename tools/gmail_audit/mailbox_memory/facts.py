"""Fact extraction from text and HVAC signals for mailbox memory."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

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


def _evidence_key(ref: dict[str, Any]) -> str:
    """Deterministic evidence identity for dedup (content-based, order-safe)."""
    return json.dumps(ref, sort_keys=True, ensure_ascii=False, default=str)


SubjectResolution = Literal["EXPLICIT", "SINGLE_SUBJECT_DEFAULT", "AMBIGUOUS"]

SUBJECT_KIND_CASE = "CASE"
SUBJECT_KIND_CUSTOMER = "CUSTOMER"
SUBJECT_KIND_PROPERTY = "PROPERTY"
SUBJECT_KIND_DEVICE = "DEVICE"
SUBJECT_KIND_SERVICE_EVENT = "SERVICE_EVENT"

CASE_FACT_KEYS = frozenset(
    {
        "agent_draft",
        "amount_total",
        "case_deadline",
        "case_label",
        "case_note",
        "deposit_invoice_number",
        "document_date",
        "due_date",
        "invoice_number",
        "issue_date",
        "nip",
        "offer_family",
        "order_number",
        "price",
        "probable_case_key",
        "reference_token",
        "service_frequency",
        "validity_date",
        "vendor",
        "warranty_term",
    }
)
CUSTOMER_FACT_KEYS = frozenset({"customer_email", "customer_name", "customer_phone"})
PROPERTY_FACT_KEYS = frozenset(
    {
        "address",
        "building_type",
        "city",
        "current_heating_source",
        "floor_heating_existing",
        "floor_heating_scope",
        "heated_area_m2",
        "installation_address",
        "postal_code",
    }
)
DEVICE_FACT_KEYS = frozenset(
    {
        "device_brand",
        "device_model",
        "error_code",
        "fault_code",
        "manufacturer",
        "power_kw",
        "product_model",
        "serial_number",
    }
)
SERVICE_EVENT_FACT_KEYS = frozenset({"preferred_service_date", "service_date"})
SUBJECT_AWARE_FACT_KEYS = frozenset(
    CASE_FACT_KEYS | CUSTOMER_FACT_KEYS | PROPERTY_FACT_KEYS | DEVICE_FACT_KEYS | SERVICE_EVENT_FACT_KEYS
)

SUBJECT_PREFIX_TO_KIND = {
    "case": SUBJECT_KIND_CASE,
    "customer": SUBJECT_KIND_CUSTOMER,
    "property": SUBJECT_KIND_PROPERTY,
    "device": SUBJECT_KIND_DEVICE,
    "service": SUBJECT_KIND_SERVICE_EVENT,
    "service_event": SUBJECT_KIND_SERVICE_EVENT,
}


@dataclass(frozen=True, slots=True)
class SubjectRef:
    kind: str
    id: str
    resolution: SubjectResolution
    evidence_basis: str = ""

    def to_metadata(self) -> dict[str, str]:
        payload = {
            "kind": str(self.kind or "").strip(),
            "id": str(self.id or "").strip(),
            "resolution": str(self.resolution or "").strip(),
        }
        if str(self.evidence_basis or "").strip():
            payload["evidence_basis"] = str(self.evidence_basis or "").strip()
        return payload


def is_subject_aware_fact_key(fact_key: str) -> bool:
    return str(fact_key or "").strip() in SUBJECT_AWARE_FACT_KEYS


def fact_subject_kind(fact_key: str) -> str:
    key = str(fact_key or "").strip()
    if key in CASE_FACT_KEYS:
        return SUBJECT_KIND_CASE
    if key in CUSTOMER_FACT_KEYS:
        return SUBJECT_KIND_CUSTOMER
    if key in PROPERTY_FACT_KEYS:
        return SUBJECT_KIND_PROPERTY
    if key in DEVICE_FACT_KEYS:
        return SUBJECT_KIND_DEVICE
    if key in SERVICE_EVENT_FACT_KEYS:
        return SUBJECT_KIND_SERVICE_EVENT
    return ""


def _subject_meta(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}


def _normalize_subject_kind(value: Any) -> str:
    key = str(value or "").strip().upper()
    return key if key in {SUBJECT_KIND_CASE, SUBJECT_KIND_CUSTOMER, SUBJECT_KIND_PROPERTY, SUBJECT_KIND_DEVICE, SUBJECT_KIND_SERVICE_EVENT} else ""


def _subject_kind_from_identity(subject_identity: str) -> str:
    prefix = str(subject_identity or "").strip().split(":", 1)[0].lower()
    return SUBJECT_PREFIX_TO_KIND.get(prefix, "")


def _stored_subject_ref(payload: dict[str, Any]) -> SubjectRef | None:
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    meta_ref = meta.get("subject_ref") if isinstance(meta.get("subject_ref"), dict) else {}
    subject_identity = str(
        meta_ref.get("id")
        or meta_ref.get("subject_id")
        or
        payload.get("subject_identity")
        or payload.get("proposition_subject_id")
        or meta.get("subject_identity")
        or meta.get("proposition_subject_id")
        or ""
    ).strip()
    if not subject_identity:
        return None
    subject_kind = _normalize_subject_kind(
        meta_ref.get("kind")
        or payload.get("subject_kind")
        or meta.get("subject_kind")
        or _subject_kind_from_identity(subject_identity)
        or fact_subject_kind(payload.get("fact_key"))
    )
    if not subject_kind:
        return None
    resolution = str(
        meta_ref.get("resolution")
        or payload.get("subject_resolution")
        or meta.get("subject_resolution")
        or "EXPLICIT"
    ).strip().upper()
    if resolution not in {"EXPLICIT", "SINGLE_SUBJECT_DEFAULT", "AMBIGUOUS"}:
        resolution = "EXPLICIT"
    evidence_basis = str(
        meta_ref.get("evidence_basis")
        or payload.get("subject_evidence_basis")
        or meta.get("subject_evidence_basis")
        or ""
    ).strip()
    return SubjectRef(
        kind=subject_kind,
        id=subject_identity,
        resolution=resolution,
        evidence_basis=evidence_basis,
    )


def _ambiguous_subject_ref(payload: dict[str, Any], *, subject_kind: str) -> SubjectRef | None:
    case_id = str(payload.get("case_id") or "").strip()
    fact_key = str(payload.get("fact_key") or "").strip()
    if not case_id or not fact_key:
        return None
    document_id = str(payload.get("document_id") or "").strip()
    message_id = str(payload.get("message_id") or "").strip()
    source_ref = str(payload.get("source_ref") or "").strip()
    fact_id = str(payload.get("fact_id") or "").strip()
    evidence_basis = ""
    if document_id:
        evidence_basis = f"document:{document_id}"
    elif message_id:
        evidence_basis = f"message:{message_id}"
    elif source_ref:
        evidence_basis = f"source:{source_ref}"
    elif fact_id:
        evidence_basis = f"fact:{fact_id}"
    else:
        evidence_basis = stable_id(
            "ambiguous",
            case_id,
            fact_key,
            str(payload.get("normalized_value") or "").strip(),
        )
    bucket = stable_id("subject", case_id, subject_kind.lower(), evidence_basis)
    return SubjectRef(
        kind=subject_kind,
        id=f"case:{case_id}:ambiguous_{subject_kind.lower()}:{bucket}",
        resolution="AMBIGUOUS",
        evidence_basis=evidence_basis,
    )


def _single_subject_candidate_from_case_facts(
    case_facts: list[dict[str, Any]] | None,
    *,
    subject_kind: str,
) -> str:
    if not case_facts:
        return ""
    candidates: set[str] = set()
    for item in case_facts:
        ref = _stored_subject_ref(item)
        if ref is None:
            continue
        if ref.kind != subject_kind or ref.resolution == "AMBIGUOUS":
            continue
        if str(ref.id or "").strip():
            candidates.add(str(ref.id or "").strip())
    if len(candidates) == 1:
        return next(iter(candidates))
    return ""


def fact_subject_ref(
    payload: dict[str, Any],
    *,
    case_facts: list[dict[str, Any]] | None = None,
) -> SubjectRef | None:
    stored = _stored_subject_ref(payload)
    if stored is not None and stored.resolution == "EXPLICIT":
        return stored
    fact_key = str(payload.get("fact_key") or "").strip()
    subject_kind = stored.kind if stored is not None else fact_subject_kind(fact_key)
    if not subject_kind:
        return None
    case_id = str(payload.get("case_id") or "").strip()
    if subject_kind == SUBJECT_KIND_CASE and case_id:
        return SubjectRef(
            kind=SUBJECT_KIND_CASE,
            id=f"case:{case_id}",
            resolution="EXPLICIT",
            evidence_basis="case_id",
        )
    if subject_kind == SUBJECT_KIND_CUSTOMER and case_id:
        return SubjectRef(
            kind=SUBJECT_KIND_CUSTOMER,
            id=f"case:{case_id}:primary_customer",
            resolution="SINGLE_SUBJECT_DEFAULT",
            evidence_basis="case_primary_customer",
        )
    if subject_kind == SUBJECT_KIND_PROPERTY and case_id:
        return SubjectRef(
            kind=SUBJECT_KIND_PROPERTY,
            id=f"case:{case_id}:primary_property",
            resolution="SINGLE_SUBJECT_DEFAULT",
            evidence_basis="case_primary_property",
        )
    if subject_kind in {SUBJECT_KIND_DEVICE, SUBJECT_KIND_SERVICE_EVENT}:
        candidate = _single_subject_candidate_from_case_facts(case_facts, subject_kind=subject_kind)
        if candidate:
            return SubjectRef(
                kind=subject_kind,
                id=candidate,
                resolution="SINGLE_SUBJECT_DEFAULT",
                evidence_basis=f"single_{subject_kind.lower()}_candidate",
            )
        return _ambiguous_subject_ref(payload, subject_kind=subject_kind)
    return None


def subject_supersession_allowed(payload: dict[str, Any]) -> bool:
    meta = _subject_meta(payload)
    if meta.get("allow_subject_supersession") is True:
        return True
    mode = str(meta.get("supersession_mode") or "").strip().lower()
    if mode in {"subject_local_explicit", "explicit_correction"}:
        return True
    origin = str(meta.get("source_origin") or "").strip().upper()
    source_type = str(payload.get("source_type") or "").strip().lower()
    return origin in {"OPERATOR", "INTERNAL_STATE", "SYSTEM"} or source_type == "agent_write"


def attach_subject_metadata(
    payload: dict[str, Any],
    *,
    case_facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    out = dict(payload)
    meta = dict(_subject_meta(out))
    ref = fact_subject_ref({**out, "metadata": meta}, case_facts=case_facts)
    if ref is None:
        out["metadata"] = meta
        return out
    meta["subject_ref"] = ref.to_metadata()
    meta["subject_kind"] = ref.kind
    meta["subject_identity"] = ref.id
    meta["subject_resolution"] = ref.resolution
    if ref.evidence_basis:
        meta["subject_evidence_basis"] = ref.evidence_basis
    else:
        meta.pop("subject_evidence_basis", None)
    out["metadata"] = meta
    return out


def fact_subject_identity(
    payload: dict[str, Any],
    *,
    case_facts: list[dict[str, Any]] | None = None,
) -> str:
    ref = fact_subject_ref(payload, case_facts=case_facts)
    return str(ref.id if ref is not None else "").strip()


def proposition_identity(
    payload: dict[str, Any],
    *,
    case_facts: list[dict[str, Any]] | None = None,
) -> tuple[str, ...]:
    scope = str(payload.get("entity_scope") or "case").strip() or "case"
    fact_key = str(payload.get("fact_key") or "").strip()
    if is_subject_aware_fact_key(fact_key):
        ref = fact_subject_ref(payload, case_facts=case_facts)
        if ref is not None and str(ref.id or "").strip():
            return (ref.kind, ref.id, fact_key)
    return (scope, fact_key)


def row_evidence_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect evidence refs from one fact row.

    Reads ``metadata.evidence_ref`` / ``metadata.evidence_refs`` plus the row's
    own source identity (source_type/source_ref/message_id/document_id) as an
    implied evidence ref. Used by the same-value append evidence merge so
    multi-source provenance survives consolidation (P1.5).
    """
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    refs: list[dict[str, Any]] = []
    ref = meta.get("evidence_ref")
    if isinstance(ref, dict):
        refs.append(dict(ref))
    for item in meta.get("evidence_refs") or []:
        if isinstance(item, dict):
            refs.append(dict(item))
    implied: dict[str, Any] = {}
    if str(payload.get("source_type") or "").strip():
        implied["source_type"] = str(payload.get("source_type") or "").strip()
    if str(payload.get("source_ref") or "").strip():
        implied["source_ref"] = str(payload.get("source_ref") or "").strip()
    if str(payload.get("message_id") or "").strip():
        implied["message_id"] = str(payload.get("message_id") or "").strip()
    if str(payload.get("document_id") or "").strip():
        implied["document_id"] = str(payload.get("document_id") or "").strip()
    # P1.5B: supporting evidence must keep its own provenance authority, not
    # only the winning row-level trio.
    for key in ("source_origin", "evidence_authority", "instruction_authority"):
        value = str(meta.get(key) or "").strip()
        if value:
            implied[key] = value
    if implied:
        refs.append(implied)
    return refs


def merge_fact_evidence(
    metadata: dict[str, Any] | None,
    payload: dict[str, Any],
    *,
    cap: int = 16,
) -> dict[str, Any]:
    """Merge incoming row evidence into existing fact metadata (P1.5).

    Deterministic, idempotent: identical evidence (including retries of the
    same row) is deduplicated. Keeps ``evidence_refs`` (list) and
    ``evidence_ref`` (first) in the metadata; existing fields are preserved.
    """
    meta = dict(metadata) if isinstance(metadata, dict) else {}
    existing: list[dict[str, Any]] = []
    seen: set[str] = set()
    ref = meta.get("evidence_ref")
    if isinstance(ref, dict):
        key = _evidence_key(ref)
        if key not in seen:
            seen.add(key)
            existing.append(dict(ref))
    for item in meta.get("evidence_refs") or []:
        if isinstance(item, dict):
            key = _evidence_key(item)
            if key not in seen:
                seen.add(key)
                existing.append(dict(item))
    merged = list(existing)
    for incoming in row_evidence_refs(payload):
        key = _evidence_key(incoming)
        if key in seen:
            continue
        seen.add(key)
        merged.append(incoming)
    if len(merged) > cap:
        merged = merged[:cap]
    out = dict(meta)
    if merged:
        out["evidence_refs"] = merged
        out["evidence_ref"] = merged[0]
    else:
        out.pop("evidence_refs", None)
        out.pop("evidence_ref", None)
    return out


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
