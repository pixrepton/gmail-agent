"""Shared EvidenceRef normalization for Case Intelligence vNext and Decision Pipeline.

Canonical entrypoints:
- ``normalize_evidence_ref`` — one dict in stable EvidenceRef shape (additive contract).
- ``normalize_evidence_refs`` — list normalize + dedupe (same semantics as CaseContextPack
  conflict/gap paths; callers should import this module or ``case_context_contract`` re-export).

Projection safety: dangerous raw keys are stripped on input; use
``assert_no_forbidden_projection_keys`` for structural tests on nested payloads.
"""

from __future__ import annotations

from typing import Any

EVIDENCE_ROLES: tuple[str, ...] = (
    "supports",
    "contradicts",
    "explains_gap",
    "historical_context",
    "operator_decision",
    "retrieval_candidate",
    "weak_signal",
    "precedent",
)

EVIDENCE_ROLES_SET: frozenset[str] = frozenset(EVIDENCE_ROLES)

SOURCE_TYPES: tuple[str, ...] = (
    "gmail_message",
    "gmail_thread",
    "drive_document",
    "calendar_event",
    "mailbox_memory",
    "operator_feedback",
    "intake_structured",
    "attachment",
    "unknown",
)

TRUST_LEVELS: frozenset[str] = frozenset({"unknown", "low", "medium", "high"})
FRESHNESS_LEVELS: frozenset[str] = frozenset({"unknown", "stale", "current"})

# Stripped from evidence dicts before normalization (never echoed in EvidenceRef output).
FORBIDDEN_EVIDENCE_INPUT_KEYS: frozenset[str] = frozenset(
    {
        "body",
        "email_body",
        "snippet",
        "prompt",
        "prompt_text",
        "raw_llm",
        "raw_response",
        "raw_body",
        "message_body",
        "attachment_bytes",
        "full_message_json",
        "raw_llm_response",
        "system_prompt",
        "excerpt",
    }
)

FORBIDDEN_PROJECTION_KEYS: frozenset[str] = frozenset(
    {
        "raw_body",
        "raw_prompt",
        "raw_llm_response",
        "system_prompt",
        "full_message_json",
    }
)


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _bounded_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _normalize_trust_level(raw: Any) -> str:
    t = str(raw or "").strip().lower()
    return t if t in TRUST_LEVELS else "unknown"


def _normalize_freshness(raw: Any) -> str:
    f = str(raw or "").strip().lower()
    return f if f in FRESHNESS_LEVELS else "unknown"


def _apply_role_conservative_trust(role: str, trust_level: str) -> str:
    """Precedent / retrieval / weak roles must not read as high authority when trust was omitted."""
    if trust_level != "unknown":
        return trust_level
    if role in {"precedent", "retrieval_candidate", "weak_signal"}:
        return "low"
    return "unknown"


def normalize_evidence_ref(
    obj: dict[str, Any] | None,
    *,
    default_role: str | None = None,
) -> dict[str, Any]:
    """Return a stable EvidenceRef dict (additive contract).

    ``default_role`` applies when ``evidence_role`` is missing (list normalizer path).
    """
    o_in = obj if isinstance(obj, dict) else {}
    o: dict[str, Any] = {k: v for k, v in o_in.items() if k not in FORBIDDEN_EVIDENCE_INPUT_KEYS}

    role_default = (
        default_role.strip()
        if isinstance(default_role, str) and default_role.strip() in EVIDENCE_ROLES_SET
        else "supports"
    )
    role_raw = str(o.get("evidence_role") or "").strip()
    if role_raw in EVIDENCE_ROLES_SET:
        role = role_raw
    elif role_raw:
        role = "weak_signal"
    else:
        role = role_default

    st = str(o.get("source_type") or o.get("type") or o.get("source_kind") or "").strip()
    if st in {"message", "gmail"}:
        st = "gmail_message"
    if not st:
        st = "unknown"

    source_id = str(
        o.get("source_id")
        or o.get("id")
        or o.get("source_ref")
        or o.get("message_id")
        or o.get("document_id")
        or o.get("chunk_id")
        or ""
    ).strip()

    ts = str(o.get("source_timestamp") or o.get("timestamp") or o.get("observed_at") or o.get("created_at") or "").strip()

    trust_level = _apply_role_conservative_trust(role, _normalize_trust_level(o.get("trust_level")))
    freshness = _normalize_freshness(o.get("freshness"))

    used_for = str(o.get("used_for") or "").strip()
    valid_until = str(o.get("valid_until") or "").strip()

    explicit_cac = "can_answer_customer" in o_in
    can_answer_customer = bool(o.get("can_answer_customer"))
    if explicit_cac and can_answer_customer and trust_level not in {"medium", "high"}:
        can_answer_customer = False
    if not explicit_cac:
        can_answer_customer = False

    conf = round(_bounded_confidence(o.get("confidence")), 4) if "confidence" in o_in else 0.0

    out: dict[str, Any] = {
        "source_type": st,
        "source_id": source_id,
        "source_owner": str(o.get("source_owner") or "").strip(),
        "source_timestamp": ts,
        "chunk_id": str(o.get("chunk_id") or "").strip(),
        "quote_id": str(o.get("quote_id") or "").strip(),
        "document_id": str(o.get("document_id") or "").strip(),
        "message_id": str(o.get("message_id") or "").strip(),
        "calendar_event_id": str(o.get("calendar_event_id") or "").strip(),
        "confidence": conf,
        "evidence_role": role,
        "trust_level": trust_level,
        "freshness": freshness,
        "can_answer_customer": bool(can_answer_customer),
    }
    if used_for:
        out["used_for"] = used_for
    if valid_until:
        out["valid_until"] = valid_until
    if ts:
        out["timestamp"] = ts
    field_val = str(o.get("field") or "").strip()
    if field_val:
        out["field"] = field_val
    return out


