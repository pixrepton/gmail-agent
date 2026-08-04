"""AI-OS Roadmap 2.1 — waiting is not stagnation (single lifecycle SoT).

Root cause closed here: three separate places answered "is this case stuck?" with three different
rules — a Guidance LLM boolean (`case_guidance.stagnation_flag`), a private lifecycle map inside
`mailbox_memory_runtime`, and a hardcoded `sla_status = "at_risk"` in `api_app` that fired for
every state that merely HAS an SLA budget.

Contract asserted here:

* `WAITING_CLIENT` inside its SLA window is `waiting`, never `stagnating`;
* `stagnating` needs lifecycle or temporal evidence (explicit `STAGNATING`, or a measured breach);
* mail silence alone is never sufficient;
* `CaseLifecycleState` owns the status -> lifecycle mapping; the runtime delegates to it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_contracts.case_lifecycle import (  # noqa: E402
    PIPELINE_CASE_STATUS_TO_LIFECYCLE,
    SLA_HOURS,
    CaseLifecycleState,
    map_case_status_to_lifecycle,
)
from stagnation_sot import (  # noqa: E402
    SLA_SOURCE_NON_TEMPORAL,
    SLA_SOURCE_TEMPORAL,
    SLA_STATUS_AT_RISK,
    SLA_STATUS_BREACHED,
    SLA_STATUS_OK,
    SLA_STATUS_UNKNOWN,
    STATUS_ACTIVE,
    STATUS_NOT_EVALUABLE,
    STATUS_STAGNATING,
    STATUS_TERMINAL,
    STATUS_WAITING,
    evaluate_waiting_vs_stagnation,
    sla_status_projection,
    waiting_vs_stagnation_from_guidance,
)

_WAITING_SLA = SLA_HOURS[CaseLifecycleState.WAITING_CLIENT]  # 168h


# ── 1. waiting != stagnating ───────────────────────────────────────────────────────────────


def test_waiting_client_within_sla_is_waiting_not_stagnating():
    out = evaluate_waiting_vs_stagnation(
        lifecycle_state=CaseLifecycleState.WAITING_CLIENT,
        hours_in_state=48,
        waiting_for="client",
    )
    assert out["status"] == STATUS_WAITING
    assert out["is_stagnating"] is False
    assert out["sla_status"] == SLA_STATUS_OK
    assert out["sla_status_source"] == SLA_SOURCE_TEMPORAL
    assert "waiting_within_sla_is_expected" in out["reason_codes"]


def test_waiting_client_close_to_the_budget_is_at_risk_but_still_not_stagnating():
    out = evaluate_waiting_vs_stagnation(
        lifecycle_state=CaseLifecycleState.WAITING_CLIENT,
        hours_in_state=_WAITING_SLA - 1,
    )
    assert out["sla_status"] == SLA_STATUS_AT_RISK
    assert out["status"] == STATUS_WAITING
    assert out["is_stagnating"] is False


def test_waiting_for_party_is_recorded_as_evidence_only():
    out = evaluate_waiting_vs_stagnation(
        lifecycle_state=CaseLifecycleState.WAITING_CLIENT,
        hours_in_state=1,
        waiting_for="Client",
    )
    assert out["waiting_for"] == "client"
    assert "waiting_for:client" in out["reason_codes"]
    assert out["is_stagnating"] is False


# ── 2. SLA breach -> stagnation candidate ──────────────────────────────────────────────────


def test_sla_breach_makes_a_waiting_case_a_stagnation_candidate():
    out = evaluate_waiting_vs_stagnation(
        lifecycle_state=CaseLifecycleState.WAITING_CLIENT,
        hours_in_state=_WAITING_SLA + 1,
        waiting_for="client",
    )
    assert out["status"] == STATUS_STAGNATING
    assert out["is_stagnating"] is True
    assert out["sla_status"] == SLA_STATUS_BREACHED
    assert out["sla_status_source"] == SLA_SOURCE_TEMPORAL
    assert "sla_breach_stagnation_candidate" in out["reason_codes"]


def test_explicit_stagnating_lifecycle_state_is_stagnating_without_a_clock():
    out = evaluate_waiting_vs_stagnation(lifecycle_state=CaseLifecycleState.STAGNATING)
    assert out["status"] == STATUS_STAGNATING
    assert "explicit_lifecycle_stagnating" in out["reason_codes"]


def test_breach_in_a_non_waiting_state_is_also_stagnating():
    out = evaluate_waiting_vs_stagnation(
        lifecycle_state=CaseLifecycleState.OFFER_PREP,
        hours_in_state=SLA_HOURS[CaseLifecycleState.OFFER_PREP] + 5,
    )
    assert out["status"] == STATUS_STAGNATING


def test_active_state_within_budget_is_active_not_waiting():
    out = evaluate_waiting_vs_stagnation(
        lifecycle_state=CaseLifecycleState.NEGOTIATION,
        hours_in_state=1,
    )
    assert out["status"] == STATUS_ACTIVE
    assert out["is_waiting"] is False
    assert out["is_stagnating"] is False


# ── 3. mail silence alone is never enough ──────────────────────────────────────────────────


def test_mail_silence_alone_without_lifecycle_is_not_stagnation():
    out = evaluate_waiting_vs_stagnation(
        lifecycle_state="",
        last_customer_signal_at="2026-01-01T00:00:00Z",
    )
    assert out["status"] == STATUS_NOT_EVALUABLE
    assert out["is_stagnating"] is False
    assert "mail_silence_alone_is_not_stagnation" in out["reason_codes"]
    assert out["sla_status"] == SLA_STATUS_UNKNOWN


def test_unmeasured_time_in_state_never_produces_a_temporal_verdict():
    out = evaluate_waiting_vs_stagnation(lifecycle_state=CaseLifecycleState.WAITING_CLIENT)
    assert out["sla_status"] == SLA_STATUS_UNKNOWN
    assert out["sla_status_source"] == SLA_SOURCE_NON_TEMPORAL
    assert "hours_in_state_unmeasured" in out["reason_codes"]
    assert out["status"] == STATUS_WAITING  # an unmeasured wait is still just a wait
    assert out["is_stagnating"] is False


def test_unknown_lifecycle_literal_is_not_evaluable_rather_than_stagnating():
    out = evaluate_waiting_vs_stagnation(lifecycle_state="some_future_state", hours_in_state=9999)
    assert out["status"] == STATUS_NOT_EVALUABLE
    assert out["is_stagnating"] is False


def test_terminal_states_are_not_stagnating():
    for state in (CaseLifecycleState.COMPLETED, CaseLifecycleState.LOST):
        out = evaluate_waiting_vs_stagnation(lifecycle_state=state, hours_in_state=100_000)
        assert out["status"] == STATUS_TERMINAL
        assert out["is_stagnating"] is False


# ── 4. the Guidance LLM is evidence, not a second SoT ──────────────────────────────────────


def test_guidance_stagnation_flag_alone_cannot_promote_a_healthy_wait():
    out = waiting_vs_stagnation_from_guidance(
        {"stagnation_flag": True, "waiting_for": "client"},
        lifecycle_state=CaseLifecycleState.WAITING_CLIENT,
        hours_in_state=2,
    )
    assert out["status"] == STATUS_WAITING
    assert out["is_stagnating"] is False
    assert "guidance_stagnation_flag:true" in out["reason_codes"]


def test_guidance_flag_false_cannot_suppress_a_real_sla_breach():
    out = waiting_vs_stagnation_from_guidance(
        {"stagnation_flag": False},
        lifecycle_state=CaseLifecycleState.WAITING_CLIENT,
        hours_in_state=_WAITING_SLA + 10,
    )
    assert out["status"] == STATUS_STAGNATING
    assert "guidance_stagnation_flag:false" in out["reason_codes"]


# ── 5. one lifecycle mapping owner ─────────────────────────────────────────────────────────


def test_runtime_lifecycle_inference_delegates_to_the_lifecycle_contract():
    from mailbox_memory_runtime import infer_lifecycle_from_case_status

    for status, expected in PIPELINE_CASE_STATUS_TO_LIFECYCLE.items():
        assert infer_lifecycle_from_case_status(status) == expected.value
    # unknown status keeps the previous conservative default
    assert infer_lifecycle_from_case_status("totally_unknown") == CaseLifecycleState.QUALIFICATION.value


def test_open_collision_between_dialects_is_explicit_not_silent():
    # `open` exists in both vocabularies with different meanings; the dialect decides, and both
    # answers stay reachable instead of one map silently winning.
    assert map_case_status_to_lifecycle("open", dialect="pipeline") == CaseLifecycleState.NEW_LEAD
    assert map_case_status_to_lifecycle("open", dialect="mailbox") == CaseLifecycleState.QUALIFICATION
    assert map_case_status_to_lifecycle("closed", dialect="pipeline") == CaseLifecycleState.COMPLETED


# ── 6. api_app SLA projection is labelled, never a fake clock ──────────────────────────────


def test_sla_projection_marks_a_non_temporal_answer_as_such():
    out = sla_status_projection(lifecycle_state="waiting_for_client")
    assert out["sla_status"] == SLA_STATUS_UNKNOWN
    assert out["sla_status_source"] == SLA_SOURCE_NON_TEMPORAL
    assert out["sla_hours"] == _WAITING_SLA
    assert out["is_stagnating"] is False


def test_sla_projection_is_temporal_when_hours_are_measured():
    out = sla_status_projection(lifecycle_state="offer_preparation", hours_in_state=1)
    assert out["sla_status"] == SLA_STATUS_OK
    assert out["sla_status_source"] == SLA_SOURCE_TEMPORAL


def test_api_app_hours_in_state_ignores_mail_traffic_timestamps():
    from api_app import _hours_in_lifecycle_state

    # latest_signal_at / updated_at must not be mistaken for time-in-state
    assert _hours_in_lifecycle_state({"latest_signal_at": "2020-01-01T00:00:00Z"}) is None
    assert _hours_in_lifecycle_state({"updated_at": "2020-01-01T00:00:00Z"}) is None
    measured = _hours_in_lifecycle_state({"lifecycle_state_since": "2020-01-01T00:00:00+00:00"})
    assert measured is not None and measured > 0
