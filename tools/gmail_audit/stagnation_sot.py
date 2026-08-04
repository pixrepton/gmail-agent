"""AI-OS Roadmap 2.1 — waiting is not stagnation.

One question, one owner. `CaseLifecycleState` (llm_contracts/case_lifecycle.py) is the single
Source of Truth for where a case is; this module answers the one derived question that was
previously answered in several places with different rules:

    is this case merely WAITING (an expected, healthy pause) or is it STAGNATING (a pause the
    operator must act on)?

Three rules, stated once:

1. `WAITING_CLIENT` inside its SLA window is `waiting`. An expected wait is not a problem and
   must never be projected to the operator as one — that is the failure mode this slice closes.
2. `stagnating` requires temporal or lifecycle EVIDENCE: either the lifecycle state is already
   `STAGNATING` (someone with authority put it there), or the case has been in its current state
   past that state's `SLA_HOURS` budget.
3. "No mail for N days" is NEVER sufficient on its own. Mail silence is exactly what
   `WAITING_CLIENT` means; without a lifecycle state and an hours-in-state measurement there is
   no SLA to breach, so the honest answer is `not_evaluable`, not `stagnating`.

Guidance-LLM fields (`case_guidance.stagnation_flag`, `case_guidance.waiting_for`) stay what they
are: evidence and projection. They may be recorded here as inputs and echoed in `reason_codes`,
but a model flag alone cannot promote a case to `stagnating`.

Pure functions only. No store, no clock reads on behalf of the caller: hours-in-state and the
customer-signal timestamp are inputs, so the same case always evaluates the same way.
"""
from __future__ import annotations

from typing import Any

from llm_contracts.case_lifecycle import (
    SLA_HOURS,
    CaseLifecycleState,
    TERMINAL_STATES,
)

SCHEMA_VERSION = "waiting_vs_stagnation.v1"

#: the operator-facing answer
STATUS_WAITING = "waiting"
STATUS_ACTIVE = "active"
STATUS_STAGNATING = "stagnating"
STATUS_TERMINAL = "terminal"
STATUS_NOT_EVALUABLE = "not_evaluable"

WAITING_VS_STAGNATION_STATUSES = (
    STATUS_WAITING,
    STATUS_ACTIVE,
    STATUS_STAGNATING,
    STATUS_TERMINAL,
    STATUS_NOT_EVALUABLE,
)

#: how `sla_status` was produced — a projection consumer must be able to tell a real time-based
#: verdict from a structural guess, which is precisely what the old hardcoded `at_risk` hid.
SLA_SOURCE_TEMPORAL = "temporal_sla_hours"
SLA_SOURCE_NON_TEMPORAL = "non_temporal_heuristic"
SLA_SOURCE_UNKNOWN = "unknown"

SLA_STATUS_OK = "ok"
SLA_STATUS_AT_RISK = "at_risk"
SLA_STATUS_BREACHED = "breached"
SLA_STATUS_UNKNOWN = "unknown"

#: fraction of the SLA budget after which a still-inside-budget case is reported `at_risk`
_AT_RISK_FRACTION = 0.8

_PL_LABELS = {
    STATUS_WAITING: "Oczekiwanie w normie (nie stagnacja)",
    STATUS_ACTIVE: "Sprawa w toku",
    STATUS_STAGNATING: "Stagnacja — wymaga ruchu operatora",
    STATUS_TERMINAL: "Sprawa zamknięta",
    STATUS_NOT_EVALUABLE: "Brak danych do oceny stagnacji",
}


def _coerce_state(value: Any) -> CaseLifecycleState | None:
    if isinstance(value, CaseLifecycleState):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        return CaseLifecycleState(text)
    except ValueError:
        return None


