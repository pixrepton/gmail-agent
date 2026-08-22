"""P1.2-C: reference-monitor argument enforcement (deterministic adversarial suite).

Every adversarial plan passes through the REAL enforcement seam
(``evaluate_semantic_policy_plan_consistency`` + ``AgentGraphEngine.run``),
not an isolated helper. A malicious/mock planner that bypasses the tool schema
must still be DENIED before execution.
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
from agent_runtime.tool_argument_constraints import (
    REASON_ARGUMENT_NOT_ALLOWED,
    REASON_ARGUMENT_OUTSIDE_CANONICAL_SET,
    REASON_CANONICAL_ARGUMENT_MISMATCH,
    project_slice_argument_constraints,
)
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from agent_runtime.tools_registry import AgentToolRegistry
from agent_runtime.store import build_initial_snapshot
from llm_contracts.engagement_snapshot_v2 import PolicyActionEnvelopeV1


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


def _envelope(
    *,
    version_id: str = "dec_1:r1",
    semantic_hash: str = "sh_1",
    action_intent: str = "ask_for_missing_data",
) -> PolicyActionEnvelopeV1:
    constraints = project_slice_argument_constraints(
        action_intent=action_intent,
        action_target="customer",
        action_channel="mail",
        canonical_decision_id="dec_1",
        decision_version_id=version_id,
        source_semantic_hash=semantic_hash,
        allowed_action_tools=["generate_draft_reply"],
    )
    return PolicyActionEnvelopeV1(
        canonical_decision_id="dec_1",
        decision_version_id=version_id,
        source_semantic_hash=semantic_hash,
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        action_intent=action_intent,
        action_target="customer",
        action_channel="mail",
        allowed_action_tools=["generate_draft_reply"],
        forbidden_tools=["request_operator_clarification"],
        argument_constraints=constraints,
        freshness="current",
    )


def _plan(
    arguments: dict | None = None,
    *,
    version_id: str = "dec_1:r1",
    semantic_hash: str = "sh_1",
) -> ToolCallPlan:
    return ToolCallPlan(
        tool_name="generate_draft_reply",
        arguments=arguments if arguments is not None else {"intent": "missing_info"},
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        decision_version_id=version_id,
        semantic_hash=semantic_hash,
    )


# --------------------------------------------------------------------------
# deterministic adversarial matrix (reference-monitor seam)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments,expected_reason",
    [
        ({"intent": "missing_info", "case_id": "case_attacker"}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "missing_info", "target": "operator"}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "missing_info", "channel": "internal"}, REASON_ARGUMENT_NOT_ALLOWED),
        (
            {"intent": "missing_info", "required_information": ["exact_symptoms", "installer_password"]},
            REASON_ARGUMENT_NOT_ALLOWED,
        ),
        ({"intent": "missing_info", "recipient": "attacker@example.com"}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "missing_info", "attachment_ids": ["att_unauthorized"]}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "missing_info", "approval_receipt": "appr_fake"}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "quote"}, REASON_ARGUMENT_OUTSIDE_CANONICAL_SET),
    ],
)
def test_adversarial_tool_call_plans_denied(arguments: dict, expected_reason: str) -> None:
    envelope = _envelope()
    consistency = evaluate_semantic_policy_plan_consistency(envelope, _plan(arguments))
    assert consistency.status == "conflicting"
    assert expected_reason in consistency.reason_codes
    assert consistency.argument_violations
    violation = consistency.argument_violations[0]
    assert violation["argument_name"] in arguments
    assert violation["decision_version_id"] == "dec_1:r1"


def test_all_correct_arguments_are_consistent() -> None:
    envelope = _envelope()
    consistency = evaluate_semantic_policy_plan_consistency(envelope, _plan())
    assert consistency.status == "consistent"
    assert consistency.argument_violations == []


def test_stale_decision_version_denied() -> None:
    envelope = _envelope(version_id="dec_1:r2")
    consistency = evaluate_semantic_policy_plan_consistency(envelope, _plan(version_id="dec_1:r1"))
    assert consistency.status == "conflicting"
    assert "STALE_DECISION_REVISION" in consistency.reason_codes


def test_semantic_hash_drift_denied() -> None:
    envelope = _envelope(semantic_hash="sh_canonical")
    consistency = evaluate_semantic_policy_plan_consistency(
        envelope,
        _plan(semantic_hash="sh_other"),
    )
    assert consistency.status == "conflicting"
    assert "canonical_semantic_drift" in consistency.reason_codes


def test_wrong_case_id_reason_is_observable() -> None:
    envelope = _envelope()
    consistency = evaluate_semantic_policy_plan_consistency(
        envelope,
        _plan({"intent": "missing_info", "case_id": "case_attacker"}),
    )
    assert consistency.argument_violations[0]["argument_name"] == "case_id"
    assert consistency.argument_violations[0]["proposed"] == "case_attacker"
    assert consistency.argument_violations[0]["reason_code"] == REASON_ARGUMENT_NOT_ALLOWED


# --------------------------------------------------------------------------
# graph-level enforcement (real execution seam, no tool runs on DENY)
# --------------------------------------------------------------------------


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


def _snapshot_with_envelope(envelope: PolicyActionEnvelopeV1):
    snap = build_initial_snapshot(
        case_id="case_1",
        engagement_id="eng_1",
        trace_id="trace_1",
    )
    return snap.model_copy(
        update={"case_kind": "awaria_naprawa", "policy_action_envelope": envelope}
    )


@pytest.mark.parametrize(
    "arguments,expected_reason",
    [
        ({"intent": "missing_info", "target": "operator"}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "missing_info", "channel": "internal"}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "missing_info", "recipient": "attacker@example.com"}, REASON_ARGUMENT_NOT_ALLOWED),
        ({"intent": "quote"}, REASON_ARGUMENT_OUTSIDE_CANONICAL_SET),
    ],
)
def test_graph_denies_adversarial_arguments_before_execution(
    arguments: dict,
    expected_reason: str,
) -> None:
    envelope = _envelope()
    snap = _snapshot_with_envelope(envelope)
    registry = _CountingRegistry()
    signal_payload = {
        "harness_mode": True,
        "policy_action_envelope": envelope.model_dump(mode="python"),
    }
    result = AgentGraphEngine(
        planner=_FixedPlanner(_plan(arguments)),
        constitution=load_constitution(),
        tool_registry=registry,
    ).run(
        snap,
        context=ToolExecutionContext.from_snapshot(
            snap,
            settings=_settings(),
            signal_payload=signal_payload,
            constitution=load_constitution(),
        ),
    )
    assert registry.calls == 0
    assert result.snapshot.hitl_gate.required is True
    consistency = result.snapshot.semantic_policy_plan_consistency
    assert consistency is not None
    assert consistency.status == "conflicting"
    assert expected_reason in consistency.reason_codes


def test_graph_allows_correct_arguments_to_reach_hitl() -> None:
    envelope = _envelope()
    snap = _snapshot_with_envelope(envelope)
    signal_payload = {
        "harness_mode": True,
        "policy_action_envelope": envelope.model_dump(mode="python"),
    }
    result = AgentGraphEngine(
        planner=_FixedPlanner(_plan()),
        constitution=load_constitution(),
        tool_registry=AgentToolRegistry(),
    ).run(
        snap,
        context=ToolExecutionContext.from_snapshot(
            snap,
            settings=_settings(),
            signal_payload=signal_payload,
            constitution=load_constitution(),
        ),
    )
    out = result.snapshot
    consistency = out.semantic_policy_plan_consistency
    assert consistency is not None
    assert consistency.status == "consistent"
    assert out.hitl_gate.required is True
    attempted = [item for item in out.agent_memory.tool_calls]
    assert any(item.tool == "generate_draft_reply" for item in attempted)
    executed_ok = [item for item in attempted if item.status == "ok"]
    assert not any(
        item.tool in {"send_email", "auto_send", "request_operator_clarification"}
        for item in executed_ok
    )
