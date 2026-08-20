"""Orchestrator — top-level case intelligence pipeline."""
from __future__ import annotations
import hashlib
from typing import Any

from .desk import build_desk_composition, merge_case_guidance_into_intelligence
from .lifecycle import build_feedback_learning_memory, build_lifecycle_revision, build_merge_split_suggestions
from .missing_info import build_missing_info
from .next_best_action import build_next_best_action
from .risks import build_risk_assessment
from .understanding import build_case_operator_brief, build_case_understanding_snapshot
from .validators import validate_case_intelligence_result


def apply_hot_state_to_case_intelligence(
    intelligence: dict[str, Any],
    hot_state: dict[str, Any] | None,
) -> dict[str, Any]:
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
    cu["cold_evidence_pointers_hot"] = hot_state.get("cold_evidence_pointers") if isinstance(hot_state.get("cold_evidence_pointers"), dict) else {}
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
    feedback_learning_memory = build_feedback_learning_memory(feedback_memory_seed)
    merge_split_suggestions = build_merge_split_suggestions(
        snapshot=snapshot, intake_result=intake_result, case_link_result=case_link_result or {})
    missing_info = build_missing_info(
        intake_result=intake_result, business_result=business_result or {},
        reply_result=reply_result or {}, case_link_result=case_link_result or {},
        attachment_intelligence=attachment_intelligence or {}, thread_memory=thread_memory or {},
        case_context_pack=case_context_pack or {})
    risk_assessment = build_risk_assessment(
        intake_result=intake_result, business_result=business_result or {},
        missing_info=missing_info, current_note_state=current_note_state or {},
        attachment_intelligence=attachment_intelligence or {}, thread_memory=thread_memory or {})
    next_best_action = build_next_best_action(
        intake_result=intake_result, case_link_result=case_link_result or {},
        business_result=business_result or {}, reply_result=reply_result or {},
        action_plan_result=action_plan_result or {}, missing_info=missing_info,
        merge_split_suggestions=merge_split_suggestions)
    case_understanding = build_case_understanding_snapshot(
        snapshot=snapshot, intake_result=intake_result, case_link_result=case_link_result or {},
        business_result=business_result or {}, next_best_action=next_best_action,
        missing_info=missing_info, risk_assessment=risk_assessment,
        merge_split_suggestions=merge_split_suggestions, case_context_pack=case_context_pack or {})
    operator_brief = build_case_operator_brief(
        case_understanding=case_understanding, next_best_action=next_best_action,
        missing_info=missing_info, risk_assessment=risk_assessment)
    desk_composition = build_desk_composition(
        intake_result=intake_result, business_result=business_result or {},
        case_understanding=case_understanding, next_best_action=next_best_action,
        missing_info=missing_info, risk_assessment=risk_assessment,
        merge_split_suggestions=merge_split_suggestions, feedback_learning_memory=feedback_learning_memory,
        preclassification_result=preclassification_result)
    lifecycle_revision = build_lifecycle_revision(
        intake_result=intake_result, case_link_result=case_link_result or {},
        case_understanding=case_understanding, desk_composition=desk_composition,
        current_note_state=current_note_state or {})

    result = {
        "case_understanding": case_understanding, "operator_brief": operator_brief,
        "next_best_action": next_best_action, "missing_info": missing_info,
        "risk_assessment": risk_assessment, "merge_split_suggestions": merge_split_suggestions,
        "desk_composition": desk_composition, "lifecycle_revision": lifecycle_revision,
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
            case_context_pack=case_context_pack or {})
    normalized["execution_metadata"] = {
        "stage_name": "case_intelligence", "shadow_only": True,
        "input_primary_action": str((action_plan_result or {}).get("primary_action") or ""),
        "input_business_next_action": str((business_result or {}).get("recommended_next_action") or ""),
        "input_reply_draft_enabled": bool((reply_result or {}).get("draft_enabled")),
        "input_case_link_decision": str((case_link_result or {}).get("decision") or ""),
    }
    return normalized


def merge_data(
    case_a: dict[str, Any],
    case_b: dict[str, Any],
    *,
    merge_log: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    log: list[dict[str, Any]] = list(merge_log or [])
    conflicts: list[str] = []
    result_data: dict[str, Any] = {}

    facts_a = list(case_a.get("facts") or case_a.get("key_facts") or [])
    facts_b = list(case_b.get("facts") or case_b.get("key_facts") or [])
    merged_facts: list[dict[str, Any]] = []
    seen_fact_values: set[tuple[str, str]] = set()
    values_by_key: dict[str, set[str]] = {}
    for fact in facts_a + facts_b:
        key = str(fact.get("fact_key") or fact.get("key") or "").strip()
        if not key:
            continue
        value = str(fact.get("normalized_value") or fact.get("value") or "").strip()
        identity = (key, value)
        if identity in seen_fact_values:
            continue
        known_values = values_by_key.setdefault(key, set())
        if value and known_values and value not in known_values:
            previous = sorted(known_values)[0]
            conflicts.append(f"fact_key={key!r}: '{previous}' vs '{value}' -- kept both as conflict")
        if value:
            known_values.add(value)
        seen_fact_values.add(identity)
        merged_facts.append(dict(fact))
    merged_facts_list = sorted(merged_facts, key=lambda f: str(f.get("observed_at") or ""), reverse=True)

    docs_a = list(case_a.get("documents") or case_a.get("docs") or [])
    docs_b = list(case_b.get("documents") or case_b.get("docs") or [])
    seen_docs: set[str] = set()
    merged_docs: list[dict[str, Any]] = []
    for doc in docs_a + docs_b:
        doc_id = str(doc.get("document_id") or doc.get("file_id") or doc.get("id") or "").strip()
        if doc_id and doc_id not in seen_docs:
            seen_docs.add(doc_id)
            merged_docs.append(dict(doc))

    history_a = list(case_a.get("history") or case_a.get("events") or [])
    history_b = list(case_b.get("history") or case_b.get("events") or [])
    seen_history_hashes: set[str] = set()
    merged_history: list[dict[str, Any]] = []
    for event in history_a + history_b:
        event_str = str(event)
        event_hash = hashlib.sha256(event_str.encode("utf-8")).hexdigest()
        if event_hash not in seen_history_hashes:
            seen_history_hashes.add(event_hash)
            merged_history.append(dict(event))
    merged_history.sort(key=lambda e: str(e.get("timestamp") or e.get("observed_at") or ""))

    result_data["facts"] = merged_facts_list
    result_data["documents"] = merged_docs
    result_data["history"] = merged_history
    result_data["merged_facts"] = len(merged_facts_list)
    result_data["merged_documents"] = len(merged_docs)
    result_data["merged_history"] = len(merged_history)

    log.append({
        "action": "merge_data", "case_a": case_a.get("case_id", "?"), "case_b": case_b.get("case_id", "?"),
        "conflicts": list(conflicts),
        "counts": {"facts": len(merged_facts_list), "documents": len(merged_docs), "history": len(merged_history)},
    })
    return {
        "merged": result_data, "merge_log": log, "conflicts": conflicts,
        "merged_facts": len(merged_facts_list), "merged_documents": len(merged_docs), "merged_history": len(merged_history),
    }
