"""Validation and normalization contract for case intelligence.

Provides all _normalize_* functions and pure helper utilities used by
every case_intelligence sub-module.
"""
from __future__ import annotations

import hashlib
from typing import Any

from evidence_ref import normalize_case_guidance_evidence_refs, strip_forbidden_evidence_like_rows
from v2_semantics import normalize_lifecycle_intent

from .constants import (
    ACTION_CHANNEL,
    ACTION_TITLE_PL,
    CASE_GUIDANCE_BUSINESS_READINESS,
    CASE_GUIDANCE_MOMENTUM,
    CASE_GUIDANCE_OPERATIONAL_STATUS,
    CASE_GUIDANCE_OPERATOR_ATTENTION,
    CASE_GUIDANCE_SOURCE_MODES,
    CASE_GUIDANCE_WAITING_FOR,
    INTELLIGENCE_ACTION_TYPES,
    INTELLIGENCE_DAY_BUCKETS,
    INTELLIGENCE_PRESENCE_MODES,
    INTELLIGENCE_SURFACE_ZONES,
    RISK_SEVERITIES,
    RISK_TYPES,
)

_GUIDANCE_MAX_REASON = 1200
_GUIDANCE_MAX_BLOCKER = 600
_GUIDANCE_MAX_HINT = 600
_GUIDANCE_MAX_STAGNATION = 400

__all__ = [
    "_guidance_clip", "_has_real_missing_info", "_guidance_dict_list", "_guidance_string_list",
    "_first_summary", "_string_or_default", "_bounded_float", "_coerce_int", "_stable_id",
    "_presence_rank", "_normalize_channel", "_normalize_urgency", "_normalize_string_list",
    "_has_missing_info", "_merge_missing_info_lists", "_missing_info_label_pl",
    "_business_priority", "_latest_meaningful_change_pl", "_attention_reason_pl",
    "_visibility_score", "_action_item", "_normalize_action_item", "_normalize_risk_item",
    "_normalize_missing_info_item", "_normalize_suggestion_item", "_normalize_case_guidance",
    "_normalize_case_understanding", "_normalize_next_best_action", "_normalize_missing_info",
    "_normalize_risk_assessment", "_normalize_merge_split_suggestions",
    "_normalize_desk_composition", "_normalize_lifecycle_revision",
    "_normalize_feedback_learning_memory", "validate_case_intelligence_result",
]


# ── Guidance helpers ────────────────────────────────────────────────


def _guidance_clip(text: str, max_len: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "\u2026"


def _has_real_missing_info(missing_info: dict[str, Any]) -> bool:
    for key in ("critical", "important", "helpful"):
        if missing_info.get(key):
            return True
    return False


def _guidance_dict_list(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [dict(item) for item in values if isinstance(item, dict)]


def _guidance_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item or "").strip()]


# ── Pure helpers ────────────────────────────────────────────────────


def _first_summary(items: list[dict[str, Any]]) -> str:
    for item in items[:3]:
        text = str(item.get("summary") or item.get("summary_pl") or "").strip()
        if text:
            return text[:180]
    return ""


def _string_or_default(value: Any, *, default: str) -> str:
    text = str(value or "").strip()
    return text or default


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, round(number, 4)))


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _stable_id(prefix: str, *parts: str) -> str:
    seed = "::".join(str(part or "").strip() for part in parts if str(part or "").strip())
    if not seed:
        seed = prefix
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _presence_rank(value: str) -> int:
    return {mode: index for index, mode in enumerate(INTELLIGENCE_PRESENCE_MODES)}.get(value, 0)


def _normalize_channel(value: str) -> str:
    return value if value in {"mail", "phone", "internal", "none"} else "internal"


def _normalize_urgency(value: str) -> str:
    return value if value in {"low", "normal", "high"} else "normal"


def _normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return normalized


def _has_missing_info(missing_info: dict[str, Any]) -> bool:
    return bool((missing_info.get("critical") or []) or (missing_info.get("important") or []) or (missing_info.get("helpful") or []))