def evidence_ref_from_message(
    *,
    message_id: str,
    source_timestamp: str = "",
    evidence_role: str = "supports",
    confidence: float = 0.75,
) -> dict[str, Any]:
    return normalize_evidence_ref(
        {
            "source_type": "gmail_message",
            "source_id": message_id,
            "message_id": message_id,
            "source_timestamp": source_timestamp,
            "evidence_role": evidence_role,
            "confidence": confidence,
        }
    )


def merge_evidence_refs(*parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in parts:
        for raw in block:
            if not isinstance(raw, dict):
                continue
            n = normalize_evidence_ref(raw)
            key = "|".join(
                (
                    n["source_type"],
                    n["source_id"],
                    n["message_id"],
                    n["chunk_id"],
                    n["evidence_role"],
                )
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(n)
    return out[:64]


def normalize_evidence_refs(refs: Any, *, default_role: str = "supports") -> list[dict[str, Any]]:
    """Normalize a list of evidence dicts; dedupe on (source_type, source_id, field, chunk_id, evidence_role).

    Skips rows with no ``source_type`` and no ``source_id`` after mapping (CaseContextPack contract).
    """
    role_default = default_role if default_role in EVIDENCE_ROLES_SET else "supports"
    out: list[dict[str, Any]] = []
    for raw in _list_of_dicts(refs):
        row = normalize_evidence_ref(raw, default_role=role_default)
        st = str(row.get("source_type") or "").strip()
        sid = str(row.get("source_id") or "").strip()
        if not st and not sid:
            continue
        if (st == "unknown" or not st) and not sid:
            continue
        out.append(row)

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in out:
        key = (
            str(row.get("source_type") or ""),
            str(row.get("source_id") or ""),
            str(row.get("field") or ""),
            str(row.get("chunk_id") or ""),
            str(row.get("evidence_role") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def normalize_case_guidance_evidence_refs(refs: Any, *, source_mode: str) -> list[dict[str, Any]]:
    """Boundary for case_guidance / LLM ledger rows: canonical EvidenceRef plus conservative trust.

    ``llm_reasoned`` and ``fallback`` modes cap trust at ``low`` and disable ``can_answer_customer``;
    raw ``excerpt`` / body / snippet keys are stripped by ``normalize_evidence_ref``.
    """
    normalized = normalize_evidence_refs(refs, default_role="supports")
    sm = str(source_mode or "").strip()
    if sm not in {"llm_reasoned", "fallback"}:
        return normalized
    out: list[dict[str, Any]] = []
    for r in normalized:
        fr = str(r.get("freshness") or "").strip() or "unknown"
        merged = {**dict(r), "trust_level": "low", "can_answer_customer": False, "freshness": fr}
        out.append(normalize_evidence_ref(merged))
    return out


def strip_forbidden_evidence_like_rows(rows: Any) -> list[dict[str, Any]]:
    """Strip raw-text keys from non-EvidenceRef sidecars (e.g. case_guidance ``conflict_refs``)."""
    out: list[dict[str, Any]] = []
    for item in _list_of_dicts(rows):
        out.append({k: v for k, v in item.items() if k not in FORBIDDEN_EVIDENCE_INPUT_KEYS})
    return out


def assert_no_forbidden_projection_keys(obj: Any, *, path: str = "") -> list[str]:
    """Return error paths if forbidden keys appear (shallow walk for tests)."""
    errors: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) in FORBIDDEN_PROJECTION_KEYS:
                errors.append(f"{path}.{k}" if path else str(k))
            elif isinstance(v, (dict, list)):
                errors.extend(assert_no_forbidden_projection_keys(v, path=f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            errors.extend(assert_no_forbidden_projection_keys(item, path=f"{path}[{i}]"))
    return errors


__all__ = [
    "EVIDENCE_ROLES",
    "EVIDENCE_ROLES_SET",
    "SOURCE_TYPES",
    "TRUST_LEVELS",
    "FRESHNESS_LEVELS",
    "FORBIDDEN_EVIDENCE_INPUT_KEYS",
    "FORBIDDEN_PROJECTION_KEYS",
    "assert_no_forbidden_projection_keys",
    "evidence_ref_from_message",
    "merge_evidence_refs",
    "normalize_case_guidance_evidence_refs",
    "normalize_evidence_ref",
    "normalize_evidence_refs",
    "strip_forbidden_evidence_like_rows",
]
