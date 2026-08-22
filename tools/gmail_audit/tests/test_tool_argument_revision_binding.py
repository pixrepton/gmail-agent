"""P1.2 revision binding: envelopes/constraints bound to the durable current CAD.

Uses the P1.1P durable mechanism (store-backed DecisionRevisionLedger + rebuild)
-- not a local variable pretending to be the current revision. A new CAD
revision invalidates the old envelope/constraint projection and requires a new
projection before execution may proceed toward HITL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import load_constitution
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.policy_action_spine import (
    evaluate_semantic_policy_plan_consistency,
)
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.tool_argument_constraints import project_slice_argument_constraints
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from agent_runtime.store import build_initial_snapshot
from canonical_action_decision import (
    DecisionRevisionLedger,
    build_business_decision_proposal,
    canonicalize,
    evaluate_decision_revision,
    request_decision_revision,
)
from llm_contracts.engagement_snapshot_v2 import PolicyActionEnvelopeV1
from mailbox_memory import InMemoryMailboxMemoryStore


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


def _br(*, missing: list[str] | None = None) -> dict[str, object]:
    return {
        "recommended_next_action": "collect_data",
        "missing_information": missing or ["error_code", "exact_symptoms"],
        "recommended_action_reason": "Brak danych diagnostycznych.",
        "urgency": "normal",
        "confidence": {"action_confidence": 0.8, "business_confidence": 0.7},
    }


def _situation(missing: list[str] | None = None) -> dict[str, object]:
    return {
        "missing_information": missing or ["error_code", "exact_symptoms"],
        "missing_critical_fields": missing or ["error_code", "exact_symptoms"],
    }


def _durable_ledger_r2() -> tuple[DecisionRevisionLedger, dict[str, object]]:
    store = InMemoryMailboxMemoryStore()
    ledger = DecisionRevisionLedger(store=store)
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    cad_r1 = canonicalize(
        proposal=proposal,
        situation_understanding=_situation(),
        case_id="case_rev",
        situation_version="sv_1",
    )
    ledger.register_cad(cad_r1)
    emitted = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,
        reason_code="CANONICAL_FACT_CHANGED",
        ledger=ledger,
    )
    result = evaluate_decision_revision(
        request=emitted["request"],
        current_cad=cad_r1,
        business_reasoning_result=_br(missing=["exact_symptoms"]),
        situation_understanding=_situation(["exact_symptoms"]),
        ledger=ledger,
    )
    assert result["outcome"] == "ACCEPTED"
    # Fresh projection from durable state (P1.1P restart semantics).
    return DecisionRevisionLedger.from_store(store), result


def _envelope(
    *,
    decision_id: str,
    version_id: str,
    semantic_hash: str,
) -> PolicyActionEnvelopeV1:
    constraints = project_slice_argument_constraints(
        action_intent="ask_for_missing_data",
        action_target="customer",
        action_channel="mail",
        canonical_decision_id=decision_id,
        decision_version_id=version_id,
        source_semantic_hash=semantic_hash,
        allowed_action_tools=["generate_draft_reply"],
    )
    return PolicyActionEnvelopeV1(
        canonical_decision_id=decision_id,
        decision_version_id=version_id,
        source_semantic_hash=semantic_hash,
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        action_intent="ask_for_missing_data",
        action_target="customer",
        action_channel="mail",
        allowed_action_tools=["generate_draft_reply"],
        forbidden_tools=["request_operator_clarification"],
        argument_constraints=constraints,
        freshness="current",
    )


def _plan(
    *,
    decision_version_id: str,
    semantic_hash: str,
) -> ToolCallPlan:
    return ToolCallPlan(
        tool_name="generate_draft_reply",
        arguments={"intent": "missing_info"},
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        decision_version_id=decision_version_id,
        semantic_hash=semantic_hash,
    )


class _CountingRegistry:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, plan: ToolCallPlan, *, context: ToolExecutionContext) -> ToolResult:
        self.calls += 1
        return ToolResult(status="ok", turn_summary_pl="executed")


class _FixedPlanner:
    def __init__(self, plan: ToolCallPlan) -> None:
        self._plan = plan

    def plan_next_tool(self, **_: object) -> ToolCallPlan:
        return self._plan


def _run_graph(envelope: PolicyActionEnvelopeV1, plan: ToolCallPlan, ledger) -> tuple[int, object]:
    snap = build_initial_snapshot(
        case_id="case_rev",
        engagement_id="eng_rev",
        trace_id="trace_rev",
    )
    snap = snap.model_copy(
        update={"case_kind": "awaria_naprawa", "policy_action_envelope": envelope}
    )
    registry = _CountingRegistry()
    signal_payload = {
        "harness_mode": True,
        "policy_action_envelope": envelope.model_dump(mode="python"),
    }
    result = AgentGraphEngine(
        planner=_FixedPlanner(plan),
        constitution=load_constitution(),
        tool_registry=registry,
    ).run(
        snap,
        context=ToolExecutionContext.from_snapshot(
            snap,
            settings=_settings(),
            signal_payload=signal_payload,
            constitution=load_constitution(),
            decision_revision_ledger=ledger,
        ),
    )
    return registry.calls, result


def test_old_envelope_denied_against_durable_current_revision() -> None:
    ledger, result = _durable_ledger_r2()
    decision_id = result["new_cad"]["decision_id"]
    r1_hash = result["old_cad"]["semantic_hash"]
    r2_hash = result["new_cad"]["semantic_hash"]

    envelope_r1 = _envelope(
        decision_id=decision_id,
        version_id=f"{decision_id}:r1",
        semantic_hash=r1_hash,
    )
    plan_r1 = _plan(decision_version_id=f"{decision_id}:r1", semantic_hash=r1_hash)

    # Envelope and plan agree with each other, but both are stale vs durable r2.
    consistency = evaluate_semantic_policy_plan_consistency(envelope_r1, plan_r1)
    assert consistency.status == "consistent"

    calls, run_result = _run_graph(envelope_r1, plan_r1, ledger)
    assert calls == 0
    assert run_result.snapshot.hitl_gate.required is True
    # Durable-current check is observable via the HITL reason (the envelope
    # and plan agree with each other, so consistency stays "consistent").
    assert "stale_decision_revision" in run_result.snapshot.hitl_gate.reason
    observed = run_result.snapshot.semantic_policy_plan_consistency
    assert observed is not None
    assert observed.status == "consistent"


def test_new_projection_from_current_revision_passes_toward_hitl() -> None:
    ledger, result = _durable_ledger_r2()
    decision_id = result["new_cad"]["decision_id"]
    r2_hash = result["new_cad"]["semantic_hash"]

    envelope_r2 = _envelope(
        decision_id=decision_id,
        version_id=f"{decision_id}:r2",
        semantic_hash=r2_hash,
    )
    plan_r2 = _plan(decision_version_id=f"{decision_id}:r2", semantic_hash=r2_hash)

    # New revision requires a new constraint projection: the r2 constraints
    # must be bound to r2 (decision/version/hash refs).
    constraint = envelope_r2.argument_constraints[0]
    assert constraint["decision_id"] == decision_id
    assert constraint["decision_version_id"] == f"{decision_id}:r2"
    assert constraint["semantic_hash"] == r2_hash

    consistency = evaluate_semantic_policy_plan_consistency(envelope_r2, plan_r2)
    assert consistency.status == "consistent"
    calls, run_result = _run_graph(envelope_r2, plan_r2, ledger)
    assert calls >= 1
    assert run_result.snapshot.hitl_gate.required is True


def test_durable_current_check_requires_ledger() -> None:
    """Without a ledger the monitor only enforces envelope-vs-plan identity."""
    ledger, result = _durable_ledger_r2()
    decision_id = result["new_cad"]["decision_id"]
    r1_hash = result["old_cad"]["semantic_hash"]
    envelope_r1 = _envelope(
        decision_id=decision_id,
        version_id=f"{decision_id}:r1",
        semantic_hash=r1_hash,
    )
    plan_r1 = _plan(decision_version_id=f"{decision_id}:r1", semantic_hash=r1_hash)
    calls, run_result = _run_graph(envelope_r1, plan_r1, None)
    # No ledger -> no durable-current enforcement; envelope/plan agree -> runs.
    assert calls >= 1


def test_mixed_versions_denied() -> None:
    ledger, result = _durable_ledger_r2()
    decision_id = result["new_cad"]["decision_id"]
    r1_hash = result["old_cad"]["semantic_hash"]
    r2_hash = result["new_cad"]["semantic_hash"]
    envelope_r1 = _envelope(
        decision_id=decision_id,
        version_id=f"{decision_id}:r1",
        semantic_hash=r1_hash,
    )
    plan_r2 = _plan(decision_version_id=f"{decision_id}:r2", semantic_hash=r2_hash)
    consistency = evaluate_semantic_policy_plan_consistency(envelope_r1, plan_r2)
    assert consistency.status == "conflicting"
    assert "STALE_DECISION_REVISION" in consistency.reason_codes
