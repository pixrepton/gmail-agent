"""P1.3: deterministic epistemic projection facts/evidence -> claims.

Rules are structural, not numeric-confidence thresholds:

    CONFIRMED  requires >=1 evidence ref AND no unresolved conflict AND a
               confirmable evidence authority (customer statement, internal
               SoT, operator statement, customer/authoritative document).
    INFERRED   requires a value plus a derivation marker (derived origin /
               DERIVED_LLM_CLAIM authority / inferred source type / explicit
               inference_basis).
    UNKNOWN    = no assertable value/basis (or confirmable authority without
               evidence).
    CONFLICTED = unresolved conflict on the proposition key; never CONFIRMED.

This module never guesses status from LLM confidence and never grants any
instruction/execution authority. P0.5 provenance dims are carried alongside
the epistemic status.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from case_context_contract import normalize_evidence_refs
from evidence_authority import ensure_provenance_defaults
from llm_contracts.epistemic_claims import (
    CONFIRMABLE_EVIDENCE_AUTHORITIES,
    CONFIRMED,
    CONFLICTED,
    DERIVED_EVIDENCE_AUTHORITIES,
    DERIVED_ORIGINS,
    DERIVED_SOURCE_TYPES,
    INFERRED,
    UNKNOWN,
    DraftClaimContext,
    EpistemicClaim,
)

_ERROR_CODE_RE = re.compile(r"\bH\d{1,3}\b", re.IGNORECASE)
_AREA_RE = re.compile(r"\b\d{2,4}\s*m2\b", re.IGNORECASE)
_CERTAINTY_RE = re.compile(
    r"\b(na pewno|z ca[łl]ą pewnością|jest (uszkodzony|uszkodzona|zepsuty|"
    r"wyłączony)|to na pewno|na 100%|zdecydowanie)\b",
    re.IGNORECASE,
)

# Deterministic value patterns for fields whose ASSERTION is detectable in
# text. Free-text fields (exact_symptoms) are not pattern-assertable and are
# covered by the composer + inferred-certainty checks (residual for P1.5).
_EPISTEMIC_VALUE_PATTERNS: dict[str, re.Pattern] = {
    "error_code": _ERROR_CODE_RE,
    "customer_reported_error_code": _ERROR_CODE_RE,
    "heated_area_m2": _AREA_RE,
}

# Case-insensitive matching sets (provenance dims are stored uppercase).
_CONFIRMABLE_LOW = {item.lower() for item in CONFIRMABLE_EVIDENCE_AUTHORITIES}
_DERIVED_ORIGINS_LOW = {item.lower() for item in DERIVED_ORIGINS}
_DERIVED_AUTHORITY_LOW = {item.lower() for item in DERIVED_EVIDENCE_AUTHORITIES}


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _norm(value: Any) -> str:
    return _text(value).lower()


def _fact_evidence_refs(fact: dict[str, Any]) -> list[dict[str, Any]]:
    """Stable EvidenceRef list for a fact row (refs + source_ref/message/document)."""
    refs: list[dict[str, Any]] = []
    for ref in fact.get("evidence_refs") or fact.get("source_refs") or []:
        if isinstance(ref, dict):
            refs.append(dict(ref))
    source_ref = _text(fact.get("source_ref"))
    message_id = _text(fact.get("message_id"))
    document_id = _text(fact.get("document_id"))
    source_type = _text(fact.get("source_type")) or "unknown"
    if source_type in {"message", "gmail"}:
        source_type = "gmail_message"
    if source_ref or message_id or document_id:
        refs.append(
            {
                "source_type": source_type,
                "source_id": source_ref or message_id or document_id,
                "source_ref": source_ref,
                "message_id": message_id,
                "document_id": document_id,
                "evidence_role": "supports",
            }
        )
    return normalize_evidence_refs(refs, default_role="supports")


def _fact_provenance(fact: dict[str, Any]) -> dict[str, str]:
    """P0.5 provenance trio for a fact row (never upgraded to a higher value)."""
    source = fact.get("metadata") if isinstance(fact.get("metadata"), dict) else {}
    return ensure_provenance_defaults(source or fact, default_origin="DERIVED")


def _is_derived_fact(fact: dict[str, Any], provenance: dict[str, str]) -> bool:
    origin = _norm(provenance.get("source_origin"))
    authority = _norm(provenance.get("evidence_authority"))
    source_type = _norm(fact.get("source_type"))
    status = _norm(fact.get("status"))
    created_by = _norm(fact.get("created_by"))
    return bool(
        origin in _DERIVED_ORIGINS_LOW
        or authority in _DERIVED_AUTHORITY_LOW
        or source_type in DERIVED_SOURCE_TYPES
        or status == "inferred"
        or created_by == "inference"
    )


def _inference_basis_for_fact(
    fact: dict[str, Any],
    evidence_refs: list[dict[str, Any]],
) -> list[str]:
    meta = fact.get("metadata") if isinstance(fact.get("metadata"), dict) else {}
    basis = [str(item).strip() for item in (meta.get("inference_basis") or []) if str(item).strip()]
    if not basis:
        basis = [
            f"{r.get('source_type') or 'unknown'}:{r.get('source_id') or ''}"
            for r in evidence_refs[:4]
            if _text(r.get("source_id"))
        ]
    return basis[:8]


def _claim_id(*, fact: dict[str, Any], key: str, value: str, status: str) -> str:
    fact_id = _text(fact.get("fact_id"))
    seed = "|".join(
        [
            fact_id,
            key,
            value,
            status,
            _text(fact.get("source_ref")),
            _text(fact.get("message_id")),
        ]
    )
    return "epi_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _project_one_fact(
    fact: dict[str, Any],
    conflict_keys: set[str],
) -> EpistemicClaim | None:
    key = _norm(fact.get("fact_key") or fact.get("key") or fact.get("predicate"))
    if not key:
        return None
    value = _text(fact.get("normalized_value") or fact.get("value"))
    provenance = _fact_provenance(fact)
    evidence_refs = _fact_evidence_refs(fact)
    origin = _norm(provenance.get("source_origin"))
    authority = _norm(provenance.get("evidence_authority"))
    instruction = _text(provenance.get("instruction_authority")) or "NONE"
    base = {
        "proposition_key": key,
        "value": value,
        "source_origin": _text(provenance.get("source_origin")),
        "evidence_authority": _text(provenance.get("evidence_authority")),
        "instruction_authority": instruction,
        "provenance_refs": [dict(provenance)],
    }

    if key in conflict_keys:
        return EpistemicClaim(
            claim_id=_claim_id(fact=fact, key=key, value=value, status=CONFLICTED),
            status=CONFLICTED,
            conflicted=True,
            decision_usable=False,
            evidence_refs=evidence_refs,
            reason_codes=["fact_conflict"],
            **base,
        )
    if not value:
        return EpistemicClaim(
            claim_id=_claim_id(fact=fact, key=key, value="", status=UNKNOWN),
            status=UNKNOWN,
            reason_codes=["missing_value"],
            **base,
        )

    if _is_derived_fact(fact, provenance):
        basis = _inference_basis_for_fact(fact, evidence_refs)
        status = INFERRED if basis else UNKNOWN
        return EpistemicClaim(
            claim_id=_claim_id(fact=fact, key=key, value=value, status=status),
            status=status,
            evidence_refs=evidence_refs,
            inference_basis=basis,
            reason_codes=["derived_claim"] if basis else ["derived_without_basis"],
            **base,
        )

    if authority in _CONFIRMABLE_LOW and evidence_refs:
        codes = ["evidence_bound"]
        if origin in {"customer_email", "quoted_content", "forwarded_content", "attachment"}:
            codes.append("customer_reported")
        return EpistemicClaim(
            claim_id=_claim_id(fact=fact, key=key, value=value, status=CONFIRMED),
            status=CONFIRMED,
            evidence_refs=evidence_refs,
            reason_codes=codes,
            **base,
        )
    if authority in _CONFIRMABLE_LOW:
        # Value present and plausible source, but no evidence ref: CONFIRMED is
        # forbidden; without a derivation basis the honest default is UNKNOWN.
        return EpistemicClaim(
            claim_id=_claim_id(fact=fact, key=key, value=value, status=UNKNOWN),
            status=UNKNOWN,
            reason_codes=["confirmed_without_evidence"],
            **base,
        )
    return EpistemicClaim(
        claim_id=_claim_id(fact=fact, key=key, value=value, status=UNKNOWN),
        status=UNKNOWN,
        reason_codes=["unconfirmed_authority_or_evidence"],
        **base,
    )


def project_epistemic_claims(
    facts: Any,
    conflicting_facts: Any = None,
) -> list[EpistemicClaim]:
    """Deterministic per-proposition epistemic projection from fact rows."""
    conflicts = conflicting_facts if isinstance(conflicting_facts, list) else []
    conflict_keys = {
        _norm(item.get("fact_key") or item.get("key"))
        for item in conflicts
        if isinstance(item, dict)
    }
    claims: list[EpistemicClaim] = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        claim = _project_one_fact(fact, conflict_keys)
        if claim is not None:
            claims.append(claim)
    return claims


def build_draft_claim_context(
    claims: Any,
    missing_fields: Any = None,
    *,
    decision_version_id: str = "",
) -> DraftClaimContext:
    """Typed draft context: confirmed / inferred / unknown / conflicted."""
    claim_list = [c for c in (claims or []) if isinstance(c, EpistemicClaim)]
    confirmed: list[EpistemicClaim] = []
    inferred: list[EpistemicClaim] = []
    unknown: list[EpistemicClaim] = []
    conflicted: list[EpistemicClaim] = []
    seen_keys: set[str] = set()
    for claim in claim_list:
        key = _norm(claim.proposition_key)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if claim.status == CONFIRMED:
            confirmed.append(claim)
        elif claim.status == INFERRED:
            inferred.append(claim)
        elif claim.status == CONFLICTED:
            conflicted.append(claim)
        else:
            unknown.append(claim)

    for raw in missing_fields or []:
        key = _norm(raw)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        unknown.append(
            EpistemicClaim(
                claim_id="epi_unknown_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
                proposition_key=key,
                value="",
                status=UNKNOWN,
                reason_codes=["missing_field"],
            )
        )
    return DraftClaimContext(
        confirmed_claims=confirmed,
        inferred_claims=inferred,
        unknown_fields=unknown,
        conflicted_fields=conflicted,
        decision_version_id=_text(decision_version_id),
    )


def build_draft_claim_context_from_store(
    store: Any,
    case_id: str,
    missing_fields: Any = None,
    *,
    decision_version_id: str = "",
) -> DraftClaimContext | None:
    """Project from the durable mailbox fact store (current facts only)."""
    if store is None:
        return None
    from mailbox_memory.active_facts import fetch_current_facts_for_case
    from mailbox_memory_runtime import split_conflicting_facts

    facts = fetch_current_facts_for_case(store, case_id)
    active, conflicts = split_conflicting_facts(facts)
    claims = project_epistemic_claims(active, conflicts)
    return build_draft_claim_context(
        claims,
        missing_fields,
        decision_version_id=decision_version_id,
    )


def _asserts_value_with_certainty(text: str, value: str) -> bool:
    low = _norm(text)
    token = _norm(value)
    if not token or token not in low:
        return False
    for match in _CERTAINTY_RE.finditer(low):
        window = low[max(0, match.start() - 120) : match.end() + 120]
        if token in window:
            return True
    return False


def evaluate_draft_epistemic_sanity(
    *,
    body: str,
    claim_context: DraftClaimContext | None,
) -> dict[str, Any]:
    """Structured epistemic guard for a customer-facing draft body.

    Deterministic checks (no LLM):
      CONFIRMED_WITHOUT_EVIDENCE  - a confirmed claim with no evidence refs;
      INFERRED_AS_CONFIRMED       - an inferred value asserted with certainty;
      CONFLICTED_FACT_ASSERTED    - a conflicted field value asserted;
      UNKNOWN_AS_CONFIRMED        - a value pattern for an UNKNOWN field;
      UNSUPPORTED_CUSTOMER_FACT   - an asserted code-like value not confirmed.
    """
    text = _norm(body)
    if not text or claim_context is None:
        return {"ok": True, "reason_codes": []}
    reasons: list[str] = []

    for claim in claim_context.confirmed_claims:
        if not claim.evidence_refs:
            reasons.append("CONFIRMED_WITHOUT_EVIDENCE")
            break

    confirmed_values = {
        _norm(claim.value) for claim in claim_context.confirmed_claims if _norm(claim.value)
    }
    for claim in claim_context.inferred_claims:
        if _asserts_value_with_certainty(text, _text(claim.value)):
            reasons.append("INFERRED_AS_CONFIRMED")
            break

    tokens = set(text.split())
    for claim in claim_context.conflicted_fields:
        value = _norm(claim.value)
        if value and value in tokens:
            reasons.append("CONFLICTED_FACT_ASSERTED")
            break

    for claim in claim_context.unknown_fields:
        pattern = _EPISTEMIC_VALUE_PATTERNS.get(_norm(claim.proposition_key))
        if pattern is not None and pattern.search(text):
            reasons.append("UNKNOWN_AS_CONFIRMED")
            break

    for match in _ERROR_CODE_RE.finditer(text):
        code = _norm(match.group(0))
        if code not in confirmed_values:
            reasons.append("UNSUPPORTED_CUSTOMER_FACT")
            break

    if not reasons:
        return {"ok": True, "reason_codes": []}
    return {
        "ok": False,
        "reason_codes": sorted(set(reasons)),
        "failure_class": "DRAFT_EPISTEMIC_SANITY_FAILED",
    }


__all__ = [
    "build_draft_claim_context",
    "build_draft_claim_context_from_store",
    "evaluate_draft_epistemic_sanity",
    "project_epistemic_claims",
]