def _merge_missing_info_lists(missing_info: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for severity in ("critical", "important", "helpful"):
        for label in missing_info.get(severity) or []:
            items.append({
                "label": str(label),
                "severity": severity,
                "reason_pl": f"Brakuje informacji: {label}.",
            })
    return items


def _missing_info_label_pl(value: str) -> str:
    mapping = {
        "installation address": "adres inwestycji",
        "phone": "telefon kontaktowy",
        "confirmed case reference": "potwierdzenie właściwej sprawy",
        "service history": "historia serwisowa",
        "delivery date confirmation": "potwierdzenie terminu dostawy",
        "payment confirmation": "potwierdzenie płatności",
    }
    text = str(value or "").strip()
    return mapping.get(text.lower(), text)


def _business_priority(*, priority: str, urgency: str, review_required: bool) -> str:
    if priority == "critical":
        return "critical"
    if priority == "high" or urgency == "high":
        return "high"
    if priority == "low" and urgency == "low" and not review_required:
        return "low"
    return "medium"


def _latest_meaningful_change_pl(intake_result: dict[str, Any], case_link_result: dict[str, Any]) -> str:
    action = str((intake_result.get("decision") or {}).get("action") or "")
    case_link_decision = str(case_link_result.get("decision") or "")
    state_change = (intake_result.get("case_assessment") or {}).get("state_change") or {}
    if action in {"create_case", "create_case_and_task"}:
        return "Pojawił sie nowy temat operacyjny wymagajacy prowadzenia jako sprawa."
    if action in {"append_to_existing_case", "update_case_state"} and case_link_decision in {"linked", "weak_link"}:
        if bool(state_change.get("detected")):
            from_state = str(state_change.get("from_state") or "poprzedni stan")
            to_state = str(state_change.get("to_state") or "nowy stan")
            return f"Sprawa zmienila stan z \"{from_state}\" na \"{to_state}\"."
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


def _visibility_score(
    *,
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    missing_info: dict[str, Any],
    risk_assessment: dict[str, Any],
    review_required: bool,
    feedback_learning_memory: dict[str, Any],
) -> float:
    score = {
        "low": 0.18,
        "medium": 0.42,
        "high": 0.68,
        "critical": 0.88,
    }.get(str(intake_result.get("priority") or "medium"), 0.42)

    if str(business_result.get("urgency") or "normal") == "high":
        score += 0.14
    elif str(business_result.get("urgency") or "normal") == "low":
        score -= 0.08

    score += min(len(missing_info.get("critical") or []) * 0.08, 0.16)
    score += min(len(risk_assessment.get("risks") or []) * 0.04, 0.16)
    if review_required:
        score += 0.08

    biases = set(feedback_learning_memory.get("preference_biases") or [])
    if "prefer_lower_presence" in biases:
        score -= 0.08
    if "allow_stronger_presence" in biases:
        score += 0.08
    if "suppress_similar_signals" in set(feedback_learning_memory.get("suppression_hints") or []):
        score -= 0.12

    return max(0.0, min(1.0, score))


# ── Item-level normalize helpers ─────────────────────────────────────


def _action_item(*, action_type: str, reason_pl: str, urgency_level: str, confidence: float, review_required: bool) -> dict[str, Any]:
    return {
        "action_type": action_type,
        "title_pl": ACTION_TITLE_PL.get(action_type, "Sprawdz nastepny ruch"),
        "reason_pl": reason_pl,
        "urgency_level": _normalize_urgency(urgency_level),
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "whether_human_review_required": review_required,
        "suggested_channel": ACTION_CHANNEL.get(action_type, "internal"),
        "optional_draft_pointer": "",
    }


def _normalize_action_item(obj: dict[str, Any]) -> dict[str, Any]:
    action_type = _string_or_default(obj.get("action_type"), default="wait")
    if action_type not in INTELLIGENCE_ACTION_TYPES:
        action_type = "wait"
    urgency_level = _normalize_urgency(_string_or_default(obj.get("urgency_level"), default="normal"))
    return {
        "action_type": action_type,
        "title_pl": _string_or_default(obj.get("title_pl"), default=ACTION_TITLE_PL.get(action_type, "Sprawdz nastepny ruch")),
        "reason_pl": _string_or_default(obj.get("reason_pl"), default="Brak uzasadnienia dzialania."),
        "urgency_level": urgency_level,
        "confidence": _bounded_float(obj.get("confidence"), default=0.0),
        "whether_human_review_required": bool(obj.get("whether_human_review_required", False)),
        "suggested_channel": _normalize_channel(_string_or_default(obj.get("suggested_channel"), default=ACTION_CHANNEL.get(action_type, "internal"))),
        "optional_draft_pointer": _string_or_default(obj.get("optional_draft_pointer"), default=""),
    }


def _normalize_risk_item(obj: dict[str, Any]) -> dict[str, Any]:
    risk_type = _string_or_default(obj.get("risk_type"), default="interpretation_risk")
    if risk_type not in RISK_TYPES:
        risk_type = "interpretation_risk"
    severity = _string_or_default(obj.get("severity"), default="medium")
    if severity not in RISK_SEVERITIES:
        severity = "medium"
    return {
        "risk_type": risk_type,
        "severity": severity,
        "reason_pl": _string_or_default(obj.get("reason_pl"), default="Brak uzasadnienia ryzyka."),
        "confidence": _bounded_float(obj.get("confidence"), default=0.0),
        "what_to_watch_for": _string_or_default(obj.get("what_to_watch_for"), default=""),
    }


def _normalize_missing_info_item(obj: dict[str, Any]) -> dict[str, Any]:
    severity = _string_or_default(obj.get("severity"), default="important")
    if severity not in {"critical", "important", "helpful"}:
        severity = "important"
    return {
        "label": _string_or_default(obj.get("label"), default="Brak informacji"),
        "severity": severity,
        "reason_pl": _string_or_default(obj.get("reason_pl"), default=""),
    }


def _normalize_suggestion_item(obj: dict[str, Any], *, default_type: str) -> dict[str, Any]:
    suggestion_type = _string_or_default(obj.get("suggestion_type"), default=default_type)
    if suggestion_type not in {"merge", "split"}:
        suggestion_type = default_type
    return {
        "candidate_case_id": _string_or_default(obj.get("candidate_case_id"), default=""),
        "candidate_case_key": _string_or_default(obj.get("candidate_case_key"), default=""),
        "suggestion_type": suggestion_type,
        "confidence": _bounded_float(obj.get("confidence"), default=0.0),
        "reason_pl": _string_or_default(obj.get("reason_pl"), default="Brak uzasadnienia sugestii."),
        "review_required": bool(obj.get("review_required", True)),
    }


# ── Normalize functions (section-level) ─────────────────────────────


def _normalize_case_guidance(obj: dict[str, Any] | None, *, source_mode: str) -> dict[str, Any]:
    o = obj if isinstance(obj, dict) else {}
    op = str(o.get("operational_status") or "").strip()
    if op not in CASE_GUIDANCE_OPERATIONAL_STATUS:
        op = "watching"
    wf = str(o.get("waiting_for") or "").strip()
    if wf not in CASE_GUIDANCE_WAITING_FOR:
        wf = "unknown"
    mom = str(o.get("momentum") or "").strip()
    if mom not in CASE_GUIDANCE_MOMENTUM:
        mom = "steady"
    br = str(o.get("business_readiness") or "").strip()
    if br not in CASE_GUIDANCE_BUSINESS_READINESS:
        br = "not_ready"
    oac = str(o.get("operator_attention_class") or "").strip()
    if oac not in CASE_GUIDANCE_OPERATOR_ATTENTION:
        oac = "watch"
    sm = str(o.get("source_mode") or source_mode or "").strip()
    if sm not in CASE_GUIDANCE_SOURCE_MODES:
        sm = source_mode if source_mode in CASE_GUIDANCE_SOURCE_MODES else "fallback"

    try:
        conf = float(o.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, round(conf, 4)))

    stagnation_flag = bool(o.get("stagnation_flag", False))

    reason = _guidance_clip(str(o.get("reason_summary_pl") or ""), _GUIDANCE_MAX_REASON)
    blocker = _guidance_clip(str(o.get("blocker_summary_pl") or ""), _GUIDANCE_MAX_BLOCKER)
    st_reason = _guidance_clip(str(o.get("stagnation_reason_pl") or ""), _GUIDANCE_MAX_STAGNATION)
    hint = _guidance_clip(str(o.get("next_step_hint_pl") or ""), _GUIDANCE_MAX_HINT)

    if sm == "skipped":
        return {
            "operational_status": "watching",
            "waiting_for": "unknown",
            "reason_summary_pl": "Brak pewnej interpretacji operacyjnej sprawy.",
            "blocker_summary_pl": "",
            "momentum": "steady",
            "stagnation_flag": False,
            "stagnation_reason_pl": "",
            "business_readiness": "not_ready",
            "operator_attention_class": "watch",
            "next_step_hint_pl": "Sprawdz sprawe recznie.",
            "confidence": 0.0,
            "source_mode": "skipped",
            "evidence_refs": [],
            "assumptions": [],
            "unsupported_claims": ["Case guidance skipped; manual interpretation required."],
            "conflict_refs": [],
        }

    if sm == "fallback" or not reason:
        reason = "Brak pewnej interpretacji operacyjnej sprawy."
        if sm == "fallback":
            blocker = ""
            st_reason = ""
            hint = "Sprawdz sprawe recznie."
            stagnation_flag = False

    if sm == "fallback":
        conf = min(conf, 0.35)

    return {
        "operational_status": op,
        "waiting_for": wf,
        "reason_summary_pl": reason,
        "blocker_summary_pl": blocker,
        "momentum": mom,
        "stagnation_flag": stagnation_flag,
        "stagnation_reason_pl": st_reason,
        "business_readiness": br,
        "operator_attention_class": oac,
        "next_step_hint_pl": hint,
        "confidence": conf,
        "source_mode": sm,
        "evidence_refs": normalize_case_guidance_evidence_refs(_guidance_dict_list(o.get("evidence_refs")), source_mode=sm),
        "assumptions": _guidance_string_list(o.get("assumptions")),
        "unsupported_claims": _guidance_string_list(o.get("unsupported_claims"))
            or (["Case guidance fallback; claims are not independently supported."] if sm == "fallback" else []),
        "conflict_refs": strip_forbidden_evidence_like_rows(_guidance_dict_list(o.get("conflict_refs"))),
    }


