"""UnderstandingOutput v1: projection-safe situation understanding."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from case_context_contract import (
    normalize_evidence_refs,
    operator_feed_conflicting_fact,
    operator_feed_plain_summary,
)
from context_quality_contract import normalize_context_quality
from log_config import get_logger

logger = get_logger("understanding_output")

UNDERSTANDING_SCHEMA_VERSION = "understanding_output.v1"


def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_understanding_output(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any] | None = None,
    business_result: dict[str, Any] | None = None,
    intelligence: dict[str, Any] | None = None,
    thread_memory: dict[str, Any] | None = None,
    attachment_intelligence: dict[str, Any] | None = None,
    case_context_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a read-only summary for operator/pipeline use.

    This contract intentionally keeps raw message body, prompt text and raw LLM
    payloads out of the result. Evidence refs are normalized, not expanded.
    """
    snap = snapshot if isinstance(snapshot, dict) else {}
    intake = intake_result if isinstance(intake_result, dict) else {}
    business = business_result if isinstance(business_result, dict) else {}
    ci = intelligence if isinstance(intelligence, dict) else {}
    tm = thread_memory if isinstance(thread_memory, dict) else {}
    ai = attachment_intelligence if isinstance(attachment_intelligence, dict) else {}
    pack = case_context_pack if isinstance(case_context_pack, dict) else {}
    _ = case_link_result

    source = snap.get("source_message") if isinstance(snap.get("source_message"), dict) else {}
    source_signal_id = str(source.get("message_id") or "").strip()
    cu = ci.get("case_understanding") if isinstance(ci.get("case_understanding"), dict) else {}
    missing = ci.get("missing_info") if isinstance(ci.get("missing_info"), dict) else {}
    risk = ci.get("risk_assessment") if isinstance(ci.get("risk_assessment"), dict) else {}

    summary_seed = (
        str(cu.get("summary_pl") or "")
        or str(business.get("summary") or "")
        or str((intake.get("decision") or {}).get("reason") or "")
        or str(source.get("subject") or "")
        or "Sygnał wymaga oceny operatora."
    )
    essence = operator_feed_plain_summary(summary_seed, fallback="Sygnał wymaga oceny operatora.")[:700]

    intent_seed = (
        str(cu.get("customer_intent_pl") or "")
        or str(business.get("customer_intent") or "")
        or str(intake.get("business_area") or "")
        or str((intake.get("decision") or {}).get("action") or "")
    )
    intent = operator_feed_plain_summary(intent_seed, fallback="Intencja wymaga potwierdzenia.")[:300]

    missing_fields = _missing_fields(missing, business)
    for q in tm.get("unresolved_questions") or []:
        s = operator_feed_plain_summary(q, fallback="")
        if s:
            missing_fields.append(s[:240])
    conflicts = _conflicts(pack)
    risk_items = _risks(risk, business)
    for flag in ai.get("combined_risk_flags") or []:
        s = operator_feed_plain_summary(flag, fallback="")
        if s:
            risk_items.append({"risk_type": "attachment_signal", "severity": "medium", "summary_pl": s[:320]})
    evidence_refs = _evidence_refs(ci, pack)
    confidence = _confidence(intake, ci, pack)
    nba = ci.get("next_best_action") if isinstance(ci.get("next_best_action"), dict) else {}
    cu = ci.get("case_understanding") if isinstance(ci.get("case_understanding"), dict) else {}
    op_brief = ci.get("operator_brief") if isinstance(ci.get("operator_brief"), dict) else {}
    desk = ci.get("desk_composition") if isinstance(ci.get("desk_composition"), dict) else {}
    cg = ci.get("case_guidance") if isinstance(ci.get("case_guidance"), dict) else {}
    primary_action = nba.get("primary_next_action") if isinstance(nba.get("primary_next_action"), dict) else {}
    case_id = str(cu.get("case_id") or "").strip()
    context_quality = _context_quality(pack, tm, ai)
    source_quality = _source_quality(source, case_link_result if isinstance(case_link_result, dict) else {}, evidence_refs)
    facts_explicit, facts_extracted, facts_inferred = _facts_from_inputs(
        intake=intake,
        source_signal_id=source_signal_id,
        source_timestamp=str(source.get("date") or ""),
        evidence_refs=evidence_refs,
    )

    uid_seed = f"{source_signal_id}|{essence}|{intent}|{len(missing_fields)}|{len(conflicts)}"
    logger.info("UNDERSTANDING_OUTPUT_BUILT", extra={"x": {
        "case_id": case_id,
        "source_signal_id": source_signal_id,
        "has_business_reasoning": bool(business_result),
        "has_intelligence": bool(intelligence),
        "missing_fields_count": len(missing_fields),
        "conflicts_count": len(conflicts),
    }})
    return {
        "schema_version": UNDERSTANDING_SCHEMA_VERSION,
        "understanding_output_id": "uo_" + hashlib.sha256(uid_seed.encode("utf-8")).hexdigest()[:22],
        "case_id": case_id,
        "source_signal_id": source_signal_id,
        "summary_pl": essence,
        "created_at": _utc(),
        "operator_explanation": {
            "essence_pl": essence,
            "customer_intent_pl": intent,
            "what_arrived_pl": operator_feed_plain_summary(cu.get("latest_meaningful_change") or "", fallback="")[:600],
            "what_is_new_pl": operator_feed_plain_summary(desk.get("body_reason_pl") or "", fallback="")[:600],
            "what_is_missing_pl": operator_feed_plain_summary(missing.get("summary_pl") or "", fallback="")[:600],
            "what_is_risk_pl": operator_feed_plain_summary(risk.get("summary_pl") or "", fallback="")[:600],
            "what_system_suggests_pl": operator_feed_plain_summary(primary_action.get("title_pl") or "", fallback="")[:400],
            "why_pl": operator_feed_plain_summary(primary_action.get("reason_pl") or cu.get("attention_reason") or "", fallback="")[:600],
            "what_we_dont_know_pl": operator_feed_plain_summary(cg.get("unsupported_claims") or "", fallback="")[:600],
            "operator_should_check_pl": "Sprawdź link sprawy, brakujące dane i ewentualne konflikty.",
        },
        "situation_summary_pl": essence,
        "situation_summary": {
            "case_family": str(cu.get("case_family") or (intake.get("case_assessment") or {}).get("case_family") or "unknown"),
            "business_area": str(cu.get("business_area") or intake.get("business_area") or ""),
            "case_link_decision": str((case_link_result or {}).get("decision") or ""),
            "intake_action": str((intake.get("decision") or {}).get("action") or ""),
        },
        "customer_intent_pl": intent,
        "current_customer_intent": intent,
        "facts_explicit": facts_explicit,
        "facts_extracted": facts_extracted,
        "facts_inferred": facts_inferred,
        "facts_disputed": [],
        "facts_invalidated": [],
        "missing_information": missing,
        "completeness_gaps": _safe_gap_rows(pack),
        "missing_critical_fields": missing_fields[:12],
        "risks": risk_items[:12],
        "conflicting_facts": conflicts[:12],
        "open_loops": [operator_feed_plain_summary(x, fallback="")[:240] for x in (tm.get("open_tasks_from_thread") or []) if str(x).strip()][:20],
        "commitments": [operator_feed_plain_summary(x, fallback="")[:240] for x in (tm.get("commitments_made") or []) if str(x).strip()][:20],
        "deadlines": [],
        "thread_delta": {
            "new_facts": [operator_feed_plain_summary(x, fallback="")[:240] for x in (cu.get("key_facts_hot") or []) if str(x).strip()][:8],
            "new_missing_info": missing_fields[:12],
            "new_conflicts": [str(x.get("title_pl") or x.get("content_pl") or "")[:240] for x in conflicts[:8] if isinstance(x, dict)],
            "operator_visible_delta_summary": operator_feed_plain_summary(cu.get("latest_meaningful_change") or "", fallback="")[:400],
            "risk_change": bool(risk_items),
        },
        "context_quality": context_quality,
        "source_quality": source_quality,
        "retrieval_support": _retrieval_support(pack),
        "similar_case_hints": [],
        "thread_continuity": {
            "last_customer_intent": intent,
            "unanswered_question": bool(tm.get("has_unanswered_question")),
            "new_commitment_detected": bool(tm.get("has_open_commitment")),
            "thread_momentum": str(tm.get("thread_state") or "unknown")[:80],
            "customer_waiting_since": str(tm.get("updated_at") or "")[:80],
        },
        "evidence_refs": evidence_refs[:24],
        "confidence": confidence,
        "unsupported_claims": _unsupported_claims(pack),
        "next_best_action_recommendation": {
            **primary_action,
            "kind": "recommendation",
            "note": "Recommendation only; not approved until PolicyDecision and operator approval.",
        },
        "situation_clusters": _situation_clusters(ci, intake),
        "source": "deterministic_projection",
    }


