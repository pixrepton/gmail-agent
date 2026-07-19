"""Integration: Settings flags → build_case_intelligence_layer → policy → ActionProposal v2."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import pytest

from action_proposal_v2 import build_policy_gated_action_proposals_v2_bundle
from gmail_intake import build_case_intelligence_layer
from policy_action_proposal import attach_policy_evaluation_to_results, evaluate_policy_for_intake_stage
from policy_decision import build_policy_decision
from understanding_output import UNDERSTANDING_SCHEMA_VERSION

from test_llm_backend_settings import _base_settings


def _pipeline_settings(**extra: object):
    return _base_settings(
        case_intelligence_vnext_enabled=True,
        understanding_output_enabled=True,
        decision_pipeline_enabled=True,
        service_request_playbook_enabled=True,
        action_proposal_v2_enabled=True,
        decision_pipeline_dry_run_only=True,
        **extra,
    )


def _minimal_stage():
    snapshot = {
        "source_message": {
            "message_id": "int-m1",
            "thread_id": "int-t1",
            "subject": "Awaria pompy",
            "body": "Proszę o serwis, urządzenie nie grzeje.",
            "date": "2026-01-10T10:00:00Z",
        }
    }
    intake = {
        "decision": {"action": "create_case"},
        "business_area": "service",
        "priority": "high",
        "case_assessment": {"case_family": "heat_pump"},
        "thread": {"thread_id": "int-t1"},
    }
    case_link = {"decision": "unlinked", "confidence": 0.3}
    business = {"risks": [], "summary": "test"}
    reply: dict = {}
    action_plan = {"primary_action": "hold"}
    return snapshot, intake, case_link, business, reply, action_plan


def test_build_case_intelligence_layer_with_flags_produces_understanding_and_pipeline():
    snapshot, intake, case_link, business, reply, action_plan = _minimal_stage()
    settings = _pipeline_settings()
    result = build_case_intelligence_layer(
        snapshot,
        intake,
        case_link,
        business,
        reply,
        action_plan,
        {"settings": settings},
    )
    uo = result.get("understanding_output")
    assert isinstance(uo, dict)
    assert uo.get("schema_version") == UNDERSTANDING_SCHEMA_VERSION
    dp = result.get("decision_pipeline")
    assert isinstance(dp, dict)
    cand = (dp.get("outputs") or {}).get("decision_candidate")
    assert isinstance(cand, dict)
    assert str(cand.get("decision_candidate_id") or "").startswith("dc_")
    if settings.service_request_playbook_enabled:
        srp = (dp.get("outputs") or {}).get("service_request_playbook")
        assert srp is not None


def test_policy_then_action_proposals_v2_attaches_to_case_intelligence():
    snapshot, intake, case_link, business, reply, action_plan = _minimal_stage()
    settings = _pipeline_settings()
    result = build_case_intelligence_layer(
        snapshot,
        intake,
        case_link,
        business,
        reply,
        action_plan,
        {"settings": settings},
    )
    mailbox_memory_result: dict = {}
    run_state = {"run_id": "integration-test-run"}
    pr, prop = evaluate_policy_for_intake_stage(
        action_plan_result=action_plan,
        intake_result=intake,
        case_link_result=case_link,
        entity_link_result=None,
        case_intelligence_result=result,
        mailbox_memory_result=mailbox_memory_result,
        snapshot=snapshot,
        case_snapshot_hot_state=None,
        run_state=run_state,
    )
    attach_policy_evaluation_to_results(
        mailbox_memory_result=mailbox_memory_result,
        case_intelligence_result=result,
        policy_report=pr,
        policy_action_proposal=prop,
    )
    dp_local = result.get("decision_pipeline")
    assert isinstance(dp_local, dict)
    cand_local = (dp_local.get("outputs") or {}).get("decision_candidate")
    assert isinstance(cand_local, dict) and cand_local.get("decision_candidate_id")
    pd_local = build_policy_decision(
        policy_report=pr.to_dict(),
        decision_candidate_id=str(cand_local.get("decision_candidate_id") or ""),
    )
    bundle = build_policy_gated_action_proposals_v2_bundle(
        decision_candidate=cand_local,
        policy_decision=pd_local,
        planner_primary_action=str((action_plan or {}).get("primary_action") or "hold"),
        dry_run_only=bool(getattr(settings, "decision_pipeline_dry_run_only", True)),
    )
    proposals_v2 = bundle["action_proposals_v2"]
    result["policy_decision"] = pd_local
    result["action_proposals_v2"] = proposals_v2
    assert isinstance(result.get("policy_decision"), dict)
    assert isinstance(result.get("action_proposals_v2"), list)


def test_build_case_intelligence_layer_attaches_policy_and_action_proposals_v2():
    from policy_action_proposal import attach_policy_and_proposals

    snapshot, intake, case_link, business, reply, action_plan = _minimal_stage()
    settings = _pipeline_settings()
    result = build_case_intelligence_layer(
        snapshot,
        intake,
        case_link,
        business,
        reply,
        action_plan,
        {"settings": settings},
    )
    mailbox_memory_result: dict = {}
    attach_policy_and_proposals(
        action_plan_result=action_plan,
        intake_result=intake,
        case_link_result=case_link,
        entity_link_result=None,
        case_intelligence_result=result,
        mailbox_memory_result=mailbox_memory_result,
        snapshot=snapshot,
        case_snapshot_hot_state=None,
        run_state={"run_id": "integration-test-run"},
        settings=settings,
    )

    pd = result.get("policy_decision")
    proposals = result.get("action_proposals_v2")
    assert isinstance(pd, dict)
    assert str(pd.get("policy_decision_id") or "").startswith("pdec_")
    assert isinstance(proposals, list) and proposals
    assert proposals[0]["decision_candidate_id"] == pd["decision_candidate_id"]
    assert proposals[0]["policy_decision_id"] == pd["policy_decision_id"]
    assert proposals[0]["action_mode"] == "dry_run"
    assert proposals[0]["requires_operator_approval"] is True
    assert proposals[0]["execution_result_ref"] == ""
