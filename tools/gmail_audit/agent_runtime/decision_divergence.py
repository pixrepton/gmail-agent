"""Detection-only comparison of existing action and case-typing surfaces."""

from __future__ import annotations

from typing import Any

from agent_runtime.tool_result import ToolCallPlan
from llm_contracts.engagement_snapshot_v2 import DecisionDivergenceObservationV1


def build_decision_comparison_inputs(
    case_intelligence_result: dict[str, Any] | None,
    *,
    message_id: str,
) -> dict[str, Any] | None:
    """Project current-signal Brain 1 values without interpreting their vocabularies."""
    intelligence = (
        case_intelligence_result
        if isinstance(case_intelligence_result, dict)
        else {}
    )
    understanding = intelligence.get("understanding_output")
    if not isinstance(understanding, dict):
        return None
    source_signal_id = str(understanding.get("source_signal_id") or "").strip()
    current_message_id = str(message_id or "").strip()
    if (
        not source_signal_id
        or not current_message_id
        or source_signal_id != current_message_id
    ):
        return None

    metadata = intelligence.get("execution_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    case_understanding = intelligence.get("case_understanding")
    case_understanding = (
        case_understanding if isinstance(case_understanding, dict) else {}
    )
    next_best_action = intelligence.get("next_best_action")
    next_best_action = (
        next_best_action if isinstance(next_best_action, dict) else {}
    )
    primary_next_action = next_best_action.get("primary_next_action")
    primary_next_action = (
        primary_next_action if isinstance(primary_next_action, dict) else {}
    )
    draft_enabled = (
        bool(metadata.get("input_reply_draft_enabled"))
        if "input_reply_draft_enabled" in metadata
        else None
    )

    return {
        "schema_version": "decision_comparison_inputs.v1",
        "source_signal_id": source_signal_id,
        "business_recommended_action": str(
            metadata.get("input_business_next_action") or ""
        ).strip(),
        "action_planner_primary_action": str(
            metadata.get("input_primary_action") or ""
        ).strip(),
        "next_best_action_type": str(
            primary_next_action.get("action_type") or ""
        ).strip(),
        "reply_draft_enabled": draft_enabled,
        "case_family": str(case_understanding.get("case_family") or "").strip(),
    }


def evaluate_decision_divergence(
    decision_inputs: dict[str, Any] | None,
    *,
    case_kind: str,
    plan: ToolCallPlan,
) -> DecisionDivergenceObservationV1:
    """Observe only relationships the current code proves; never map names by guess."""
    if not isinstance(decision_inputs, dict) or not decision_inputs:
        return DecisionDivergenceObservationV1(
            status="missing_inputs",
            action_tree_status="missing_inputs",
            case_typing_status="missing_inputs",
            tool_relation_status="missing_inputs",
            reason_codes=["decision_comparison_inputs_absent"],
            case_kind=_bounded(case_kind),
            tool_name=_bounded(plan.tool_name),
        )

    business_action = _bounded(
        decision_inputs.get("business_recommended_action")
    )
    legacy_action = _bounded(
        decision_inputs.get("action_planner_primary_action")
    )
    nba_action = _bounded(decision_inputs.get("next_best_action_type"))
    case_family = _bounded(decision_inputs.get("case_family"))
    normalized_case_kind = _bounded(case_kind)
    tool_name = _bounded(plan.tool_name)
    draft_enabled = (
        bool(decision_inputs.get("reply_draft_enabled"))
        if decision_inputs.get("reply_draft_enabled") is not None
        else None
    )
    reasons: list[str] = []

    action_values = (business_action, legacy_action, nba_action)
    if not all(action_values):
        action_tree_status = "missing_inputs"
        reasons.append("action_tree_input_missing")
    elif (
        business_action == "reply"
        and legacy_action == "hold"
        and nba_action == "answer_customer"
        and draft_enabled is False
    ):
        action_tree_status = "divergence_detected"
        reasons.append(
            "reply_without_draft_legacy_hold_nba_answer_customer"
        )
    elif len(set(action_values)) == 1:
        action_tree_status = "same_literal"
    else:
        action_tree_status = "not_evaluable"
        reasons.append("no_formal_action_vocabulary_mapping")

    if not case_family or not normalized_case_kind:
        case_typing_status = "missing_inputs"
        reasons.append("case_typing_input_missing")
    elif case_family == normalized_case_kind:
        case_typing_status = "same_literal"
    else:
        case_typing_status = "different_unmapped_literals"
        reasons.append("no_formal_case_family_case_kind_mapping")

    if not tool_name:
        tool_relation_status = "missing_inputs"
        reasons.append("tool_plan_input_missing")
    else:
        tool_relation_status = "not_evaluable"
        reasons.append("no_formal_action_to_tool_mapping")

    if action_tree_status == "divergence_detected":
        status = "divergence_detected"
    elif (
        action_tree_status == "missing_inputs"
        or case_typing_status == "missing_inputs"
        or tool_relation_status == "missing_inputs"
    ):
        status = "missing_inputs"
    else:
        status = "not_evaluable"

    return DecisionDivergenceObservationV1(
        status=status,
        action_tree_status=action_tree_status,
        case_typing_status=case_typing_status,
        tool_relation_status=tool_relation_status,
        reason_codes=_dedupe(reasons),
        source_signal_id=_bounded(decision_inputs.get("source_signal_id")),
        business_recommended_action=business_action,
        action_planner_primary_action=legacy_action,
        next_best_action_type=nba_action,
        reply_draft_enabled=draft_enabled,
        case_family=case_family,
        case_kind=normalized_case_kind,
        tool_name=tool_name,
    )


def _bounded(value: Any, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))[:12]


__all__ = [
    "build_decision_comparison_inputs",
    "evaluate_decision_divergence",
]