def _normalize_case_understanding(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": _string_or_default(obj.get("case_id"), default=""),
        "case_family": _string_or_default(obj.get("case_family"), default="unknown"),
        "business_area": _string_or_default(obj.get("business_area"), default=""),
        "current_state": _string_or_default(obj.get("current_state"), default="none"),
        "state_confidence": _bounded_float(obj.get("state_confidence"), default=0.0),
        "business_priority": _string_or_default(obj.get("business_priority"), default="medium"),
        "summary_short": _string_or_default(obj.get("summary_short"), default="Brak krotkiego podsumowania."),
        "summary_operator": _string_or_default(obj.get("summary_operator"), default="Brak operator summary."),
        "latest_meaningful_change": _string_or_default(obj.get("latest_meaningful_change"), default="Brak ostatniej zmiany."),
        "attention_reason": _string_or_default(obj.get("attention_reason"), default="Brak uzasadnienia uwagi."),
        "blockers": _normalize_string_list(obj.get("blockers")),
        "risks": [_normalize_risk_item(item) for item in (obj.get("risks") or []) if isinstance(item, dict)],
        "missing_info": [_normalize_missing_info_item(item) for item in (obj.get("missing_info") or []) if isinstance(item, dict)],
        "recommended_next_actions": [_normalize_action_item(item) for item in (obj.get("recommended_next_actions") or []) if isinstance(item, dict)],
        "merge_candidates": [_normalize_suggestion_item(item, default_type="merge") for item in (obj.get("merge_candidates") or []) if isinstance(item, dict)],
        "split_suspicions": [_normalize_suggestion_item(item, default_type="split") for item in (obj.get("split_suspicions") or []) if isinstance(item, dict)],
        "confidence_overall": _bounded_float(obj.get("confidence_overall"), default=0.0),
        "review_required": bool(obj.get("review_required", False)),
        "review_flags": _normalize_string_list(obj.get("review_flags")),
    }


