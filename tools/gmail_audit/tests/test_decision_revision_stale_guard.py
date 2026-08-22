"""P1.1-C: supersession + stale artifact invalidation in the runtime guard."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import AgentConstitution
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.policy_action_spine import (
    evaluate_semantic_policy_plan_consistency,
)
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from canonical_action_decision import (
    DecisionRevisionLedger,
    approval_binds_revision,
    evaluate_decision_revision,
    request_decision_revision,
)
from execution_runtime import ActionProposal, normalize_action_proposal
from llm_contracts.engagement_snapshot_v2 import PolicyActionEnvelopeV1


def _envelope(
    *,
    decision_id: str,
    version_id: str,
    proposal_id: str = "apv2_1",
) -> PolicyActionEnvelopeV1:
    return PolicyActionEnvelopeV1(
        canonical_decision_id=decision_id,
        decision_version_id=version_id,
        source_semantic_hash="sh_x",
        policy_decision_id="pdec_1",
        action_proposal_id=proposal_id,
        action_intent="ask_for_missing_data",
        action_target="customer",
        action_channel="mail",
        allowed_action_tools=["generate_draft_reply"],
        forbidden_tools=["request_operator_clarification"],
        freshness="current",
    )


def test_plan_bound_to_superseded_revision_is_denied() -> None:
    envelope_r2 = _envelope(decision_id="dec_1", version_id="dec_1:r2")
    stale_plan = ToolCallPlan(
        tool_name="generate_draft_reply",
        arguments={"intent": "missing_info"},
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        decision_version_id="dec_1:r1",
    )
    consistency = evaluate_semantic_policy_plan_consistency(envelope_r2, stale_plan)
    assert consistency.status == "conflicting"
    assert consistency.reason_codes == ["STALE_DECISION_REVISION"]


def test_plan_bound_to_current_revision_remains_consistent() -> None:
    envelope_r2 = _envelope(decision_id="dec_1", version_id="dec_1:r2")
    current_plan = ToolCallPlan(
        tool_name="generate_draft_reply",
        arguments={"intent": "missing_info"},
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        decision_version_id="dec_1:r2",
    )
    consistency = evaluate_semantic_policy_plan_consistency(envelope_r2, current_plan)
    assert consistency.status == "consistent"


def test_graph_denies_stale_revision_plan_before_execution() -> None:
    class _Planner:
        def __init__(self, plan: ToolCallPlan) -> None:
            self.plan = plan

        def plan_next_tool(self, **_: object) -> ToolCallPlan:
            return self.plan

    class _Registry:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, plan, *, context):
            self.calls += 1
            from agent_runtime.tool_result import ToolResult

            return ToolResult(status="ok", turn_summary_pl="executed")

    constitution = AgentConstitution(
        hvac_rules="",
        company_context="",
        forbidden_actions=(),
        tool_allowlist=("generate_draft_reply",),
        tool_budget={},
    )
    envelope = _envelope(decision_id="dec_1", version_id="dec_1:r2")
    snapshot = build_initial_snapshot(
        case_id="case_1",
        engagement_id="eng_1",
        trace_id="trace_1",
    )
    snapshot = snapshot.model_copy(
        update={"policy_action_envelope": envelope}
    )
    stale_plan = ToolCallPlan(
        tool_name="generate_draft_reply",
        arguments={"intent": "missing_info"},
        decision_version_id="dec_1:r1",
    )
    registry = _Registry()
    result = AgentGraphEngine(
        planner=_Planner(stale_plan),
        constitution=constitution,
        tool_registry=registry,
    ).run(
        snapshot,
        context=ToolExecutionContext.from_snapshot(
            snapshot,
            signal_payload={
                "harness_mode": True,
                "source_kind": "gmail",
                "policy_action_envelope": envelope.model_dump(mode="python"),
            },
            constitution=constitution,
        ),
    )
    assert registry.calls == 0
    assert result.snapshot.hitl_gate.required is True
    assert "semantic_tool_mismatch" in result.snapshot.hitl_gate.reason
    consistency = result.snapshot.semantic_policy_plan_consistency
    assert consistency is not None
    assert "STALE_DECISION_REVISION" in consistency.reason_codes


def test_approval_proposal_binds_decision_version() -> None:
    proposal = normalize_action_proposal(
        {
            "case_id": "case_1",
            "action_type": "prepare_reply_draft",
            "decision_version_id": "dec_1:r1",
        }
    )
    assert proposal.decision_version_id == "dec_1:r1"
    cad_r2 = {"decision_id": "dec_1", "decision_version_id": "dec_1:r2"}
    cad_r1 = {"decision_id": "dec_1", "decision_version_id": "dec_1:r1"}
    assert approval_binds_revision(proposal.to_dict(), cad_r1) is True
    assert approval_binds_revision(proposal.to_dict(), cad_r2) is False


def test_old_approval_cannot_authorize_new_revision_in_ledger_flow() -> None:
    ledger = DecisionRevisionLedger()
    from canonical_action_decision import build_business_decision_proposal, canonicalize

    br = {
        "recommended_next_action": "collect_data",
        "missing_information": ["error_code", "exact_symptoms"],
        "recommended_action_reason": "x",
        "urgency": "normal",
        "confidence": {"action_confidence": 0.8, "business_confidence": 0.7},
    }
    situation = {"missing_information": ["error_code", "exact_symptoms"]}
    proposal = build_business_decision_proposal(br)
    cad_r1 = canonicalize(
        proposal=proposal,
        situation_understanding=situation,
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
        business_reasoning_result=br,
        situation_understanding=situation,
        ledger=ledger,
    )
    assert result["outcome"] == "ACCEPTED"
    old_approval = {
        "approval_id": "appr_1",
        "decision_version_id": f"{cad_r1['decision_id']}:r1",
    }
    assert approval_binds_revision(old_approval, result["new_cad"]) is False
    assert approval_binds_revision(old_approval, result["old_cad"]) is True


def test_bounded_revision_runtime_trajectory() -> None:
    """Production-faithful bounded trajectory: r1 -> revision -> r2; old plan
    DENIED (STALE_DECISION_REVISION), new plan passes to HITL."""
    from agent_runtime.constitution import load_constitution
    from agent_runtime.tools_registry import AgentToolRegistry
    from test_closeout_p0_bounded_runtime_slice import (
        _br as _closeout_br,
        _cad as _closeout_cad,
        _handoff as _closeout_handoff,
        _settings as _closeout_settings,
    )

    ledger = DecisionRevisionLedger()
    cad_r1 = _closeout_cad()
    ledger.register_cad(cad_r1)
    handoff_r1, _ = _closeout_handoff(cad_r1)
    envelope_r1 = PolicyActionEnvelopeV1.model_validate(
        handoff_r1["signal_payload"]["policy_action_envelope"]
    )
    assert envelope_r1.decision_version_id == f"{cad_r1['decision_id']}:r1"

    br_r2 = dict(_closeout_br())
    br_r2["missing_information"] = ["exact_symptoms"]
    emitted = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=1,
        reason_code="CANONICAL_FACT_CHANGED",
        ledger=ledger,
    )
    revision = evaluate_decision_revision(
        request=emitted["request"],
        current_cad=cad_r1,
        business_reasoning_result=br_r2,
        situation_understanding={"missing_information": ["exact_symptoms"]},
        ledger=ledger,
    )
    assert revision["outcome"] == "ACCEPTED"
    cad_r2 = revision["new_cad"]
    handoff_r2, _ = _closeout_handoff(cad_r2)
    envelope_r2 = PolicyActionEnvelopeV1.model_validate(
        handoff_r2["signal_payload"]["policy_action_envelope"]
    )
    assert envelope_r2.decision_version_id == f"{cad_r2['decision_id']}:r2"
    assert envelope_r1.decision_version_id != envelope_r2.decision_version_id

    class _Planner:
        def __init__(self, plan: ToolCallPlan) -> None:
            self.plan = plan

        def plan_next_tool(self, **_: object) -> ToolCallPlan:
            return self.plan

    class _Registry:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def execute(self, plan, *, context):
            from agent_runtime.tool_result import ToolResult

            self.executed.append(plan.tool_name)
            return ToolResult(
                status="ok",
                turn_summary_pl="executed",
                snapshot_delta={
                    "hitl_gate": {"required": True, "reason": "fixture_executed"},
                    "operational_status": {"code": "pending_operator"},
                },
            )

    snapshot = build_initial_snapshot(
        case_id="case_closeout_service_1",
        engagement_id="eng_p11",
        trace_id="trace_p11",
    )
    snapshot = snapshot.model_copy(
        update={"case_kind": "awaria_naprawa", "policy_action_envelope": envelope_r2}
    )
    registry = _Registry()
    stale = AgentGraphEngine(
        planner=_Planner(
            ToolCallPlan(
                tool_name="generate_draft_reply",
                arguments={"intent": "missing_info"},
                decision_version_id=f"{cad_r1['decision_id']}:r1",
            )
        ),
        constitution=load_constitution(),
        tool_registry=registry,
    ).run(
        snapshot,
        context=ToolExecutionContext.from_snapshot(
            snapshot,
            settings=_closeout_settings(),
            signal_payload=handoff_r2["signal_payload"],
        ),
    )
    stale_consistency = stale.snapshot.semantic_policy_plan_consistency
    assert stale_consistency is not None
    assert stale_consistency.status == "conflicting"
    assert "STALE_DECISION_REVISION" in stale_consistency.reason_codes
    assert stale.snapshot.hitl_gate.required is True
    assert registry.executed == []

    fresh = AgentGraphEngine(
        planner=_Planner(
            ToolCallPlan(
                tool_name="generate_draft_reply",
                arguments={"intent": "missing_info"},
                decision_version_id=f"{cad_r2['decision_id']}:r2",
            )
        ),
        constitution=load_constitution(),
        tool_registry=AgentToolRegistry(),
    ).run(
        snapshot,
        context=ToolExecutionContext.from_snapshot(
            snapshot,
            settings=_closeout_settings(),
            signal_payload=handoff_r2["signal_payload"],
        ),
    )
    assert fresh.snapshot.hitl_gate.required is True
    assert any(
        item.tool == "generate_draft_reply" and item.status == "ok"
        for item in fresh.snapshot.agent_memory.tool_calls
    )
