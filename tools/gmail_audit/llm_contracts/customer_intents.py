"""P1.4: structural customer intent contract (bounded vocabulary).

One customer turn may contain N significant business intents. Each intent is
an independent semantic object with its own status, required information,
evidence and execution authority. The list is intentionally unbounded in
count; the first enforced slice uses a bounded canonical vocabulary.

Rules:
- intent existence is NEVER decided by a confidence threshold; the LLM may
  propose intents, but downstream deterministic guards verify consequences.
- authority stays per intent: a read/informational intent never grants
  execution authority to a write intent.
- the first enforced CAD slice remains single-action; one ``primary
  actionable intent`` is chosen deterministically, the rest stay explicitly
  open (status/blocking_gaps).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CUSTOMER_INTENT_PROJECTION_SCHEMA_VERSION = "customer_intent_projection.v1"

# Bounded canonical intent vocabulary for the first vertical slice. Unknown
# free-text types are normalized to ``other``; nothing is invented downstream.
CUSTOMER_INTENT_TYPES = (
    "service_problem",
    "schedule_service",
    "document_request",
    "other",
)

# Independent per-intent lifecycle statuses (never collapsed to one global
# turn status).
CUSTOMER_INTENT_STATUSES = (
    "READY",
    "NEEDS_INFORMATION",
    "BLOCKED",
    "INFORMATIONAL_ONLY",
)

# Per-intent execution authority. Write intents are never granted execution
# authority by this slice; they remain HITL_ONLY (approval required) or NONE.
EXECUTION_AUTHORITY_VALUES = ("NONE", "HITL_ONLY", "DRAFT_ONLY")


class CustomerIntent(BaseModel):
    """One significant customer intent with independent lifecycle state."""

    intent_id: str
    intent_type: str
    description: str = ""
    source_span: str = ""
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    required_information: list[str] = Field(default_factory=list)
    blocking_gaps: list[str] = Field(default_factory=list)
    status: str = "NEEDS_INFORMATION"
    decision_state: str = "NOT_DECIDED"
    execution_authority: str = "NONE"
    confidence: float | None = None

    model_config = {"extra": "ignore"}


class CustomerIntentProjection(BaseModel):
    """Deterministic projection of all intents for one case turn."""

    schema_version: Literal["customer_intent_projection.v1"] = CUSTOMER_INTENT_PROJECTION_SCHEMA_VERSION
    case_id: str = ""
    source_signal_id: str = ""
    intents: list[CustomerIntent] = Field(default_factory=list)
    primary_actionable_intent: str = ""
    missing_information_by_intent: dict[str, list[str]] = Field(default_factory=dict)
    # field -> sorted intent_ids that need the same information (dedup basis).
    shared_required_information: dict[str, list[str]] = Field(default_factory=dict)
    created_at: str = ""

    model_config = {"extra": "ignore"}

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")


__all__ = [
    "CUSTOMER_INTENT_PROJECTION_SCHEMA_VERSION",
    "CUSTOMER_INTENT_STATUSES",
    "CUSTOMER_INTENT_TYPES",
    "EXECUTION_AUTHORITY_VALUES",
    "CustomerIntent",
    "CustomerIntentProjection",
]
