"""DecisionCandidate v1 contract helpers.

Pure projection-safe envelope. This module does not run policy, create action
proposals, mutate memory, or execute any external action.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from case_context_contract import (
    feed_projection_summary_line,
    normalize_evidence_refs,
    operator_feed_completeness_gap,
    operator_feed_conflicting_fact,
    operator_feed_plain_summary,
)
from context_quality_contract import normalize_context_quality

DECISION_CANDIDATE_SCHEMA_VERSION = "decision_candidate.v1"

_RECOMMENDED_MODES = {"projection_only", "operator_review_only", "not_ready"}
_FORBIDDEN_KEYS = frozenset(
    {
        "body",
        "email_body",
        "snippet",
        "prompt",
        "prompt_text",
        "raw_llm",
        "raw_response",
        "raw_body",
        "message_body",
        "attachment_bytes",
        "values",
        "facts_in_conflict",
    }
)

_LINEAGE_KEYS = frozenset(
    {
        "pipeline_run_id",
        "pipeline_schema_version",
        "topic_result_id",
        "case_type_result_id",
        "priority_sla_result_id",
        "intake_case_link_input_hash",
        "staleness_scope",
    }
)


def _build_decision_candidate_lineage(
    supplement: dict[str, Any] | None,
    *,
    topic_result: dict[str, Any] | None,
    case_type_result: dict[str, Any] | None,
    priority_sla: dict[str, Any] | None,
) -> dict[str, str]:
    """Read-only correlation ids — not a workflow graph; no TTL enforcement here."""
    out: dict[str, str] = {}
    sup = supplement if isinstance(supplement, dict) else {}
    for key in _LINEAGE_KEYS:
        if key not in sup or sup[key] is None:
            continue
        raw = str(sup[key]).strip()
        if raw:
            out[key] = raw[:160]
    tr = topic_result if isinstance(topic_result, dict) else {}
    cr = case_type_result if isinstance(case_type_result, dict) else {}
    ps = priority_sla if isinstance(priority_sla, dict) else {}
    if "topic_result_id" not in out:
        tid = str(tr.get("topic_result_id") or "").strip()
        if tid:
            out["topic_result_id"] = tid[:160]
    if "case_type_result_id" not in out:
        cid = str(cr.get("case_type_result_id") or "").strip()
        if cid:
            out["case_type_result_id"] = cid[:160]
    if "priority_sla_result_id" not in out:
        pid = str(ps.get("priority_sla_result_id") or "").strip()
        if pid:
            out["priority_sla_result_id"] = pid[:160]
    if not out:
        return {}
    out.setdefault("staleness_scope", "re_evaluate_on_new_signal_or_replay")
    return out


def build_decision_candidate(
    *,
    case_id: str = "",
    source_signal_id: str = "",
    topic: str | dict[str, Any] = "",
    case_type: str | dict[str, Any] = "",
    priority: str | dict[str, Any] = "",
    sla_risk: str | dict[str, Any] = "",
    owner_hint: str = "",
    next_best_action: Any = "",
    recommended_mode: str = "",
    risk_class_candidate: str = "unknown",
    decision_basis: list[dict[str, Any]] | None = None,
    blocking_gaps: list[dict[str, Any]] | None = None,
    review_only_warnings: list[Any] | None = None,
    not_ready_reasons: list[str] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    context_quality_ref: dict[str, Any] | None = None,
    case_context_pack: dict[str, Any] | None = None,
    topic_result: dict[str, Any] | None = None,
    case_type_result: dict[str, Any] | None = None,
    priority_sla: dict[str, Any] | None = None,
    understanding_ref: dict[str, Any] | None = None,
    next_best_action_code: str = "",
    lineage_supplement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a read-only DecisionCandidate envelope.

    The extra *_result parameters keep the contract compatible with existing
    WIP callers, but this function remains pure and projection-only.

    When ``understanding_ref`` is an UnderstandingOutput dict, its
    ``understanding_output_id`` is copied for traceability only (does not change
    candidate identity hash).
    """
    uo_ref = understanding_ref if isinstance(understanding_ref, dict) else {}
    understanding_output_id = str(uo_ref.get("understanding_output_id") or "").strip()[:96]
    pack = case_context_pack if isinstance(case_context_pack, dict) else {}

    topic_value = _topic_value(topic, topic_result)
    case_type_value = _case_type_value(case_type, case_type_result)
    priority_value = _priority_value(priority, priority_sla)
    sla_value = _sla_value(sla_risk, priority_sla)
    nba = next_best_action if next_best_action not in (None, "") else next_best_action_code
    safe_nba = sanitize_decision_candidate_for_projection(nba)

    context_quality = _context_quality(context_quality_ref, pack)
    weak_or_missing_seen = False

    safe_basis: list[dict[str, Any]] = []
    collected_refs: list[dict[str, Any]] = []
    review_warnings: list[Any] = []
    for item in decision_basis or []:
        safe = _sanitize_basis_row(item)
        if _basis_is_usable(item):
            safe_basis.append(safe)
            collected_refs.extend(normalize_evidence_refs(item.get("evidence_refs") or item.get("source_refs")))
        else:
            weak_or_missing_seen = True
            review_warnings.append(_warning_from_row(item, kind="basis_not_usable"))

    pack_conflicts = [row for row in (pack.get("conflicting_facts") or []) if isinstance(row, dict)]
    for row in pack_conflicts:
        safe_conf = operator_feed_conflicting_fact(row)
        if _row_is_weak_or_missing(row):
            weak_or_missing_seen = True
        if safe_conf:
            review_warnings.append({"kind": "conflict_review", "item": safe_conf})

    gap_rows = [row for row in (pack.get("completeness_gaps") or []) if isinstance(row, dict)]
    safe_blocking_gaps = [operator_feed_completeness_gap(row) for row in (blocking_gaps or []) if isinstance(row, dict)]
    for row in gap_rows:
        safe_gap = operator_feed_completeness_gap(row)
        if not safe_gap:
            continue
        if _row_is_weak_or_missing(row):
            weak_or_missing_seen = True
        if str(row.get("severity") or "").lower() == "blocking" or _row_is_weak_or_missing(row):
            safe_blocking_gaps.append(safe_gap)

    for item in review_only_warnings or []:
        review_warnings.append(sanitize_decision_candidate_for_projection(item))

    reasons = _dedupe_strings([*(not_ready_reasons or []), *list(context_quality.get("not_ready_reasons") or [])])
    if weak_or_missing_seen and "weak_or_missing_evidence" not in reasons:
        reasons.append("weak_or_missing_evidence")
    if safe_blocking_gaps and "blocking_gaps" not in reasons:
        reasons.append("blocking_gaps")

    ready = bool(context_quality.get("ready_for_decision"))
    action_readiness = str(context_quality.get("action_readiness") or "").strip().lower()
    has_blockers = bool(context_quality.get("has_blocking_conflicts") or context_quality.get("has_blocking_gaps"))
    mode = _recommended_mode(
        requested=recommended_mode,
        ready=ready,
        action_readiness=action_readiness,
        has_blockers=has_blockers,
    )
    if mode == "projection_only" and review_warnings:
        mode = "operator_review_only"

    explicit_refs = normalize_evidence_refs(evidence_refs or [])
    all_refs = _dedupe_refs([*explicit_refs, *collected_refs])

    candidate = {
        "decision_candidate_id": "",
        "schema_version": DECISION_CANDIDATE_SCHEMA_VERSION,
        "case_id": _safe_string(case_id),
        "source_signal_id": _safe_string(source_signal_id),
        "topic": _safe_string(topic_value),
        "case_type": _safe_string(case_type_value),
        "priority": _safe_string(priority_value),
        "sla_risk": _safe_string(sla_value),
        "owner_hint": _safe_string(owner_hint),
        "next_best_action": safe_nba,
        "recommended_mode": mode,
        "automation_eligibility": "not_eligible",
        "risk_class_candidate": _risk_class(risk_class_candidate),
        "decision_basis": safe_basis,
        "blocking_gaps": safe_blocking_gaps[:12],
        "review_only_warnings": review_warnings[:24],
        "not_ready_reasons": reasons[:16],
        "evidence_refs": all_refs[:24],
        "context_quality_ref": context_quality,
        "requires_policy": mode != "projection_only",
        "requires_operator_review": True,
        "understanding_output_id": understanding_output_id,
    }
    candidate["decision_candidate_id"] = _candidate_id(candidate)
    sanitized = sanitize_decision_candidate_for_projection(candidate)
    valid, _errors = validate_decision_candidate(sanitized)
    lineage = _build_decision_candidate_lineage(
        lineage_supplement,
        topic_result=topic_result,
        case_type_result=case_type_result,
        priority_sla=priority_sla,
    )
    if lineage:
        valid = dict(valid)
        valid["lineage"] = lineage
    return valid


