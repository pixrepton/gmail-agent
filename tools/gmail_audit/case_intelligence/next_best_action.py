"""Next best action for case intelligence."""
from __future__ import annotations
from typing import Any

from .constants import ACTION_CHANNEL, ACTION_TITLE_PL, INTELLIGENCE_ACTION_TYPES
from .validators import _action_item, _bounded_float, _normalize_urgency, _string_or_default


def build_next_best_action(
    *,
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    business_result: dict[str, Any],
    reply_result: dict[str, Any],
    action_plan_result: dict[str, Any],
    missing_info: dict[str, Any],
    merge_split_suggestions: dict[str, Any],
) -> dict[str, Any]:
    review_required = bool((intake_result.get("review") or {}).get("required")) or str(action_plan_result.get("primary_action") or "") == "create_review"
    business_action = str(business_result.get("recommended_next_action") or "").strip()
    primary_action_plan = str(action_plan_result.get("primary_action") or "").strip()
    case_link_decision = str(case_link_result.get("decision") or "").strip()
    business_area = str(intake_result.get("business_area") or "").strip()
    case_family = str((intake_result.get("case_assessment") or {}).get("case_family") or "").strip()

    action_type = "wait"
    if review_required:
        action_type = "review_required"
    elif business_action == "wait":
        action_type = "wait"
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
    elif case_family == "lead_opportunity" and not missing_info.get("critical") and primary_action_plan == "prepare_reply":
        action_type = "prepare_offer"
    elif primary_action_plan in {"update_case", "create_task", "hold"}:
        action_type = "escalate_internal"

    primary = {
        "action_type": action_type,
        "title_pl": ACTION_TITLE_PL.get(action_type, "Sprawdz nastepny ruch"),
        "reason_pl": _string_or_default(business_result.get("recommended_action_reason") or action_plan_result.get("why_this_action"),
            default="System wskazuje ten ruch jako najbezpieczniejszy kolejny krok."),
        "urgency_level": _normalize_urgency(str(business_result.get("urgency") or "normal")),
        "confidence": max(_bounded_float(action_plan_result.get("confidence"), default=0.0),
            _bounded_float((business_result.get("confidence") or {}).get("action_confidence"), default=0.0)),
        "whether_human_review_required": review_required or action_type in {"review_required", "merge_with_existing_case", "split_case_review"},
        "suggested_channel": ACTION_CHANNEL.get(action_type, "internal"),
        "optional_draft_pointer": str(reply_result.get("recommended_variant") or "") if bool(reply_result.get("draft_enabled")) and action_type in {"answer_customer", "ask_for_missing_data"} else "",
    }

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
