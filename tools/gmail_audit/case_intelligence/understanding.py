"""Case understanding — snapshot and operator brief."""
from __future__ import annotations
from typing import Any

from case_identity import derive_canonical_case_id
from dash_preview import resolve_case_key_metadata

from .validators import (
    _bounded_float,
    _first_summary,
    _has_missing_info,
    _merge_missing_info_lists,
    _normalize_string_list,
    _string_or_default,
)


def _resolve_case_id(*, intake_result: dict[str, Any], case_link_result: dict[str, Any]) -> str:
    case_key_info = resolve_case_key_metadata(intake_result)
    projected_case_key = str(case_key_info.get("case_key") or "").strip()
    case_family = str((intake_result.get("case_assessment") or {}).get("case_family") or "unknown")
    thread_id = str((intake_result.get("thread") or {}).get("thread_id") or "").strip()
    return derive_canonical_case_id(
        case_family=case_family,
        selected_case_key=str(case_link_result.get("selected_case_key") or ""),
        projected_case_key=projected_case_key,
        thread_id=thread_id,
    )


def _latest_meaningful_change_pl(intake_result: dict[str, Any], case_link_result: dict[str, Any]) -> str:
    action = str((intake_result.get("decision") or {}).get("action") or "")
    case_link_decision = str(case_link_result.get("decision") or "")
    state_change = (intake_result.get("case_assessment") or {}).get("state_change") or {}
    if action in {"create_case", "create_case_and_task"}:
        return "Pojawil sie nowy temat operacyjny wymagajacy prowadzenia jako sprawa."
    if action in {"append_to_existing_case", "update_case_state"} and case_link_decision in {"linked", "weak_link"}:
        if bool(state_change.get("detected")):
            from_s = str(state_change.get("from_state") or "poprzedni stan")
            to_s = str(state_change.get("to_state") or "nowy stan")
            return f"Sprawa zmienila stan z \"{from_s}\" na \"{to_s}\"."
        return "Do istniejacej sprawy doszedl nowy sygnal zmieniajacy kontekst operacyjny."
    if action == "review":
        return "Temat trafil do recznej oceny zamiast bezposredniej automatyzacji."
    if action == "mark_reference":
        return "Temat zostal zachowany jako referencja bez aktywnej kartki."
    if action == "create_task":
        return "Pojawilo sie konkretne dzialanie do wykonania."
    return "System zaktualizowal rozumienie tej sprawy."


def _attention_reason_pl(
    priority: str,
    missing_info: dict[str, Any],
    risk_assessment: dict[str, Any],
    next_best_action: dict[str, Any],
    intake_result: dict[str, Any],
) -> str:
    if missing_info.get("critical"):
        return "Bez uzupelnienia krytycznych danych sprawa nie ruszy dalej."
    top_risk = (risk_assessment.get("risks") or [{}])[0]
    if top_risk and top_risk.get("reason_pl"):
        return str(top_risk["reason_pl"])
    action_title = str((next_best_action.get("primary_next_action") or {}).get("title_pl") or "").strip()
    if action_title:
        return f"Najbardziej sensowny kolejny ruch to: {action_title.lower()}."
    if priority in {"high", "critical"}:
        return "Temat ma podwyzszona wage operacyjna."
    return _string_or_default(intake_result.get("reason"), default="Temat wymaga spokojnej uwagi operatora.")


def _business_priority(*, priority: str, urgency: str, review_required: bool) -> str:
    if priority == "critical":
        return "critical"
    if priority == "high" or urgency == "high":
        return "high"
    if priority == "low" and urgency == "low" and not review_required:
        return "low"
    return "medium"