def _normalize_next_best_action(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "primary_next_action": _normalize_action_item(obj.get("primary_next_action") or {}),
        "secondary_actions": [_normalize_action_item(item) for item in (obj.get("secondary_actions") or []) if isinstance(item, dict)],
    }


def _normalize_missing_info(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary_pl": _string_or_default(obj.get("summary_pl"), default="Brak istotnych brakow informacji."),
        "critical": _normalize_string_list(obj.get("critical")),
        "important": _normalize_string_list(obj.get("important")),
        "helpful": _normalize_string_list(obj.get("helpful")),
        "customer_question_draft_pl": _string_or_default(obj.get("customer_question_draft_pl"), default=""),
        "operator_checklist_pl": _normalize_string_list(obj.get("operator_checklist_pl")),
    }


def _normalize_risk_assessment(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary_pl": _string_or_default(obj.get("summary_pl"), default="Brak wyraznych ryzyk."),
        "risks": [_normalize_risk_item(item) for item in (obj.get("risks") or []) if isinstance(item, dict)],
    }


def _normalize_merge_split_suggestions(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary_pl": _string_or_default(obj.get("summary_pl"), default="Brak silnych przeslanek do merge lub split."),
        "merge_candidates": [_normalize_suggestion_item(item, default_type="merge") for item in (obj.get("merge_candidates") or []) if isinstance(item, dict)],
        "split_suspicions": [_normalize_suggestion_item(item, default_type="split") for item in (obj.get("split_suspicions") or []) if isinstance(item, dict)],
    }


