"""P2 Customer Identity — Poziom 2 binding suggestions (suggest-only, no auto-merge)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

SUGGESTION_PENDING = "pending_operator"
SUGGESTION_APPROVED = "approved"
SUGGESTION_REJECTED = "rejected"

_SIGNAL_NIP = "nip_match"
_SIGNAL_PHONE = "phone_match"
_SIGNAL_FUZZY = "fuzzy_name_address"

_NIP_RE = re.compile(r"\b\d{10}\b|\b\d{3}-\d{3}-\d{2}-\d{2}\b")
_PHONE_RE = re.compile(r"\+\d{9,15}|\b\d{9}\b")


def _new_id(prefix: str = "ibs") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_nip(raw: str) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits if len(digits) == 10 else ""


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("48") and len(digits) > 9:
        digits = digits[2:]
    return digits[-9:] if len(digits) >= 9 else ""


def _metadata_signal(meta: dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = meta.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _coerce_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            import json

            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _identity_signals(row: dict[str, Any]) -> dict[str, str]:
    meta = _coerce_metadata(row.get("metadata"))
    display = str(row.get("display_name") or meta.get("display_name") or "").strip()
    nip = normalize_nip(_metadata_signal(meta, "nip", "tax_id", "company_nip"))
    phone = normalize_phone(_metadata_signal(meta, "phone", "mobile", "contact_phone"))
    address = _metadata_signal(meta, "address_norm", "address", "installation_address")
    if not nip:
        nip = normalize_nip(display)
    return {"nip": nip, "phone": phone, "address": address.lower(), "display": display.lower()}


def _fuzzy_name_address_score(a: dict[str, str], b: dict[str, str]) -> float:
    if not a.get("display") or not b.get("display"):
        return 0.0
    if a["display"] == b["display"] and a.get("address") and a["address"] == b.get("address"):
        return 0.9
    tokens_a = set(a["display"].split())
    tokens_b = set(b["display"].split())
    if not tokens_a or not tokens_b:
        return 0.0
    overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
    if overlap >= 0.85 and a.get("address") and a["address"] == b.get("address"):
        return 0.87
    return 0.0


def detect_identity_binding_suggestions(
    store: Any,
    *,
    within_days: int = 90,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Scan identities and emit Poziom 2 suggestions — never auto-merge."""
    list_fn = getattr(store, "list_identities_recent", None)
    if not callable(list_fn):
        return []
    rows = list(list_fn(within_days=within_days, limit=max(limit * 4, 100)) or [])
    suggestions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for i, src in enumerate(rows):
        if not isinstance(src, dict):
            continue
        src_id = str(src.get("identity_id") or "").strip()
        if not src_id:
            continue
        src_sig = _identity_signals(src)
        for dst in rows[i + 1 :]:
            if not isinstance(dst, dict):
                continue
            dst_id = str(dst.get("identity_id") or "").strip()
            if not dst_id or dst_id == src_id:
                continue
            dst_sig = _identity_signals(dst)
            signal_type = ""
            confidence = 0.0
            evidence: dict[str, Any] = {}
            if src_sig["nip"] and src_sig["nip"] == dst_sig["nip"]:
                signal_type = _SIGNAL_NIP
                confidence = 0.8
                evidence = {"nip": src_sig["nip"]}
            elif src_sig["phone"] and src_sig["phone"] == dst_sig["phone"]:
                signal_type = _SIGNAL_PHONE
                confidence = 0.75
                evidence = {"phone": src_sig["phone"]}
            else:
                fuzzy = _fuzzy_name_address_score(src_sig, dst_sig)
                if fuzzy >= 0.85:
                    signal_type = _SIGNAL_FUZZY
                    confidence = fuzzy
                    evidence = {"display_a": src_sig["display"], "display_b": dst_sig["display"]}
            if not signal_type:
                continue
            key = (min(src_id, dst_id), max(src_id, dst_id), signal_type)
            if key in seen:
                continue
            seen.add(key)
            suggestions.append(
                {
                    "source_identity_id": src_id,
                    "target_identity_id": dst_id,
                    "signal_type": signal_type,
                    "confidence": confidence,
                    "evidence_json": evidence,
                }
            )
            if len(suggestions) >= limit:
                return suggestions
    return suggestions


def upsert_binding_suggestions(store: Any, suggestions: list[dict[str, Any]]) -> int:
    upsert = getattr(store, "upsert_identity_binding_suggestion", None)
    if not callable(upsert):
        return 0
    count = 0
    for row in suggestions:
        if upsert(**row):
            count += 1
    return count


SIGNAL_LABELS_PL: dict[str, str] = {
    _SIGNAL_NIP: "Ten sam NIP",
    _SIGNAL_PHONE: "Ten sam telefon",
    _SIGNAL_FUZZY: "Podobna nazwa i adres",
}


def _identity_summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    meta = _coerce_metadata(row.get("metadata"))
    return {
        "identity_id": str(row.get("identity_id") or ""),
        "primary_email": str(row.get("primary_email") or ""),
        "display_name": str(row.get("display_name") or ""),
        "identity_kind": str(meta.get("identity_kind") or ""),
    }


