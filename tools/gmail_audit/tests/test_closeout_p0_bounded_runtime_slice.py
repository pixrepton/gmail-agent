"""P0 closeout: one bounded production-faithful runtime trajectory.

First enforced CAD slice: ``ask_for_missing_data / customer / mail``.

Proves, on the real planner-spine handoff harness:

* CAD semantic identity (decision_id + semantic_hash) survives CAD -> ActionPlan
  -> NBA -> DecisionCandidate -> Policy/APv2 -> PolicyActionEnvelopeV1;
* effective tools offer ``generate_draft_reply`` and never offer
  ``request_operator_clarification`` for a frozen customer/mail decision;
* the runtime trajectory ends in HITL (approval required) with no live send and
  no operator-clarification execution;
* a plan that tries ``request_operator_clarification`` is DENIED before
  execution with ``canonical_semantic_drift`` /
  ``semantic_tool_forbidden_for_action_intent``.

Deterministic gate: no hypothesis, no guarded skip, no LLM call.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from action_planner import plan_actions
from agent_runtime.constitution import load_constitution
from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_ALLOWLIST
from agent_runtime.effective_tools import compute_effective_available_tools
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools_registry import AgentToolRegistry
from canonical_action_decision import (
    build_canonical_decision_for_stage,
    canonical_decision_code,
)
from case_intelligence.next_best_action import build_next_best_action
from decision_candidate import build_decision_candidate
from eval_planner_spine_handoff import build_production_faithful_planner_signal
from llm_contracts.engagement_snapshot_v2 import PolicyActionEnvelopeV1


CASE_ID = "case_closeout_service_1"
MESSAGE_ID = "msg_closeout_service_1"
SIGNAL_ID = "sig_closeout_service_1"


def _br() -> dict[str, object]:
    return {
        "recommended_next_action": "collect_data",
        "missing_information": ["model urządzenia", "opis objawu lub kod błędu"],
        "recommended_action_reason": (
            "Klient zgłosił niejasny problem serwisowy bez danych diagnostycznych."
        ),
        "urgency": "normal",
        "confidence": {"action_confidence": 0.82, "business_confidence": 0.7},
    }


def _situation() -> dict[str, object]:
    return {
        "missing_information": ["model urządzenia", "opis objawu lub kod błędu"],
        "missing_critical_fields": ["model urządzenia", "opis objawu lub kod błędu"],
    }


def _intake() -> dict[str, object]:
    return {
        "business_area": "service",
        "review_required": True,
        "message_id": MESSAGE_ID,
        "decision": {"action": "review"},
        "review": {
            "required": True,
            "flags": ["ambiguous_signal", "insufficient_thread_context"],
        },
    }


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=4,
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=1,
        kalk_top_max_retries=1,
    )


def _cad() -> dict[str, object]:
    cad, failure = build_canonical_decision_for_stage(
        business_reasoning_result=_br(),
        situation_understanding=_situation(),
        case_context_pack={},
        intake_result=_intake(),
        case_id=CASE_ID,
        situation_version="sv_1",
    )
    assert cad is not None and failure is None
    assert cad["semantic_status"] == "FROZEN"
    return cad


def _build_intelligence(cad: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    plan = plan_actions(
        _intake(),
        {"decision": "no_link"},
        _br(),
        {"draft_enabled": True, "drafts": []},
        None,
        canonical_decision=cad,
    )
    nba = build_next_best_action(
        intake_result=_intake(),
        case_link_result={"decision": "no_link"},
        business_result=_br(),
        reply_result={"draft_enabled": True, "drafts": []},
        action_plan_result=plan,
        missing_info={
            "critical": ["model urządzenia", "opis objawu lub kod błędu"],
            "important": [],
            "helpful": [],
        },
        merge_split_suggestions={},
        canonical_decision=cad,
    )
    primary = nba["primary_next_action"]
    candidate = build_decision_candidate(
        case_id=CASE_ID,
        source_signal_id=MESSAGE_ID,
        topic="service",
        case_type="awaria_naprawa",
        priority="normal",
        sla_risk="low",
        next_best_action=primary,
        next_best_action_code=canonical_decision_code(cad),
        risk_class_candidate="low",
        case_context_pack={},
    )
    intel: dict[str, object] = {
        "understanding_output": {
            "source_signal_id": MESSAGE_ID,
            "operator_explanation": {
                "essence_pl": "Niejasny problem serwisowy",
                "why_pl": "brak danych diagnostycznych",
            },
            "missing_critical_fields": [
                "model urządzenia",
                "opis objawu lub kod błędu",
            ],
            "case_family": "awaria_naprawa",
        },
        "case_understanding": {"case_family": "awaria_naprawa", "case_id": CASE_ID},
        "next_best_action": nba,
        "decision_candidate": candidate,
        "execution_metadata": {
            "stage_name": "case_intelligence",
            "shadow_only": True,
            "input_primary_action": str(plan.get("primary_action") or ""),
            "input_business_next_action": "collect_data",
            "input_reply_draft_enabled": True,
        },
        "mailbox_memory_context_pack": {},
    }
    return plan, intel


def _handoff(cad: dict[str, object]) -> tuple[dict[str, Any], dict[str, object]]:
    plan, intel = _build_intelligence(cad)
    # Existing engagement: the customer is reporting a service fault on a
    # thread with prior messages and evidence-backed facts, so policy restricts
    # authority (review/approval) instead of treating this as an unknown
    # first-contact or low-confidence fact change.
    snapshot = {
        "thread": {"thread_position": "latest", "message_count": 3},
        "source_message": {"message_id": MESSAGE_ID},
    }
    hot_state = {
        "schema_version": "case_snapshot_hot_state.v1",
        "snapshot_id": f"hot_{CASE_ID}",
        "case": {
            "case_id": CASE_ID,
            "operational_status": "OK",
            # Existing client with approved outreach: policy restricts
            # authority (review/approval) but must not block the customer
            # draft of the frozen customer/mail decision.
            "metadata": {"approved_outreach": True},
        },
        "latest_activity": {"thread_message_count": 3, "is_first_contact": False},
        "key_facts": [
            {
                "fact_key": "reported_fault",
                "value": "brak ogrzewania",
                "source_ref": MESSAGE_ID,
            }
        ],
        "snapshot_meta": {"confidence": 0.95, "review_required": False},
    }
    handoff = build_production_faithful_planner_signal(
        case_id=CASE_ID,
        signal_id=SIGNAL_ID,
        message_id=MESSAGE_ID,
        subject="Awaria pompy ciepła",
        body=(
            "Dzień dobry, pompa nie grzeje od wczoraj. "
            "Nie wiem, co dalej robić."
        ),
        case_intelligence_result=intel,
        action_plan_result=plan,
        case_kind="awaria_naprawa",
        policy_required=True,
        harness_mode=False,
        snapshot=snapshot,
        case_snapshot_hot_state=hot_state,
    )
    return handoff, plan


def _snapshot_with_envelope(
    signal: dict[str, Any],
) -> tuple[Any, PolicyActionEnvelopeV1]:
    envelope = PolicyActionEnvelopeV1.model_validate(
        signal["policy_action_envelope"]
    )
    snap = build_initial_snapshot(
        case_id=CASE_ID,
        engagement_id="eng_closeout_service_1",
        trace_id="trace_closeout_service_1",
    )
    snap = snap.model_copy(
        update={"case_kind": "awaria_naprawa", "policy_action_envelope": envelope}
    )
    return snap, envelope


def test_closeout_semantic_hash_full_chain_cad_to_envelope() -> None:
    cad = _cad()
    handoff, plan = _handoff(cad)
    signal = handoff["signal_payload"]
    envelope = PolicyActionEnvelopeV1.model_validate(
        signal["policy_action_envelope"]
    )

    assert envelope.freshness == "current"
    assert envelope.canonical_decision_id == cad["decision_id"]
    assert envelope.source_semantic_hash == cad["semantic_hash"]
    # APv2 projects into its own execution vocabulary; the canonical semantic
    # identity (hash) and target/channel are the enforced surface.
    assert envelope.action_intent == "prepare_reply_draft"
    assert envelope.action_target == "customer"
    assert envelope.action_channel == "mail"
    assert envelope.allowed_by_policy is True
    assert "generate_draft_reply" in envelope.allowed_action_tools
    assert "request_operator_clarification" in envelope.forbidden_tools

    # Seam identity: CAD -> ActionPlan -> NBA -> envelope.
    assert plan["canonical_decision_id"] == cad["decision_id"]
    assert plan["semantic_hash"] == cad["semantic_hash"]
    intel = handoff["case_intelligence_result"]
    primary = intel["next_best_action"]["primary_next_action"]
    assert primary["canonical_decision_id"] == cad["decision_id"]
    assert primary["semantic_hash"] == cad["semantic_hash"]
    assert primary["action_type"] == "ask_for_missing_data"
    assert primary["suggested_channel"] == "mail"


class _DraftPlanner:
    def __init__(self, semantic_hash: str) -> None:
        self._semantic_hash = semantic_hash

    def plan_next_tool(self, **_: object) -> ToolCallPlan:
        return ToolCallPlan(
            tool_name="generate_draft_reply",
            arguments={"intent": "missing_info"},
            semantic_hash=self._semantic_hash,
        )


class _RocPlanner:
    def __init__(self, semantic_hash: str) -> None:
        self._semantic_hash = semantic_hash

    def plan_next_tool(self, **_: object) -> ToolCallPlan:
        return ToolCallPlan(
            tool_name="request_operator_clarification",
            arguments={"ask_pl": "proszę o decyzję operatora"},
            semantic_hash=self._semantic_hash,
        )


def test_closeout_bounded_runtime_trajectory_customer_mail() -> None:
    cad = _cad()
    handoff, _ = _handoff(cad)
    signal = handoff["signal_payload"]
    snap, envelope = _snapshot_with_envelope(signal)

    # Tool visibility / authority: draft visible, operator clarification not.
    effective = compute_effective_available_tools(
        tuple(MAIL_AGENT_TOOL_ALLOWLIST),
        constitution=load_constitution(),
        settings=_settings(),
        snapshot=snap,
    )
    offered = set(effective.offered)
    assert "generate_draft_reply" in offered
    assert "request_operator_clarification" not in offered
    roc_filter = next(
        item
        for item in effective.filtered
        if item.tool_name == "request_operator_clarification"
    )
    assert roc_filter.offered is False
    assert roc_filter.reason_code == "SEMANTIC_TOOL_FORBIDDEN"

    ctx = ToolExecutionContext.from_snapshot(
        snap,
        settings=_settings(),
        signal_payload=signal,
    )
    engine = AgentGraphEngine(
        planner=_DraftPlanner(cad["semantic_hash"]),
        constitution=load_constitution(),
        tool_registry=AgentToolRegistry(),
    )
    result = engine.run(snap, context=ctx)
    out = result.snapshot

    assert out.policy_action_envelope is not None
    assert out.policy_action_envelope.source_semantic_hash == cad["semantic_hash"]
    consistency = out.semantic_policy_plan_consistency
    assert consistency is not None
    assert consistency.status == "consistent"
    assert "canonical_semantic_drift" not in consistency.reason_codes

    # HITL required, no live send, operator clarification never executed.
    assert out.hitl_gate.required is True
    executed = [
        item for item in out.agent_memory.tool_calls if item.status == "ok"
    ]
    assert not any(
        item.tool in {"send_email", "auto_send", "request_operator_clarification"}
        for item in executed
    )


def test_closeout_negative_forbidden_tool_denied_before_execution() -> None:
    cad = _cad()
    handoff, _ = _handoff(cad)
    signal = handoff["signal_payload"]
    snap, _ = _snapshot_with_envelope(signal)

    ctx = ToolExecutionContext.from_snapshot(
        snap,
        settings=_settings(),
        signal_payload=signal,
    )
    engine = AgentGraphEngine(
        planner=_RocPlanner(cad["semantic_hash"]),
        constitution=load_constitution(),
        tool_registry=AgentToolRegistry(),
    )
    result = engine.run(snap, context=ctx)
    out = result.snapshot

    consistency = out.semantic_policy_plan_consistency
    assert consistency is not None
    assert consistency.status == "conflicting"
    assert any(
        code in consistency.reason_codes
        for code in (
            "canonical_semantic_drift",
            "semantic_tool_forbidden_for_action_intent",
        )
    )
    assert out.hitl_gate.required is True
    assert "semantic_tool_mismatch" in out.hitl_gate.reason
    assert not any(
        item.tool == "request_operator_clarification" and item.status == "ok"
        for item in out.agent_memory.tool_calls
    )
