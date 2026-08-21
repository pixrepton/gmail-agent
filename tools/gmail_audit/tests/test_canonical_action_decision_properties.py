"""Deterministic spine property suite for the CanonicalActionDecision boundary.

P0 gate: pure pytest parametrize (no hypothesis, no guarded skip) — the gate
must always run. Metamorphic LLM cohort (paraphrase/noise/prompt injection) is
a separate P0.5 track and intentionally NOT part of this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from action_planner import plan_actions as build_action_plan_result
from action_proposal_v2 import build_action_proposals_v2
from agent_runtime.effective_tools import semantic_envelope_gate_reason
from agent_runtime.policy_action_spine import (
    _semantic_tool_constraints,
    evaluate_semantic_policy_plan_consistency,
)
from agent_runtime.tool_result import ToolCallPlan
from canonical_action_decision import (
    CANONICAL_CHANNELS,
    CANONICAL_TARGETS,
    build_business_decision_proposal,
    build_decision_revision_request,
    canonical_decision_code,
    canonicalization_failure_review_state,
    canonicalize,
)
from case_intelligence.next_best_action import build_next_best_action
from decision_candidate import build_decision_candidate
from llm_contracts.engagement_snapshot_v2 import PolicyActionEnvelopeV1
from policy_action_proposal import build_policy_action_proposal


def _br(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "recommended_next_action": "collect_data",
        "missing_information": ["what_exactly_is_not_working", "observed_symptoms"],
        "recommended_action_reason": "Customer described a service fault without details.",
        "urgency": "normal",
        "confidence": {"action_confidence": 0.84, "business_confidence": 0.8},
    }
    result.update(overrides)
    return result


def _situation(missing: list[str] | None = None) -> dict[str, object]:
    return {
        "understanding_output_id": "uo_1",
        "missing_information": missing or ["what_exactly_is_not_working", "observed_symptoms"],
    }


def _cad(**br_overrides: object) -> dict[str, object]:
    proposal = build_business_decision_proposal(_br(**br_overrides))
    assert proposal is not None
    cad = canonicalize(
        proposal=proposal,
        situation_understanding=_situation(
            br_overrides.get("missing_information")  # type: ignore[arg-type]
        ),
        case_id="case_456",
        situation_version="18",
    )
    assert cad.get("semantic_status") == "FROZEN"
    return cad


# (a) Semantic conservation: no layer may change goal/action_type/target/channel
# or substitute request_operator_clarification for the frozen customer/mail
# decision.
def test_property_semantic_conservation_through_full_chain() -> None:
    cad = _cad()

    # ActionPlan projects into its own execution vocabulary.
    plan = build_action_plan_result(
        {"decision": {"action": "create_case"}, "review_required": True},
        {"decision": "no_link"},
        _br(),
        {"draft_enabled": True, "drafts": []},
        None,
        canonical_decision=cad,
    )
    assert plan["primary_action"] == "prepare_reply"
    assert plan["execution_step"] == "prepare_reply"
    assert plan["canonical_decision_id"] == cad["decision_id"]

    # Case Intelligence projects the canonical action (own vocabulary).
    nba = build_next_best_action(
        intake_result={"business_area": "service", "review_required": True},
        case_link_result={"decision": "no_link"},
        business_result=_br(),
        reply_result={"draft_enabled": True},
        action_plan_result={"primary_action": "prepare_reply"},
        missing_info={"critical": [], "important": ["observed_symptoms"], "helpful": []},
        merge_split_suggestions={},
        canonical_decision=cad,
    )
    primary = nba["primary_next_action"]
    assert primary["action_type"] == "ask_for_missing_data"
    assert primary["suggested_channel"] == "mail"
    assert primary["canonical_decision_id"] == cad["decision_id"]

    # DecisionCandidate normalizes the canonical code (A17).
    candidate = build_decision_candidate(
        case_id="case_456",
        source_signal_id="msg_1",
        next_best_action_code=canonical_decision_code(cad),
    )
    assert candidate["next_best_action"] == "ask_for_missing_data"

    # Policy proposal keeps the CAD id and maps to the reply action class.
    proposal = build_policy_action_proposal(
        action_plan_result={
            "primary_action": "prepare_reply",
            "canonical_decision_id": cad["decision_id"],
        },
        intake_result={},
        case_link_result={},
        case_intelligence_result={},
        entity_link_result={},
        snapshot={},
    )
    assert proposal["action_class"] == "LIVE_REPLY"
    assert proposal["canonical_decision_id"] == cad["decision_id"]

    # APv2 produces a customer-facing draft proposal, never operator
    # clarification.
    apv2 = build_action_proposals_v2(
        decision_candidate=candidate,
        policy_decision={
            "policy_decision_id": "pdec_1",
            "decision_candidate_id": candidate["decision_candidate_id"],
            "status": "allowed_with_review",
            "allowed_actions": ["prepare_reply_draft", "request_missing_info", "mark_attention_required"],
            "risk_class": "low",
        },
        primary_action_type="prepare_reply",
    )
    assert apv2 and apv2[0]["action_type"] == "prepare_reply_draft"

    # Tool envelope: customer/mail constraints forbid operator clarification.
    constraints = _semantic_tool_constraints(
        proposal_action_type="request_missing_info",
        candidate=candidate,
        canonical_decision_id=cad["decision_id"],
    )
    assert constraints["action_target"] == "customer"
    assert constraints["action_channel"] == "mail"
    assert "request_operator_clarification" in constraints["forbidden_tools"]
    assert constraints["canonical_decision_id"] == cad["decision_id"]


# (b) Review invariant: review_required=true never changes the addressee.
@pytest.mark.parametrize("review_required", [True, False])
def test_property_review_does_not_retarget(review_required: bool) -> None:
    cad = _cad()
    intake = {"business_area": "service", "review_required": review_required}
    plan = build_action_plan_result(
        intake,
        {"decision": "no_link"},
        _br(),
        {"draft_enabled": True, "drafts": []},
        None,
        canonical_decision=cad,
    )
    nba = build_next_best_action(
        intake_result=intake,
        case_link_result={"decision": "no_link"},
        business_result=_br(),
        reply_result={"draft_enabled": True},
        action_plan_result={"primary_action": "prepare_reply"},
        missing_info={"critical": [], "important": [], "helpful": []},
        merge_split_suggestions={},
        canonical_decision=cad,
    )
    assert plan["primary_action"] == "prepare_reply"
    assert nba["primary_next_action"]["action_type"] == "ask_for_missing_data"
    assert nba["primary_next_action"]["suggested_channel"] == "mail"


# (c)+(d) Tool availability / forbidden tool: ROC is not offered and the
# reference monitor blocks it with canonical_semantic_drift.
def test_property_forbidden_tool_not_offered_and_blocked() -> None:
    envelope = PolicyActionEnvelopeV1(
        canonical_decision_id="dec_123",
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        action_intent="ask_for_missing_data",
        action_target="customer",
        action_channel="mail",
        forbidden_tools=["request_operator_clarification"],
        allowed_action_tools=["generate_draft_reply"],
        freshness="current",
    )
    decision = semantic_envelope_gate_reason("request_operator_clarification", snapshot=type(
        "Snap", (), {"policy_action_envelope": envelope}
    )())
    assert decision is not None
    assert decision.reason_code == "SEMANTIC_TOOL_FORBIDDEN"
    assert decision.offered is False

    plan = ToolCallPlan(
        tool_name="request_operator_clarification",
        arguments={},
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
    )
    consistency = evaluate_semantic_policy_plan_consistency(envelope, plan)
    assert consistency.status == "conflicting"
    assert "canonical_semantic_drift" in consistency.reason_codes


# (d) Unsupported channel: canonicalization rejects every illegal channel.
@pytest.mark.parametrize("channel", ["phone", "internal", "none", ""])
def test_property_unsupported_channel_rejected(channel: str) -> None:
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    proposal["channel"] = channel
    failure = canonicalize(proposal=proposal, situation_understanding=_situation())
    assert failure["schema_version"] == "canonicalization_failure.v1"
    assert "channel_illegal_for_action_type" in failure["reason_codes"]


# (e) Missing-info permutation: required_information must be a subset of the
# state's missing-information surface.
@pytest.mark.parametrize(
    ("required", "state", "expected"),
    [
        (["a"], ["a"], "ok"),
        (["a", "b"], ["a", "b"], "ok"),
        (["a"], ["b"], "fail"),
        (["a", "b"], ["a"], "fail"),
        (["a"], [], "fail"),
    ],
)
def test_property_missing_info_subset(required: list[str], state: list[str], expected: str) -> None:
    proposal = build_business_decision_proposal(_br(missing_information=required))
    assert proposal is not None
    outcome = canonicalize(
        proposal=proposal,
        situation_understanding=_situation(state),
        case_id="case_456",
        situation_version="18",
    )
    if expected == "ok":
        assert outcome["semantic_status"] == "FROZEN"
    else:
        assert outcome["schema_version"] == "canonicalization_failure.v1"
        assert any(
            code in outcome["reason_codes"]
            for code in ("required_information_not_in_state", "missing_information_unavailable")
        )


# (f) Policy cannot retarget: action_class mapping never turns the customer
# reply into an operator-directed class.
@pytest.mark.parametrize(
    "primary_action",
    ["prepare_reply", "create_review", "create_task", "update_case", "ignore", "hold"],
)
def test_property_policy_action_class_mapping(primary_action: str) -> None:
    proposal = build_policy_action_proposal(
        action_plan_result={"primary_action": primary_action, "canonical_decision_id": "dec_1"},
        intake_result={},
        case_link_result={},
        case_intelligence_result={},
        entity_link_result={},
        snapshot={},
    )
    expected = {
        "prepare_reply": "LIVE_REPLY",
        "create_task": "CREATE_TASK",
        "update_case": "UPDATE_CASE",
        "create_review": "REVIEW_ESCALATION",
        "ignore": "OBSERVE",
        "hold": "HOLD",
    }
    assert proposal["action_class"] == expected[primary_action]
    assert proposal["canonical_decision_id"] == "dec_1"


# (g) Confidence/authority invariant: confidence and created_at are not part of
# the semantic identity; the semantic signature is stable across them.
def test_property_confidence_and_created_at_not_in_identity() -> None:
    cad_a = _cad()
    cad_b = canonicalize(
        proposal=build_business_decision_proposal(
            _br(confidence={"action_confidence": 0.5, "business_confidence": 0.4})
        ),
        situation_understanding=_situation(),
        case_id="case_456",
        situation_version="18",
    )
    assert cad_b["semantic_status"] == "FROZEN"
    assert cad_a["semantic_hash"] == cad_b["semantic_hash"]
    assert cad_a["semantic_status"] == cad_b["semantic_status"] == "FROZEN"


# (h) CanonicalizationFailure -> NEEDS_REVIEW, never a new business action.
@pytest.mark.parametrize(
    "proposal_mutator",
    [
        lambda p: p.update({"channel": "phone"}),
        lambda p: p.update({"action_type": "not_a_type"}),
        lambda p: p.update({"goal": "wrong_goal"}),
        lambda p: p.update({"target": "universe"}),
        lambda p: p.update({"required_information": []}),
    ],
)
def test_property_failure_maps_to_needs_review(proposal_mutator: object) -> None:
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    proposal_mutator(proposal)  # type: ignore[operator]
    failure = canonicalize(proposal=proposal, situation_understanding=_situation())
    assert failure["schema_version"] == "canonicalization_failure.v1"
    assert failure["decision_state"] == "NO_CANONICAL_DECISION"
    state = canonicalization_failure_review_state(failure)
    assert state["workflow_state"] == "NEEDS_REVIEW"
    assert "action_type" not in state
    assert "recommended_next_action" not in state


# (i) decision_id stability across revision requests; semantic_hash changes
# only when the canonical payload changes.
def test_property_decision_id_and_semantic_hash_rules() -> None:
    cad = _cad()
    revision_request = build_decision_revision_request(
        decision_id=cad["decision_id"],
        revision=2,
        reason_code="NEW_CONFLICTING_EVIDENCE",
        source_layer="case_intelligence",
    )
    assert revision_request["decision_id"] == cad["decision_id"]
    assert revision_request["revision"] == 2

    same = _cad()
    assert same["semantic_hash"] == cad["semantic_hash"]
    different = _cad(missing_information=["device_model"])
    assert different["semantic_hash"] != cad["semantic_hash"]
