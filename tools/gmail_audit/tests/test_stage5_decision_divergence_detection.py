"""Stage 5: detection-only comparison of Brain 1 action views and Brain 2 planning."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from action_planner import select_primary_action
from agent_runtime.agent_reconcile import build_decision_comparison_inputs
from agent_runtime.decision_divergence import evaluate_decision_divergence
from agent_runtime.graph import (
    _apply_tool_result,
    _ground_current_signal,
    _observe_policy_plan,
)
from agent_runtime.openai_agent_client import _compact_view
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from case_intelligence.next_best_action import build_next_best_action
from llm_contracts.engagement_snapshot_v2 import DecisionDivergenceObservationV1


MESSAGE_ID = "msg_stage5_1"


def _decision_inputs(
    *,
    source_signal_id: str = MESSAGE_ID,
    business_action: str = "reply",
    legacy_action: str = "hold",
    nba_action: str = "answer_customer",
    draft_enabled: bool = False,
    case_family: str = "lead_opportunity",
) -> dict:
    return {
        "schema_version": "decision_comparison_inputs.v1",
        "source_signal_id": source_signal_id,
        "business_recommended_action": business_action,
        "action_planner_primary_action": legacy_action,
        "next_best_action_type": nba_action,
        "reply_draft_enabled": draft_enabled,
        "case_family": case_family,
    }


def _case_intelligence(*, source_signal_id: str = MESSAGE_ID) -> dict:
    inputs = _decision_inputs(source_signal_id=source_signal_id)
    return {
        "understanding_output": {"source_signal_id": source_signal_id},
        "case_understanding": {"case_family": inputs["case_family"]},
        "next_best_action": {
            "primary_next_action": {"action_type": inputs["next_best_action_type"]}
        },
        "execution_metadata": {
            "input_business_next_action": inputs["business_recommended_action"],
            "input_primary_action": inputs["action_planner_primary_action"],
            "input_reply_draft_enabled": inputs["reply_draft_enabled"],
        },
    }


def test_real_reply_without_draft_contract_is_observed_as_divergence() -> None:
    intake: dict = {}
    business = {"recommended_next_action": "reply"}
    reply = {"draft_enabled": False}
    legacy = select_primary_action(intake, {}, business, reply)
    nba = build_next_best_action(
        intake_result=intake,
        case_link_result={},
        business_result=business,
        reply_result=reply,
        action_plan_result={"primary_action": legacy},
        missing_info={},
        merge_split_suggestions={},
    )["primary_next_action"]["action_type"]

    assert legacy == "hold"
    assert nba == "answer_customer"
    observation = evaluate_decision_divergence(
        {
            **_decision_inputs(),
            "action_planner_primary_action": legacy,
            "next_best_action_type": nba,
        },
        case_kind="zapytanie_klienta",
        plan=ToolCallPlan(
            tool_name="request_operator_clarification",
            arguments={"ask_pl": "fixture"},
        ),
    )

    assert observation.status == "divergence_detected"
    assert observation.action_tree_status == "divergence_detected"
    assert (
        "reply_without_draft_legacy_hold_nba_answer_customer"
        in observation.reason_codes
    )


def test_comparison_inputs_require_current_signal_correlation() -> None:
    current = build_decision_comparison_inputs(
        _case_intelligence(),
        message_id=MESSAGE_ID,
    )
    assert current == _decision_inputs()

    assert (
        build_decision_comparison_inputs(
            _case_intelligence(source_signal_id="foreign"),
            message_id=MESSAGE_ID,
        )
        is None
    )


def test_case_typing_and_tool_relation_are_reported_without_invented_mapping() -> None:
    observation = evaluate_decision_divergence(
        _decision_inputs(
            business_action="collect_data",
            legacy_action="hold",
            nba_action="ask_for_missing_data",
        ),
        case_kind="wycena_oferta",
        plan=ToolCallPlan(
            tool_name="request_operator_clarification",
            arguments={},
        ),
    )

    assert observation.status == "not_evaluable"
    assert observation.case_typing_status == "different_unmapped_literals"
    assert observation.tool_relation_status == "not_evaluable"
    assert "no_formal_action_vocabulary_mapping" in observation.reason_codes
    assert "no_formal_case_family_case_kind_mapping" in observation.reason_codes
    assert "no_formal_action_to_tool_mapping" in observation.reason_codes


def test_missing_inputs_are_explicit_and_never_fabricated() -> None:
    observation = evaluate_decision_divergence(
        None,
        case_kind="niezaklasyfikowane",
        plan=ToolCallPlan(tool_name="search_gmail_thread", arguments={}),
    )

    assert observation.status == "missing_inputs"
    assert observation.action_tree_status == "missing_inputs"
    assert observation.business_recommended_action == ""
    assert observation.reason_codes == ["decision_comparison_inputs_absent"]


def test_observation_does_not_rewrite_tool_or_arguments() -> None:
    snapshot = build_initial_snapshot(
        case_id="case_stage5",
        engagement_id="eng_stage5",
        signal_id="sig_stage5",
        trace_id="trace_stage5",
    ).model_copy(update={"case_kind": "zapytanie_klienta"})
    raw = ToolCallPlan(
        tool_name="request_operator_clarification",
        arguments={"ask_pl": "fixture"},
    )

    correlated, observed = _observe_policy_plan(
        snapshot,
        raw,
        decision_inputs=_decision_inputs(),
    )

    assert correlated.tool_name == raw.tool_name
    assert correlated.arguments == raw.arguments
    assert observed.decision_divergence_observation is not None
    assert observed.decision_divergence_observation.status == "divergence_detected"


def test_new_signal_clears_previous_observation() -> None:
    snapshot = build_initial_snapshot(
        case_id="case_stage5",
        engagement_id="eng_stage5",
        signal_id="sig_stage5",
        trace_id="trace_stage5",
    ).model_copy(
        update={
            "decision_divergence_observation": evaluate_decision_divergence(
                _decision_inputs(),
                case_kind="zapytanie_klienta",
                plan=ToolCallPlan(
                    tool_name="request_operator_clarification",
                    arguments={},
                ),
            )
        }
    )

    grounded = _ground_current_signal(snapshot, {"subject": "new signal"})
    assert grounded.decision_divergence_observation is None


def test_tool_delta_cannot_overwrite_runtime_observation() -> None:
    original = evaluate_decision_divergence(
        _decision_inputs(),
        case_kind="zapytanie_klienta",
        plan=ToolCallPlan(
            tool_name="request_operator_clarification",
            arguments={},
        ),
    )
    snapshot = build_initial_snapshot(
        case_id="case_stage5",
        engagement_id="eng_stage5",
        signal_id="sig_stage5",
        trace_id="trace_stage5",
    ).model_copy(update={"decision_divergence_observation": original})
    forged = DecisionDivergenceObservationV1(
        status="not_evaluable",
        action_tree_status="not_evaluable",
        case_typing_status="same_literal",
        tool_relation_status="not_evaluable",
        reason_codes=["forged_by_tool"],
    )

    updated = _apply_tool_result(
        snapshot,
        ToolCallPlan(tool_name="search_gmail_thread", arguments={}),
        ToolResult(
            status="ok",
            turn_summary_pl="fixture",
            snapshot_delta={
                "decision_divergence_observation": forged.model_dump(mode="python")
            },
        ),
    )

    assert updated.decision_divergence_observation == original


def test_observation_is_not_a_planner_input() -> None:
    snapshot = build_initial_snapshot(
        case_id="case_stage5",
        engagement_id="eng_stage5",
        signal_id="sig_stage5",
        trace_id="trace_stage5",
    ).model_copy(
        update={
            "decision_divergence_observation": evaluate_decision_divergence(
                _decision_inputs(),
                case_kind="zapytanie_klienta",
                plan=ToolCallPlan(
                    tool_name="request_operator_clarification",
                    arguments={},
                ),
            )
        }
    )

    view = _compact_view(snapshot)
    assert "decision_divergence_observation" not in view


def test_legacy_snapshot_without_observation_remains_valid() -> None:
    snapshot = build_initial_snapshot(
        case_id="case_stage5",
        engagement_id="eng_stage5",
        signal_id="sig_stage5",
        trace_id="trace_stage5",
    )
    payload = snapshot.model_dump(mode="python")
    payload.pop("decision_divergence_observation", None)

    rebuilt = type(snapshot).model_validate(payload)
    assert rebuilt.decision_divergence_observation is None
