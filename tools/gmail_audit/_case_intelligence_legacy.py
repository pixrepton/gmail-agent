"""Case-first AI intelligence composition over the existing Gmail Intake stages."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from case_identity import derive_canonical_case_id
from dash_preview import resolve_case_key_metadata
from evidence_ref import normalize_case_guidance_evidence_refs, strip_forbidden_evidence_like_rows
from v2_semantics import CANONICAL_LIFECYCLE_INTENTS, is_case_only_transition, normalize_lifecycle_intent


INTELLIGENCE_PRESENCE_MODES = ("silent", "subtle", "standard", "advisory", "strong", "alarm")
INTELLIGENCE_SURFACE_ZONES = ("desk", "day", "case_only", "silent")
INTELLIGENCE_DAY_BUCKETS = ("teraz", "dzisiaj", "w_najblizszym_czasie", "do_obserwacji")
INTELLIGENCE_LIFECYCLE_INTENTS = CANONICAL_LIFECYCLE_INTENTS
INTELLIGENCE_ACTION_TYPES = (
    "answer_customer",
    "call",
    "wait",
    "follow_up_supplier",
    "prepare_offer",
    "ask_for_missing_data",
    "escalate_internal",
    "merge_with_existing_case",
    "split_case_review",
    "move_to_case_only",
    "resolve_note",
    "review_required",
)
RISK_TYPES = (
    "lead_loss_risk",
    "operational_delay_risk",
    "logistics_risk",
    "finance_risk",
    "interpretation_risk",
    "aging_risk",
    "customer_silence_risk",
    "supplier_dependency_risk",
)
RISK_SEVERITIES = ("low", "medium", "high", "critical")

CASE_GUIDANCE_OPERATIONAL_STATUS = (
    "active_review",
    "waiting",
    "blocked",
    "follow_up_needed",
    "ready",
    "stagnating",
    "watching",
)
CASE_GUIDANCE_WAITING_FOR = (
    "none",
    "client",
    "operator",
    "supplier",
    "document",
    "quote",
    "schedule",
    "payment",
    "unknown",
)
CASE_GUIDANCE_MOMENTUM = ("growing", "steady", "slowing", "stalled")
CASE_GUIDANCE_BUSINESS_READINESS = (
    "not_ready",
    "needs_data",
    "ready_for_offer",
    "ready_for_followup",
    "ready_for_close",
)
CASE_GUIDANCE_OPERATOR_ATTENTION = ("watch", "keep_visible", "act_soon", "act_now", "case_only_ok")
CASE_GUIDANCE_SOURCE_MODES = ("llm_reasoned", "fallback", "skipped")

_GUIDANCE_MAX_REASON = 1200
_GUIDANCE_MAX_BLOCKER = 600
_GUIDANCE_MAX_HINT = 600
_GUIDANCE_MAX_STAGNATION = 400

ACTION_TITLE_PL = {
    "answer_customer": "Odpowiedz klientowi",
    "call": "Zadzwoń",
    "wait": "Poczekaj",
    "follow_up_supplier": "Sprawdź temat u dostawcy",
    "prepare_offer": "Przygotuj ofertę lub handoff",
    "ask_for_missing_data": "Poproś o brakujące dane",
    "escalate_internal": "Przekaż dalej wewnętrznie",
    "merge_with_existing_case": "Połącz z istniejącą sprawą",
    "split_case_review": "Sprawdź, czy trzeba rozdzielić sprawę",
    "move_to_case_only": "Zostaw tylko w sprawie",
    "resolve_note": "Wygasz kartkę",
    "review_required": "Wymagana ręczna ocena",
}

ACTION_CHANNEL = {
    "answer_customer": "mail",
    "call": "phone",
    "wait": "none",
    "follow_up_supplier": "phone",
    "prepare_offer": "internal",
    "ask_for_missing_data": "mail",
    "escalate_internal": "internal",
    "merge_with_existing_case": "internal",
    "split_case_review": "internal",
    "move_to_case_only": "none",
    "resolve_note": "none",
    "review_required": "internal",
}

RISK_TYPE_LABELS_PL = {
    "lead_loss_risk": "ryzyko utraty leada",
    "operational_delay_risk": "ryzyko opóźnienia operacyjnego",
    "logistics_risk": "ryzyko logistyczne",
    "finance_risk": "ryzyko finansowe",
    "interpretation_risk": "ryzyko błędnej interpretacji",
    "aging_risk": "ryzyko zalegania",
    "customer_silence_risk": "ryzyko ciszy po stronie klienta",
    "supplier_dependency_risk": "ryzyko zależności od dostawcy",
}

MISSING_INFO_CRITICAL_KEYWORDS = (
    "address",
    "adres",
    "phone",
    "telefon",
    "metra",
    "m2",
    "moc",
    "power",
    "confirmed case",
    "case reference",
)
MISSING_INFO_IMPORTANT_KEYWORDS = (
    "term",
    "termin",
    "delivery",
    "dostaw",
    "payment",
    "platn",
    "service history",
    "supplier",
    "contact",
)


def apply_hot_state_to_case_intelligence(
    intelligence: dict[str, Any],
    hot_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Merge formal CaseSnapshotHotState into case intelligence as the preferred compact state surface.
    Does not remove legacy fields; adds V2.1-primary pointers for downstream reasoning.
    """
    if not hot_state or not isinstance(intelligence, dict):
        return intelligence
    out = dict(intelligence)
    cu = dict(out.get("case_understanding") or {})
    case_block = hot_state.get("case") if isinstance(hot_state.get("case"), dict) else {}
    cu["case_snapshot_hot_state_primary"] = True
    cu["operational_status_hot"] = str(case_block.get("operational_status") or "")
    summary_hot = str(case_block.get("summary_text") or "").strip()
    if summary_hot:
        cu["summary_short"] = summary_hot[:200]
    cu["active_conflicts_hot"] = list(hot_state.get("active_conflicts") or [])
    cu["key_facts_hot"] = list(hot_state.get("key_facts") or [])[:12]
    cu["open_loops_hot"] = list(hot_state.get("open_loops") or [])[:12]
    cu["recommended_next_step_hot"] = str(hot_state.get("recommended_next_step") or "")
    cu["cold_evidence_pointers_hot"] = hot_state.get("cold_evidence_pointers") if isinstance(
        hot_state.get("cold_evidence_pointers"), dict
    ) else {}
    out["case_understanding"] = cu
    meta = dict(out.get("execution_metadata") or {})
    meta["hot_state_schema"] = str(hot_state.get("schema_version") or "")
    meta["hot_state_snapshot_id"] = str(hot_state.get("snapshot_id") or "")
    out["execution_metadata"] = meta
    return out