def build_case_understanding_snapshot(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    business_result: dict[str, Any],
    next_best_action: dict[str, Any],
    missing_info: dict[str, Any],
    risk_assessment: dict[str, Any],
    merge_split_suggestions: dict[str, Any],
    case_context_pack: dict[str, Any],
) -> dict[str, Any]:
    case_assessment = intake_result.get("case_assessment") or {}
    priority = _business_priority(
        priority=str(intake_result.get("priority") or "low"),
        urgency=str(business_result.get("urgency") or "normal"),
        review_required=bool(intake_result.get("review_required") or (intake_result.get("review") or {}).get("required")),
    )
    case_id = _resolve_case_id(intake_result=intake_result, case_link_result=case_link_result)
    state_detected = str(case_assessment.get("state_detected") or "none")
    if state_detected == "none":
        state_detected = str(business_result.get("customer_state_guess") or "unclear")

    blockers = []
    mailbox_open_questions = list(((case_context_pack.get("snapshot") or {}).get("open_questions") or [])[:3])
    context_quality = case_context_pack.get("context_quality") if isinstance(case_context_pack.get("context_quality"), dict) else {}
    context_conflicts = [x for x in list(case_context_pack.get("conflicting_facts") or []) if isinstance(x, dict)]
    context_gaps = [x for x in list(case_context_pack.get("completeness_gaps") or []) if isinstance(x, dict)]
    if missing_info.get("critical"):
        blockers.append("Brak krytycznych informacji do dalszego ruchu.")
    if bool(case_link_result.get("decision") in {"weak_link", "competing_links"}):
        blockers.append("Powiązanie sprawy wymaga potwierdzenia.")
    if bool(intake_result.get("review_required") or (intake_result.get("review") or {}).get("required")):
        blockers.append("Temat wymaga recznej oceny przed bezpiecznym ruchem.")

    if bool(context_quality.get("has_blocking_conflicts")):
        first = _first_summary(context_conflicts)
        blockers.append(f"CaseContextPack: blokujacy konflikt{': ' + first if first else ''}.")
    if bool(context_quality.get("has_blocking_gaps")):
        first = _first_summary(context_gaps)
        blockers.append(f"CaseContextPack: blokujacy brak{': ' + first if first else ''}.")
    elif int(context_quality.get("evidence_warning_count") or 0) > 0:
        blockers.append("CaseContextPack: czesc konfliktow/brakow ma slabe lub brakujace evidence.")

    for item in mailbox_open_questions:
        text = str(item or "").strip()
        if text:
            blockers.append(f"Mailbox memory: {text}")

    summary_operator = _string_or_default(business_result.get("business_interpretation"), default=str(intake_result.get("reason") or "Brak interpretacji biznesowej."))
    latest_meaningful_change = _latest_meaningful_change_pl(intake_result, case_link_result)
    attention_reason = _attention_reason_pl(priority, missing_info, risk_assessment, next_best_action, intake_result)

    confidence_values = [
        _bounded_float(intake_result.get("confidence", {}).get("case_link_confidence"), default=0.0),
        _bounded_float(intake_result.get("confidence", {}).get("decision_confidence"), default=0.0),
        _bounded_float((business_result.get("confidence") or {}).get("business_confidence"), default=0.0),
        _bounded_float((business_result.get("confidence") or {}).get("action_confidence"), default=0.0),
    ]
    confidence_overall = round(sum(confidence_values) / max(len(confidence_values), 1), 4)

    review_flags = list((intake_result.get("review") or {}).get("flags") or [])
    if bool((next_best_action.get("primary_next_action") or {}).get("whether_human_review_required")):
        review_flags = sorted(set(review_flags + ["intelligence_review"]))
    if bool(context_quality.get("has_blocking_conflicts")):
        review_flags = sorted(set(review_flags + ["context_pack_blocking_conflict"]))
    if bool(context_quality.get("has_blocking_gaps")):
        review_flags = sorted(set(review_flags + ["context_pack_blocking_gap"]))
    if int(context_quality.get("evidence_warning_count") or 0) > 0:
        review_flags = sorted(set(review_flags + ["context_pack_evidence_warning"]))

    return {
        "case_id": case_id,
        "case_family": str(case_assessment.get("case_family") or "unknown"),
        "business_area": str(intake_result.get("business_area") or ""),
        "current_state": state_detected,
        "state_confidence": _bounded_float((intake_result.get("confidence") or {}).get("case_link_confidence"), default=0.0),
        "business_priority": priority,
        "summary_short": _string_or_default(business_result.get("business_summary_short"), default=summary_operator[:160]),
        "summary_operator": summary_operator,
        "latest_meaningful_change": latest_meaningful_change,
        "attention_reason": attention_reason,
        "blockers": blockers,
        "risks": list(risk_assessment.get("risks") or []),
        "missing_info": _merge_missing_info_lists(missing_info),
        "recommended_next_actions": [next_best_action["primary_next_action"], *list(next_best_action.get("secondary_actions") or [])],
        "merge_candidates": list(merge_split_suggestions.get("merge_candidates") or []),
        "split_suspicions": list(merge_split_suggestions.get("split_suspicions") or []),
        "confidence_overall": confidence_overall,
        "review_required": bool((intake_result.get("review") or {}).get("required"))
            or bool(blockers and any("Powi" in blocker and "potwierdzenia" in blocker for blocker in blockers))
            or bool(context_quality.get("has_blocking_conflicts"))
            or bool(context_quality.get("has_blocking_gaps")),
        "review_flags": review_flags,
    }


def build_case_operator_brief(
    *,
    case_understanding: dict[str, Any],
    next_best_action: dict[str, Any],
    missing_info: dict[str, Any],
    risk_assessment: dict[str, Any],
) -> dict[str, Any]:
    brief_lines = [
        _string_or_default(case_understanding.get("summary_operator"), default="Brak podsumowania sprawy."),
        _string_or_default(case_understanding.get("latest_meaningful_change"), default=""),
    ]
    if missing_info.get("summary_pl") and _has_missing_info(missing_info):
        brief_lines.append(str(missing_info.get("summary_pl") or ""))
    elif risk_assessment.get("summary_pl"):
        brief_lines.append(str(risk_assessment.get("summary_pl") or ""))
    for blocker in list(case_understanding.get("blockers") or [])[:2]:
        text = str(blocker or "").strip()
        if text:
            brief_lines.append(text)
    next_action = (next_best_action.get("primary_next_action") or {})
    if next_action.get("title_pl"):
        brief_lines.append(f"System sugeruje: {next_action['title_pl'].strip()}.")
    brief = " ".join(line.strip() for line in brief_lines if str(line).strip())
    return {"brief_pl": brief}
