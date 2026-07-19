"""Formal multi-domain confidence and human review routing layer."""

from __future__ import annotations

from typing import Any


CONFIDENCE_DOMAINS = (
    "confidence_case_link",
    "confidence_attachment_extraction",
    "confidence_thread_memory",
    "confidence_next_action",
    "confidence_missing_info",
    "confidence_merge_split",
    "confidence_surface_decision",
)

REVIEW_MODES = (
    "auto_safe",
    "suggest_only",
    "review_before_write",
    "review_before_send",
    "review_before_merge",
    "review_before_case_create",
    "review_before_surface_escalation",
)

DEFAULT_THRESHOLDS: dict[str, float] = {
    "confidence_case_link": 0.70,
    "confidence_attachment_extraction": 0.50,
    "confidence_thread_memory": 0.60,
    "confidence_next_action": 0.65,
    "confidence_missing_info": 0.55,
    "confidence_merge_split": 0.60,
    "confidence_surface_decision": 0.50,
}


def build_confidence_domains(
    *,
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any] | None = None,
    business_result: dict[str, Any] | None = None,
    attachment_intelligence: dict[str, Any] | None = None,
    thread_memory: dict[str, Any] | None = None,
    action_plan_result: dict[str, Any] | None = None,
    case_intelligence_result: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Build per-domain confidence scores from all available stage outputs."""
    intake_confidence = intake_result.get("confidence") if isinstance(intake_result.get("confidence"), dict) else {}
    business_confidence = (business_result or {}).get("confidence") if isinstance((business_result or {}).get("confidence"), dict) else {}
    case_understanding = (case_intelligence_result or {}).get("case_understanding") or {}

    case_link_conf = float((case_link_result or {}).get("confidence") or intake_confidence.get("case_link_confidence") or 0.0)
    attachment_conf = _attachment_confidence(attachment_intelligence or {})
    thread_conf = _thread_confidence(thread_memory or {})
    next_action_conf = max(
        float((action_plan_result or {}).get("confidence") or 0.0),
        float(business_confidence.get("action_confidence") or 0.0),
    )
    missing_info_conf = float(business_confidence.get("business_confidence") or intake_confidence.get("signal_confidence") or 0.0)
    merge_split_conf = _merge_split_confidence(case_intelligence_result or {})
    surface_conf = float(case_understanding.get("confidence_overall") or 0.0)

    return {
        "confidence_case_link": _clamp(case_link_conf),
        "confidence_attachment_extraction": _clamp(attachment_conf),
        "confidence_thread_memory": _clamp(thread_conf),
        "confidence_next_action": _clamp(next_action_conf),
        "confidence_missing_info": _clamp(missing_info_conf),
        "confidence_merge_split": _clamp(merge_split_conf),
        "confidence_surface_decision": _clamp(surface_conf),
    }


def route_review(
    confidence_domains: dict[str, float],
    *,
    intake_result: dict[str, Any] | None = None,
    case_intelligence_result: dict[str, Any] | None = None,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Determine the review mode and per-decision review requirements."""
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    review_required = bool((intake_result or {}).get("review_required") or (intake_result or {}).get("review", {}).get("required"))

    flags: list[str] = []
    blocked_domains: list[str] = []

    for domain, value in confidence_domains.items():
        threshold = thresholds.get(domain, 0.5)
        if value < threshold:
            blocked_domains.append(domain)

    case_link_low = confidence_domains.get("confidence_case_link", 0.0) < thresholds.get("confidence_case_link", 0.7)
    attachment_low = confidence_domains.get("confidence_attachment_extraction", 0.0) < thresholds.get("confidence_attachment_extraction", 0.5)
    next_action_low = confidence_domains.get("confidence_next_action", 0.0) < thresholds.get("confidence_next_action", 0.65)
    merge_split_low = confidence_domains.get("confidence_merge_split", 0.0) < thresholds.get("confidence_merge_split", 0.6)
    surface_low = confidence_domains.get("confidence_surface_decision", 0.0) < thresholds.get("confidence_surface_decision", 0.5)

    intake_action = str((intake_result or {}).get("decision", {}).get("action") or "")
    is_action_bearing = intake_action not in {"ignore", "mark_reference", "mark_watchlist", ""}

    review_mode = "auto_safe"
    if review_required and _has_merge_candidates(case_intelligence_result):
        review_mode = "review_before_merge"
        flags.append("intake_review_required")
        flags.append("weak_case_link_with_merge")
    elif review_required:
        review_mode = "review_before_write"
        flags.append("intake_review_required")
    elif case_link_low and _has_merge_candidates(case_intelligence_result):
        review_mode = "review_before_merge"
        flags.append("weak_case_link_with_merge")
    elif case_link_low and is_action_bearing:
        review_mode = "review_before_case_create"
        flags.append("weak_case_link")
    elif next_action_low:
        review_mode = "review_before_write"
        flags.append("uncertain_next_action")
    elif attachment_low:
        review_mode = "suggest_only"
        flags.append("low_attachment_confidence")
    elif merge_split_low and _has_merge_candidates(case_intelligence_result):
        review_mode = "review_before_merge"
        flags.append("uncertain_merge_split")
    elif surface_low:
        review_mode = "review_before_surface_escalation"
        flags.append("uncertain_surface_decision")

    return {
        "review_mode": review_mode,
        "review_required": review_required or review_mode != "auto_safe",
        "flags": flags,
        "blocked_domains": blocked_domains,
        "automation_safe": review_mode == "auto_safe" and not blocked_domains,
        "review_reason_pl": _review_reason_pl(review_mode, flags),
    }