def build_case_intelligence(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any] | None,
    business_result: dict[str, Any] | None,
    reply_result: dict[str, Any] | None,
    action_plan_result: dict[str, Any] | None,
    feedback_memory_seed: dict[str, Any] | None = None,
    current_note_state: dict[str, Any] | None = None,
    attachment_intelligence: dict[str, Any] | None = None,
    thread_memory: dict[str, Any] | None = None,
    case_context_pack: dict[str, Any] | None = None,
    decision_candidate_enabled: bool = False,
    preclassification_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the V3 AI Intelligence Layer MVP from existing foundation outputs and substrate layers."""
    feedback_learning_memory = build_feedback_learning_memory(feedback_memory_seed)
    merge_split_suggestions = build_merge_split_suggestions(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link_result or {},
    )
    missing_info = build_missing_info(
        intake_result=intake_result,
        business_result=business_result or {},
        reply_result=reply_result or {},
        case_link_result=case_link_result or {},
        attachment_intelligence=attachment_intelligence or {},
        thread_memory=thread_memory or {},
    )
    risk_assessment = build_risk_assessment(
        intake_result=intake_result,
        business_result=business_result or {},
        missing_info=missing_info,
        current_note_state=current_note_state or {},
        attachment_intelligence=attachment_intelligence or {},
        thread_memory=thread_memory or {},
    )
    next_best_action = build_next_best_action(
        intake_result=intake_result,
        case_link_result=case_link_result or {},
        business_result=business_result or {},
        reply_result=reply_result or {},
        action_plan_result=action_plan_result or {},
        missing_info=missing_info,
        merge_split_suggestions=merge_split_suggestions,
    )
    case_understanding = build_case_understanding_snapshot(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link_result or {},
        business_result=business_result or {},
        next_best_action=next_best_action,
        missing_info=missing_info,
        risk_assessment=risk_assessment,
        merge_split_suggestions=merge_split_suggestions,
        case_context_pack=case_context_pack or {},
    )
    operator_brief = build_case_operator_brief(
        case_understanding=case_understanding,
        next_best_action=next_best_action,
        missing_info=missing_info,
        risk_assessment=risk_assessment,
    )
    desk_composition = build_desk_composition(
        intake_result=intake_result,
        business_result=business_result or {},
        case_understanding=case_understanding,
        next_best_action=next_best_action,
        missing_info=missing_info,
        risk_assessment=risk_assessment,
        merge_split_suggestions=merge_split_suggestions,
        feedback_learning_memory=feedback_learning_memory,
        preclassification_result=preclassification_result,
    )
    lifecycle_revision = build_lifecycle_revision(
        intake_result=intake_result,
        case_link_result=case_link_result or {},
        case_understanding=case_understanding,
        desk_composition=desk_composition,
        current_note_state=current_note_state or {},
    )
    result = {
        "case_understanding": case_understanding,
        "operator_brief": operator_brief,
        "next_best_action": next_best_action,
        "missing_info": missing_info,
        "risk_assessment": risk_assessment,
        "merge_split_suggestions": merge_split_suggestions,
        "desk_composition": desk_composition,
        "lifecycle_revision": lifecycle_revision,
        "feedback_learning_memory": feedback_learning_memory,
        "mailbox_memory_context_pack": case_context_pack or {},
    }
    normalized = validate_case_intelligence_result(result)
    if decision_candidate_enabled:
        from decision_candidate import build_decision_candidate

        source_message = snapshot.get("source_message") if isinstance(snapshot.get("source_message"), dict) else {}
        normalized["decision_candidate"] = build_decision_candidate(
            case_id=str(case_understanding.get("case_id") or ""),
            source_signal_id=str(source_message.get("message_id") or intake_result.get("message_id") or ""),
            topic=str(intake_result.get("business_area") or ""),
            case_type=str(case_understanding.get("case_family") or ""),
            priority=str(case_understanding.get("business_priority") or intake_result.get("priority") or ""),
            sla_risk=str((risk_assessment.get("risks") or [{}])[0].get("severity") or ""),
            owner_hint=str((case_understanding.get("current_owner") or "")),
            next_best_action=next_best_action.get("primary_next_action") or {},
            risk_class_candidate=str((risk_assessment.get("risks") or [{}])[0].get("severity") or "unknown"),
            case_context_pack=case_context_pack or {},
        )
    normalized["execution_metadata"] = {
        "stage_name": "case_intelligence",
        "shadow_only": True,
        "input_primary_action": str((action_plan_result or {}).get("primary_action") or ""),
        "input_business_next_action": str((business_result or {}).get("recommended_next_action") or ""),
        "input_case_link_decision": str((case_link_result or {}).get("decision") or ""),
    }
    return normalized


def _guidance_clip(text: str, max_len: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


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


def _first_summary(items: list[dict[str, Any]]) -> str:
    for item in items[:3]:
        text = str(item.get("summary") or item.get("summary_pl") or "").strip()
        if text:
            return text[:180]
    return ""


def _normalize_case_guidance(obj: dict[str, Any] | None, *, source_mode: str) -> dict[str, Any]:
    """Enforce enums, bounds, and conservative defaults for Rich Case Guidance v1."""
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
            "next_step_hint_pl": "Sprawdź sprawę ręcznie.",
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
            hint = "Sprawdź sprawę ręcznie."
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


def merge_case_guidance_into_intelligence(
    base_intelligence: dict[str, Any],
    case_guidance: dict[str, Any],
) -> dict[str, Any]:
    """
    Attach normalized case_guidance and enrich operator_brief + desk_composition.
    Does not modify lifecycle_revision or presence/surface/day fields.
    """
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
    sensible = (
        len(hint) >= 6
        and hint.lower() != "sprawdź sprawę ręcznie."
        and (float(cg.get("confidence") or 0.0) >= 0.18 or cg.get("source_mode") == "llm_reasoned")
    )
    if sensible:
        desk["assistant_suggestion_pl"] = hint
    merged["desk_composition"] = desk

    return merged


def validate_case_intelligence_result(obj: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize the case-intelligence contract."""
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
        review_required=bool(intake_result.get("review_required") or intake_result.get("review", {}).get("required")),
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
    if bool(intake_result.get("review_required") or intake_result.get("review", {}).get("required")):
        blockers.append("Temat wymaga ręcznej oceny przed bezpiecznym ruchem.")

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

    summary_operator = _string_or_default(
        business_result.get("business_interpretation"),
        default=str(intake_result.get("reason") or "Brak interpretacji biznesowej."),
    )
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
        "summary_short": _string_or_default(
            business_result.get("business_summary_short"),
            default=summary_operator[:160],
        ),
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
        "title_pl": ACTION_TITLE_PL.get(action_type, "Sprawdź następny ruch"),
        "reason_pl": _string_or_default(
            business_result.get("recommended_action_reason") or action_plan_result.get("why_this_action"),
            default="System wskazuje ten ruch jako najbezpieczniejszy kolejny krok.",
        ),
        "urgency_level": _normalize_urgency(str(business_result.get("urgency") or "normal")),
        "confidence": max(
            _bounded_float(action_plan_result.get("confidence"), default=0.0),
            _bounded_float((business_result.get("confidence") or {}).get("action_confidence"), default=0.0),
        ),
        "whether_human_review_required": review_required or action_type in {"review_required", "merge_with_existing_case", "split_case_review"},
        "suggested_channel": ACTION_CHANNEL.get(action_type, "internal"),
        "optional_draft_pointer": str(reply_result.get("recommended_variant") or "") if bool(reply_result.get("draft_enabled")) and action_type in {"answer_customer", "ask_for_missing_data"} else "",
    }

    secondary_actions: list[dict[str, Any]] = []
    if merge_split_suggestions.get("merge_candidates"):
        secondary_actions.append(
            _action_item(
                action_type="merge_with_existing_case",
                reason_pl="System widzi podobieństwo do istniejącej sprawy i sugeruje ręczne sprawdzenie merge.",
                urgency_level="normal",
                confidence=max(
                    [_bounded_float(item.get("confidence"), default=0.0) for item in merge_split_suggestions.get("merge_candidates") or [{}]]
                ),
                review_required=True,
            )
        )
    if merge_split_suggestions.get("split_suspicions"):
        secondary_actions.append(
            _action_item(
                action_type="split_case_review",
                reason_pl="Sygnał może mieszać dwa różne wątki i warto to sprawdzić przed dalszym ruchem.",
                urgency_level="normal",
                confidence=max(
                    [_bounded_float(item.get("confidence"), default=0.0) for item in merge_split_suggestions.get("split_suspicions") or [{}]]
                ),
                review_required=True,
            )
        )
    if action_type not in {"wait", "move_to_case_only"} and not review_required:
        secondary_actions.append(
            _action_item(
                action_type="move_to_case_only",
                reason_pl="Jeżeli operator nie chce tego na Biurku, temat można zostawić tylko w pamięci sprawy.",
                urgency_level="low",
                confidence=0.55,
                review_required=False,
            )
        )

    return {
        "primary_next_action": primary,
        "secondary_actions": secondary_actions[:2],
    }