def validate_understanding_invariants(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Normalize the UnderstandingOutput shape and flag projection-safety issues."""
    obj = dict(raw if isinstance(raw, dict) else {})
    errors: list[str] = []
    if obj.get("schema_version") != UNDERSTANDING_SCHEMA_VERSION:
        errors.append("invalid_schema_version")
        obj["schema_version"] = UNDERSTANDING_SCHEMA_VERSION
    for key in ("source_signal_id", "situation_summary_pl", "customer_intent_pl", "source"):
        obj[key] = str(obj.get(key) or "").strip()[:1000]
    oe = obj.get("operator_explanation") if isinstance(obj.get("operator_explanation"), dict) else {}
    obj["operator_explanation"] = {
        "essence_pl": operator_feed_plain_summary(oe.get("essence_pl") or obj.get("situation_summary_pl"), fallback="")[:700],
        "customer_intent_pl": operator_feed_plain_summary(oe.get("customer_intent_pl") or obj.get("customer_intent_pl"), fallback="")[:300],
    }
    obj["missing_critical_fields"] = [
        operator_feed_plain_summary(x, fallback="")[:240]
        for x in (obj.get("missing_critical_fields") or [])
        if str(x).strip()
    ][:12]
    obj["conflicting_facts"] = [
        operator_feed_conflicting_fact(x)
        for x in (obj.get("conflicting_facts") or [])
        if isinstance(x, dict) and operator_feed_conflicting_fact(x)
    ][:12]
    risks: list[dict[str, Any]] = []
    for risk in obj.get("risks") or []:
        if not isinstance(risk, dict):
            continue
        safe = {
            "risk_type": str(risk.get("risk_type") or "operational_risk")[:80],
            "severity": str(risk.get("severity") or "medium")[:40],
            "summary_pl": operator_feed_plain_summary(
                risk.get("summary_pl") or risk.get("reason_pl") or "",
                fallback="Ryzyko wymaga weryfikacji operatora.",
            )[:320],
        }
        if not obj.get("source_signal_id"):
            safe["unsupported"] = True
        risks.append(safe)
    obj["risks"] = risks[:12]
    obj["evidence_refs"] = normalize_evidence_refs(obj.get("evidence_refs") or [])[:24]
    try:
        obj["confidence"] = max(0.0, min(1.0, float(obj.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        obj["confidence"] = 0.0
        errors.append("invalid_confidence")
    for forbidden in ("body", "snippet", "prompt", "raw_llm", "raw_response", "raw_body", "message_body"):
        if forbidden in obj:
            obj.pop(forbidden, None)
            errors.append(f"removed_forbidden_{forbidden}")
    errors.extend(_validate_understanding_situation_only(obj))
    return obj, errors


_SITUATION_ONLY_FORBIDDEN_TOP = frozenset(
    {
        "policy_decision_id",
        "policy_decision",
        "allowed_by_policy",
        "action_mode",
        "execution_result_ref",
        "execution_status",
        "action_proposals_v2",
        "proposal_id",
        "blocked_actions",
        "allowed_actions",
    }
)


def _validate_understanding_situation_only(obj: dict[str, Any]) -> list[str]:
    """Strip forbidden execution/policy keys; enforce NBA recommendation disclaimer."""
    errs: list[str] = []
    for key in _SITUATION_ONLY_FORBIDDEN_TOP:
        if key in obj:
            obj.pop(key, None)
            errs.append(f"situation_only_removed_top_level:{key}")
    nba = obj.get("next_best_action_recommendation")
    if not isinstance(nba, dict) or not nba:
        return errs
    if str(nba.get("kind") or "") != "recommendation":
        errs.append("next_best_action_recommendation.invalid_kind_must_be_recommendation")
    note = str(nba.get("note") or "")
    if "PolicyDecision" not in note and "not approved" not in note.lower():
        nba["note"] = "Recommendation only; not approved until PolicyDecision and operator approval."
        errs.append("next_best_action_recommendation.disclaimer_restored")
    obj["next_best_action_recommendation"] = nba
    return errs


def validate_understanding_situation_only(obj: dict[str, Any]) -> list[str]:
    """Non-mutating diagnostic: reports situation-only violations on a shallow copy + NBA clone."""
    o = obj if isinstance(obj, dict) else {}
    probe = dict(o)
    nba = probe.get("next_best_action_recommendation")
    if isinstance(nba, dict):
        probe["next_best_action_recommendation"] = dict(nba)
    return list(_validate_understanding_situation_only(probe))


def _missing_fields(missing: dict[str, Any], business: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("critical", "important", "helpful"):
        values = missing.get(key) if isinstance(missing.get(key), list) else []
        for item in values:
            s = operator_feed_plain_summary(item, fallback="")
            if s:
                out.append(s[:240])
    for item in business.get("missing_information") or []:
        s = operator_feed_plain_summary(item, fallback="")
        if s:
            out.append(s[:240])
    return _dedupe(out)


def _conflicts(pack: dict[str, Any]) -> list[dict[str, Any]]:
    rows = pack.get("conflicting_facts") if isinstance(pack.get("conflicting_facts"), list) else []
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            safe = operator_feed_conflicting_fact(row)
            if safe:
                out.append(safe)
    return out


def _risks(risk: dict[str, Any], business: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in risk.get("risks") or []:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "risk_type": str(item.get("risk_type") or "operational_risk")[:80],
                "severity": str(item.get("severity") or "medium")[:40],
                "summary_pl": operator_feed_plain_summary(
                    item.get("reason_pl") or item.get("summary_pl") or item.get("watch") or "",
                    fallback="Ryzyko wymaga weryfikacji operatora.",
                )[:320],
            }
        )
    for item in business.get("risks") or []:
        s = operator_feed_plain_summary(item, fallback="")
        if s:
            out.append({"risk_type": "business_signal", "severity": "medium", "summary_pl": s[:320]})
    return out


def _evidence_refs(ci: dict[str, Any], pack: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for key in ("evidence_refs", "source_refs"):
        refs.extend(normalize_evidence_refs(ci.get(key) or []))
        refs.extend(normalize_evidence_refs(pack.get(key) or []))
    for row in pack.get("facts") or []:
        if isinstance(row, dict):
            refs.extend(normalize_evidence_refs(row.get("evidence_refs") or row.get("source_refs")))
    return refs


def _confidence(intake: dict[str, Any], ci: dict[str, Any], pack: dict[str, Any]) -> float:
    candidates = []
    cd = ci.get("confidence_domains") if isinstance(ci.get("confidence_domains"), dict) else {}
    for key in ("confidence", "confidence_overall", "confidence_case_link"):
        candidates.append(cd.get(key))
    cq = pack.get("context_quality") if isinstance(pack.get("context_quality"), dict) else {}
    candidates.append(cq.get("confidence"))
    candidates.append(intake.get("confidence_score"))
    for value in candidates:
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if 0.0 <= f <= 1.0:
            return f
    return 0.5


def _unsupported_claims(pack: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for row in pack.get("conflicting_facts") or []:
        if not isinstance(row, dict):
            continue
        if row.get("decision_usable") is False or not normalize_evidence_refs(row.get("evidence_refs") or row.get("source_refs")):
            s = operator_feed_plain_summary(row.get("field_name") or row.get("summary_pl") or "", fallback="")
            if s:
                out.append(s[:180])
    return _dedupe(out)[:12]


def _facts_from_inputs(
    *,
    intake: dict[str, Any],
    source_signal_id: str,
    source_timestamp: str,
    evidence_refs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    base_refs = evidence_refs[:4] or [
        {
            "source_type": "gmail_message",
            "source_id": source_signal_id,
            "message_id": source_signal_id,
            "source_timestamp": source_timestamp,
            "evidence_role": "supports",
            "confidence": 0.7,
        }
    ] if source_signal_id else []
    explicit: list[dict[str, Any]] = []
    extracted: list[dict[str, Any]] = []
    inferred: list[dict[str, Any]] = []
    extracted_data = intake.get("extracted_data") if isinstance(intake.get("extracted_data"), dict) else {}
    for key, value in extracted_data.items():
        if value in (None, "", [], {}):
            continue
        safe_value = operator_feed_plain_summary(value, fallback="")
        if safe_value:
            extracted.append(_fact_row("message", str(key), safe_value[:500], "extracted", 0.72, base_refs))
    area = str(intake.get("business_area") or "").strip()
    if area:
        explicit.append(_fact_row("case", "business_area", area[:120], "extracted", 0.78, base_refs))
    interpretation = str((intake.get("case_assessment") or {}).get("interpretation") or "").strip()
    if interpretation:
        inferred.append(_fact_row("case", "interpretation_hypothesis", interpretation[:500], "inferred", 0.45, base_refs))
    return explicit, extracted, inferred


def _fact_row(
    subject: str,
    predicate: str,
    value: str,
    status: str,
    confidence: float,
    refs: list[dict[str, Any]],
) -> dict[str, Any]:
    seed = f"{subject}|{predicate}|{value}|{status}"
    return {
        "fact_id": "fact_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16],
        "subject": subject,
        "predicate": predicate,
        "value": operator_feed_plain_summary(value, fallback="")[:1000],
        "status": status,
        "confidence": max(0.0, min(1.0, confidence)),
        "source_refs": normalize_evidence_refs(refs)[:8],
        "created_by": "intake_structured" if status == "extracted" else "intake_inference",
    }


def _safe_gap_rows(pack: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rows = pack.get("completeness_gaps") if isinstance(pack.get("completeness_gaps"), list) else []
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "field_name": str(row.get("field_name") or row.get("gap_type") or "")[:120],
                "summary_pl": operator_feed_plain_summary(row.get("summary_pl") or row.get("summary") or "", fallback="")[:240],
                "severity": str(row.get("severity") or "warning")[:40],
                "evidence_status": str(row.get("evidence_status") or row.get("status") or "")[:40],
            }
        )
    return out


def _context_quality(pack: dict[str, Any], thread_memory: dict[str, Any], attachment_intelligence: dict[str, Any]) -> dict[str, Any]:
    src = pack.get("context_quality") if isinstance(pack.get("context_quality"), dict) else {}
    gaps = pack.get("completeness_gaps") if isinstance(pack.get("completeness_gaps"), list) else []
    conflicts = pack.get("conflicting_facts") if isinstance(pack.get("conflicting_facts"), list) else []
    blocking_gaps = any(str(x.get("severity") or "").lower() == "blocking" for x in gaps if isinstance(x, dict))
    weak_count = int(src.get("weak_evidence_count") or 0)
    if not weak_count:
        weak_count = sum(1 for x in conflicts if isinstance(x, dict) and not normalize_evidence_refs(x.get("evidence_refs") or x.get("source_refs")))
    return normalize_context_quality(
        {
            **src,
            "ready_for_decision": bool(src.get("ready_for_decision")) and not blocking_gaps,
            "weak_evidence_count": weak_count,
            "evidence_warning_count": int(src.get("evidence_warning_count") or 0),
            "has_blocking_conflicts": bool(src.get("has_blocking_conflicts")),
            "has_blocking_gaps": bool(src.get("has_blocking_gaps")) or blocking_gaps,
            "thread_has_unanswered_question": bool(thread_memory.get("has_unanswered_question")),
            "attachment_risk_count": len(attachment_intelligence.get("combined_risk_flags") or []),
        }
    )


def _source_quality(source: dict[str, Any], case_link_result: dict[str, Any], evidence_refs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "has_source_signal": bool(source.get("message_id")),
        "case_link_decision": str(case_link_result.get("decision") or ""),
        "case_link_confidence": case_link_result.get("confidence") or 0.0,
        "evidence_ref_count": len(evidence_refs),
    }


def _retrieval_support(pack: dict[str, Any]) -> dict[str, Any]:
    chunks = pack.get("relevant_chunks") if isinstance(pack.get("relevant_chunks"), list) else []
    vector = pack.get("vector_retrieval") if isinstance(pack.get("vector_retrieval"), dict) else {}
    return {
        "relevant_chunk_count": len(chunks),
        "vector_status": str(vector.get("status") or vector.get("mode") or "")[:80],
    }


def _situation_clusters(ci: dict[str, Any], intake: dict[str, Any]) -> list[str]:
    flm = ci.get("feedback_learning_memory") if isinstance(ci.get("feedback_learning_memory"), dict) else {}
    clusters = [str(x) for x in (flm.get("explicit_signals") or []) if str(x).strip()][:12]
    if clusters:
        return clusters
    area = str(intake.get("business_area") or "").lower()
    return ["service_missing_data_pattern"] if area == "service" else ["client_followup_pattern"]


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        s = str(value or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


__all__ = [
    "UNDERSTANDING_SCHEMA_VERSION",
    "build_understanding_output",
    "validate_understanding_invariants",
    "validate_understanding_situation_only",
]
