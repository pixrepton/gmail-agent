"""Shadow-first v2 projection contract for Daszek AI Desk."""

from __future__ import annotations

import hashlib
from typing import Any

from case_context_contract import build_case_context_pack_vnext
from dash_preview import resolve_case_key_metadata
from evidence_ref import normalize_case_guidance_evidence_refs, strip_forbidden_evidence_like_rows
from operator_projection_quality import (
    build_case_readiness_projection,
    build_readiness_facets_projection,
    build_understanding_quality_projection,
)
from v2_semantics import CANONICAL_DESK_NOTE_COMMANDS, command_from_lifecycle_intent, decision_type_from_command


ALLOWED_PRESENCE_MODES = {"silent", "subtle", "standard", "advisory", "strong", "alarm"}
ALLOWED_CASE_COMMANDS = {"noop", "upsert_case", "update_state", "close_case", "reopen_case", "merge_case"}
ALLOWED_DESK_NOTE_COMMANDS = set(CANONICAL_DESK_NOTE_COMMANDS)
ALLOWED_LIFECYCLES = {
    "candidate",
    "active",
    "snoozed",
    "resolved_by_user",
    "resolved_by_ai",
    "expired",
    "merged",
    "withdrawn",
}

BUSINESS_AREA_LABELS_PL = {
    "sales": "sprzedaż",
    "service": "serwis",
    "logistics": "logistyka",
    "finance": "finanse",
    "general_admin": "administracja",
    "internal_coordination": "koordynacja wewnętrzna",
    "security": "bezpieczeństwo",
    "compliance_legal": "zgodność i formalności",
    "supplier_commercial": "dostawcy",
    "marketing": "marketing",
    "operations": "operacje",
}

NEXT_STEP_LABELS_PL = {
    "reply": "Przygotuj odpowiedź i potwierdź kolejny krok z klientem lub dostawcą.",
    "call": "Skontaktuj się telefonicznie i potwierdź dalsze działania.",
    "collect_data": "Uzupełnij brakujące informacje przed dalszym ruchem.",
    "create_task": "Zapisz temat jako działanie operacyjne i dopilnuj kolejnego kroku.",
    "update_case": "Zaktualizuj sprawę i potwierdź dalszy przebieg tematu.",
    "wait": "Na razie obserwuj temat bez dodatkowej eskalacji.",
    "ignore": "Nie wystawiaj tego tematu na biurko.",
    "escalate_review": "Sprawdź temat ręcznie i zdecyduj o dalszym działaniu.",
    "prepare_reply": "Przygotuj krótką odpowiedź i zbierz brakujące dane.",
    "create_review": "Przekaż temat do ręcznej oceny operatora.",
    "hold": "Zatrzymaj temat do spokojnej weryfikacji operacyjnej.",
}


