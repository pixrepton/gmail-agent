"""Desk composition and case guidance merging for case intelligence."""
from __future__ import annotations
import copy
from typing import Any

from evidence_ref import normalize_case_guidance_evidence_refs, strip_forbidden_evidence_like_rows
from operator_visibility_policy import DESK_SUPPRESSION_REASON_NON_BUSINESS, should_suppress_desk_and_tasks
from v2_semantics import normalize_lifecycle_intent

from .constants import INTELLIGENCE_DAY_BUCKETS, INTELLIGENCE_PRESENCE_MODES, INTELLIGENCE_SURFACE_ZONES
from .validators import (
    _bounded_float, _guidance_clip, _guidance_dict_list, _guidance_string_list,
    _has_missing_info, _has_real_missing_info, _normalize_case_guidance,
    _string_or_default, _visibility_score,
)


def merge_case_guidance_into_intelligence(
    base_intelligence: dict[str, Any],
    case_guidance: dict[str, Any],
) -> dict[str, Any]:
    merged = copy.deepcopy(base_intelligence)
    cg = _normalize_case_guidance(case_guidance, source_mode=str(case_guidance.get("source_mode") or "fallback"))
    merged["case_guidance"] = cg

    missing_info = merged.get("missing_info") or {}
    risk_assessment = merged.get("risk_assessment") or {}
    nba = merged.get("next_best_action") or {}
    primary = nba.get("primary_next_action") or {}

    brief_lines: list[str] = [cg["reason_summary_pl"]]
    if cg.get("blocker_summary_pl"):
        brief_lines.append(str(cg["blocker_summary_pl"]))
    if missing_info.get("summary_pl") and _has_real_missing_info(missing_info):
        brief_lines.append(str(missing_info.get("summary_pl") or ""))
    elif risk_assessment.get("summary_pl"):
        brief_lines.append(str(risk_assessment.get("summary_pl") or ""))
    if primary.get("title_pl"):
        brief_lines.append(f"System sugeruje: {str(primary['title_pl']).strip()}.")

    merged.setdefault("operator_brief", {})
    merged["operator_brief"]["brief_pl"] = " ".join(line.strip() for line in brief_lines if str(line).strip())

    desk = merged.get("desk_composition") or {}
    if cg.get("reason_summary_pl"):
        desk["body_reason_pl"] = cg["reason_summary_pl"]
    hint = str(cg.get("next_step_hint_pl") or "").strip()
    sensible = (len(hint) >= 6
        and hint.lower() != "sprawdz sprawe recznie."
        and (float(cg.get("confidence") or 0.0) >= 0.18 or cg.get("source_mode") == "llm_reasoned"))
    if sensible:
        desk["assistant_suggestion_pl"] = hint
    merged["desk_composition"] = desk
    return merged


