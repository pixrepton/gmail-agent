"""AI-OS Intelligence Spine P1.3: epistemic status contract.

Central invariant:

    AI-OS MUST NOT PRESENT
    INFERRED OR UNKNOWN INFORMATION
    AS A CONFIRMED FACT.

and:

    UNKNOWN != ERROR ; INFERRED != FORBIDDEN.

The P0.5 provenance trio (source_origin / evidence_authority /
instruction_authority) and the epistemic status are SEPARATE dimensions:

    evidence_authority  : how strong is this content as a statement about the
                          world (and what may it control);
    epistemic_status    : what does the system KNOW about this specific
                          proposition.

Status belongs to a PROPOSITION, never to a whole source. A customer message
may make ``customer_reported_error_code`` CONFIRMED while the derived
``device_fault_cause`` stays INFERRED and ``exact_symptoms`` stays UNKNOWN.

CONFIRMED never means "absolutely true in the universe"; it means the system
holds sufficient, non-conflicting support (per the existing fact/evidence
rules) to use this exact proposition as a customer-facing fact.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


EpistemicStatus = Literal["CONFIRMED", "INFERRED", "UNKNOWN", "CONFLICTED"]

CONFIRMED = "CONFIRMED"
INFERRED = "INFERRED"
UNKNOWN = "UNKNOWN"
CONFLICTED = "CONFLICTED"

EPISTEMIC_STATUSES: tuple[str, ...] = (CONFIRMED, INFERRED, UNKNOWN, CONFLICTED)

# Evidence authorities strong enough to support a CONFIRMED proposition when an
# evidence reference is present and there is no unresolved conflict.
CONFIRMABLE_EVIDENCE_AUTHORITIES = frozenset(
    {
        "INTERNAL_SOT",
        "OPERATOR_STATEMENT",
        "CUSTOMER_STATEMENT",
        "CUSTOMER_DOCUMENT",
        "AUTHORITATIVE_DOCUMENT",
    }
)

# Origins / markers that make a claim derived (INFERRED), never automatically
# CONFIRMED.
DERIVED_ORIGINS = frozenset({"DERIVED", "TOOL_RESULT"})
DERIVED_EVIDENCE_AUTHORITIES = frozenset({"DERIVED_LLM_CLAIM"})
DERIVED_SOURCE_TYPES = frozenset(
    {"inference", "inferred", "derived", "llm_reasoning", "interpretation_hypothesis"}
)


class EpistemicClaim(StrictModel):
    """One proposition with an explicit epistemic status and evidence linkage."""

    claim_id: str
    proposition_key: str
    value: str = ""
    status: EpistemicStatus
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    provenance_refs: list[dict[str, Any]] = Field(default_factory=list)
    inference_basis: list[str] = Field(default_factory=list)
    conflicted: bool = False
    decision_usable: bool = True
    # P0.5 provenance trio kept alongside; status never replaces it.
    source_origin: str = ""
    evidence_authority: str = ""
    instruction_authority: str = "NONE"
    reason_codes: list[str] = Field(default_factory=list)


class DraftClaimContext(StrictModel):
    """Structured evidence context for a customer-facing draft.

    confirmed_claims   -> may be asserted as fact (evidence-bound).
    inferred_claims    -> may only be conditional/possibility or omitted.
    unknown_fields     -> must not be asserted; may become questions.
    conflicted_fields  -> must not be asserted as certainty.
    """

    confirmed_claims: list[EpistemicClaim] = Field(default_factory=list)
    inferred_claims: list[EpistemicClaim] = Field(default_factory=list)
    unknown_fields: list[EpistemicClaim] = Field(default_factory=list)
    conflicted_fields: list[EpistemicClaim] = Field(default_factory=list)
    decision_version_id: str = ""


__all__ = [
    "CONFIRMABLE_EVIDENCE_AUTHORITIES",
    "CONFIRMED",
    "CONFLICTED",
    "DERIVED_EVIDENCE_AUTHORITIES",
    "DERIVED_ORIGINS",
    "DERIVED_SOURCE_TYPES",
    "EPISTEMIC_STATUSES",
    "EpistemicClaim",
    "EpistemicStatus",
    "INFERRED",
    "UNKNOWN",
    "DraftClaimContext",
]
