"""P2.0 Customer identity metadata — identity_kind + property_anchor (RFC v1 §9)."""

from __future__ import annotations

import re
from typing import Any

IDENTITY_KIND_PERSON = "person"
IDENTITY_KIND_ORGANIZATION = "organization"
VALID_IDENTITY_KINDS = {IDENTITY_KIND_PERSON, IDENTITY_KIND_ORGANIZATION}

BINDING_LEVEL_TECHNICAL = 1

_ORG_LOCAL_PARTS = frozenset(
    {
        "biuro",
        "office",
        "info",
        "kontakt",
        "contact",
        "handel",
        "marketing",
        "bok",
        "admin",
        "sprzedaz",
        "sales",
        "noreply",
        "no-reply",
    }
)
_ORG_NAME_RE = re.compile(
    r"\b(sp\.?\s*z\s*o\.?o\.?|s\.?a\.?|spółka|firma|ltd|gmbh|inc\.?|company)\b",
    re.IGNORECASE,
)
_NIP_RE = re.compile(r"\b(\d{10}|\d{3}-\d{3}-\d{2}-\d{2})\b")
_WS_RE = re.compile(r"\s+")


def _coerce_dict(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, dict) else {}


def normalize_address_norm(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    text = _WS_RE.sub(" ", text)
    return text


def normalize_nip(raw: str) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits if len(digits) == 10 else ""


def infer_identity_kind(
    *,
    email: str = "",
    display_name: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    meta = _coerce_dict(metadata)
    if str(meta.get("identity_kind") or "").strip() in VALID_IDENTITY_KINDS:
        return str(meta["identity_kind"]).strip()
    if normalize_nip(str(meta.get("nip") or meta.get("tax_id") or "")):
        return IDENTITY_KIND_ORGANIZATION
    if str(meta.get("company_name") or meta.get("organization") or "").strip():
        return IDENTITY_KIND_ORGANIZATION
    name = str(display_name or meta.get("display_name") or "").strip()
    if name and _ORG_NAME_RE.search(name):
        return IDENTITY_KIND_ORGANIZATION
    local = str(email or "").split("@", 1)[0].strip().lower()
    if local in _ORG_LOCAL_PARTS:
        return IDENTITY_KIND_ORGANIZATION
    return IDENTITY_KIND_PERSON


def build_property_anchor(
    *,
    address: str = "",
    address_norm: str = "",
    nip: str = "",
    investment_key: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    anchor: dict[str, Any] = {}
    addr = normalize_address_norm(address_norm or address)
    if addr:
        anchor["address_norm"] = addr
    nip_norm = normalize_nip(nip)
    if nip_norm:
        anchor["nip"] = nip_norm
    key = str(investment_key or "").strip()
    if key:
        anchor["investment_key"] = key
    for item in (_coerce_dict(extra)).items():
        if item[1] is not None and str(item[1]).strip():
            anchor.setdefault(str(item[0]), item[1])
    return anchor


def normalize_identity_metadata(metadata: dict[str, Any] | None, *, email: str = "", display_name: str = "") -> dict[str, Any]:
    meta = _coerce_dict(metadata)
    kind = infer_identity_kind(email=email, display_name=display_name, metadata=meta)
    meta["identity_kind"] = kind
    roles = meta.get("contact_roles")
    if roles is not None and not isinstance(roles, list):
        meta["contact_roles"] = [str(roles)]
    return meta


def normalize_engagement_metadata(
    metadata: dict[str, Any] | None,
    *,
    property_anchor: dict[str, Any] | None = None,
    binding_level: int = BINDING_LEVEL_TECHNICAL,
) -> dict[str, Any]:
    meta = _coerce_dict(metadata)
    anchor_in = _coerce_dict(property_anchor) or _coerce_dict(meta.get("property_anchor"))
    anchor = build_property_anchor(
        address_norm=str(anchor_in.get("address_norm") or anchor_in.get("address") or ""),
        nip=str(anchor_in.get("nip") or ""),
        investment_key=str(anchor_in.get("investment_key") or ""),
    )
    if anchor:
        meta["property_anchor"] = anchor
    if binding_level > 0:
        meta["binding_level_applied"] = int(binding_level)
    return meta


def merge_identity_metadata(
    existing: dict[str, Any] | None,
    *,
    email: str = "",
    display_name: str = "",
    hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = _coerce_dict(existing)
    for key, value in _coerce_dict(hints).items():
        if value is not None and str(value).strip():
            base[key] = value
    return normalize_identity_metadata(base, email=email, display_name=display_name)


def merge_engagement_metadata(
    existing: dict[str, Any] | None,
    *,
    property_anchor: dict[str, Any] | None = None,
    binding_level: int = BINDING_LEVEL_TECHNICAL,
    hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = _coerce_dict(existing)
    for key, value in _coerce_dict(hints).items():
        if key == "property_anchor" and isinstance(value, dict):
            merged_anchor = build_property_anchor(
                address_norm=str(value.get("address_norm") or value.get("address") or ""),
                nip=str(value.get("nip") or ""),
                investment_key=str(value.get("investment_key") or ""),
                extra=_coerce_dict(base.get("property_anchor")),
            )
            if merged_anchor:
                base["property_anchor"] = merged_anchor
            continue
        if value is not None and str(value).strip():
            base[key] = value
    return normalize_engagement_metadata(
        base,
        property_anchor=_coerce_dict(property_anchor) or _coerce_dict(base.get("property_anchor")),
        binding_level=binding_level,
    )


def extract_property_hints_from_links(links: list[dict[str, Any]]) -> dict[str, Any]:
    anchor: dict[str, Any] = {}
    for item in links:
        if not isinstance(item, dict):
            continue
        link_type = str(item.get("link_type") or "").strip()
        target_id = str(item.get("target_id") or "").strip()
        meta = _coerce_dict(item.get("metadata"))
        if link_type == "mailbox_case" and target_id and not anchor.get("investment_key"):
            anchor["investment_key"] = target_id
        addr = str(meta.get("address_norm") or meta.get("address") or meta.get("installation_address") or "")
        if addr and not anchor.get("address_norm"):
            anchor["address_norm"] = normalize_address_norm(addr)
        nip = normalize_nip(str(meta.get("nip") or meta.get("tax_id") or ""))
        if nip and not anchor.get("nip"):
            anchor["nip"] = nip
    return anchor


def extract_identity_hints_from_payload(
    *,
    email: str = "",
    display_name: str = "",
    identity_metadata: dict[str, Any] | None = None,
    engagement_metadata: dict[str, Any] | None = None,
    links: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    id_hints = _coerce_dict(identity_metadata)
    eng_hints = _coerce_dict(engagement_metadata)
    property_anchor = extract_property_hints_from_links(list(links or []))
    if property_anchor:
        eng_hints.setdefault("property_anchor", property_anchor)
    id_meta = merge_identity_metadata(id_hints, email=email, display_name=display_name)
    eng_meta = merge_engagement_metadata(eng_hints, property_anchor=property_anchor)
    return id_meta, eng_meta