def enrich_binding_suggestion(store: Any, row: dict[str, Any]) -> dict[str, Any]:
    get_identity = getattr(store, "get_identity", None)
    source_id = str(row.get("source_identity_id") or "")
    target_id = str(row.get("target_identity_id") or "")
    source = get_identity(source_id) if callable(get_identity) and source_id else None
    target = get_identity(target_id) if callable(get_identity) and target_id else None
    signal_type = str(row.get("signal_type") or "")
    return {
        **dict(row),
        "schema_version": "identity_binding_suggestion.v1",
        "signal_label_pl": SIGNAL_LABELS_PL.get(signal_type, signal_type),
        "source_identity": _identity_summary(source),
        "target_identity": _identity_summary(target),
    }


def get_binding_suggestion(store: Any, *, suggestion_id: str) -> dict[str, Any] | None:
    fn = getattr(store, "get_identity_binding_suggestion", None)
    if not callable(fn):
        return None
    row = fn(suggestion_id=str(suggestion_id or "").strip())
    if not row:
        return None
    return enrich_binding_suggestion(store, dict(row))


def fetch_binding_suggestions(
    store: Any,
    *,
    status: str = SUGGESTION_PENDING,
    limit: int = 50,
    enrich: bool = True,
) -> list[dict[str, Any]]:
    fn = getattr(store, "list_identity_binding_suggestions", None)
    if not callable(fn):
        return []
    rows = list(fn(status=status, limit=limit) or [])
    if not enrich:
        return rows
    return [enrich_binding_suggestion(store, dict(row)) for row in rows]


def update_binding_suggestion_status(
    store: Any,
    *,
    suggestion_id: str,
    status: str,
    reviewed_by: str = "operator",
) -> bool:
    fn = getattr(store, "update_identity_binding_suggestion_status", None)
    if not callable(fn):
        return False
    return bool(fn(suggestion_id=suggestion_id, status=status, reviewed_by=reviewed_by))


def execute_identity_merge(
    store: Any,
    *,
    suggestion_id: str,
    operator_id: str = "operator",
) -> dict[str, Any]:
    """P2.1 — Merge two identities from an approved suggestion.

    Steps:
    1. Load suggestion; assert status == SUGGESTION_APPROVED.
    2. Guard: reject if both identities have active, conflicting engagements
       (i.e. the target would get two open engagements from different email threads).
    3. store.merge_identities() — repoint engagements, write the audit log, and delete the
       source identity in one atomic transaction (see that method's docstring for why the
       log write must precede the delete).
    4. Return summary dict.

    Raises ValueError if guards fail (caller should return 409 HTTP).
    """
    import json as _json  # noqa: PLC0415

    get_suggestion = getattr(store, "get_identity_binding_suggestion", None)
    if not callable(get_suggestion):
        raise RuntimeError("store.get_identity_binding_suggestion not available")

    suggestion = get_suggestion(suggestion_id=suggestion_id)
    if not suggestion:
        raise ValueError(f"Suggestion {suggestion_id!r} not found")
    if str(suggestion.get("status") or "") != SUGGESTION_APPROVED:
        raise ValueError(
            f"Suggestion {suggestion_id!r} must be approved first "
            f"(status={suggestion.get('status')!r})"
        )

    src_id = str(suggestion.get("source_identity_id") or "").strip()
    tgt_id = str(suggestion.get("target_identity_id") or "").strip()
    if not src_id or not tgt_id:
        raise ValueError("Suggestion missing identity IDs")
    if src_id == tgt_id:
        raise ValueError("Cannot merge identity with itself")

    # Guard: check for conflicting active engagements
    list_eng = getattr(store, "list_engagements_for_identity", None)
    if callable(list_eng):
        src_engs = [e for e in (list_eng(identity_id=src_id, status="open") or [])
                    if str(e.get("status") or "") == "open"]
        tgt_engs = [e for e in (list_eng(identity_id=tgt_id, status="open") or [])
                    if str(e.get("status") or "") == "open"]
        if src_engs and tgt_engs:
            raise ValueError(
                f"Both identities have active open engagements "
                f"(src={len(src_engs)} tgt={len(tgt_engs)}). "
                f"Operator must resolve manually (RFC §8 edge case)."
            )

    # Atomic: repoint engagements + write audit log + delete source identity, one transaction.
    log_id = _new_id("iml")
    merge_fn = getattr(store, "merge_identities", None)
    if not callable(merge_fn):
        raise RuntimeError("store.merge_identities not available")
    repointed = merge_fn(
        source_identity_id=src_id,
        target_identity_id=tgt_id,
        suggestion_id=suggestion_id,
        operator_id=operator_id,
        log_id=log_id,
        detail={
            "signal_type": suggestion.get("signal_type"),
            "confidence": suggestion.get("confidence"),
        },
    )

    return {
        "merged": True,
        "log_id": log_id,
        "source_identity_id": src_id,
        "target_identity_id": tgt_id,
        "engagements_repointed": repointed,
    }


__all__ = [
    "SUGGESTION_APPROVED",
    "SUGGESTION_PENDING",
    "SUGGESTION_REJECTED",
    "detect_identity_binding_suggestions",
    "enrich_binding_suggestion",
    "execute_identity_merge",
    "fetch_binding_suggestions",
    "get_binding_suggestion",
    "normalize_nip",
    "normalize_phone",
    "update_binding_suggestion_status",
    "upsert_binding_suggestions",
]
