"""Unit/contract tests for the CanonicalActionDecision boundary (P0)."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from canonical_action_decision import (
    CANONICAL_ACTION_DECISION_SCHEMA_VERSION,
    build_business_decision_proposal,
    build_decision_revision_request,
    canonical_decision_code,
    canonicalization_failure_review_state,
    canonicalize,
    semantic_hash_of,
    semantic_signature_matches,
)
from action_planner import plan_actions as build_action_plan_result
from case_intelligence.next_best_action import build_next_best_action
from action_proposal_v2 import build_action_proposals_v2
from agent_runtime.policy_action_spine import evaluate_semantic_policy_plan_consistency
from agent_runtime.tool_result import ToolCallPlan
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


def test_build_proposal_from_collect_data() -> None:
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    assert proposal["schema_version"] == "business_decision_proposal.v1"
    assert proposal["goal"] == "obtain_missing_service_information"
    assert proposal["action_type"] == "ask_for_missing_data"
    assert proposal["target"] == "customer"
    assert proposal["channel"] == "mail"
    assert proposal["required_information"] == ["what_exactly_is_not_working", "observed_symptoms"]
    assert proposal["confidence"] == 0.84
    assert proposal["risk_class"] == "low"
    assert proposal["proposal_id"].startswith("bprop_")


def test_build_proposal_non_collect_data_returns_none() -> None:
    for action in ("escalate_review", "reply", "call", "wait"):
        assert build_business_decision_proposal(_br(recommended_next_action=action)) is None
    assert build_business_decision_proposal(None) is None


def test_build_proposal_high_urgency_maps_risk_class() -> None:
    proposal = build_business_decision_proposal(_br(urgency="high"))
    assert proposal is not None
    assert proposal["risk_class"] == "high"


def test_canonicalize_success_frozen_decision() -> None:
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    cad = canonicalize(
        proposal=proposal,
        situation_understanding=_situation(),
        case_id="case_456",
        situation_version="18",
    )
    assert cad["schema_version"] == CANONICAL_ACTION_DECISION_SCHEMA_VERSION
    assert cad["semantic_status"] == "FROZEN"
    assert cad["decision_id"].startswith("dec_")
    assert cad["revision"] == 1
    assert cad["case_id"] == "case_456"
    assert cad["situation_version"] == "18"
    assert cad["action_type"] == "ask_for_missing_data"
    assert cad["target"] == "customer"
    assert cad["channel"] == "mail"
    assert cad["semantic_hash"].startswith("sh_")


def test_semantic_hash_stable_and_sensitive() -> None:
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    base = {
        "proposal": proposal,
        "situation_understanding": _situation(),
        "case_id": "case_456",
        "situation_version": "18",
    }
    cad_a = canonicalize(**base)  # type: ignore[arg-type]
    cad_b = canonicalize(**base)  # type: ignore[arg-type]
    assert cad_a["semantic_hash"] == cad_b["semantic_hash"]
    assert semantic_signature_matches(cad_a, cad_b) is True

    # Different required_information changes the semantic signature.
    other_proposal = build_business_decision_proposal(
        _br(missing_information=["device_model"])
    )
    assert other_proposal is not None
    cad_c = canonicalize(
        proposal=other_proposal,
        situation_understanding=_situation(["device_model"]),
        case_id="case_456",
        situation_version="18",
    )
    assert cad_c["semantic_hash"] != cad_a["semantic_hash"]

    # Manual hash: confidence/created_at are not part of the identity.
    manual = semantic_hash_of(
        case_id="case_456",
        situation_version="18",
        goal="obtain_missing_service_information",
        action_type="ask_for_missing_data",
        target="customer",
        channel="mail",
        required_information=["observed_symptoms", "what_exactly_is_not_working"],
    )
    assert manual == cad_a["semantic_hash"]


def test_canonicalize_rejects_illegal_channel() -> None:
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    proposal["channel"] = "phone"
    failure = canonicalize(proposal=proposal, situation_understanding=_situation())
    assert failure["schema_version"] == "canonicalization_failure.v1"
    assert "channel_illegal_for_action_type" in failure["reason_codes"]
    assert failure["decision_state"] == "NO_CANONICAL_DECISION"


def test_canonicalize_requires_state_support() -> None:
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    # State does not list one of the required items.
    failure = canonicalize(
        proposal=proposal,
        situation_understanding=_situation(["observed_symptoms"]),
    )
    assert "required_information_not_in_state" in failure["reason_codes"]

    # No state missing-information surface at all -> cannot prove support.
    failure_empty = canonicalize(proposal=proposal, situation_understanding={})
    assert "missing_information_unavailable" in failure_empty["reason_codes"]


def test_canonicalize_rejects_conflicted_fact_as_certainty() -> None:
    proposal = build_business_decision_proposal(
        _br(missing_information=["installation address"])
    )
    assert proposal is not None
    pack = {
        "conflicting_facts": [
            {
                "fact_key": "installation address",
                "trust_state": "conflicted",
                "decision_usable": False,
            }
        ]
    }
    failure = canonicalize(
        proposal=proposal,
        situation_understanding=_situation(["installation address"]),
        case_context_pack=pack,
    )
    assert "conflicted_fact_as_certainty" in failure["reason_codes"]


def test_failure_maps_to_needs_review_state() -> None:
    failure = canonicalize(proposal=None)
    state = canonicalization_failure_review_state(failure)
    assert state["workflow_state"] == "NEEDS_REVIEW"
    assert state["operational_status"] == "pending_operator_review"
    assert state["decision_state"] == "NO_CANONICAL_DECISION"
    # NEEDS_REVIEW is a workflow state, never an action_type.
    assert "action_type" not in state


def test_decision_revision_request_contract() -> None:
    request = build_decision_revision_request(
        decision_id="dec_123",
        revision=1,
        reason_code="NEW_CONFLICTING_EVIDENCE",
        failed_precondition="customer_identity_confirmed",
        source_layer="case_intelligence",
    )
    assert request["schema_version"] == "decision_revision_request.v1"
    assert request["decision_id"] == "dec_123"
    assert request["revision"] == 1
    assert request["reason_code"] == "NEW_CONFLICTING_EVIDENCE"
    assert request["failed_precondition"] == "customer_identity_confirmed"
    assert request["requested_at"]

    unknown = build_decision_revision_request(decision_id="dec_1", reason_code="BOGUS")
    assert unknown["reason_code"] == "NEW_CONFLICTING_EVIDENCE"


def test_canonical_decision_code() -> None:
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    cad = canonicalize(proposal=proposal, situation_understanding=_situation())
    assert canonical_decision_code(cad) == "ask_for_missing_data"
    assert canonical_decision_code({}) == ""


def _cad() -> dict[str, object]:
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    cad = canonicalize(proposal=proposal, situation_understanding=_situation(), case_id="case_456")
    assert cad["semantic_status"] == "FROZEN"
    return cad


def test_action_plan_derives_from_canonical_decision() -> None:
    cad = _cad()
    plan = build_action_plan_result(
        {"decision": {"action": "create_case"}, "review_required": True},
        {"decision": "no_link"},
        _br(),
        {"draft_enabled": True, "drafts": []},
        None,
        canonical_decision=cad,
    )
    # CAD-derived: customer/mail clarification -> prepare_reply, even when the
    # intake review flag is set (review never changes the addressee).
    assert plan["primary_action"] == "prepare_reply"
    assert plan["canonical_decision_id"] == cad["decision_id"]
    assert plan["execution_step"] == "prepare_reply"
    assert plan["semantic_hash"] == cad["semantic_hash"]


def test_action_plan_unknown_frozen_semantic_holds() -> None:
    cad = dict(_cad())
    cad["action_type"] = "unknown_action"
    plan = build_action_plan_result(
        {"decision": {"action": "create_case"}},
        {"decision": "no_link"},
        _br(),
        {"draft_enabled": True, "drafts": []},
        None,
        canonical_decision=cad,
    )
    assert plan["primary_action"] == "hold"


def test_next_best_action_projects_canonical_decision() -> None:
    cad = _cad()
    intake = {
        "business_area": "service",
        "review": {"required": True, "flags": ["ambiguous_signal"]},
        "review_required": True,
    }
    nba = build_next_best_action(
        intake_result=intake,
        case_link_result={"decision": "no_link"},
        business_result=_br(),
        reply_result={"draft_enabled": True},
        action_plan_result={"primary_action": "prepare_reply"},
        missing_info={"critical": [], "important": ["observed_symptoms"], "helpful": []},
        merge_split_suggestions={},
        canonical_decision=cad,
    )
    primary = nba["primary_next_action"]
    # Case Intelligence projects the frozen action into its own vocabulary and
    # keeps the customer/mail addressee even though review_required is true.
    assert primary["action_type"] == "ask_for_missing_data"
    assert primary["suggested_channel"] == "mail"
    assert primary["canonical_decision_id"] == cad["decision_id"]
    assert primary["semantic_hash"] == cad["semantic_hash"]
    assert primary["whether_human_review_required"] is True


def test_apv2_reads_nba_dict_action_type() -> None:
    """A17 fix: a dict NBA must not be stringified; the code branch works."""
    candidate = {
        "decision_candidate_id": "dc_1",
        "case_id": "case_456",
        "source_signal_id": "msg_1",
        "next_best_action": {"action_type": "ask_for_missing_data", "suggested_channel": "mail"},
        "evidence_refs": [],
    }
    policy_decision = {
        "policy_decision_id": "pdec_1",
        "decision_candidate_id": "dc_1",
        "status": "insufficient_context",
        "allowed_actions": ["request_missing_info", "mark_attention_required"],
        "risk_class": "low",
    }
    proposals = build_action_proposals_v2(
        decision_candidate=candidate,
        policy_decision=policy_decision,
        primary_action_type="prepare_reply",
    )
    assert proposals
    assert proposals[0]["action_type"] == "request_missing_info"


def test_policy_action_proposal_carries_canonical_decision_id() -> None:
    proposal = build_policy_action_proposal(
        action_plan_result={"primary_action": "prepare_reply", "canonical_decision_id": "dec_123"},
        intake_result={},
        case_link_result={},
        case_intelligence_result={},
        entity_link_result={},
        snapshot={},
    )
    assert proposal["canonical_decision_id"] == "dec_123"


def test_decision_candidate_normalizes_canonical_code() -> None:
    from decision_candidate import build_decision_candidate

    candidate = build_decision_candidate(
        case_id="case_456",
        source_signal_id="msg_1",
        next_best_action_code="ask_for_missing_data",
    )
    assert candidate["next_best_action"] == "ask_for_missing_data"


def test_reference_monitor_emits_canonical_semantic_drift() -> None:
    envelope = PolicyActionEnvelopeV1(
        canonical_decision_id="dec_123",
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
        forbidden_tools=["request_operator_clarification"],
        freshness="current",
    )
    plan = ToolCallPlan(
        tool_name="request_operator_clarification",
        arguments={},
        policy_decision_id="pdec_1",
        action_proposal_id="apv2_1",
    )
    consistency = evaluate_semantic_policy_plan_consistency(envelope, plan)
    assert consistency.status == "conflicting"
    assert "semantic_tool_forbidden_for_action_intent" in consistency.reason_codes
    assert "canonical_semantic_drift" in consistency.reason_codes
