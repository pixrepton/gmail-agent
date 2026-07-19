"""Pydantic contract for the case_guidance stage LLM output (schema v1)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class CaseGuidanceResult(BaseModel):
    operational_status: str
    waiting_for: str
    reason_summary_pl: str
    blocker_summary_pl: str
    momentum: str
    stagnation_flag: bool
    stagnation_reason_pl: str
    business_readiness: str
    operator_attention_class: str
    next_step_hint_pl: str
    confidence: float
    evidence_refs: list[dict[str, Any]] = []
    assumptions: list[str] = []
    unsupported_claims: list[str] = []
    conflict_refs: list[dict[str, Any]] = []

    model_config = {"extra": "ignore"}
