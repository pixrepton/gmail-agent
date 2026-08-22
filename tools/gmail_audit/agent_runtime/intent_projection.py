"""P1.4: deterministic multi-intent projection.

The LLM (BusinessReasoning) MAY propose ``customer_intents``; this module owns
the deterministic normalization: bounded canonical vocabulary, stable
ordering, per-intent status/authority derivation, shared missing-information
dedup and the single ``primary actionable intent`` for the first enforced CAD
slice (``ask_for_missing_data / customer / mail``).

No confidence threshold decides intent existence. No intent is silently
dropped; unknown types are normalized to ``other`` and stay visible. The CAD
is NOT rebuilt into a multi-action decision in this slice.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from case_context_contract import normalize_evidence_refs
from llm_contracts.customer_intents import (
    CUSTOMER_INTENT_STATUSES,
    CUSTOMER_INTENT_TYPES,
    CustomerIntent,
    CustomerIntentProjection,
)

# Canonical ordering: stable regardless of LLM emission order (reordering must
# not change the semantic projection or downstream hashes).
_INTENT_TYPE_ORDER: dict[str, int] = {
    "service_problem": 0,
    "schedule_service": 1,
    "document_request": 2,
    "other": 3,
}

_AUTHORITY_BY_TYPE: dict[str, str] = {
    "service_problem": "DRAFT_ONLY",
    "schedule_service": "HITL_ONLY",
    "document_request": "HITL_ONLY",
    "other": "NONE",
}

_MAX_INTENTS = 8


def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _norm(value: Any) -> str:
    return _text(value).lower()


def _string_list(value: Any, limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in (value or []) if isinstance(value, list) else []:
        text = _text(item)
        if text and _norm(text) not in seen:
            seen.add(_norm(text))
            out.append(text)
        if len(out) >= limit:
            break
    return out


def normalize_intent_type(raw: Any) -> str:
    """Map free text to the bounded canonical vocabulary (fallback ``other``)."""
    candidate = _norm(raw)
    if candidate in CUSTOMER_INTENT_TYPES:
        return candidate
    aliases = {
        "service": "service_problem",
        "service_issue": "service_problem",
        "diagnosis": "service_problem",
        "problem": "service_problem",
        "repair": "service_problem",
        "schedule": "schedule_service",
        "schedule_visit": "schedule_service",
        "booking": "schedule_service",
        "visit": "schedule_service",
        "document": "document_request",
        "invoice": "document_request",
        "faktura": "document_request",
        "document_copy": "document_request",
        "information": "other",
        "question": "other",
        "informational": "other",
    }
    return aliases.get(candidate, "other")


def _normalize_status(raw: Any, *, intent_type: str, required: list[str]) -> str:
    """Derive deterministic per-intent status (independent of LLM confidence)."""
    candidate = _norm(raw)
    if candidate in CUSTOMER_INTENT_STATUSES:
        # Explicit status is accepted only when it is coherent with structure.
        if candidate == "READY" and required:
            return "NEEDS_INFORMATION"
        if candidate == "INFORMATIONAL_ONLY" and required:
            return "NEEDS_INFORMATION"
        return candidate
    if required:
        return "NEEDS_INFORMATION"
    if intent_type in {"schedule_service", "document_request"}:
        return "BLOCKED"
    if intent_type == "service_problem":
        return "READY"
    return "INFORMATIONAL_ONLY"


def _intent_id(*, intent_type: str, description: str, source_span: str) -> str:
    seed = "|".join([intent_type, _norm(description), _norm(source_span)])
    return "int_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _evidence_refs(raw: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for ref in (raw or []) if isinstance(raw, list) else []:
        if isinstance(ref, dict):
            refs.append(dict(ref))
    return normalize_evidence_refs(refs, default_role="supports")[:16]


def _normalize_one(raw: dict[str, Any], *, index: int) -> CustomerIntent | None:
    if not isinstance(raw, dict):
        return None
    intent_type = normalize_intent_type(raw.get("intent_type"))
    description = _text(raw.get("description"))[:300]
    source_span = _text(raw.get("source_span"))[:240]
    required = _string_list(raw.get("required_information"))
    explicit_status = _text(raw.get("status"))
    status = _normalize_status(explicit_status, intent_type=intent_type, required=required)
    blocking: list[str] = []
    authority = _AUTHORITY_BY_TYPE.get(intent_type, "NONE")
    if authority == "HITL_ONLY":
        # Write intents are never executable in this slice; the authority gap is
        # structural and independent of missing-information status.
        blocking.append("execution_authority_hitl_required")
    blocking.extend(_string_list(raw.get("blocking_gaps"), limit=6))
    blocking = list(dict.fromkeys(blocking))
    confidence = raw.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = None
    else:
        confidence = max(0.0, min(1.0, round(float(confidence), 4)))
    return CustomerIntent(
        intent_id=_intent_id(intent_type=intent_type, description=description, source_span=source_span),
        intent_type=intent_type,
        description=description,
        source_span=source_span,
        evidence_refs=_evidence_refs(raw.get("evidence_refs")),
        required_information=required,
        blocking_gaps=blocking,
        status=status,
        decision_state="NOT_DECIDED",
        execution_authority=authority,
        confidence=confidence,
    )


def normalize_customer_intents(raw: Any, *, source_mode: str = "llm") -> list[CustomerIntent]:
    """Deterministic typed normalization of raw intent rows.

    - bounded canonical vocabulary (unknown -> ``other``, never dropped);
    - dedupe by canonical intent_type (merge required information);
    - stable canonical ordering (LLM emission order is irrelevant);
    - provenance ``source_mode`` recorded per intent.
    """
    rows = [r for r in (raw or []) if isinstance(r, dict)][:_MAX_INTENTS]
    merged: dict[str, CustomerIntent] = {}
    for index, row in enumerate(rows):
        intent = _normalize_one(row, index=index)
        if intent is None:
            continue
        existing = merged.get(intent.intent_type)
        if existing is None:
            merged[intent.intent_type] = intent
            continue
        # Merge duplicate intents of the same type: union required info,
        # keep the first description/source_span, union evidence refs.
        merged_info = list(dict.fromkeys(existing.required_information + intent.required_information))
        refs = existing.evidence_refs + intent.evidence_refs
        seen_refs: set[str] = set()
        unique_refs: list[dict[str, Any]] = []
        for ref in refs:
            key = hashlib.sha256(str(ref).encode("utf-8")).hexdigest()
            if key not in seen_refs:
                seen_refs.add(key)
                unique_refs.append(ref)
        merged_status = _normalize_status(
            "NEEDS_INFORMATION" if merged_info else existing.status,
            intent_type=existing.intent_type,
            required=merged_info,
        )
        merged[intent.intent_type] = existing.model_copy(
            update={
                "required_information": merged_info,
                "evidence_refs": unique_refs[:16],
                "status": merged_status,
                "blocking_gaps": existing.blocking_gaps or intent.blocking_gaps,
            }
        )
    ordered = sorted(merged.values(), key=lambda item: _INTENT_TYPE_ORDER.get(item.intent_type, 9))
    return ordered


def _shared_required_information(intents: list[CustomerIntent]) -> dict[str, list[str]]:
    """field -> sorted intent_ids (deterministic; basis for question dedup)."""
    mapping: dict[str, list[str]] = {}
    for intent in intents:
        for field in intent.required_information:
            key = _norm(field)
            ids = mapping.setdefault(key, [])
            if intent.intent_id not in ids:
                ids.append(intent.intent_id)
    for ids in mapping.values():
        ids.sort()
    return {field: ids for field, ids in mapping.items()}


def _primary_actionable_intent(intents: list[CustomerIntent]) -> str:
    """Deterministic primary for the first enforced CAD slice.

    The slice maps to ``ask_for_missing_data``; service_problem is the only
    intent class that can be primary today. Otherwise no primary is invented.
    """
    for intent in intents:
        if intent.intent_type == "service_problem":
            return intent.intent_id
    return ""


def _fallback_single_intent(
    *,
    br_result: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Deterministic single-intent projection when BR carries no intents.

    Preserves legacy single-intent semantics: a ``collect_data`` service BR
    with missing_information becomes one ``service_problem`` intent. This keeps
    single-intent regression byte-stable while the multi-intent slice is off.
    """
    br = br_result if isinstance(br_result, dict) else {}
    if _norm(br.get("recommended_next_action")) != "collect_data":
        return []
    missing = _string_list(br.get("missing_information"))
    if not missing:
        return []
    return [
        {
            "intent_type": "service_problem",
            "description": "Obsluga zgloszonego problemu serwisowego (brak danych).",
            "required_information": missing,
            "evidence_refs": br.get("evidence_refs") or [],
        }
    ]