def _normalize_desk_composition(obj: dict[str, Any]) -> dict[str, Any]:
    presence_mode = _string_or_default(obj.get("presence_mode"), default="silent")
    if presence_mode not in INTELLIGENCE_PRESENCE_MODES:
        presence_mode = "silent"
    surface_zone = _string_or_default(obj.get("surface_zone"), default="silent")
    if surface_zone not in INTELLIGENCE_SURFACE_ZONES:
        surface_zone = "silent"
    day_bucket = _string_or_default(obj.get("day_bucket"), default="w_najblizszym_czasie")
    if day_bucket not in INTELLIGENCE_DAY_BUCKETS:
        day_bucket = "w_najblizszym_czasie"
    lifecycle_intent = normalize_lifecycle_intent(_string_or_default(obj.get("lifecycle_intent"), default="noop"), surface_zone)
    return {
        "should_surface": bool(obj.get("should_surface", False)),
        "presence_mode": presence_mode,
        "surface_zone": surface_zone,
        "day_bucket": day_bucket,
        "title_pl": _string_or_default(obj.get("title_pl"), default="Kartka AI"),
        "body_short_pl": _string_or_default(obj.get("body_short_pl"), default="Brak krotkiego opisu."),
        "body_reason_pl": _string_or_default(obj.get("body_reason_pl"), default="Brak uzasadnienia."),
        "assistant_suggestion_pl": _string_or_default(obj.get("assistant_suggestion_pl"), default="Sprawdz temat recznie."),
        "visibility_score": _bounded_float(obj.get("visibility_score"), default=0.0),
        "lifecycle_intent": lifecycle_intent,
        "review_required": bool(obj.get("review_required", False)),
        "trace_summary": _string_or_default(obj.get("trace_summary"), default=""),
    }


