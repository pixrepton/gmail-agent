"""Deterministic eligibility contract for call_kalk_top_quote (KEEP_AND_RESTRICT).

P1.4A (2026-08-18): the planner must not be offered kalk-top when the current
authoritative case/business state does not support a quote/calculation action,
and kalk-top must not be invoked when true required technical inputs are absent.

Two independent layers, kept separate by design:

    BUSINESS_ELIGIBILITY  - may this case attempt a calculation at all right now?
    TECHNICAL_READINESS   - does the case carry the data the current kalk-top API
                             contract truly requires for a valid request?

This module uses existing production concepts only (CaseKindLiteral,
DecisionDivergenceObservationV1.business_recommended_action,
CaseUnderstandingProjection.risks, HvacProfile.heated_area_m2). No benchmark
case IDs, no Fresh38 fixture wording, no benchmark-only keyword rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

# Journey: case kinds that permit quote/offer activity (existing literal set).
SALES_CASE_KINDS = frozenset({"wycena_oferta", "zapytanie_klienta"})

# Quote intent: an explicit offer/quote request. `zapytanie_klienta` is a
# pre-offer inquiry (e.g. a technical compatibility question) and does not by
# itself require a calculation.
QUOTE_CASE_KINDS = frozenset({"wycena_oferta"})

# Business states that mean "not quote-ready" (authoritative BR literals).
NOT_READY_ACTIONS = frozenset(
    {"collect_data", "escalate_review", "ignore", "wait"}
)

# Blocking contradiction risk types relevant to calculation.
BLOCKING_RISK_TYPES = frozenset({"contradiction", "conflicting_facts", "conflict"})
BLOCKING_RISK_SEVERITIES = frozenset({"high", "critical"})


@dataclass(frozen=True)
class KalkEligibilityDecision:
    """One deterministic decision for the kalk-top tool in a case context."""

    offered: bool
    business_eligible: bool
    technically_ready: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "offered": self.offered,
            "business_eligible": self.business_eligible,
            "technically_ready": self.technically_ready,
            "reasons": list(self.reasons),
        }


def _bounded(value: Any) -> str:
    return str(value or "").strip().lower()


def _first_readiness_action(
    *,
    business_recommended_action: Any,
    next_best_action_type: Any,
    action_planner_primary_action: Any,
) -> str:
    """Prefer the authoritative BR recommendation, then existing fallbacks."""
    for value in (
        business_recommended_action,
        next_best_action_type,
        action_planner_primary_action,
    ):
        normalized = _bounded(value)
        if normalized:
            return normalized
    return ""


def evaluate_business_eligibility(
    *,
    case_kind: Any,
    business_recommended_action: Any = None,
    next_best_action_type: Any = None,
    action_planner_primary_action: Any = None,
    risks: Iterable[dict[str, Any]] | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Business eligibility per KEEP_AND_RESTRICT (no new ontology)."""
    reasons: list[str] = []
    kind = _bounded(case_kind)

    if kind not in SALES_CASE_KINDS:
        reasons.append(f"journey_not_quote_permitted:case_kind={kind or 'unknown'}")
        return False, tuple(reasons)
    if kind not in QUOTE_CASE_KINDS:
        reasons.append(f"quote_intent_missing:case_kind={kind}")
        return False, tuple(reasons)

    readiness = _first_readiness_action(
        business_recommended_action=business_recommended_action,
        next_best_action_type=next_best_action_type,
        action_planner_primary_action=action_planner_primary_action,
    )
    if not readiness:
        reasons.append("business_readiness_unknown")
        return False, tuple(reasons)
    if readiness in NOT_READY_ACTIONS:
        reasons.append(f"business_not_quote_ready:{readiness}")
        return False, tuple(reasons)

    for risk in risks or []:
        if not isinstance(risk, dict):
            continue
        risk_type = _bounded(risk.get("risk_type"))
        severity = _bounded(risk.get("severity"))
        if risk_type in BLOCKING_RISK_TYPES and severity in BLOCKING_RISK_SEVERITIES:
            reasons.append(f"blocking_contradiction:{risk_type}:{severity}")
            return False, tuple(reasons)

    return True, tuple(reasons)


def evaluate_technical_readiness(*, heated_area_m2: Any) -> tuple[bool, tuple[str, ...]]:
    """Technical readiness against the CURRENT kalk-top API contract.

    The kalk-top validator requires building geometry: a positive
    `heated_area` (or building_length+building_width). The gmail-agent client
    path sends `building.heated_area` only, so a positive heated_area_m2 is the
    single true required technical input for this path.
    """
    try:
        area = float(heated_area_m2)
    except (TypeError, ValueError):
        area = 0.0
    if area <= 0.0:
        return False, ("required_input_missing:heated_area_m2",)
    return True, ()


def decision_from_snapshot(
    snapshot: EngagementSnapshotV2,
    *,
    decision_context: dict[str, Any] | None = None,
) -> KalkEligibilityDecision:
    """Build the eligibility decision from snapshot + caller decision context.

    `decision_context` is the production `decision_comparison_inputs` payload
    (authoritative BusinessReasoning output) and wins over any observed
    divergence record already attached to the snapshot.
    """
    context = decision_context if isinstance(decision_context, dict) else {}
    observed = getattr(snapshot, "decision_divergence_observation", None)
    observed_action = (
        getattr(observed, "business_recommended_action", "") if observed is not None else ""
    )

    business_ready, business_reasons = evaluate_business_eligibility(
        case_kind=snapshot.case_kind,
        business_recommended_action=context.get("business_recommended_action")
        or observed_action,
        next_best_action_type=context.get("next_best_action_type"),
        action_planner_primary_action=context.get("action_planner_primary_action"),
        risks=(
            [r.model_dump() for r in snapshot.case_understanding.risks]
            if snapshot.case_understanding is not None
            else None
        ),
    )
    technical_ready, technical_reasons = evaluate_technical_readiness(
        heated_area_m2=snapshot.hvac_profile.heated_area_m2,
    )

    if not business_ready:
        return KalkEligibilityDecision(
            offered=False,
            business_eligible=False,
            technically_ready=technical_ready,
            reasons=business_reasons + technical_reasons,
        )
    if not technical_ready:
        return KalkEligibilityDecision(
            offered=False,
            business_eligible=True,
            technically_ready=False,
            reasons=technical_reasons,
        )
    return KalkEligibilityDecision(
        offered=True,
        business_eligible=True,
        technically_ready=True,
        reasons=(),
    )


__all__ = [
    "KalkEligibilityDecision",
    "SALES_CASE_KINDS",
    "QUOTE_CASE_KINDS",
    "NOT_READY_ACTIONS",
    "evaluate_business_eligibility",
    "evaluate_technical_readiness",
    "decision_from_snapshot",
]