def _coerce_hours(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    if hours < 0:
        return None
    return hours


def evaluate_waiting_vs_stagnation(
    *,
    lifecycle_state: Any,
    hours_in_state: Any = None,
    waiting_for: str = "",
    last_customer_signal_at: str = "",
    guidance_stagnation_flag: bool | None = None,
) -> dict[str, Any]:
    """Decide `waiting` vs `stagnating` for ONE case, from lifecycle + SLA evidence.

    Args:
        lifecycle_state: `CaseLifecycleState` or its string value. The SoT input.
        hours_in_state: hours spent in `lifecycle_state`, if measured. `None` means unmeasured —
            never treated as zero and never treated as a breach.
        waiting_for: which party the case waits on (`client`, `operator`, `supplier`, ...),
            recorded as evidence only.
        last_customer_signal_at: last inbound customer signal timestamp, echoed for the operator.
            Deliberately NOT used to derive stagnation — see rule 3 in the module docstring.
        guidance_stagnation_flag: the Guidance LLM's opinion. Recorded, never decisive.

    Returns:
        A projection dict. `status` is one of `WAITING_VS_STAGNATION_STATUSES`;
        `is_stagnating` is the single boolean a consumer should branch on.
    """
    state = _coerce_state(lifecycle_state)
    hours = _coerce_hours(hours_in_state)
    party = str(waiting_for or "").strip().lower()
    reason_codes: list[str] = []

    if guidance_stagnation_flag is not None:
        reason_codes.append(
            f"guidance_stagnation_flag:{'true' if guidance_stagnation_flag else 'false'}"
        )
    if party:
        reason_codes.append(f"waiting_for:{party}")

    if state is None:
        # No lifecycle state = no SLA = nothing to breach. Mail silence alone stops here.
        reason_codes.append("no_lifecycle_state")
        if str(last_customer_signal_at or "").strip():
            reason_codes.append("mail_silence_alone_is_not_stagnation")
        return _result(
            status=STATUS_NOT_EVALUABLE,
            state=None,
            hours=hours,
            sla_hours=None,
            sla_status=SLA_STATUS_UNKNOWN,
            sla_status_source=SLA_SOURCE_UNKNOWN,
            reason_codes=reason_codes,
            waiting_for=party,
            last_customer_signal_at=last_customer_signal_at,
        )

    if state in TERMINAL_STATES:
        reason_codes.append(f"terminal_state:{state.value}")
        return _result(
            status=STATUS_TERMINAL,
            state=state,
            hours=hours,
            sla_hours=None,
            sla_status=SLA_STATUS_OK,
            sla_status_source=SLA_SOURCE_NON_TEMPORAL,
            reason_codes=reason_codes,
            waiting_for=party,
            last_customer_signal_at=last_customer_signal_at,
        )

    sla_hours = SLA_HOURS.get(state)
    sla_status, sla_source, sla_reasons = _sla_verdict(hours=hours, sla_hours=sla_hours)
    reason_codes.extend(sla_reasons)

    if state is CaseLifecycleState.STAGNATING:
        # Explicit lifecycle verdict — the only non-temporal route to `stagnating`.
        reason_codes.append("explicit_lifecycle_stagnating")
        status = STATUS_STAGNATING
    elif sla_status == SLA_STATUS_BREACHED:
        reason_codes.append("sla_breach_stagnation_candidate")
        status = STATUS_STAGNATING
    elif state is CaseLifecycleState.WAITING_CLIENT:
        reason_codes.append("waiting_within_sla_is_expected")
        status = STATUS_WAITING
    else:
        status = STATUS_ACTIVE

    return _result(
        status=status,
        state=state,
        hours=hours,
        sla_hours=sla_hours,
        sla_status=sla_status,
        sla_status_source=sla_source,
        reason_codes=reason_codes,
        waiting_for=party,
        last_customer_signal_at=last_customer_signal_at,
    )


def _sla_verdict(*, hours: float | None, sla_hours: int | None) -> tuple[str, str, list[str]]:
    """SLA verdict for one state. Only a MEASURED duration can produce a temporal verdict."""
    if sla_hours is None:
        return SLA_STATUS_UNKNOWN, SLA_SOURCE_UNKNOWN, ["state_without_sla_budget"]
    if hours is None:
        # The state HAS a budget, but nobody measured how long we have been in it. Reporting
        # `at_risk` here is what the old api_app code did; it is a structural guess, not a clock.
        return SLA_STATUS_UNKNOWN, SLA_SOURCE_NON_TEMPORAL, ["hours_in_state_unmeasured"]
    if hours > float(sla_hours):
        return SLA_STATUS_BREACHED, SLA_SOURCE_TEMPORAL, [f"sla_breached:{sla_hours}h"]
    if hours >= float(sla_hours) * _AT_RISK_FRACTION:
        return SLA_STATUS_AT_RISK, SLA_SOURCE_TEMPORAL, [f"sla_at_risk:{sla_hours}h"]
    return SLA_STATUS_OK, SLA_SOURCE_TEMPORAL, [f"sla_within_budget:{sla_hours}h"]


def _result(
    *,
    status: str,
    state: CaseLifecycleState | None,
    hours: float | None,
    sla_hours: int | None,
    sla_status: str,
    sla_status_source: str,
    reason_codes: list[str],
    waiting_for: str,
    last_customer_signal_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "is_stagnating": status == STATUS_STAGNATING,
        "is_waiting": status == STATUS_WAITING,
        "lifecycle_state": state.value if state is not None else "",
        "hours_in_state": hours,
        "sla_hours": sla_hours,
        "sla_status": sla_status,
        "sla_status_source": sla_status_source,
        "waiting_for": waiting_for,
        "last_customer_signal_at": str(last_customer_signal_at or "").strip(),
        "reason_codes": [str(code)[:80] for code in reason_codes][:10],
        "operator_label_pl": _PL_LABELS.get(status, ""),
    }


def sla_status_projection(
    *,
    lifecycle_state: Any,
    hours_in_state: Any = None,
) -> dict[str, Any]:
    """Small adapter for callers (e.g. `/cases/{id}/state-summary`) that only need SLA fields.

    Always carries `sla_status_source`, so `at_risk` derived from a real duration is
    distinguishable from `unknown` derived from "this state merely HAS an SLA budget".
    """
    verdict = evaluate_waiting_vs_stagnation(
        lifecycle_state=lifecycle_state,
        hours_in_state=hours_in_state,
    )
    return {
        "sla_status": verdict["sla_status"],
        "sla_status_source": verdict["sla_status_source"],
        "sla_hours": verdict["sla_hours"],
        "hours_in_state": verdict["hours_in_state"],
        "stagnation_status": verdict["status"],
        "is_stagnating": verdict["is_stagnating"],
    }


def waiting_vs_stagnation_from_guidance(
    case_guidance: dict[str, Any] | None,
    *,
    lifecycle_state: Any,
    hours_in_state: Any = None,
    last_customer_signal_at: str = "",
) -> dict[str, Any]:
    """Evaluate using a Guidance projection as EVIDENCE, with lifecycle still deciding."""
    cg = case_guidance if isinstance(case_guidance, dict) else {}
    flag = cg.get("stagnation_flag")
    return evaluate_waiting_vs_stagnation(
        lifecycle_state=lifecycle_state,
        hours_in_state=hours_in_state,
        waiting_for=str(cg.get("waiting_for") or ""),
        last_customer_signal_at=last_customer_signal_at,
        guidance_stagnation_flag=bool(flag) if flag is not None else None,
    )


__all__ = [
    "SCHEMA_VERSION",
    "SLA_SOURCE_NON_TEMPORAL",
    "SLA_SOURCE_TEMPORAL",
    "SLA_SOURCE_UNKNOWN",
    "SLA_STATUS_AT_RISK",
    "SLA_STATUS_BREACHED",
    "SLA_STATUS_OK",
    "SLA_STATUS_UNKNOWN",
    "STATUS_ACTIVE",
    "STATUS_NOT_EVALUABLE",
    "STATUS_STAGNATING",
    "STATUS_TERMINAL",
    "STATUS_WAITING",
    "WAITING_VS_STAGNATION_STATUSES",
    "evaluate_waiting_vs_stagnation",
    "sla_status_projection",
    "waiting_vs_stagnation_from_guidance",
]