def build_missing_info(
    *,
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    reply_result: dict[str, Any],
    case_link_result: dict[str, Any],
    attachment_intelligence: dict[str, Any] | None = None,
    thread_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_items = [str(item).strip() for item in (business_result.get("missing_information") or []) if str(item).strip()]
    if str(case_link_result.get("decision") or "") in {"weak_link", "competing_links"}:
        raw_items.append("confirmed case reference")

    critical: list[str] = []
    important: list[str] = []
    helpful: list[str] = []
    for item in raw_items:
        localized_item = _missing_info_label_pl(item)
        lowered = item.lower()
        if any(keyword in lowered for keyword in MISSING_INFO_CRITICAL_KEYWORDS):
            critical.append(localized_item)
        elif any(keyword in lowered for keyword in MISSING_INFO_IMPORTANT_KEYWORDS):
            important.append(localized_item)
        else:
            helpful.append(localized_item)

    summary_parts = []
    if critical:
        summary_parts.append("Brakuje krytycznych danych: " + ", ".join(critical) + ".")
    if important:
        summary_parts.append("Warto uzupełnić: " + ", ".join(important) + ".")
    if helpful:
        summary_parts.append("Dodatkowo pomocne: " + ", ".join(helpful) + ".")
    summary_pl = " ".join(summary_parts).strip() or "Brak istotnych braków informacji."

    customer_question_draft_pl = ""
    if bool(reply_result.get("draft_enabled")) and (reply_result.get("drafts") or []):
        customer_question_draft_pl = str((reply_result.get("drafts") or [{}])[0].get("body") or "").strip()
    elif critical or important:
        requested = critical[:2] + important[:2]
        customer_question_draft_pl = "Dzień dobry, żeby ruszyć dalej, prosimy o: " + ", ".join(requested) + "."

    operator_checklist_pl = []
    for item in critical:
        operator_checklist_pl.append(f"Ustal krytyczne dane: {item}.")
    for item in important:
        operator_checklist_pl.append(f"Sprawdź ważny brak: {item}.")
    for item in helpful:
        operator_checklist_pl.append(f"Jeśli się da, doprecyzuj: {item}.")

    return {
        "summary_pl": summary_pl,
        "critical": critical,
        "important": important,
        "helpful": helpful,
        "customer_question_draft_pl": customer_question_draft_pl,
        "operator_checklist_pl": operator_checklist_pl,
    }


def build_risk_assessment(
    *,
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    missing_info: dict[str, Any],
    current_note_state: dict[str, Any],
    attachment_intelligence: dict[str, Any] | None = None,
    thread_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_risks = [str(item).strip() for item in (business_result.get("risks") or []) if str(item).strip()]
    for flag in (attachment_intelligence or {}).get("combined_risk_flags") or []:
        text = str(flag).strip()
        if text and text not in raw_risks:
            raw_risks.append(text)
    if (thread_memory or {}).get("has_unanswered_question"):
        raw_risks.append("unanswered_customer_question")
    risks: list[dict[str, Any]] = []

    for item in raw_risks:
        risks.append(_map_raw_risk_to_item(item, intake_result=intake_result, business_result=business_result))

    if missing_info.get("critical") and str((intake_result.get("case_assessment") or {}).get("case_family") or "") == "lead_opportunity":
        risks.append(
            _risk_item(
                risk_type="lead_loss_risk",
                severity="medium",
                reason_pl="Lead jest aktywny, ale bez krytycznych danych może utknąć na etapie kwalifikacji.",
                confidence=0.72,
                watch="Czy klient odpowie z danymi potrzebnymi do kolejnego kroku.",
            )
        )
    existing_risk_types = {str(item.get("risk_type") or "") for item in risks}
    state_detected = str((intake_result.get("case_assessment") or {}).get("state_detected") or "")
    if str(intake_result.get("business_area") or "") in {"procurement", "logistics"} and (
        raw_risks or state_detected in {"delivery_at_risk", "ordered", "delayed", "received"}
    ) and not existing_risk_types.intersection({"logistics_risk", "supplier_dependency_risk"}):
        risks.append(
            _risk_item(
                risk_type="logistics_risk",
                severity="medium" if str(intake_result.get("priority") or "low") != "critical" else "high",
                reason_pl="Temat dotyczy logistyki lub dostawy i może wpłynąć na kolejne działania operacyjne.",
                confidence=0.7,
                watch="Czy pojawi się nowy termin dostawy albo potwierdzenie odbioru.",
            )
        )
    try:
        age_days = float(current_note_state.get("age_days") or 0.0)
    except (TypeError, ValueError):
        age_days = 0.0
    if age_days >= 5:
        risks.append(
            _risk_item(
                risk_type="aging_risk",
                severity="medium",
                reason_pl="Temat zalega już kilka dni bez wyraźnego zamknięcia.",
                confidence=0.65,
                watch="Czy pojawia się realny postęp, czy tylko zaleganie bez ruchu.",
            )
        )

    risks = _dedupe_risk_items(risks)
    if risks:
        summary_pl = "Najważniejsze ryzyko: " + risks[0]["reason_pl"]
    else:
        summary_pl = "Na ten moment nie widać wyraźnych ryzyk operacyjnych."

    return {
        "summary_pl": summary_pl,
        "risks": risks,
    }


def build_merge_split_suggestions(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
) -> dict[str, Any]:
    merge_candidates: list[dict[str, Any]] = []
    raw_candidates = list(case_link_result.get("candidates") or [])
    selected_case_key = str(case_link_result.get("selected_case_key") or "").strip()
    if selected_case_key and not any(str((candidate or {}).get("case_key") or "").strip() == selected_case_key for candidate in raw_candidates):
        raw_candidates.insert(
            0,
            {
                "case_key": selected_case_key,
                "match_confidence": case_link_result.get("confidence") or intake_result.get("confidence", {}).get("case_link_confidence") or 0.0,
            },
        )

    for candidate in raw_candidates[:3]:
        if not isinstance(candidate, dict):
            continue
        case_key = str(candidate.get("case_key") or "").strip()
        confidence = _bounded_float(candidate.get("match_confidence") or candidate.get("score"), default=0.0)
        if not case_key or confidence < 0.45:
            continue
        merge_candidates.append(
            {
                "candidate_case_id": _stable_id("case", case_key),
                "candidate_case_key": case_key,
                "suggestion_type": "merge",
                "confidence": confidence,
                "reason_pl": "Nowy sygnał wygląda podobnie do istniejącej sprawy i może wymagać połączenia zamiast tworzenia równoległego wątku.",
                "review_required": True,
            }
        )

    split_suspicions: list[dict[str, Any]] = []
    review_flags = set((intake_result.get("review") or {}).get("flags") or [])
    secondary_signals = list(intake_result.get("secondary_signals") or [])
    references = ((intake_result.get("extracted_data") or {}).get("references") or {})
    reference_groups = sum(1 for key in ("invoice_numbers", "shipment_numbers", "order_numbers", "transaction_numbers", "case_ids") if references.get(key))
    if "multiple_competing_signals" in review_flags or (secondary_signals and reference_groups >= 2):
        split_suspicions.append(
            {
                "candidate_case_id": "",
                "suggestion_type": "split",
                "confidence": 0.68 if secondary_signals else 0.55,
                "reason_pl": "Sygnał może mieszać dwa niezależne wątki i warto to zweryfikować przed dalszym prowadzeniem sprawy.",
                "review_required": True,
            }
        )

    summary_pl = ""
    if merge_candidates:
        summary_pl = "System widzi możliwe powiązanie z istniejącą sprawą."
    elif split_suspicions:
        summary_pl = "System podejrzewa, że temat może zawierać więcej niż jeden wątek."
    else:
        summary_pl = "Brak silnych przesłanek do merge lub split."

    _ = snapshot
    return {
        "summary_pl": summary_pl,
        "merge_candidates": merge_candidates,
        "split_suspicions": split_suspicions,
    }


def build_feedback_learning_memory(feedback_memory_seed: dict[str, Any] | None) -> dict[str, Any]:
    counts = feedback_memory_seed if isinstance(feedback_memory_seed, dict) else {}
    explicit_signals: list[str] = []
    preference_biases: list[str] = []
    suppression_hints: list[str] = []

    too_strong = _coerce_int(counts.get("za_mocne"), default=0)
    too_weak = _coerce_int(counts.get("za_slabe"), default=0)
    case_only = _coerce_int(counts.get("tylko_w_sprawie"), default=0)
    hide_kind = _coerce_int(counts.get("nie_pokazuj_takich"), default=0)
    helpful = _coerce_int(counts.get("trafne"), default=0)

    if helpful:
        explicit_signals.append("helpful")
    if too_strong:
        explicit_signals.append("too_strong")
        preference_biases.append("prefer_lower_presence")
    if too_weak:
        explicit_signals.append("too_weak")
        preference_biases.append("allow_stronger_presence")
    if case_only:
        explicit_signals.append("case_only")
        suppression_hints.append("prefer_case_only_for_repeated_updates")
    if hide_kind:
        explicit_signals.append("hide_this_kind")
        suppression_hints.append("suppress_similar_signals")

    emphasis_hint = "Neutralne ustawienie ekspozycji."
    if too_strong > too_weak:
        emphasis_hint = "Lekko oszczędzaj uwagę na podobnych tematach."
    elif too_weak > too_strong:
        emphasis_hint = "Można odrobinę mocniej podbijać podobne tematy."

    tone_hint = "Krótko, rzeczowo i po polsku."
    if hide_kind or case_only:
        tone_hint = "Jeszcze krócej i bez nadmiernego wzmacniania."

    return {
        "explicit_signals": explicit_signals,
        "implicit_signals": [],
        "preference_biases": preference_biases,
        "suppression_hints": suppression_hints,
        "tone_hint_pl": tone_hint,
        "emphasis_hint_pl": emphasis_hint,
    }


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

    from operator_visibility_policy import (
        DESK_SUPPRESSION_REASON_NON_BUSINESS,
        should_suppress_desk_and_tasks,
    )

    if should_suppress_desk_and_tasks(
        business_result=business_result,
        preclassification_result=preclassification_result,
    ):
        title_pl = _string_or_default((intake_result.get("message") or {}).get("subject"), default="Kartka AI")
        return {
            "should_surface": False,
            "presence_mode": "silent",
            "surface_zone": "case_only",
            "day_bucket": "do_obserwacji",
            "title_pl": title_pl,
            "body_short_pl": _string_or_default(case_understanding.get("summary_short"), default=case_understanding.get("summary_operator")),
            "body_reason_pl": _string_or_default(case_understanding.get("attention_reason"), default="Brak uzasadnienia."),
            "assistant_suggestion_pl": _string_or_default(primary_action.get("title_pl"), default="Sprawdź temat ręcznie."),
            "visibility_score": 0.0,
            "lifecycle_intent": "move_to_case_only",
            "review_required": review_required,
            "desk_tasks_suppressed": True,
            "desk_suppression_reason": DESK_SUPPRESSION_REASON_NON_BUSINESS,
            "trace_summary": _string_or_default(
                (merge_split_suggestions.get("summary_pl") if merge_split_suggestions.get("summary_pl") != "Brak silnych przesłanek do merge lub split." else "")
                or case_understanding.get("latest_meaningful_change"),
                default="Brak dodatkowego śladu.",
            ),
        }

    visibility_score = _visibility_score(
        intake_result=intake_result,
        business_result=business_result,
        missing_info=missing_info,
        risk_assessment=risk_assessment,
        review_required=review_required,
        feedback_learning_memory=feedback_learning_memory,
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
        "should_surface": should_surface,
        "presence_mode": presence_mode,
        "surface_zone": surface_zone,
        "day_bucket": day_bucket,
        "title_pl": _string_or_default((intake_result.get("message") or {}).get("subject"), default="Kartka AI"),
        "body_short_pl": _string_or_default(case_understanding.get("summary_short"), default=case_understanding.get("summary_operator")),
        "body_reason_pl": _string_or_default(case_understanding.get("attention_reason"), default="Brak uzasadnienia."),
        "assistant_suggestion_pl": _string_or_default(primary_action.get("title_pl"), default="Sprawdź temat ręcznie."),
        "visibility_score": round(visibility_score, 4),
        "lifecycle_intent": lifecycle_intent,
        "review_required": review_required,
        "trace_summary": _string_or_default(
            (merge_split_suggestions.get("summary_pl") if merge_split_suggestions.get("summary_pl") != "Brak silnych przesłanek do merge lub split." else "")
            or case_understanding.get("latest_meaningful_change"),
            default="Brak dodatkowego śladu.",
        ),
    }


def build_lifecycle_revision(
    *,
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    case_understanding: dict[str, Any],
    desk_composition: dict[str, Any],
    current_note_state: dict[str, Any],
) -> dict[str, Any]:
    current_presence = str(current_note_state.get("presence_mode") or "")
    target_presence = str(desk_composition.get("presence_mode") or "silent")
    target_zone = str(desk_composition.get("surface_zone") or "silent")
    action = str((intake_result.get("decision") or {}).get("action") or "")

    lifecycle_intent = normalize_lifecycle_intent(str(desk_composition.get("lifecycle_intent") or "noop"), target_zone)
    if current_presence:
        if is_case_only_transition(lifecycle_intent, target_zone):
            lifecycle_intent = "move_to_case_only"
        elif not bool(desk_composition.get("should_surface")):
            lifecycle_intent = "suppress" if action in {"ignore", "mark_reference"} else "move_to_case_only"
        elif _presence_rank(target_presence) > _presence_rank(current_presence):
            lifecycle_intent = "escalate_presence"
        elif _presence_rank(target_presence) < _presence_rank(current_presence):
            lifecycle_intent = "deescalate_presence"
        elif action in {"append_to_existing_case", "update_case_state"} or str(case_link_result.get("decision") or "") in {"linked", "weak_link"}:
            lifecycle_intent = "update"
        else:
            lifecycle_intent = "update"
    elif lifecycle_intent in {"create", "update", "suppress", "move_to_case_only"}:
        pass
    elif target_zone == "case_only":
        lifecycle_intent = "move_to_case_only"
    elif not bool(desk_composition.get("should_surface")):
        lifecycle_intent = "suppress"
    elif action in {"append_to_existing_case", "update_case_state"} or str(case_link_result.get("decision") or "") in {"linked", "weak_link"}:
        lifecycle_intent = "update"

    reason_pl = _string_or_default(
        case_understanding.get("attention_reason"),
        default="Lifecycle pozostaje zgodny z aktualnym zrozumieniem sprawy.",
    )

    lifecycle_intent = normalize_lifecycle_intent(lifecycle_intent, target_zone)

    return {
        "lifecycle_intent": lifecycle_intent,
        "target_presence_mode": target_presence if target_presence in INTELLIGENCE_PRESENCE_MODES else "silent",
        "target_surface_zone": target_zone if target_zone in INTELLIGENCE_SURFACE_ZONES else "silent",
        "reason_pl": reason_pl,
        "should_create": lifecycle_intent == "create",
        "should_update": lifecycle_intent in {"update", "escalate_presence", "deescalate_presence", "move_to_case_only"},
    }


def _normalize_case_understanding(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": _string_or_default(obj.get("case_id"), default=""),
        "case_family": _string_or_default(obj.get("case_family"), default="unknown"),
        "business_area": _string_or_default(obj.get("business_area"), default=""),
        "current_state": _string_or_default(obj.get("current_state"), default="none"),
        "state_confidence": _bounded_float(obj.get("state_confidence"), default=0.0),
        "business_priority": _string_or_default(obj.get("business_priority"), default="medium"),
        "summary_short": _string_or_default(obj.get("summary_short"), default="Brak krótkiego podsumowania."),
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
        "summary_pl": _string_or_default(obj.get("summary_pl"), default="Brak istotnych braków informacji."),
        "critical": _normalize_string_list(obj.get("critical")),
        "important": _normalize_string_list(obj.get("important")),
        "helpful": _normalize_string_list(obj.get("helpful")),
        "customer_question_draft_pl": _string_or_default(obj.get("customer_question_draft_pl"), default=""),
        "operator_checklist_pl": _normalize_string_list(obj.get("operator_checklist_pl")),
    }


def _normalize_risk_assessment(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary_pl": _string_or_default(obj.get("summary_pl"), default="Brak wyraźnych ryzyk."),
        "risks": [_normalize_risk_item(item) for item in (obj.get("risks") or []) if isinstance(item, dict)],
    }


def _normalize_merge_split_suggestions(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary_pl": _string_or_default(obj.get("summary_pl"), default="Brak silnych przesłanek do merge lub split."),
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
    lifecycle_intent = normalize_lifecycle_intent(
        _string_or_default(obj.get("lifecycle_intent"), default="noop"),
        surface_zone,
    )
    return {
        "should_surface": bool(obj.get("should_surface", False)),
        "presence_mode": presence_mode,
        "surface_zone": surface_zone,
        "day_bucket": day_bucket,
        "title_pl": _string_or_default(obj.get("title_pl"), default="Kartka AI"),
        "body_short_pl": _string_or_default(obj.get("body_short_pl"), default="Brak krótkiego opisu."),
        "body_reason_pl": _string_or_default(obj.get("body_reason_pl"), default="Brak uzasadnienia."),
        "assistant_suggestion_pl": _string_or_default(obj.get("assistant_suggestion_pl"), default="Sprawdź temat ręcznie."),
        "visibility_score": _bounded_float(obj.get("visibility_score"), default=0.0),
        "lifecycle_intent": lifecycle_intent,
        "review_required": bool(obj.get("review_required", False)),
        "trace_summary": _string_or_default(obj.get("trace_summary"), default=""),
    }


def _normalize_lifecycle_revision(obj: dict[str, Any]) -> dict[str, Any]:
    target_surface_zone = _string_or_default(obj.get("target_surface_zone"), default="silent")
    if target_surface_zone not in INTELLIGENCE_SURFACE_ZONES:
        target_surface_zone = "silent"
    lifecycle_intent = normalize_lifecycle_intent(
        _string_or_default(obj.get("lifecycle_intent"), default="noop"),
        target_surface_zone,
    )
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
        "tone_hint_pl": _string_or_default(obj.get("tone_hint_pl"), default="Krótko, rzeczowo i po polsku."),
        "emphasis_hint_pl": _string_or_default(obj.get("emphasis_hint_pl"), default="Neutralne ustawienie ekspozycji."),
    }


def _normalize_action_item(obj: dict[str, Any]) -> dict[str, Any]:
    action_type = _string_or_default(obj.get("action_type"), default="wait")
    if action_type not in INTELLIGENCE_ACTION_TYPES:
        action_type = "wait"
    urgency_level = _normalize_urgency(_string_or_default(obj.get("urgency_level"), default="normal"))
    return {
        "action_type": action_type,
        "title_pl": _string_or_default(obj.get("title_pl"), default=ACTION_TITLE_PL.get(action_type, "Sprawdź następny ruch")),
        "reason_pl": _string_or_default(obj.get("reason_pl"), default="Brak uzasadnienia działania."),
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


def _map_raw_risk_to_item(raw_risk: str, *, intake_result: dict[str, Any], business_result: dict[str, Any]) -> dict[str, Any]:
    lowered = raw_risk.lower()
    priority = str(intake_result.get("priority") or "medium")
    severity = "high" if priority in {"critical", "high"} or str(business_result.get("urgency") or "") == "high" else "medium"

    if "weak_case_link" in lowered or "manual_review" in lowered or "reasoning_unavailable" in lowered:
        return _risk_item(
            risk_type="interpretation_risk",
            severity=severity,
            reason_pl="System nie ma jeszcze wystarczająco pewnego zrozumienia lub linku sprawy.",
            confidence=0.74,
            watch="Czy pojawi się potwierdzenie sprawy albo mocniejszy kontekst.",
        )
    if "scope" in lowered:
        return _risk_item(
            risk_type="lead_loss_risk",
            severity="medium",
            reason_pl="Zakres sprawy lub leada nie jest jeszcze wystarczająco doprecyzowany.",
            confidence=0.68,
            watch="Czy klient poda dane potrzebne do kwalifikacji.",
        )
    if "visit" in lowered or "delay" in lowered:
        return _risk_item(
            risk_type="operational_delay_risk",
            severity=severity,
            reason_pl="Brak potwierdzenia terminu może opóźnić dalszy ruch operacyjny.",
            confidence=0.72,
            watch="Czy termin zostanie potwierdzony lub skorygowany.",
        )
    if "supplier" in lowered:
        return _risk_item(
            risk_type="supplier_dependency_risk",
            severity="medium",
            reason_pl="Dalszy przebieg zależy od ruchu po stronie dostawcy.",
            confidence=0.7,
            watch="Czy dostawca potwierdzi termin lub status.",
        )
    if "finance" in lowered or "payment" in lowered:
        return _risk_item(
            risk_type="finance_risk",
            severity="medium",
            reason_pl="Sprawa ma komponent finansowy wymagający potwierdzenia.",
            confidence=0.66,
            watch="Czy pojawi się potwierdzenie płatności lub rozliczenia.",
        )
    return _risk_item(
        risk_type="interpretation_risk",
        severity="medium",
        reason_pl=f"System wykrył sygnał ryzyka: {raw_risk}.",
        confidence=0.6,
        watch="Czy kolejne sygnały potwierdzą ten kierunek.",
    )


def _risk_item(*, risk_type: str, severity: str, reason_pl: str, confidence: float, watch: str) -> dict[str, Any]:
    return {
        "risk_type": risk_type,
        "severity": severity,
        "reason_pl": reason_pl,
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "what_to_watch_for": watch,
    }


def _action_item(*, action_type: str, reason_pl: str, urgency_level: str, confidence: float, review_required: bool) -> dict[str, Any]:
    return {
        "action_type": action_type,
        "title_pl": ACTION_TITLE_PL.get(action_type, "Sprawdź następny ruch"),
        "reason_pl": reason_pl,
        "urgency_level": _normalize_urgency(urgency_level),
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "whether_human_review_required": review_required,
        "suggested_channel": ACTION_CHANNEL.get(action_type, "internal"),
        "optional_draft_pointer": "",
    }


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
        return "Pojawił się nowy temat operacyjny wymagający prowadzenia jako sprawa."
    if action in {"append_to_existing_case", "update_case_state"} and case_link_decision in {"linked", "weak_link"}:
        if bool(state_change.get("detected")):
            from_state = str(state_change.get("from_state") or "poprzedni stan")
            to_state = str(state_change.get("to_state") or "nowy stan")
            return f"Sprawa zmieniła stan z „{from_state}” na „{to_state}”."
        return "Do istniejącej sprawy doszedł nowy sygnał zmieniający kontekst operacyjny."
    if action == "review":
        return "Temat trafił do ręcznej oceny zamiast bezpośredniej automatyzacji."
    if action == "mark_reference":
        return "Temat został zachowany jako referencja bez aktywnej kartki."
    if action == "create_task":
        return "Pojawiło się konkretne działanie do wykonania."
    return "System zaktualizował rozumienie tej sprawy."


def _attention_reason_pl(
    priority: str,
    missing_info: dict[str, Any],
    risk_assessment: dict[str, Any],
    next_best_action: dict[str, Any],
    intake_result: dict[str, Any],
) -> str:
    if missing_info.get("critical"):
        return "Bez uzupełnienia krytycznych danych sprawa nie ruszy dalej."
    top_risk = (risk_assessment.get("risks") or [{}])[0]
    if top_risk and top_risk.get("reason_pl"):
        return str(top_risk["reason_pl"])
    action_title = str((next_best_action.get("primary_next_action") or {}).get("title_pl") or "").strip()
    if action_title:
        return f"Najbardziej sensowny kolejny ruch to: {action_title.lower()}."
    if priority in {"high", "critical"}:
        return "Temat ma podwyższoną wagę operacyjną."
    return _string_or_default(intake_result.get("reason"), default="Temat wymaga spokojnej uwagi operatora.")


def _business_priority(*, priority: str, urgency: str, review_required: bool) -> str:
    if priority == "critical":
        return "critical"
    if priority == "high" or urgency == "high":
        return "high"
    if priority == "low" and urgency == "low" and not review_required:
        return "low"
    return "medium"


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


def _merge_missing_info_lists(missing_info: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for severity in ("critical", "important", "helpful"):
        for label in missing_info.get(severity) or []:
            items.append(
                {
                    "label": str(label),
                    "severity": severity,
                    "reason_pl": f"Brakuje informacji: {label}.",
                }
            )
    return items


def _dedupe_risk_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_type: dict[str, dict[str, Any]] = {}
    for item in items:
        risk_type = str(item.get("risk_type") or "").strip()
        if not risk_type:
            continue
        existing = by_type.get(risk_type)
        if not existing or _severity_rank(item.get("severity")) > _severity_rank(existing.get("severity")):
            by_type[risk_type] = item
    return sorted(by_type.values(), key=lambda item: (_severity_rank(item.get("severity")), _bounded_float(item.get("confidence"), default=0.0)), reverse=True)


def _has_missing_info(missing_info: dict[str, Any]) -> bool:
    return bool((missing_info.get("critical") or []) or (missing_info.get("important") or []) or (missing_info.get("helpful") or []))


def _severity_rank(value: Any) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(str(value or "").strip(), 0)


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


def merge_data(
    case_a: dict[str, Any],
    case_b: dict[str, Any],
    *,
    merge_log: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Faktycznie scala dane dwóch spraw w jedną (PR-Merge).

    Algorytm:
    1. Facts — deduplikacja po fact_key, zachowanie nowszych
    2. Documents — deduplikacja po document_id/file_id
    3. History/events — połączenie, sortowanie po timestamp
    4. Konflikty — wykrycie konfliktów (różne wartości tego samego klucza)
    5. merge_log — zapis audytu

    Returns:
        dict z polami:
          - merged: dict ze scalonymi danymi
          - merge_log: list[dict] z wpisami audytu
          - conflicts: list[str] z wykrytymi konfliktami
          - merged_facts: int
          - merged_documents: int
          - merged_history: int
    """
    log: list[dict[str, Any]] = list(merge_log or [])
    conflicts: list[str] = []
    result_data: dict[str, Any] = {}

    # ── 1. Facts — dedup po fact_key ─────────────────────────────────
    facts_a: list[dict[str, Any]] = list(case_a.get("facts") or case_a.get("key_facts") or [])
    facts_b: list[dict[str, Any]] = list(case_b.get("facts") or case_b.get("key_facts") or [])
    merged_facts_map: dict[str, dict[str, Any]] = {}

    for fact in facts_a:
        key = str(fact.get("fact_key") or fact.get("key") or "").strip()
        if key:
            merged_facts_map[key] = dict(fact)

    for fact in facts_b:
        key = str(fact.get("fact_key") or fact.get("key") or "").strip()
        if not key:
            continue
        existing = merged_facts_map.get(key)
        if existing is not None:
            # Conflict detection: różne wartości tego samego faktu
            old_val = str(existing.get("normalized_value") or existing.get("value") or "")
            new_val = str(fact.get("normalized_value") or fact.get("value") or "")
            if old_val and new_val and old_val != new_val:
                conflicts.append(
                    f"fact_key={key!r}: '{old_val}' vs '{new_val}' — zachowano nowszą"
                )
                # Zachowaj nowszy (wg observed_at)
                old_ts = existing.get("observed_at") or ""
                new_ts = fact.get("observed_at") or ""
                if new_ts >= old_ts:
                    merged_facts_map[key] = dict(fact)
        else:
            merged_facts_map[key] = dict(fact)

    merged_facts_list = sorted(
        merged_facts_map.values(),
        key=lambda f: str(f.get("observed_at") or ""),
        reverse=True,
    )

    # ── 2. Documents — dedup po document_id / file_id ───────────────
    docs_a: list[dict[str, Any]] = list(case_a.get("documents") or case_a.get("docs") or [])
    docs_b: list[dict[str, Any]] = list(case_b.get("documents") or case_b.get("docs") or [])
    seen_docs: set[str] = set()
    merged_docs: list[dict[str, Any]] = []

    for doc in docs_a + docs_b:
        doc_id = str(doc.get("document_id") or doc.get("file_id") or doc.get("id") or "").strip()
        if doc_id and doc_id not in seen_docs:
            seen_docs.add(doc_id)
            merged_docs.append(dict(doc))

    # ── 3. History/events — merge + sort ────────────────────────────
    history_a: list[dict[str, Any]] = list(case_a.get("history") or case_a.get("events") or [])
    history_b: list[dict[str, Any]] = list(case_b.get("history") or case_b.get("events") or [])
    seen_history_hashes: set[str] = set()
    merged_history: list[dict[str, Any]] = []

    for event in history_a + history_b:
        event_str = str(event)
        event_hash = hashlib.sha256(event_str.encode("utf-8")).hexdigest()
        if event_hash not in seen_history_hashes:
            seen_history_hashes.add(event_hash)
            merged_history.append(dict(event))

    merged_history.sort(
        key=lambda e: str(e.get("timestamp") or e.get("observed_at") or ""),
    )

    # ── 4. Merge metadata ───────────────────────────────────────────
    result_data["facts"] = merged_facts_list
    result_data["documents"] = merged_docs
    result_data["history"] = merged_history
    result_data["merged_facts"] = len(merged_facts_list)
    result_data["merged_documents"] = len(merged_docs)
    result_data["merged_history"] = len(merged_history)

    # ── 5. Merge log (audit) ────────────────────────────────────────
    log.append({
        "action": "merge_data",
        "case_a": case_a.get("case_id", "?"),
        "case_b": case_b.get("case_id", "?"),
        "conflicts": list(conflicts),
        "counts": {
            "facts": len(merged_facts_list),
            "documents": len(merged_docs),
            "history": len(merged_history),
        },
    })

    return {
        "merged": result_data,
        "merge_log": log,
        "conflicts": conflicts,
        "merged_facts": len(merged_facts_list),
        "merged_documents": len(merged_docs),
        "merged_history": len(merged_history),
    }


__all__ = [
    "build_case_intelligence",
    "build_case_operator_brief",
    "build_case_understanding_snapshot",
    "build_desk_composition",
    "build_feedback_learning_memory",
    "build_lifecycle_revision",
    "build_merge_split_suggestions",
    "build_missing_info",
    "build_next_best_action",
    "build_risk_assessment",
    "merge_case_guidance_into_intelligence",
    "merge_data",
    "validate_case_intelligence_result",
    "_normalize_case_guidance",
]
