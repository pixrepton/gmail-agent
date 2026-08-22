"""Next best action for case intelligence."""
from __future__ import annotations
from typing import Any

from .constants import ACTION_CHANNEL, ACTION_TITLE_PL, INTELLIGENCE_ACTION_TYPES
from .missing_info import _has_actionable_current_step, _is_service_context, _is_technical_question_context
from .validators import _action_item, _bounded_float, _normalize_urgency, _string_or_default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _has_customer_clarification_reply_path(
    business_result: dict[str, Any],
    reply_result: dict[str, Any],
    *,
    primary_action_plan: str = "",
) -> bool:
    return (
        str(business_result.get("recommended_next_action") or "").strip() == "collect_data"
        and _truthy(reply_result.get("draft_enabled"))
        and (
            _truthy(business_result.get("customer_clarification_possible"))
            or str(primary_action_plan or "").strip() == "prepare_reply"
        )
    )


def _soft_review_from_action_plan(intake_result: dict[str, Any], primary_action_plan: str) -> bool:
    review_obj_present = isinstance(intake_result.get("review"), dict)
    review = intake_result.get("review") if review_obj_present else {}
    review_flags = [str(flag).strip() for flag in (review.get("flags") or []) if str(flag).strip()]
    if review_flags:
        return False
    return (review_obj_present and bool(review.get("required"))) or primary_action_plan == "create_review"


def build_next_best_action(
    *,
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    business_result: dict[str, Any],
    reply_result: dict[str, Any],
    action_plan_result: dict[str, Any],
    missing_info: dict[str, Any],
    merge_split_suggestions: dict[str, Any],
    canonical_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    business_action = str(business_result.get("recommended_next_action") or "").strip()
    primary_action_plan = str(action_plan_result.get("primary_action") or "").strip()
    case_link_decision = str(case_link_result.get("decision") or "").strip()
    business_area = str(intake_result.get("business_area") or "").strip()
    case_family = str((intake_result.get("case_assessment") or {}).get("case_family") or "").strip()
    review_obj_present = isinstance(intake_result.get("review"), dict)
    review = intake_result.get("review") if review_obj_present else {}
    hard_review_required = bool(review.get("required")) and bool(review.get("flags") or [])
    if not review_obj_present and bool(intake_result.get("review_required")):
        hard_review_required = True
    soft_review_required = _soft_review_from_action_plan(intake_result, primary_action_plan)
    current_action_ready = (
        not missing_info.get("critical")
        and _has_actionable_current_step(intake_result, business_result)
    )
    review_required = hard_review_required or (soft_review_required and not current_action_ready)
    customer_clarification_ready = _has_customer_clarification_reply_path(
        business_result,
        reply_result,
        primary_action_plan=primary_action_plan,
    )

    canonical_decision_id = str((canonical_decision or {}).get("decision_id") or "").strip()
    semantic_hash = str((canonical_decision or {}).get("semantic_hash") or "").strip()
    decision_version_id = str(
        (canonical_decision or {}).get("decision_version_id") or ""
    ).strip()
    action_type = "wait"
    if isinstance(canonical_decision, dict) and canonical_decision.get("semantic_status") == "FROZEN":
        # Case Intelligence never re-selects the business action after the CAD
        # is frozen; it projects the canonical action into its own vocabulary.
        cad_action = str(canonical_decision.get("action_type") or "").strip()
        if cad_action in INTELLIGENCE_ACTION_TYPES:
            action_type = cad_action
    elif customer_clarification_ready:
        action_type = "ask_for_missing_data"
    elif review_required:
        action_type = "review_required"
    elif business_action == "wait":
        action_type = "wait"
    elif current_action_ready and business_action in {"collect_data", "escalate_review"}:
        if _is_service_context(intake_result, business_result):
            urgency = _normalize_urgency(str(business_result.get("urgency") or intake_result.get("priority") or "normal"))
            action_type = "answer_customer" if urgency == "low" or _is_technical_question_context(intake_result, business_result) else "escalate_internal"
        else:
            action_type = "answer_customer"
    elif business_action == "collect_data":
        action_type = "ask_for_missing_data"
    elif business_action == "reply":
        action_type = "answer_customer"
    elif business_action == "call":
        action_type = "call"
    elif case_link_decision in {"weak_link", "competing_links"} and merge_split_suggestions.get("merge_candidates"):
        action_type = "review_required"
    elif business_area in {"procurement", "logistics", "supplier_commercial"}:
        action_type = "follow_up_supplier"
    elif (
        case_family == "lead_opportunity"
        and not missing_info.get("critical")
        and not missing_info.get("important")
        and not missing_info.get("helpful")
        and primary_action_plan == "prepare_reply"
    ):
        action_type = "prepare_offer"
    elif primary_action_plan in {"update_case", "create_task", "hold"}:
        action_type = "escalate_internal"

    cad_channel = str((canonical_decision or {}).get("channel") or "").strip()
    primary = {
        "action_type": action_type,
        "title_pl": ACTION_TITLE_PL.get(action_type, "Sprawdz nastepny ruch"),
        "reason_pl": _string_or_default(business_result.get("recommended_action_reason") or action_plan_result.get("why_this_action"),
            default="System wskazuje ten ruch jako najbezpieczniejszy kolejny krok."),
        "urgency_level": _normalize_urgency(str(business_result.get("urgency") or "normal")),
        "confidence": max(_bounded_float(action_plan_result.get("confidence"), default=0.0),
            _bounded_float((business_result.get("confidence") or {}).get("action_confidence"), default=0.0)),
        "whether_human_review_required": review_required or action_type in {"review_required", "merge_with_existing_case", "split_case_review"},
        "suggested_channel": cad_channel or ACTION_CHANNEL.get(action_type, "internal"),
        "optional_draft_pointer": str(reply_result.get("recommended_variant") or "") if bool(reply_result.get("draft_enabled")) and action_type in {"answer_customer", "ask_for_missing_data"} else "",
    }
    if canonical_decision_id:
        primary["canonical_decision_id"] = canonical_decision_id
    if semantic_hash:
        primary["semantic_hash"] = semantic_hash
    if decision_version_id:
        primary["decision_version_id"] = decision_version_id

    secondary_actions: list[dict[str, Any]] = []
    if merge_split_suggestions.get("merge_candidates"):
        secondary_actions.append(_action_item(action_type="merge_with_existing_case",
            reason_pl="System widzi podobienstwo do istniejacej sprawy i sugeruje reczne sprawdzenie merge.",
            urgency_level="normal",
            confidence=max([_bounded_float(item.get("confidence"), default=0.0) for item in merge_split_suggestions.get("merge_candidates") or [{}]]),
            review_required=True))
    if merge_split_suggestions.get("split_suspicions"):
        secondary_actions.append(_action_item(action_type="split_case_review",
            reason_pl="Sygnal moze mieszac dwa niezalezne watki i warto to sprawdzic przed dalszym ruchem.",
            urgency_level="normal",
            confidence=max([_bounded_float(item.get("confidence"), default=0.0) for item in merge_split_suggestions.get("split_suspicions") or [{}]]),
            review_required=True))
    if action_type not in {"wait", "move_to_case_only"} and not review_required:
        secondary_actions.append(_action_item(action_type="move_to_case_only",
            reason_pl="Jezeli operator nie chce tego na Biurku, temat mozna zostawic tylko w pamieci sprawy.",
            urgency_level="low", confidence=0.55, review_required=False))

    return {"primary_next_action": primary, "secondary_actions": secondary_actions[:2]}
