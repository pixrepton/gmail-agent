"""AI-OS Roadmap 2.2 — `CaseReadinessState` composed from existing facets.

Contract asserted here:

* the thin `readiness_facets` projection survives untouched — `case_readiness` is added next to it;
* the composed verdict is decided by ordered rules, and every verdict names the input that decided
  it in `reason_codes`;
* a Guidance `stagnation_flag` alone never reaches `needs_review` (waiting != stagnating);
* readiness is a projection: it computes nothing about feed membership.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_contracts.case_readiness import (  # noqa: E402
    CASE_READINESS_STATES,
    CaseReadinessState,
    build_case_readiness,
)
from llm_contracts.case_lifecycle import CaseLifecycleState, SLA_HOURS  # noqa: E402
from operator_projection_quality import (  # noqa: E402
    build_case_readiness_projection,
    build_readiness_facets_projection,
)
from stagnation_sot import evaluate_waiting_vs_stagnation  # noqa: E402


def _facets(**overrides):
    base = {
        "context_readiness": "review_only",
        "ready_for_decision": False,
        "ready_for_operator_review": True,
        "blocked_by_data": False,
        "policy_status": "",
        "gap_count": 0,
        "conflict_count": 0,
        "operator_label_pl": "Wymaga przeglądu operatora",
    }
    base.update(overrides)
    return base


# ── enum surface ───────────────────────────────────────────────────────────────────────────


def test_all_seven_readiness_states_exist():
    assert CASE_READINESS_STATES == (
        "ready_for_decision",
        "ready_for_approval",
        "ready_for_execution",
        "blocked",
        "waiting_external",
        "needs_review",
        "no_action_required",
    )


# ── ordered rules ──────────────────────────────────────────────────────────────────────────


def test_pending_hitl_gate_is_ready_for_approval_even_with_data_gaps():
    out = build_case_readiness(
        readiness_facets=_facets(blocked_by_data=True, gap_count=3),
        hitl_required=True,
    )
    assert out["state"] == CaseReadinessState.READY_FOR_APPROVAL.value
    assert "decided_by:hitl_gate_required" in out["reason_codes"]
    assert out["operator_action_pending"] is True


def test_policy_requiring_operator_approval_is_ready_for_approval():
    out = build_case_readiness(
        readiness_facets=_facets(),
        policy_status="pending_operator_approval",
        policy_requires_operator_approval=True,
    )
    assert out["state"] == CaseReadinessState.READY_FOR_APPROVAL.value


def test_approved_policy_without_further_approval_is_ready_for_execution():
    out = build_case_readiness(
        readiness_facets=_facets(),
        policy_status="approved",
    )
    assert out["state"] == CaseReadinessState.READY_FOR_EXECUTION.value
    assert "policy_status:approved" in out["reason_codes"]


def test_critical_data_gaps_are_blocked():
    out = build_case_readiness(readiness_facets=_facets(blocked_by_data=True, gap_count=2))
    assert out["state"] == CaseReadinessState.BLOCKED.value
    assert out["blocked_by_data"] is True
    assert out["gap_count"] == 2


def test_decision_ready_facets_are_ready_for_decision():
    out = build_case_readiness(
        readiness_facets=_facets(context_readiness="decision_ready", ready_for_decision=True)
    )
    assert out["state"] == CaseReadinessState.READY_FOR_DECISION.value


def test_nothing_pending_is_no_action_required():
    out = build_case_readiness(
        readiness_facets=_facets(context_readiness="", ready_for_operator_review=False),
        case_guidance={"operator_attention_class": "case_only_ok"},
    )
    assert out["state"] == CaseReadinessState.NO_ACTION_REQUIRED.value
    assert out["operator_action_pending"] is False


# ── waiting vs stagnation, again, at the readiness layer ────────────────────────────────────


def test_sot_waiting_verdict_is_waiting_external_not_needs_review():
    verdict = evaluate_waiting_vs_stagnation(
        lifecycle_state=CaseLifecycleState.WAITING_CLIENT,
        hours_in_state=24,
        waiting_for="client",
    )
    out = build_case_readiness(readiness_facets=_facets(), waiting_vs_stagnation=verdict)
    assert out["state"] == CaseReadinessState.WAITING_EXTERNAL.value
    assert out["is_stagnating"] is False
    assert "decided_by:stagnation_sot_waiting" in out["reason_codes"]


def test_sot_confirmed_stagnation_becomes_needs_review():
    verdict = evaluate_waiting_vs_stagnation(
        lifecycle_state=CaseLifecycleState.WAITING_CLIENT,
        hours_in_state=SLA_HOURS[CaseLifecycleState.WAITING_CLIENT] + 24,
    )
    out = build_case_readiness(readiness_facets=_facets(), waiting_vs_stagnation=verdict)
    assert out["state"] == CaseReadinessState.NEEDS_REVIEW.value
    assert out["is_stagnating"] is True
    assert "decided_by:stagnation_sot_confirmed" in out["reason_codes"]


def test_guidance_stagnation_flag_alone_does_not_reach_needs_review():
    verdict = evaluate_waiting_vs_stagnation(
        lifecycle_state=CaseLifecycleState.WAITING_CLIENT,
        hours_in_state=2,
        guidance_stagnation_flag=True,
    )
    out = build_case_readiness(
        readiness_facets=_facets(),
        case_guidance={"stagnation_flag": True, "operational_status": "stagnating", "waiting_for": "client"},
        waiting_vs_stagnation=verdict,
    )
    assert out["state"] == CaseReadinessState.WAITING_EXTERNAL.value
    assert "guidance_stagnation_flag_not_confirmed_by_sot" in out["reason_codes"]


def test_guidance_waiting_projection_is_labelled_when_no_sot_evidence_exists():
    out = build_case_readiness(
        readiness_facets=_facets(),
        case_guidance={"operational_status": "waiting", "waiting_for": "supplier"},
    )
    assert out["state"] == CaseReadinessState.WAITING_EXTERNAL.value
    assert "decided_by:guidance_waiting_projection" in out["reason_codes"]
    assert out["waiting_for"] == "supplier"


def test_guidance_waiting_without_a_counterparty_is_not_waiting_external():
    out = build_case_readiness(
        readiness_facets=_facets(),
        case_guidance={"operational_status": "waiting", "waiting_for": "none"},
    )
    assert out["state"] != CaseReadinessState.WAITING_EXTERNAL.value


# ── projection wiring ──────────────────────────────────────────────────────────────────────


def _intel(**overrides):
    intel = {
        "case_guidance": {"business_readiness": "needs_data", "operational_status": "active_review"},
        "case_understanding": {"review_required": False},
        "missing_info": {"critical": [], "important": []},
    }
    intel.update(overrides)
    return intel


def test_readiness_projection_uses_the_same_facets_it_is_composed_from():
    intel = _intel(missing_info={"critical": ["heated_area_m2"], "important": []})
    facets = build_readiness_facets_projection(intel)
    out = build_case_readiness_projection(intel, readiness_facets=facets)
    assert facets["blocked_by_data"] is True
    assert out["state"] == CaseReadinessState.BLOCKED.value
    assert out["schema_version"] == "case_readiness.v1"


def test_readiness_projection_builds_facets_itself_when_not_supplied():
    out = build_case_readiness_projection(_intel())
    assert out["state"] in CASE_READINESS_STATES


def _minimal_intake() -> dict:
    return {
        "schema_version": "1.0",
        "source": {"channel": "gmail", "mailbox": "m", "observed_at": "2026-01-01T00:00:00"},
        "message": {
            "message_id": "mid1",
            "date": "2026-01-01",
            "sender": "a@b.c",
            "subject": "Sub",
            "snippet": "sn",
            "has_attachments": False,
        },
        "thread": {"thread_id": "tid", "thread_position": "latest", "is_reply_or_forward": False},
        "business_area": "sales",
        "primary_signal": {"code": "x", "name": "X", "description": "d", "business_significance": "b"},
        "case_assessment": {
            "case_family": "lead_opportunity",
            "is_new_case": True,
            "state_detected": "new",
            "state_change": {"detected": False},
        },
        "decision": {"action": "create_case", "action_rationale": "r"},
        "priority": "medium",
        "confidence": {
            "signal_confidence": 0.8,
            "case_link_confidence": 0.7,
            "decision_confidence": 0.7,
            "extraction_confidence": 0.7,
        },
        "review": {"required": False, "flags": []},
        "reason": "reason",
        "extracted_data": {"entities": {}, "dates": [], "amounts": [], "references": {}, "deadlines": []},
    }


def test_v2_projection_carries_both_readiness_facets_and_case_readiness():
    from dash_projection_v2 import build_v2_shadow_projection

    projection = build_v2_shadow_projection(
        _minimal_intake(),
        stage_outputs={
            "case_link_result": {"decision": "no_link", "selected_case_key": "", "confidence": 0.0},
            "action_plan_result": {"primary_action": "prepare_reply", "safe_for_live_push": False},
            "business_reasoning_result": {},
            "case_intelligence_result": _intel(),
        },
    )
    for patch_key in ("case_patch", "desk_note_patch"):
        patch = projection[patch_key]
        assert "readiness_facets" in patch, f"{patch_key} lost the thin facets"
        assert patch["case_readiness"]["state"] in CASE_READINESS_STATES
        assert patch["case_readiness"]["reason_codes"]


def test_case_readiness_never_decides_feed_membership():
    # membership belongs to feed_visibility; readiness must not appear in its inputs
    import feed_visibility

    source = Path(feed_visibility.__file__).read_text(encoding="utf-8")
    assert "case_readiness" not in source
    assert "CaseReadinessState" not in source