def build_v2_shadow_projection(
    intake_output: dict[str, Any],
    *,
    stage_outputs: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build the parallel v2 projection contract without changing the v1 preview seam."""
    stage_outputs = stage_outputs or {}
    signal_projection = _build_signal_projection(intake_output, stage_outputs=stage_outputs, run_id=run_id)
    case_patch = _build_case_patch(
        intake_output,
        signal_projection=signal_projection,
        stage_outputs=stage_outputs,
    )
    desk_note_patch = _build_desk_note_patch(
        intake_output,
        signal_projection=signal_projection,
        case_patch=case_patch,
        stage_outputs=stage_outputs,
    )
    decision_trace = _build_decision_trace(
        intake_output,
        signal_projection=signal_projection,
        case_patch=case_patch,
        desk_note_patch=desk_note_patch,
        stage_outputs=stage_outputs,
    )
    projection = {
        "signal_projection": signal_projection,
        "case_patch": case_patch,
        "desk_note_patch": desk_note_patch,
        "decision_trace": decision_trace,
    }
    validate_v2_shadow_projection(projection)
    return projection


def validate_v2_shadow_projection(obj: dict[str, Any]) -> dict[str, Any]:
    """Validate the minimal v2 projection contract produced by the runtime."""
    if not isinstance(obj, dict):
        raise ValueError("V2 shadow projection must be a JSON object.")

    signal_projection = _require_object(obj.get("signal_projection"), "signal_projection")
    case_patch = _require_object(obj.get("case_patch"), "case_patch")
    desk_note_patch = _require_object(obj.get("desk_note_patch"), "desk_note_patch")
    decision_trace = _require_object(obj.get("decision_trace"), "decision_trace")

    signal_id = str(signal_projection.get("signal_id") or "").strip()
    if not signal_id:
        raise ValueError("signal_projection.signal_id must be present.")

    case_command = str(case_patch.get("command") or "").strip()
    if case_command not in ALLOWED_CASE_COMMANDS:
        raise ValueError(f"Unsupported case_patch.command: {case_command or '<missing>'}")

    desk_command = str(desk_note_patch.get("command") or "").strip()
    if desk_command not in ALLOWED_DESK_NOTE_COMMANDS:
        raise ValueError(f"Unsupported desk_note_patch.command: {desk_command or '<missing>'}")

    presence_mode = str(desk_note_patch.get("presence_mode") or "").strip()
    if presence_mode not in ALLOWED_PRESENCE_MODES:
        raise ValueError(f"Unsupported desk_note_patch.presence_mode: {presence_mode or '<missing>'}")

    lifecycle = str(desk_note_patch.get("lifecycle") or "").strip()
    if lifecycle not in ALLOWED_LIFECYCLES:
        raise ValueError(f"Unsupported desk_note_patch.lifecycle: {lifecycle or '<missing>'}")

    if signal_id not in _normalize_string_list(desk_note_patch.get("source_signal_ids")):
        raise ValueError("desk_note_patch.source_signal_ids must include signal_projection.signal_id.")

    if str(decision_trace.get("trigger_signal_id") or "").strip() != signal_id:
        raise ValueError("decision_trace.trigger_signal_id must match signal_projection.signal_id.")

    if str(decision_trace.get("presence_mode") or "").strip() != presence_mode:
        raise ValueError("decision_trace.presence_mode must match desk_note_patch.presence_mode.")

    return obj


def _build_signal_projection(
    intake_output: dict[str, Any],
    *,
    stage_outputs: dict[str, Any],
    run_id: str | None,
) -> dict[str, Any]:
    source = intake_output.get("source") or {}
    message = intake_output.get("message") or {}
    thread = intake_output.get("thread") or {}
    review = intake_output.get("review") or {}
    confidence = intake_output.get("confidence") if isinstance(intake_output.get("confidence"), dict) else {}
    case_assessment = intake_output.get("case_assessment") or {}
    preclassification = stage_outputs.get("preclassification_result") or {}

    message_id = str(message.get("message_id") or "").strip()
    thread_id = str(thread.get("thread_id") or "").strip()
    canonical_signal_id = str(stage_outputs.get("canonical_signal_id") or "").strip()
    signal_id = canonical_signal_id or _stable_id("sig", message_id)

    return {
        "signal_id": signal_id,
        "observed_at": _observed_at(intake_output),
        "source_kind": str(source.get("channel") or "gmail"),
        "source_ref": {
            "mailbox": str(source.get("mailbox") or ""),
            "message_id": message_id,
            "thread_id": thread_id,
            "received_at": str(message.get("date") or ""),
        },
        "intake": {
            "decision_action": str((intake_output.get("decision") or {}).get("action") or ""),
            "business_area": str(intake_output.get("business_area") or ""),
            "case_family": str(case_assessment.get("case_family") or "unknown"),
            "state_detected": str(case_assessment.get("state_detected") or "none"),
            "state_change_detected": bool((case_assessment.get("state_change") or {}).get("detected")),
            "primary_signal_code": str((intake_output.get("primary_signal") or {}).get("code") or ""),
            "primary_signal_name": str((intake_output.get("primary_signal") or {}).get("name") or ""),
            "review_required": bool(review.get("required")),
            "review_flags": _normalize_string_list(review.get("flags")),
            "preclassification_lane": str(preclassification.get("lane") or "intake_llm"),
        },
        "confidence": {
            "signal_confidence": _bounded_float(confidence.get("signal_confidence")),
            "case_link_confidence": _bounded_float(confidence.get("case_link_confidence")),
            "decision_confidence": _bounded_float(confidence.get("decision_confidence")),
            "extraction_confidence": _bounded_float(confidence.get("extraction_confidence")),
        },
        "artifacts": {
            "run_id": str(run_id or "").strip(),
            "shadow_contract": "daszek_v2_projection",
        },
    }


def _primary_summary_pl_from_case_snapshot_hot_state(
    stage_outputs: dict[str, Any],
    intake_output: dict[str, Any],
) -> str:
    """Prefer formal Hot State summary for desk/case UI; fall back to legacy intake summary."""
    mm = stage_outputs.get("mailbox_memory_result") or {}
    if isinstance(mm, dict):
        hot = mm.get("case_snapshot_hot_state")
        if isinstance(hot, dict):
            case = hot.get("case") if isinstance(hot.get("case"), dict) else {}
            text = str(case.get("summary_text") or "").strip()
            if text:
                return text
    return _case_summary_pl(intake_output)


def _primary_case_snapshot_projection_data(
    stage_outputs: dict[str, Any],
) -> dict[str, Any]:
    """Prefer formal Hot State arrays for projection payloads; fall back to compatibility snapshot shape."""
    mailbox_memory = stage_outputs.get("mailbox_memory_result") or {}
    case_context_pack = mailbox_memory.get("context_pack") if isinstance(mailbox_memory, dict) else {}
    if not isinstance(case_context_pack, dict):
        case_context_pack = {}
    case_snapshot = case_context_pack.get("snapshot") if isinstance(case_context_pack.get("snapshot"), dict) else {}
    hot_state = mailbox_memory.get("case_snapshot_hot_state") if isinstance(mailbox_memory, dict) else {}
    if not isinstance(hot_state, dict):
        hot_state = {}

    key_facts = list(hot_state.get("key_facts") or case_snapshot.get("key_facts") or [])
    conflicting_facts = list(hot_state.get("active_conflicts") or case_snapshot.get("conflicting_facts") or [])
    latest_documents = list(hot_state.get("documents_summary") or case_snapshot.get("latest_documents") or [])

    effective_case_snapshot = dict(case_snapshot)
    if key_facts:
        effective_case_snapshot["key_facts"] = key_facts
    if conflicting_facts:
        effective_case_snapshot["conflicting_facts"] = conflicting_facts
    if latest_documents:
        effective_case_snapshot["latest_documents"] = latest_documents

    return {
        "case_context_pack": case_context_pack,
        "case_snapshot": effective_case_snapshot,
        "key_facts": key_facts,
        "conflicting_facts": conflicting_facts,
        "latest_documents": latest_documents,
    }


def _case_guidance_projection_slice(cg: dict[str, Any]) -> dict[str, Any]:
    """Projection-safe case_guidance slice: EvidenceRef metadata only; strip raw sidecar text."""
    c = cg if isinstance(cg, dict) else {}
    sm = str(c.get("source_mode") or "")
    return {
        "evidence_refs": normalize_case_guidance_evidence_refs(c.get("evidence_refs") or [], source_mode=sm),
        "assumptions": list(c.get("assumptions") or []),
        "unsupported_claims": list(c.get("unsupported_claims") or []),
        "conflict_refs": strip_forbidden_evidence_like_rows(c.get("conflict_refs") or []),
    }


def _build_case_patch(
    intake_output: dict[str, Any],
    *,
    signal_projection: dict[str, Any],
    stage_outputs: dict[str, Any],
) -> dict[str, Any]:
    case_assessment = intake_output.get("case_assessment") or {}
    action = str((intake_output.get("decision") or {}).get("action") or "")
    intelligence_result = stage_outputs.get("case_intelligence_result") or {}
    case_understanding = intelligence_result.get("case_understanding") or {}
    operator_brief = intelligence_result.get("operator_brief") or {}
    next_best_action = (intelligence_result.get("next_best_action") or {}).get("primary_next_action") or {}
    missing_info = intelligence_result.get("missing_info") or {}
    risk_assessment = intelligence_result.get("risk_assessment") or {}
    merge_split = intelligence_result.get("merge_split_suggestions") or {}
    case_key_info = resolve_case_key_metadata(intake_output)
    projection_state = _primary_case_snapshot_projection_data(stage_outputs)
    case_context_pack = projection_state["case_context_pack"]
    case_snapshot = projection_state["case_snapshot"]
    drive_projection = _drive_projection_fields(case_context_pack, case_snapshot)
    case_family = str(case_assessment.get("case_family") or "unknown")
    case_link_result = stage_outputs.get("case_link_result") or {}
    case_link_decision = str(case_link_result.get("decision") or "").strip()
    selected_case_key = str(case_link_result.get("selected_case_key") or "").strip()
    projected_case_key = str(case_key_info.get("case_key") or "").strip()
    case_key = selected_case_key if case_link_decision in {"linked", "weak_link", "competing_links"} and selected_case_key else projected_case_key
    case_key_source = "linked" if case_key == selected_case_key and case_key else str(case_key_info.get("case_key_source") or "none")
    state_change = case_assessment.get("state_change") or {}
    command = _case_command(action, case_key=case_key, case_family=case_family, state_change=state_change)
    effective_case_key = case_key
    existing_case_id = (
        str(case_snapshot.get("case_id") or "").strip()
        or str(case_understanding.get("case_id") or "").strip()
    )
    case_id = existing_case_id or _stable_id(
        "case",
        effective_case_key
        or f"{case_family}:{signal_projection['source_ref']['thread_id'] or signal_projection['source_ref']['message_id']}",
    )

    cg = intelligence_result.get("case_guidance") or {}
    understanding_quality = build_understanding_quality_projection(intelligence_result)
    readiness_facets = build_readiness_facets_projection(
        intelligence_result,
        projection_state={
            "conflicting_facts": projection_state["conflicting_facts"],
            "completeness_gaps": drive_projection.get("completeness_gaps") or [],
        },
    )
    # Roadmap 2.2: composed verdict alongside — never instead of — the thin facets.
    case_readiness = build_case_readiness_projection(
        intelligence_result,
        readiness_facets=readiness_facets,
    )
    case_patch = {
        "command": command,
        "case_id": case_id,
        "case_key": effective_case_key,
        "family": case_family,
        "business_area": str(case_understanding.get("business_area") or intake_output.get("business_area") or ""),
        "business_priority": str(case_understanding.get("business_priority") or intake_output.get("priority") or "low"),
        "status": "open" if command != "noop" else "none",
        "current_state": str(case_assessment.get("state_detected") or "none"),
        "state_confidence": _bounded_float(case_understanding.get("state_confidence")),
        "state_change_to": str(state_change.get("to_state") or ""),
        "title_pl": _case_title_pl(intake_output),
        "summary_pl": _primary_summary_pl_from_case_snapshot_hot_state(stage_outputs, intake_output),
        "operator_brief_pl": str(operator_brief.get("brief_pl") or ""),
        "latest_meaningful_change_pl": str(case_understanding.get("latest_meaningful_change") or ""),
        "attention_reason_pl": str(case_understanding.get("attention_reason") or ""),
        "operational_status": str(cg.get("operational_status") or ""),
        "waiting_for": str(cg.get("waiting_for") or ""),
        "guidance_reason_summary_pl": str(cg.get("reason_summary_pl") or ""),
        "case_guidance": _case_guidance_projection_slice(cg),
        "blocker_summary_pl": str(cg.get("blocker_summary_pl") or ""),
        "momentum": str(cg.get("momentum") or ""),
        "stagnation_flag": bool(cg.get("stagnation_flag", False)),
        "stagnation_reason_pl": str(cg.get("stagnation_reason_pl") or ""),
        "business_readiness": str(cg.get("business_readiness") or ""),
        "operator_attention_class": str(cg.get("operator_attention_class") or ""),
        "next_step_hint_pl": str(cg.get("next_step_hint_pl") or ""),
        "guidance_confidence": _bounded_float(cg.get("confidence")),
        "primary_next_action_type": str(next_best_action.get("action_type") or ""),
        "primary_next_action_title_pl": str(next_best_action.get("title_pl") or ""),
        "primary_next_action_reason_pl": str(next_best_action.get("reason_pl") or ""),
        "missing_info_summary_pl": str(missing_info.get("summary_pl") or ""),
        "risk_summary_pl": str(risk_assessment.get("summary_pl") or ""),
        "blockers": list(case_understanding.get("blockers") or []),
        "risks": list(risk_assessment.get("risks") or []),
        "missing_info": list(case_understanding.get("missing_info") or []),
        "merge_candidates": list(merge_split.get("merge_candidates") or []),
        "split_suspicions": list(merge_split.get("split_suspicions") or []),
        "case_snapshot": case_snapshot,
        "key_facts": list(projection_state["key_facts"] or []),
        "latest_documents": list(projection_state["latest_documents"] or []),
        "conflicting_facts": list(projection_state["conflicting_facts"] or []),
        "drive_documents_summary": drive_projection["drive_documents_summary"],
        "completeness_gaps": drive_projection["completeness_gaps"],
        "graph_hints": drive_projection["graph_hints"],
        "reference_documents": drive_projection["reference_documents"],
        "warranty_service_state": drive_projection["warranty_service_state"],
        "media_evidence_presence": drive_projection["media_evidence_presence"],
        "related_entities": drive_projection["related_entities"],
        "operator_visible_conflicts": drive_projection["operator_visible_conflicts"],
        "evidence_cards": drive_projection["evidence_cards"],
        "service_signals": drive_projection["service_signals"],
        "marketing_signals": drive_projection["marketing_signals"],
        "action_proposals": list(case_context_pack.get("action_proposals") or []),
        "execution_results": list(case_context_pack.get("execution_results") or []),
        "calendar": dict(case_context_pack.get("calendar") or {}),
        "document_intelligence": dict(case_context_pack.get("document_intelligence") or {}),
        "source_refs": list(case_context_pack.get("source_refs") or []),
        "review_required": bool(case_understanding.get("review_required", False)),
        "review_flags": list(case_understanding.get("review_flags") or []),
        "latest_signal_id": signal_projection["signal_id"],
        "case_link_decision": case_link_decision or "no_link",
        "case_key_source": case_key_source,
        "readiness_facets": readiness_facets,
        "case_readiness": case_readiness,
    }
    if understanding_quality is not None:
        case_patch["understanding_quality"] = understanding_quality
    return case_patch


def _build_desk_note_patch(
    intake_output: dict[str, Any],
    *,
    signal_projection: dict[str, Any],
    case_patch: dict[str, Any],
    stage_outputs: dict[str, Any],
) -> dict[str, Any]:
    action = str((intake_output.get("decision") or {}).get("action") or "")
    action_plan = stage_outputs.get("action_plan_result") or {}
    business_result = stage_outputs.get("business_reasoning_result") or {}
    case_link_result = stage_outputs.get("case_link_result") or {}
    case_assessment = intake_output.get("case_assessment") or {}
    intelligence_result = stage_outputs.get("case_intelligence_result") or {}
    desk_composition = intelligence_result.get("desk_composition") or {}
    operator_brief = intelligence_result.get("operator_brief") or {}
    next_best_action = (intelligence_result.get("next_best_action") or {}).get("primary_next_action") or {}
    missing_info = intelligence_result.get("missing_info") or {}
    risk_assessment = intelligence_result.get("risk_assessment") or {}
    merge_split = intelligence_result.get("merge_split_suggestions") or {}
    feedback_learning_memory = intelligence_result.get("feedback_learning_memory") or {}
    case_understanding = intelligence_result.get("case_understanding") or {}
    att_intel = intelligence_result.get("attachment_intelligence") or {}
    thread_mem = intelligence_result.get("thread_memory") or {}
    review_routing = intelligence_result.get("review_routing") or {}
    automation_policy = intelligence_result.get("automation_policy") or {}
    cg = intelligence_result.get("case_guidance") or {}
    projection_state = _primary_case_snapshot_projection_data(stage_outputs)
    case_context_pack = projection_state["case_context_pack"]
    case_snapshot = projection_state["case_snapshot"]
    drive_projection = _drive_projection_fields(case_context_pack, case_snapshot)

    presence_mode = _presence_mode(
        intake_output,
        action_plan=action_plan,
        business_result=business_result,
        intelligence_result=intelligence_result,
    )
    command = _desk_note_command(
        action,
        action_plan=action_plan,
        case_link_result=case_link_result,
        presence_mode=presence_mode,
        intelligence_result=intelligence_result,
    )
    note_anchor = (
        str(case_patch.get("case_id") or "").strip()
        or str(case_patch.get("case_key") or "").strip()
        or str(signal_projection["source_ref"].get("thread_id") or "").strip()
        or str(signal_projection["source_ref"].get("message_id") or "").strip()
    )
    desk_note_id = _stable_id("note", note_anchor)
    desk_case_id = (
        str(case_patch.get("case_id") or "").strip()
        or str(case_understanding.get("case_id") or "").strip()
    )
    understanding_quality = build_understanding_quality_projection(intelligence_result)
    readiness_facets = build_readiness_facets_projection(
        intelligence_result,
        projection_state={
            "conflicting_facts": projection_state["conflicting_facts"],
            "completeness_gaps": drive_projection.get("completeness_gaps") or [],
        },
    )
    case_readiness = build_case_readiness_projection(
        intelligence_result,
        readiness_facets=readiness_facets,
    )

    desk_note_patch = {
        "command": command,
        "desk_note_id": desk_note_id,
        "case_id": desk_case_id,
        "presence_mode": presence_mode,
        "surface_zone": str(desk_composition.get("surface_zone") or ("silent" if presence_mode == "silent" else "desk")),
        "day_bucket": str(desk_composition.get("day_bucket") or _default_day_bucket(presence_mode)),
        "lifecycle": _desk_note_lifecycle(command, intelligence_result=intelligence_result),
        "title_pl": str(desk_composition.get("title_pl") or _desk_note_title_pl(intake_output)),
        "summary_pl": _primary_summary_pl_from_case_snapshot_hot_state(stage_outputs, intake_output)
        or str(desk_composition.get("body_short_pl") or "").strip()
        or _desk_note_summary_pl(intake_output, action_plan=action_plan),
        "why_now_pl": str(desk_composition.get("body_reason_pl") or _why_now_pl(intake_output, action_plan=action_plan, business_result=business_result)),
        "recommended_next_step_pl": str(desk_composition.get("assistant_suggestion_pl") or _recommended_next_step_pl(action_plan=action_plan, business_result=business_result)),
        "assistant_suggestion_pl": str(desk_composition.get("assistant_suggestion_pl") or ""),
        "operator_brief_pl": str(operator_brief.get("brief_pl") or ""),
        "primary_next_action_type": str(next_best_action.get("action_type") or ""),
        "primary_next_action_title_pl": str(next_best_action.get("title_pl") or ""),
        "primary_next_action_reason_pl": str(next_best_action.get("reason_pl") or ""),
        "missing_info_summary_pl": str(missing_info.get("summary_pl") or ""),
        "customer_question_draft_pl": str(missing_info.get("customer_question_draft_pl") or ""),
        "operator_checklist_pl": list(missing_info.get("operator_checklist_pl") or []),
        "risk_summary_pl": str(risk_assessment.get("summary_pl") or ""),
        "risks": list(risk_assessment.get("risks") or []),
        "blockers": list(case_understanding.get("blockers") or []),
        "missing_info": list(case_understanding.get("missing_info") or []),
        "merge_candidates": list(merge_split.get("merge_candidates") or []),
        "split_suspicions": list(merge_split.get("split_suspicions") or []),
        "feedback_learning_memory": feedback_learning_memory,
        "visibility_score": float(desk_composition.get("visibility_score") or 0.0),
        "trace_summary_pl": str(desk_composition.get("trace_summary") or ""),
        "attachment_summary_pl": str(att_intel.get("summary_pl") or ""),
        "thread_summary_pl": str(thread_mem.get("canonical_thread_summary") or ""),
        "unresolved_questions": list(thread_mem.get("unresolved_questions") or []),
        "case_snapshot": case_snapshot,
        "key_facts": list(projection_state["key_facts"] or []),
        "latest_documents": list(projection_state["latest_documents"] or []),
        "conflicting_facts": list(projection_state["conflicting_facts"] or []),
        "drive_documents_summary": drive_projection["drive_documents_summary"],
        "completeness_gaps": drive_projection["completeness_gaps"],
        "graph_hints": drive_projection["graph_hints"],
        "reference_documents": drive_projection["reference_documents"],
        "warranty_service_state": drive_projection["warranty_service_state"],
        "media_evidence_presence": drive_projection["media_evidence_presence"],
        "related_entities": drive_projection["related_entities"],
        "operator_visible_conflicts": drive_projection["operator_visible_conflicts"],
        "evidence_cards": drive_projection["evidence_cards"],
        "service_signals": drive_projection["service_signals"],
        "marketing_signals": drive_projection["marketing_signals"],
        "action_proposals": list(case_context_pack.get("action_proposals") or []),
        "execution_results": list(case_context_pack.get("execution_results") or []),
        "calendar": dict(case_context_pack.get("calendar") or {}),
        "document_intelligence": dict(case_context_pack.get("document_intelligence") or {}),
        "source_refs": list(case_context_pack.get("source_refs") or []),
        "review_mode": str(review_routing.get("review_mode") or ""),
        "review_reason_pl": str(review_routing.get("review_reason_pl") or ""),
        "automation_policy": automation_policy if isinstance(automation_policy, dict) else {},
        "operational_status": str(cg.get("operational_status") or ""),
        "waiting_for": str(cg.get("waiting_for") or ""),
        "guidance_reason_summary_pl": str(cg.get("reason_summary_pl") or ""),
        "case_guidance": _case_guidance_projection_slice(cg),
        "blocker_summary_pl": str(cg.get("blocker_summary_pl") or ""),
        "stagnation_flag": bool(cg.get("stagnation_flag", False)),
        "operator_attention_class": str(cg.get("operator_attention_class") or ""),
        "next_step_hint_pl": str(cg.get("next_step_hint_pl") or ""),
        "guidance_confidence": _bounded_float(cg.get("confidence")),
        "source_signal_ids": [signal_projection["signal_id"]],
        "source_message_id": str(signal_projection["source_ref"].get("message_id") or ""),
        "case_family": str(case_assessment.get("case_family") or "unknown"),
        "business_priority": str(case_understanding.get("business_priority") or intake_output.get("priority") or "low"),
        "safe_for_live_push": bool(action_plan.get("safe_for_live_push", False)),
        "safe_for_operator_projection": bool(action_plan.get("safe_for_operator_projection", False)),
        "priority": str(intake_output.get("priority") or "low"),
        "readiness_facets": readiness_facets,
        "case_readiness": case_readiness,
    }
    if understanding_quality is not None:
        desk_note_patch["understanding_quality"] = understanding_quality
    return desk_note_patch


def _build_decision_trace(
    intake_output: dict[str, Any],
    *,
    signal_projection: dict[str, Any],
    case_patch: dict[str, Any],
    desk_note_patch: dict[str, Any],
    stage_outputs: dict[str, Any],
) -> dict[str, Any]:
    observed_at = _observed_at(intake_output)
    desk_note_id = str(desk_note_patch.get("desk_note_id") or "").strip()
    command = str(desk_note_patch.get("command") or "").strip()
    intelligence_result = stage_outputs.get("case_intelligence_result") or {}
    lifecycle_revision = intelligence_result.get("lifecycle_revision") or {}
    lifecycle_intent = str(lifecycle_revision.get("lifecycle_intent") or "").strip()
    target_zone = str(lifecycle_revision.get("target_surface_zone") or "").strip()
    decision_type = decision_type_from_command(
        command,
        lifecycle_intent=lifecycle_intent,
        target_zone=target_zone,
    )
    trace_subject_type = "desk_note" if desk_note_id else "signal"
    trace_subject_id = desk_note_id or signal_projection["signal_id"]

    return {
        "trace_id": _stable_id("trace", signal_projection["signal_id"], decision_type),
        "subject_type": trace_subject_type,
        "subject_id": trace_subject_id,
        "case_id": str(case_patch.get("case_id") or ""),
        "trigger_signal_id": signal_projection["signal_id"],
        "decision_type": decision_type,
        "actor": "ai",
        "reason_summary_pl": str(desk_note_patch.get("trace_summary_pl") or "").strip()
        or _why_now_pl(
            intake_output,
            action_plan=stage_outputs.get("action_plan_result") or {},
            business_result=stage_outputs.get("business_reasoning_result") or {},
        ),
        "presence_mode": str(desk_note_patch.get("presence_mode") or "silent"),
        "created_at": observed_at,
    }


def _drive_projection_fields(case_context_pack: dict[str, Any], case_snapshot: dict[str, Any]) -> dict[str, Any]:
    drive_documents_summary = list(case_context_pack.get("drive_documents_summary") or case_snapshot.get("drive_documents_summary") or [])
    completeness_gaps = list(case_context_pack.get("completeness_gaps") or case_snapshot.get("completeness_gaps") or [])
    graph_hints = list(case_context_pack.get("graph_hints") or case_snapshot.get("graph_hints") or [])
    reference_documents = list(case_context_pack.get("reference_documents") or case_snapshot.get("reference_documents") or [])
    vnext = build_case_context_pack_vnext(case_context_pack)
    return {
        "drive_documents_summary": drive_documents_summary,
        "completeness_gaps": completeness_gaps,
        "graph_hints": graph_hints,
        "reference_documents": reference_documents,
        "warranty_service_state": _derive_warranty_service_state(case_snapshot, drive_documents_summary),
        "media_evidence_presence": _derive_media_evidence_presence(drive_documents_summary),
        "related_entities": _derive_related_entities(graph_hints),
        "operator_visible_conflicts": _derive_operator_visible_conflicts(case_snapshot, completeness_gaps),
        "evidence_cards": list(vnext.get("evidence_cards") or []),
        "service_signals": list(vnext.get("service_signals") or []),
        "marketing_signals": list(vnext.get("marketing_signals") or []),
    }


def _derive_warranty_service_state(case_snapshot: dict[str, Any], drive_documents_summary: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = {str(item.get("document_kind") or "") for item in drive_documents_summary}
    key_facts = list(case_snapshot.get("key_facts") or [])
    warranty_term = _fact_value(key_facts, "warranty_term")
    service_frequency = _fact_value(key_facts, "service_frequency")
    has_warranty = "warranty_card" in kinds or bool(warranty_term)
    has_service_protocol = "service_protocol" in kinds
    state = "unknown"
    if has_warranty and has_service_protocol:
        state = "warranty_and_service"
    elif has_warranty:
        state = "warranty_only"
    elif has_service_protocol:
        state = "service_only"
    return {
        "state": state,
        "has_warranty_card": has_warranty,
        "has_service_protocol": has_service_protocol,
        "warranty_term": warranty_term,
        "service_frequency": service_frequency,
    }


def _derive_media_evidence_presence(drive_documents_summary: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = [str(item.get("document_kind") or "") for item in drive_documents_summary]
    lanes = [str(item.get("lane") or "") for item in drive_documents_summary]
    media_asset_count = sum(1 for kind in kinds if kind == "media_asset")
    return {
        "evidence_present": any(kind in {"media_bundle", "media_asset", "scan_backlog"} for kind in kinds),
        "has_media_bundle": "media_bundle" in kinds,
        "media_asset_count": media_asset_count,
        "scan_backlog_present": "scan_backlog" in kinds or "scans_intake" in lanes,
    }


def _derive_related_entities(graph_hints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "relation_type": str(item.get("relation_type") or ""),
            "related_title": str(item.get("related_title") or ""),
            "related_node_type": str(item.get("related_node_type") or ""),
            "confidence": _bounded_float(item.get("confidence")),
        }
        for item in graph_hints[:10]
    ]


def _derive_operator_visible_conflicts(case_snapshot: dict[str, Any], completeness_gaps: list[str]) -> list[dict[str, Any]]:
    conflicts = [
        {
            "kind": "conflicting_fact",
            "fact_key": str(item.get("fact_key") or ""),
            "values": list(item.get("values") or []),
            "summary_pl": f"Konflikt danych dla {str(item.get('fact_key') or '')}: {', '.join(item.get('values') or [])}",
        }
        for item in list(case_snapshot.get("conflicting_facts") or [])[:8]
    ]
    gaps = [
        {
            "kind": "completeness_gap",
            "fact_key": "",
            "values": [],
            "summary_pl": str(gap or ""),
        }
        for gap in completeness_gaps[:8]
    ]
    return conflicts + gaps


def _fact_value(facts: list[dict[str, Any]], fact_key: str) -> str:
    for item in facts:
        if str(item.get("fact_key") or "") == fact_key:
            return str(item.get("value") or "")
    return ""


def _case_command(action: str, *, case_key: str, case_family: str, state_change: dict[str, Any]) -> str:
    if not action or action in {"ignore", "mark_reference"}:
        return "noop"
    if action == "update_case_state" and case_key:
        return "update_state"
    if action == "append_to_existing_case" and case_key:
        return "update_state" if bool(state_change.get("detected")) else "upsert_case"
    return "upsert_case"


def _desk_note_command(
    action: str,
    *,
    action_plan: dict[str, Any],
    case_link_result: dict[str, Any],
    presence_mode: str,
    intelligence_result: dict[str, Any],
) -> str:
    lifecycle_revision = intelligence_result.get("lifecycle_revision") or {}
    lifecycle_intent = str(lifecycle_revision.get("lifecycle_intent") or "").strip()
    target_zone = str(lifecycle_revision.get("target_surface_zone") or "").strip()
    semantic_command = command_from_lifecycle_intent(lifecycle_intent, target_zone)
    if semantic_command:
        return semantic_command
    projection_mode = str(action_plan.get("daszek_projection_mode") or "")
    case_link_decision = str(case_link_result.get("decision") or "")
    if action == "ignore" or projection_mode == "ignore" or presence_mode == "silent":
        return "suppress"
    if action == "mark_reference":
        return "suppress"
    if action in {"append_to_existing_case", "update_case_state"} and case_link_decision in {"linked", "weak_link"}:
        return "update"
    return "create"


def _presence_mode(
    intake_output: dict[str, Any],
    *,
    action_plan: dict[str, Any],
    business_result: dict[str, Any],
    intelligence_result: dict[str, Any],
) -> str:
    desk_composition = intelligence_result.get("desk_composition") or {}
    candidate_presence = str(desk_composition.get("presence_mode") or "").strip()
    if candidate_presence in ALLOWED_PRESENCE_MODES:
        return candidate_presence

    action = str((intake_output.get("decision") or {}).get("action") or "")
    priority = str(intake_output.get("priority") or "low")
    review_required = bool((intake_output.get("review") or {}).get("required"))
    urgency = str(business_result.get("urgency") or "normal")
    primary_action = str(action_plan.get("primary_action") or "")

    if action == "ignore" or primary_action == "ignore":
        return "silent"
    if action == "mark_reference":
        return "silent"
    if action == "mark_watchlist":
        return "subtle"
    if priority == "critical" or (urgency == "high" and review_required):
        return "alarm"
    if priority == "high" or primary_action == "create_review" or urgency == "high":
        return "strong"
    if primary_action == "prepare_reply":
        return "advisory"
    return "standard"


def _desk_note_lifecycle(command: str, *, intelligence_result: dict[str, Any]) -> str:
    lifecycle_revision = intelligence_result.get("lifecycle_revision") or {}
    target_zone = str(lifecycle_revision.get("target_surface_zone") or "").strip()
    if target_zone == "case_only":
        return "active"
    if command == "suppress":
        return "withdrawn"
    if command == "withdraw":
        return "withdrawn"
    if command == "resolve":
        return "resolved_by_ai"
    if command == "merge":
        return "merged"
    return "active"


def _desk_note_title_pl(intake_output: dict[str, Any]) -> str:
    subject = str((intake_output.get("message") or {}).get("subject") or "").strip()
    if subject:
        return subject
    signal_name = str((intake_output.get("primary_signal") or {}).get("name") or "").strip()
    if signal_name:
        return signal_name
    return "Temat operacyjny"


def _desk_note_summary_pl(intake_output: dict[str, Any], *, action_plan: dict[str, Any]) -> str:
    action = str((intake_output.get("decision") or {}).get("action") or "")
    primary_action = str(action_plan.get("primary_action") or "")
    if action == "mark_reference":
        return "AI uznało ten sygnał za informacyjny i zostawia go poza głównym biurkiem."
    if action == "mark_watchlist":
        return "AI zostawia temat do cichej obserwacji bez zajmowania głównego biurka."
    if primary_action == "create_review" or action == "review":
        return "AI wykryło temat wymagający ręcznej oceny operatora."
    if action in {"append_to_existing_case", "update_case_state"}:
        return "AI rozpoznało kontynuację istniejącej sprawy i aktualizuje jej kontekst operacyjny."
    if action in {"create_case", "create_case_and_task"}:
        return "AI rozpoznało nową sprawę operacyjną wymagającą dalszego prowadzenia."
    if action == "create_task" or primary_action == "create_task":
        return "AI rozpoznało samodzielny temat operacyjny wymagający działania."
    return "AI rozpoznało temat, ale nie widzi potrzeby silnej ekspozycji na biurku."


def _why_now_pl(
    intake_output: dict[str, Any],
    *,
    action_plan: dict[str, Any],
    business_result: dict[str, Any],
) -> str:
    priority = str(intake_output.get("priority") or "low")
    business_area = BUSINESS_AREA_LABELS_PL.get(str(intake_output.get("business_area") or ""), "operacje")
    review_required = bool((intake_output.get("review") or {}).get("required"))
    primary_action = str(action_plan.get("primary_action") or "")
    urgency = str(business_result.get("urgency") or "normal")

    if priority == "critical" or urgency == "high":
        return f"Temat w obszarze {business_area} ma wysoką pilność i wymaga szybkiej decyzji."
    if review_required or primary_action == "create_review":
        return f"AI nie uznało automatycznej interpretacji za wystarczająco bezpieczną dla obszaru {business_area}."
    if primary_action == "prepare_reply":
        return f"Temat w obszarze {business_area} wymaga kontaktu zwrotnego lub zebrania danych."
    if str((intake_output.get("decision") or {}).get("action") or "") in {"append_to_existing_case", "update_case_state"}:
        return f"AI powiązało sygnał z istniejącą sprawą i uznało, że trzeba zaktualizować jej stan."
    return f"AI uznało temat w obszarze {business_area} za operacyjnie istotny na teraz."


def _recommended_next_step_pl(*, action_plan: dict[str, Any], business_result: dict[str, Any]) -> str:
    business_action = str(business_result.get("recommended_next_action") or "").strip()
    primary_action = str(action_plan.get("primary_action") or "").strip()
    return (
        NEXT_STEP_LABELS_PL.get(primary_action)
        or NEXT_STEP_LABELS_PL.get(business_action)
        or "Sprawdź temat ręcznie i zdecyduj o następnym kroku."
    )


def _default_day_bucket(presence_mode: str) -> str:
    if presence_mode in {"alarm", "strong"}:
        return "teraz"
    if presence_mode in {"advisory", "standard"}:
        return "dzisiaj"
    return "w_najblizszym_czasie"


def _case_title_pl(intake_output: dict[str, Any]) -> str:
    family = str((intake_output.get("case_assessment") or {}).get("case_family") or "unknown")
    subject = str((intake_output.get("message") or {}).get("subject") or "").strip()
    if subject:
        return subject
    if family != "unknown":
        return f"Sprawa: {family.replace('_', ' ')}"
    return "Sprawa operacyjna"


def _case_summary_pl(intake_output: dict[str, Any]) -> str:
    business_area = BUSINESS_AREA_LABELS_PL.get(str(intake_output.get("business_area") or ""), "operacje")
    state = str((intake_output.get("case_assessment") or {}).get("state_detected") or "none")
    if state and state != "none":
        return f"Sprawa dotyczy obszaru {business_area} i obecnie ma stan: {state.replace('_', ' ')}."
    return f"Sprawa dotyczy obszaru {business_area} i wymaga pamięci operacyjnej."


def _observed_at(intake_output: dict[str, Any]) -> str:
    source = intake_output.get("source") or {}
    message = intake_output.get("message") or {}
    return str(source.get("observed_at") or message.get("date") or "")


def _require_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    return value


def _normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            normalized.append(text)
    return normalized


def _bounded_float(value: Any) -> float:
    try:
        return round(max(0.0, min(1.0, float(value or 0.0))), 4)
    except (TypeError, ValueError):
        return 0.0


def _stable_id(prefix: str, *parts: str) -> str:
    seed = "::".join(str(part or "").strip() for part in parts if str(part or "").strip())
    if not seed:
        seed = prefix
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


__all__ = [
    "build_v2_shadow_projection",
    "validate_v2_shadow_projection",
]