def apply_confidence_to_intelligence(
    case_intelligence_result: dict[str, Any],
    *,
    confidence_domains: dict[str, float],
    review_routing: dict[str, Any],
) -> dict[str, Any]:
    """Enrich case intelligence result with formal confidence domains and review routing."""
    case_intelligence_result["confidence_domains"] = confidence_domains
    case_intelligence_result["review_routing"] = review_routing

    case_understanding = case_intelligence_result.get("case_understanding") or {}
    if review_routing.get("review_required") and not case_understanding.get("review_required"):
        case_understanding["review_required"] = True
        if review_routing.get("flags"):
            existing_flags = set(case_understanding.get("review_flags") or [])
            existing_flags.update(review_routing["flags"])
            case_understanding["review_flags"] = sorted(existing_flags)
        case_intelligence_result["case_understanding"] = case_understanding

    desk = case_intelligence_result.get("desk_composition") or {}
    if review_routing.get("review_mode") == "review_before_surface_escalation" and desk.get("presence_mode") in {"strong", "alarm"}:
        desk["presence_mode"] = "advisory"
        desk["review_required"] = True
        case_intelligence_result["desk_composition"] = desk

    return case_intelligence_result


def _attachment_confidence(attachment_intelligence: dict[str, Any]) -> float:
    attachments = attachment_intelligence.get("attachments") or []
    if not attachments:
        return 1.0
    confidences = [float(a.get("extraction_confidence") or 0.0) for a in attachments]
    return sum(confidences) / max(len(confidences), 1)


def _thread_confidence(thread_memory: dict[str, Any]) -> float:
    if not thread_memory or not thread_memory.get("thread_id"):
        return 0.5
    message_count = int(thread_memory.get("message_count") or 1)
    has_unanswered = bool(thread_memory.get("has_unanswered_question"))
    base = min(0.5 + message_count * 0.1, 0.9)
    if has_unanswered:
        base -= 0.1
    return max(0.0, min(1.0, base))


def _merge_split_confidence(case_intelligence_result: dict[str, Any]) -> float:
    merge_split = case_intelligence_result.get("merge_split_suggestions") or {}
    candidates = merge_split.get("merge_candidates") or []
    suspicions = merge_split.get("split_suspicions") or []
    if not candidates and not suspicions:
        return 1.0
    all_confidences = [float((item or {}).get("confidence") or 0.0) for item in candidates + suspicions]
    return sum(all_confidences) / max(len(all_confidences), 1)


def _has_merge_candidates(case_intelligence_result: dict[str, Any] | None) -> bool:
    return bool(((case_intelligence_result or {}).get("merge_split_suggestions") or {}).get("merge_candidates"))


def _review_reason_pl(review_mode: str, flags: list[str]) -> str:
    reasons: dict[str, str] = {
        "auto_safe": "System jest wystarczająco pewny, aby działać bezpiecznie.",
        "suggest_only": "System sugeruje, ale nie jest pewien rozpoznania załącznika.",
        "review_before_write": "Wymagana ręczna ocena przed zapisem decyzji.",
        "review_before_send": "Wymagana ocena przed wysłaniem odpowiedzi.",
        "review_before_merge": "Wymagane potwierdzenie przed połączeniem spraw.",
        "review_before_case_create": "Wymagane potwierdzenie przed utworzeniem nowej sprawy.",
        "review_before_surface_escalation": "Niska pewność systemu obniża ekspozycję na Biurku.",
    }
    return reasons.get(review_mode, "Wymaga ręcznej oceny.")


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


__all__ = [
    "apply_confidence_to_intelligence",
    "build_confidence_domains",
    "route_review",
]