def _normalize_lifecycle_revision(obj: dict[str, Any]) -> dict[str, Any]:
    target_surface_zone = _string_or_default(obj.get("target_surface_zone"), default="silent")
    if target_surface_zone not in INTELLIGENCE_SURFACE_ZONES:
        target_surface_zone = "silent"
    lifecycle_intent = normalize_lifecycle_intent(_string_or_default(obj.get("lifecycle_intent"), default="noop"), target_surface_zone)
    target_presence_mode = _string_or_default(obj.get("target_presence_mode"), default="silent")
    if target_presence_mode not in INTELLIGENCE_PRESENCE_MODES:
        target_presence_mode = "silent"
    return {
        "lifecycle_intent": lifecycle_intent,
        "target_presence_mode": target_presence_mode,
        "target_surface_zone": target_surface_zone,
        "reason_pl": _string_or_default(obj.get("reason_pl"), default="Brak uzasadnienia lifecycle."),
        "should_create": bool(obj.get("should_create", False)),
        "should_update": bool(obj.get("should_update", False)),
    }


def _normalize_feedback_learning_memory(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "explicit_signals": _normalize_string_list(obj.get("explicit_signals")),
        "implicit_signals": _normalize_string_list(obj.get("implicit_signals")),
        "preference_biases": _normalize_string_list(obj.get("preference_biases")),
        "suppression_hints": _normalize_string_list(obj.get("suppression_hints")),
        "tone_hint_pl": _string_or_default(obj.get("tone_hint_pl"), default="Krotko, rzeczowo i po polsku."),
        "emphasis_hint_pl": _string_or_default(obj.get("emphasis_hint_pl"), default="Neutralne ustawienie ekspozycji."),
    }


# ── Top-level validation ────────────────────────────────────────────


def validate_case_intelligence_result(obj: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError("CaseIntelligenceResult must be a JSON object.")

    case_understanding = _normalize_case_understanding(obj.get("case_understanding") or {})
    operator_brief = {
        "brief_pl": _string_or_default((obj.get("operator_brief") or {}).get("brief_pl"), default="Brak briefu operatora."),
    }
    next_best_action = _normalize_next_best_action(obj.get("next_best_action") or {})
    missing_info = _normalize_missing_info(obj.get("missing_info") or {})
    risk_assessment = _normalize_risk_assessment(obj.get("risk_assessment") or {})
    merge_split_suggestions = _normalize_merge_split_suggestions(obj.get("merge_split_suggestions") or {})
    desk_composition = _normalize_desk_composition(obj.get("desk_composition") or {})
    lifecycle_revision = _normalize_lifecycle_revision(obj.get("lifecycle_revision") or {})
    feedback_learning_memory = _normalize_feedback_learning_memory(obj.get("feedback_learning_memory") or {})
    raw_cg = obj.get("case_guidance")
    cg_dict = raw_cg if isinstance(raw_cg, dict) else {}
    sm = str(cg_dict.get("source_mode") or "").strip()
    if sm not in CASE_GUIDANCE_SOURCE_MODES:
        sm = "skipped"
    case_guidance = _normalize_case_guidance(cg_dict, source_mode=sm)

    out = {
        "case_understanding": case_understanding,
        "operator_brief": operator_brief,
        "next_best_action": next_best_action,
        "missing_info": missing_info,
        "risk_assessment": risk_assessment,
        "merge_split_suggestions": merge_split_suggestions,
        "desk_composition": desk_composition,
        "lifecycle_revision": lifecycle_revision,
        "feedback_learning_memory": feedback_learning_memory,
        "case_guidance": case_guidance,
    }
    cgr = obj.get("case_guidance_result")
    if isinstance(cgr, dict):
        out["case_guidance_result"] = cgr
    mmcp = obj.get("mailbox_memory_context_pack")
    if isinstance(mmcp, dict):
        out["mailbox_memory_context_pack"] = mmcp
    em = obj.get("execution_metadata")
    if isinstance(em, dict):
        out["execution_metadata"] = em
    return out