def validate_decision_candidate(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Normalize and validate DecisionCandidate v1 invariants."""
    candidate = sanitize_decision_candidate_for_projection(raw if isinstance(raw, dict) else {})
    errors: list[str] = []
    if candidate.get("schema_version") != DECISION_CANDIDATE_SCHEMA_VERSION:
        errors.append("invalid_schema_version")
        candidate["schema_version"] = DECISION_CANDIDATE_SCHEMA_VERSION
    for key in ("case_id", "source_signal_id", "topic", "case_type", "priority", "sla_risk", "owner_hint"):
        candidate[key] = _safe_string(candidate.get(key))
    if not candidate["case_id"]:
        errors.append("missing_case_id")
    if not candidate["source_signal_id"]:
        errors.append("missing_source_signal_id")
    mode = str(candidate.get("recommended_mode") or "").strip()
    if mode not in _RECOMMENDED_MODES:
        mode = "operator_review_only"
    cq = _context_quality(candidate.get("context_quality_ref"), {})
    ready = bool(cq.get("ready_for_decision"))
    action_readiness = str(cq.get("action_readiness") or "").strip().lower()
    if not ready and mode == "projection_only":
        mode = "operator_review_only"
    if action_readiness == "not_ready" or cq.get("has_blocking_conflicts") or cq.get("has_blocking_gaps"):
        mode = "not_ready"
    candidate["recommended_mode"] = mode
    candidate["automation_eligibility"] = "not_eligible"
    candidate["requires_operator_review"] = True
    candidate["requires_policy"] = mode != "projection_only"
    candidate["risk_class_candidate"] = _risk_class(candidate.get("risk_class_candidate"))
    candidate["context_quality_ref"] = cq

    basis: list[dict[str, Any]] = []
    warnings = [item for item in (candidate.get("review_only_warnings") or [])]
    for item in candidate.get("decision_basis") or []:
        if not isinstance(item, dict):
            continue
        if _basis_is_usable(item):
            basis.append(_sanitize_basis_row(item))
        else:
            warnings.append(_warning_from_row(item, kind="basis_not_usable"))
    candidate["decision_basis"] = basis[:24]
    candidate["review_only_warnings"] = [sanitize_decision_candidate_for_projection(item) for item in warnings][:24]
    candidate["blocking_gaps"] = [
        sanitize_decision_candidate_for_projection(item)
        for item in (candidate.get("blocking_gaps") or [])
        if isinstance(item, dict)
    ][:12]
    candidate["not_ready_reasons"] = _dedupe_strings(candidate.get("not_ready_reasons") or [])[:16]
    candidate["evidence_refs"] = _dedupe_refs(normalize_evidence_refs(candidate.get("evidence_refs") or []))[:24]

    cid = str(candidate.get("decision_candidate_id") or "").strip()
    if not cid.startswith("dc_"):
        candidate["decision_candidate_id"] = _candidate_id(candidate)
    return candidate, errors


def sanitize_decision_candidate_for_projection(value: Any) -> Any:
    """Return a projection-safe object: no raw content keys, contact PII, or raw conflict values."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key) in _FORBIDDEN_KEYS:
                continue
            out[str(key)] = sanitize_decision_candidate_for_projection(item)
        return out
    if isinstance(value, list):
        return [sanitize_decision_candidate_for_projection(item) for item in value]
    if isinstance(value, str):
        return operator_feed_plain_summary(value, fallback="Pole projekcji wymaga weryfikacji operatora.")
    return value


def _candidate_id(candidate: dict[str, Any]) -> str:
    seed = {
        "schema_version": DECISION_CANDIDATE_SCHEMA_VERSION,
        "case_id": str(candidate.get("case_id") or ""),
        "source_signal_id": str(candidate.get("source_signal_id") or ""),
        "topic": str(candidate.get("topic") or ""),
        "case_type": str(candidate.get("case_type") or ""),
        "next_best_action": candidate.get("next_best_action"),
    }
    blob = json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "dc_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _context_quality(explicit: Any, pack: dict[str, Any]) -> dict[str, Any]:
    src = explicit if isinstance(explicit, dict) else pack.get("context_quality")
    return normalize_context_quality(src if isinstance(src, dict) else {})


def _basis_is_usable(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    refs = normalize_evidence_refs(row.get("evidence_refs") or row.get("source_refs"))
    if not refs:
        return False
    if row.get("decision_usable") is not True:
        return False
    if str(row.get("evidence_status") or "").strip().lower() in {"weak", "missing", "weak_evidence"}:
        return False
    return True


def _row_is_weak_or_missing(row: dict[str, Any]) -> bool:
    refs = normalize_evidence_refs(row.get("evidence_refs") or row.get("source_refs"))
    status = str(row.get("evidence_status") or row.get("status") or "").strip().lower()
    return not refs or status in {"weak", "missing", "weak_evidence"} or row.get("decision_usable") is False


def _sanitize_basis_row(row: dict[str, Any]) -> dict[str, Any]:
    safe = sanitize_decision_candidate_for_projection(dict(row))
    refs = normalize_evidence_refs(row.get("evidence_refs") or row.get("source_refs"))
    if refs:
        safe["evidence_refs"] = refs
    summary = feed_projection_summary_line(row)
    if summary:
        safe["summary"] = operator_feed_plain_summary(summary, fallback="Przeslanka wymaga weryfikacji operatora.")
    return safe


def _warning_from_row(row: dict[str, Any], *, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "summary": operator_feed_plain_summary(
            feed_projection_summary_line(row),
            fallback="Sygnał wymaga weryfikacji operatora.",
        ),
        "evidence_status": _safe_string(row.get("evidence_status") or row.get("status")),
        "decision_usable": bool(row.get("decision_usable")),
    }


def _recommended_mode(*, requested: str, ready: bool, action_readiness: str, has_blockers: bool) -> str:
    if has_blockers or action_readiness == "not_ready":
        return "not_ready"
    if not ready or action_readiness == "review_only":
        return "operator_review_only"
    req = str(requested or "").strip()
    return req if req in _RECOMMENDED_MODES else "projection_only"


def _topic_value(value: str | dict[str, Any], topic_result: dict[str, Any] | None) -> str:
    if isinstance(value, dict):
        return str(value.get("topic") or value.get("topic_code") or value.get("label") or "")
    tr = topic_result if isinstance(topic_result, dict) else {}
    return str(value or tr.get("topic") or tr.get("topic_code") or tr.get("label") or "")


def _case_type_value(value: str | dict[str, Any], case_type_result: dict[str, Any] | None) -> str:
    if isinstance(value, dict):
        return str(value.get("case_type") or value.get("type") or value.get("label") or "")
    cr = case_type_result if isinstance(case_type_result, dict) else {}
    return str(value or cr.get("case_type") or cr.get("type") or cr.get("label") or "")


def _priority_value(value: str | dict[str, Any], priority_sla: dict[str, Any] | None) -> str:
    if isinstance(value, dict):
        return str(value.get("priority") or value.get("priority_level") or "")
    ps = priority_sla if isinstance(priority_sla, dict) else {}
    return str(value or ps.get("priority") or ps.get("priority_level") or "")


def _sla_value(value: str | dict[str, Any], priority_sla: dict[str, Any] | None) -> str:
    if isinstance(value, dict):
        return str(value.get("sla_risk") or value.get("sla") or "")
    ps = priority_sla if isinstance(priority_sla, dict) else {}
    return str(value or ps.get("sla_risk") or ps.get("sla") or "")


def _risk_class(value: Any) -> str:
    v = str(value or "unknown").strip().lower()
    return v if v in {"unknown", "low", "medium", "high"} else "unknown"


def _safe_string(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return str(sanitize_decision_candidate_for_projection(raw)).strip()[:500]


def _dedupe_strings(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        s = _safe_string(item)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _dedupe_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        key = (
            str(ref.get("source_type") or ""),
            str(ref.get("source_id") or ""),
            str(ref.get("field") or ""),
            str(ref.get("evidence_role") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(sanitize_decision_candidate_for_projection(ref))
    return out


__all__ = [
    "DECISION_CANDIDATE_SCHEMA_VERSION",
    "build_decision_candidate",
    "sanitize_decision_candidate_for_projection",
    "validate_decision_candidate",
]
