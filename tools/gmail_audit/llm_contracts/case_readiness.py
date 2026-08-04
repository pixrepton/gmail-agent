"""AI-OS Roadmap 2.2 — `CaseReadinessState`: one answer to "what can be done with this case now?"

The thin `readiness_facets` projection (operator_projection_quality / projection_envelope) answers
several small questions independently: is context ready, is it blocked by data, how many gaps and
conflicts are there. It stays exactly as it is — it is the facet layer and other surfaces read it.

What was missing was a single composed verdict. Without it, every consumer (Daszek sections, the
operator desk filter, X1) re-combined the same facets with its own rules, which is how "ready" and
"blocked" ended up meaning different things on different screens.

This module adds that verdict and nothing else:

* it composes EXISTING inputs (readiness facets, Guidance projection, policy/HITL status, the
  waiting-vs-stagnation SoT verdict) — it computes no new business truth of its own;
* it is a projection, not a Source of Truth: lifecycle stays owned by `CaseLifecycleState`,
  policy by the policy engine, membership by `feed_visibility`;
* every verdict carries `reason_codes` naming the input that decided it, so a wrong answer is
  traceable to a wrong input rather than to an opaque score.

Deliberate boundaries:

* The Guidance LLM cannot promote a case to `needs_review` by flagging stagnation. Only the
  waiting-vs-stagnation SoT (lifecycle + SLA evidence) can; an unconfirmed model flag is recorded
  as a reason code and changes nothing. See `stagnation_sot`.
* No import of `stagnation_sot` from here: `llm_contracts` stays a leaf. The caller passes the
  verdict in (`operator_projection_quality.build_case_readiness_projection` does exactly that).
* Readiness NEVER decides feed membership. `feed_visibility` owns that and must not read this.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

SCHEMA_VERSION = "case_readiness.v1"


class CaseReadinessState(str, Enum):
    """What the operator can actually do with this case right now."""

    READY_FOR_DECISION = "ready_for_decision"
    READY_FOR_APPROVAL = "ready_for_approval"
    READY_FOR_EXECUTION = "ready_for_execution"
    BLOCKED = "blocked"
    WAITING_EXTERNAL = "waiting_external"
    NEEDS_REVIEW = "needs_review"
    NO_ACTION_REQUIRED = "no_action_required"


CASE_READINESS_STATES: tuple[str, ...] = tuple(state.value for state in CaseReadinessState)

#: readiness values that mean the operator has outstanding work on this case
PENDING_READINESS_STATES = frozenset(
    {
        CaseReadinessState.READY_FOR_DECISION.value,
        CaseReadinessState.READY_FOR_APPROVAL.value,
        CaseReadinessState.READY_FOR_EXECUTION.value,
        CaseReadinessState.BLOCKED.value,
        CaseReadinessState.NEEDS_REVIEW.value,
    }
)

_OPERATOR_LABELS_PL = {
    CaseReadinessState.READY_FOR_DECISION: "Gotowa do decyzji operatora",
    CaseReadinessState.READY_FOR_APPROVAL: "Czeka na zatwierdzenie operatora",
    CaseReadinessState.READY_FOR_EXECUTION: "Zatwierdzona — do wykonania",
    CaseReadinessState.BLOCKED: "Zablokowana brakami danych",
    CaseReadinessState.WAITING_EXTERNAL: "Oczekiwanie na stronę zewnętrzną",
    CaseReadinessState.NEEDS_REVIEW: "Wymaga przeglądu operatora",
    CaseReadinessState.NO_ACTION_REQUIRED: "Brak działania po naszej stronie",
}

#: policy statuses that mean an operator approval is genuinely outstanding
_APPROVAL_PENDING_POLICY_STATUSES = frozenset(
    {"pending_operator_approval", "awaiting_approval", "pending_approval", "hitl_required"}
)

#: policy statuses that mean the decision is made and execution is what remains
_EXECUTION_READY_POLICY_STATUSES = frozenset({"approved", "allowed", "auto_allowed", "executable"})

#: guidance `waiting_for` values that name no real counterparty
_EMPTY_WAITING_PARTIES = frozenset({"", "none", "unknown"})

_DECISION_READY_BUSINESS_READINESS = frozenset(
    {"ready_for_offer", "ready_for_followup", "ready_for_close", "ready", "ready_for_decision"}
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _truthy(value: Any) -> bool:
    return bool(value) if value is not None else False


def build_case_readiness(
    *,
    readiness_facets: dict[str, Any] | None = None,
    case_guidance: dict[str, Any] | None = None,
    policy_status: str = "",
    policy_requires_operator_approval: bool | None = None,
    policy_allowed: bool | None = None,
    hitl_required: bool | None = None,
    waiting_vs_stagnation: dict[str, Any] | None = None,
    review_required: bool | None = None,
) -> dict[str, Any]:
    """Compose one `CaseReadinessState` from existing projections.

    Args:
        readiness_facets: the thin facet projection (`context_readiness`, `blocked_by_data`, ...).
        case_guidance: Guidance LLM projection — evidence only.
        policy_status: canonical policy/proposal status string, when known.
        policy_requires_operator_approval: from `PolicyActionEnvelopeV1`, when known.
        policy_allowed: `allowed_by_policy`, when known.
        hitl_required: real HITL gate state, when known.
        waiting_vs_stagnation: verdict from `stagnation_sot.evaluate_waiting_vs_stagnation`.
        review_required: explicit review flag from Understanding, when known.

    Returns:
        Projection dict with `state`, `reason_codes` and the echoed inputs that decided it.
    """
    facets = _as_dict(readiness_facets)
    cg = _as_dict(case_guidance)
    sot = _as_dict(waiting_vs_stagnation)

    context_readiness = str(facets.get("context_readiness") or "").strip().lower()
    blocked_by_data = _truthy(facets.get("blocked_by_data")) or context_readiness == "not_ready"
    facet_ready_for_decision = _truthy(facets.get("ready_for_decision")) or context_readiness == "decision_ready"

    status = str(policy_status or facets.get("policy_status") or "").strip().lower()
    business_readiness = str(cg.get("business_readiness") or "").strip().lower()
    attention_class = str(cg.get("operator_attention_class") or "").strip().lower()
    guidance_status = str(cg.get("operational_status") or "").strip().lower()
    waiting_party = str(cg.get("waiting_for") or sot.get("waiting_for") or "").strip().lower()

    sot_status = str(sot.get("status") or "").strip().lower()
    sot_stagnating = _truthy(sot.get("is_stagnating"))
    guidance_stagnation = _truthy(cg.get("stagnation_flag"))

    reason_codes: list[str] = []
    if context_readiness:
        reason_codes.append(f"context_readiness:{context_readiness}")
    if status:
        reason_codes.append(f"policy_status:{status}")
    if sot_status:
        reason_codes.append(f"stagnation_sot:{sot_status}")
    if guidance_stagnation and not sot_stagnating:
        # The model thinks it is stuck and the SoT does not agree (or could not be evaluated).
        # Recorded, never acted on — waiting is not stagnation.
        reason_codes.append("guidance_stagnation_flag_not_confirmed_by_sot")

    state, decided_by = _decide_state(
        blocked_by_data=blocked_by_data,
        facet_ready_for_decision=facet_ready_for_decision,
        policy_status=status,
        policy_requires_operator_approval=policy_requires_operator_approval,
        policy_allowed=policy_allowed,
        hitl_required=hitl_required,
        review_required=review_required,
        context_readiness=context_readiness,
        business_readiness=business_readiness,
        attention_class=attention_class,
        guidance_status=guidance_status,
        waiting_party=waiting_party,
        sot_status=sot_status,
        sot_stagnating=sot_stagnating,
    )
    reason_codes.append(f"decided_by:{decided_by}")

    return {
        "schema_version": SCHEMA_VERSION,
        "state": state.value,
        "operator_label_pl": _OPERATOR_LABELS_PL[state],
        "operator_action_pending": state.value in PENDING_READINESS_STATES,
        "blocked_by_data": blocked_by_data,
        "policy_status": status,
        "waiting_for": waiting_party,
        "waiting_status": sot_status,
        "is_stagnating": sot_stagnating,
        "gap_count": int(facets.get("gap_count") or 0),
        "conflict_count": int(facets.get("conflict_count") or 0),
        "context_readiness": context_readiness,
        "reason_codes": [str(code)[:80] for code in reason_codes][:12],
    }


def _decide_state(
    *,
    blocked_by_data: bool,
    facet_ready_for_decision: bool,
    policy_status: str,
    policy_requires_operator_approval: bool | None,
    policy_allowed: bool | None,
    hitl_required: bool | None,
    review_required: bool | None,
    context_readiness: str,
    business_readiness: str,
    attention_class: str,
    guidance_status: str,
    waiting_party: str,
    sot_status: str,
    sot_stagnating: bool,
) -> tuple[CaseReadinessState, str]:
    """Ordered rules. The order IS the contract, so it is written once, in one place.

    A concrete outstanding operator decision outranks everything: an approval waiting on the desk
    is actionable even when data is incomplete, and hiding it behind `blocked` would lose it.
    """
    if _truthy(hitl_required):
        return CaseReadinessState.READY_FOR_APPROVAL, "hitl_gate_required"
    if _truthy(policy_requires_operator_approval) or policy_status in _APPROVAL_PENDING_POLICY_STATUSES:
        return CaseReadinessState.READY_FOR_APPROVAL, "policy_requires_operator_approval"
    if policy_status in _EXECUTION_READY_POLICY_STATUSES or (
        policy_allowed is True and policy_requires_operator_approval is False
    ):
        return CaseReadinessState.READY_FOR_EXECUTION, "policy_approved_execution_pending"
    if blocked_by_data:
        return CaseReadinessState.BLOCKED, "blocked_by_data"
    if sot_stagnating:
        # Only the SoT can bring us here — a Guidance flag alone never does.
        return CaseReadinessState.NEEDS_REVIEW, "stagnation_sot_confirmed"
    if _truthy(review_required):
        return CaseReadinessState.NEEDS_REVIEW, "review_required"
    if sot_status == "waiting":
        return CaseReadinessState.WAITING_EXTERNAL, "stagnation_sot_waiting"
    if guidance_status == "waiting" and waiting_party not in _EMPTY_WAITING_PARTIES:
        # No lifecycle/SLA evidence available on this path; `waiting_external` is benign and is
        # labelled as coming from the Guidance projection rather than from the SoT.
        return CaseReadinessState.WAITING_EXTERNAL, "guidance_waiting_projection"
    if facet_ready_for_decision or business_readiness in _DECISION_READY_BUSINESS_READINESS:
        return CaseReadinessState.READY_FOR_DECISION, "facets_decision_ready"
    if context_readiness == "review_only" or attention_class in {"act_now", "act_soon", "keep_visible"}:
        return CaseReadinessState.NEEDS_REVIEW, "review_only_context"
    return CaseReadinessState.NO_ACTION_REQUIRED, "no_pending_operator_work"


__all__ = [
    "CASE_READINESS_STATES",
    "PENDING_READINESS_STATES",
    "SCHEMA_VERSION",
    "CaseReadinessState",
    "build_case_readiness",
]
