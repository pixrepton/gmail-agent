"""Pydantic contract for the business_reasoning stage output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class BusinessReasoningConfidence(BaseModel):
    business_confidence: float
    action_confidence: float

    model_config = {"extra": "ignore"}


class BusinessReasoningResult(BaseModel):
    business_interpretation: str
    business_area: str
    customer_state_guess: str
    recommended_next_action: str
    recommended_action_reason: str
    missing_information: list[str]
    risks: list[str]
    urgency: str
    operator_note: str | None = None
    business_summary_short: str | None = None
    reply_recommended: bool | None = None
    human_review_bias: str | None = None
    safety_notes: list[str] = []
    evidence_refs: list[dict[str, Any]] = []
    assumptions: list[str] = []
    unsupported_claims: list[str] = []
    conflict_refs: list[dict[str, Any]] = []
    confidence: BusinessReasoningConfidence

    model_config = {"extra": "ignore"}