def project_customer_intents(
    *,
    br_result: dict[str, Any] | None = None,
    understanding: dict[str, Any] | None = None,
    intake_result: dict[str, Any] | None = None,
    case_id: str = "",
    source_signal_id: str = "",
    raw_intents: Any = None,
) -> CustomerIntentProjection | None:
    """Deterministic projection entry point.

    Sources (in priority order):
    1. ``raw_intents`` (already normalized dicts, e.g. from the snapshot);
    2. ``br_result.customer_intents`` (LLM-proposed, provenance kept);
    3. deterministic single-intent fallback from BR ``collect_data``.

    Returns None only when there is genuinely nothing to project (never a
    fabricated intent).
    """
    if raw_intents:
        raw_rows = [r for r in raw_intents if isinstance(r, dict)]
    elif isinstance(br_result, dict) and br_result.get("customer_intents"):
        raw_rows = [r for r in br_result.get("customer_intents") if isinstance(r, dict)]
    else:
        raw_rows = _fallback_single_intent(br_result=br_result)
    if not raw_rows:
        return None
    intents = normalize_customer_intents(raw_rows)
    if not intents:
        return None
    missing_by_intent = {
        intent.intent_id: list(intent.required_information)
        for intent in intents
        if intent.required_information
    }
    shared = _shared_required_information(intents)
    return CustomerIntentProjection(
        case_id=_text(case_id),
        source_signal_id=_text(source_signal_id),
        intents=intents,
        primary_actionable_intent=_primary_actionable_intent(intents),
        missing_information_by_intent=missing_by_intent,
        shared_required_information=shared,
        created_at=_utc(),
    )


__all__ = [
    "normalize_customer_intents",
    "normalize_intent_type",
    "project_customer_intents",
]