def build_desk_composition(
    *,
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    case_understanding: dict[str, Any],
    next_best_action: dict[str, Any],
    missing_info: dict[str, Any],
    risk_assessment: dict[str, Any],
    merge_split_suggestions: dict[str, Any],
    feedback_learning_memory: dict[str, Any],
    preclassification_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    primary_action = next_best_action.get("primary_next_action") or {}
    review_required = bool(primary_action.get("whether_human_review_required")) or bool(case_understanding.get("review_required"))
    ignore_like = str((intake_result.get("decision") or {}).get("action") or "") in {"ignore", "mark_reference"}

    if should_suppress_desk_and_tasks(business_result=business_result, preclassification_result=preclassification_result):
        title_pl = _string_or_default((intake_result.get("message") or {}).get("subject"), default="Kartka AI")
        return {
            "should_surface": False, "presence_mode": "silent", "surface_zone": "case_only",
            "day_bucket": "do_obserwacji", "title_pl": title_pl,
            "body_short_pl": _string_or_default(case_understanding.get("summary_short"), default=case_understanding.get("summary_operator")),
            "body_reason_pl": _string_or_default(case_understanding.get("attention_reason"), default="Brak uzasadnienia."),
            "assistant_suggestion_pl": _string_or_default(primary_action.get("title_pl"), default="Sprawdz temat recznie."),
            "visibility_score": 0.0, "lifecycle_intent": "move_to_case_only", "review_required": review_required,
            "desk_tasks_suppressed": True, "desk_suppression_reason": DESK_SUPPRESSION_REASON_NON_BUSINESS,
            "trace_summary": _string_or_default(
                (merge_split_suggestions.get("summary_pl") if merge_split_suggestions.get("summary_pl") != "Brak silnych przeslanek do merge lub split." else "")
                or case_understanding.get("latest_meaningful_change"), default="Brak dodatkowego sladu."),
        }

    visibility_score = _visibility_score(
        intake_result=intake_result, business_result=business_result,
        missing_info=missing_info, risk_assessment=risk_assessment,
        review_required=review_required, feedback_learning_memory=feedback_learning_memory,
    )

    should_surface = not ignore_like and visibility_score >= 0.22
    surface_zone = "desk"
    if not should_surface:
        surface_zone = "silent" if ignore_like else "case_only"
    elif "prefer_case_only_for_repeated_updates" in (feedback_learning_memory.get("suppression_hints") or []):
        surface_zone = "case_only"
    elif str(primary_action.get("action_type") or "") in {"wait", "move_to_case_only"} or visibility_score < 0.5:
        surface_zone = "day"

    if surface_zone in {"silent", "case_only"}:
        presence_mode = "silent"
    elif visibility_score >= 0.9:
        presence_mode = "alarm"
    elif visibility_score >= 0.74:
        presence_mode = "strong"
    elif visibility_score >= 0.54:
        presence_mode = "advisory"
    elif visibility_score >= 0.34:
        presence_mode = "standard"
    else:
        presence_mode = "subtle"

    if surface_zone == "case_only":
        presence_mode = "silent"

    if surface_zone == "desk":
        day_bucket = "teraz" if presence_mode in {"strong", "alarm"} else "dzisiaj"
    elif surface_zone == "day":
        day_bucket = "dzisiaj" if presence_mode in {"standard", "advisory"} else "w_najblizszym_czasie"
    else:
        day_bucket = "do_obserwacji"

    lifecycle_intent = "create"
    if not should_surface:
        lifecycle_intent = "suppress" if ignore_like else "move_to_case_only"
    elif str((intake_result.get("decision") or {}).get("action") or "") in {"append_to_existing_case", "update_case_state"}:
        lifecycle_intent = "update"
    elif review_required and presence_mode in {"strong", "alarm"}:
        lifecycle_intent = "escalate_presence"

    lifecycle_intent = normalize_lifecycle_intent(lifecycle_intent, surface_zone)

    return {
        "should_surface": should_surface, "presence_mode": presence_mode, "surface_zone": surface_zone,
        "day_bucket": day_bucket,
        "title_pl": _string_or_default((intake_result.get("message") or {}).get("subject"), default="Kartka AI"),
        "body_short_pl": _string_or_default(case_understanding.get("summary_short"), default=case_understanding.get("summary_operator")),
        "body_reason_pl": _string_or_default(case_understanding.get("attention_reason"), default="Brak uzasadnienia."),
        "assistant_suggestion_pl": _string_or_default(primary_action.get("title_pl"), default="Sprawdz temat recznie."),
        "visibility_score": round(visibility_score, 4), "lifecycle_intent": lifecycle_intent,
        "review_required": review_required,
        "trace_summary": _string_or_default(
            (merge_split_suggestions.get("summary_pl") if merge_split_suggestions.get("summary_pl") != "Brak silnych przeslanek do merge lub split." else "")
            or case_understanding.get("latest_meaningful_change"), default="Brak dodatkowego sladu."),
    }
