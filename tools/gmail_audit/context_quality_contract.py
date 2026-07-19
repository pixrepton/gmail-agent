"""Minimal ContextQuality contract helpers.

ContextQuality remains an embedded payload on CaseContextPack / DecisionCandidate.
This module owns only normalization and readiness guardrails.
"""

from __future__ import annotations

from typing import Any, Final

ACTION_READINESS_VALUES: Final[set[str]] = {"decision_ready", "review_only", "not_ready"}
CONTEXT_QUALITY_CORE_KEYS: Final[tuple[str, ...]] = (
    "ready_for_decision",
    "operator_review_possible",
    "action_readiness",
    "not_ready_reasons",
    "weak_evidence_count",
    "evidence_warning_count",
    "conflict_count",
    "gap_count",
    "has_blocking_conflicts",
    "has_blocking_gaps",
)
CONTEXT_QUALITY_EMBEDDED_EXTRA_KEYS: Final[tuple[str, ...]] = (
    "ready_for_operator_review",
    "source_diversity_count",
    "thread_has_unanswered_question",
    "attachment_risk_count",
)
FORBIDDEN_CONTEXT_QUALITY_KEYS: Final[frozenset[str]] = frozenset(
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


def _bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "tak"}:
            return True
        if v in {"0", "false", "no", "n", "nie"}:
            return False
    return bool(value)


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _reason(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "@" in raw or "." in raw:
        return ""
    safe = []
    for ch in raw[:80]:
        safe.append(ch if ch.isalnum() or ch in {"_", "-"} else "_")
    return "".join(safe).strip("_")[:80]


def _reasons(values: Any) -> list[str]:
    if not isinstance(values, list):
        values = [values] if values not in (None, "") else []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        reason = _reason(item)
        if not reason or reason in seen:
            continue
        seen.add(reason)
        out.append(reason)
    return out[:12]


def normalize_context_quality(row: dict[str, Any] | None, *, embedded: bool = True) -> dict[str, Any]:
    """Return the canonical embedded ContextQuality shape.

    Guardrails:
    - blocking conflicts/gaps always mean not_ready;
    - weak/missing evidence or evidence warnings block decision_ready;
    - invalid action_readiness falls back conservatively;
    - forbidden/raw diagnostic keys are never copied.
    """

    src = row if isinstance(row, dict) else {}
    safe_src = {str(k): v for k, v in src.items() if str(k) not in FORBIDDEN_CONTEXT_QUALITY_KEYS}

    has_blocking_conflicts = _bool(safe_src.get("has_blocking_conflicts"))
    has_blocking_gaps = _bool(safe_src.get("has_blocking_gaps"))
    weak_evidence_count = _count(safe_src.get("weak_evidence_count"))
    evidence_warning_count = _count(safe_src.get("evidence_warning_count"))
    conflict_count = _count(safe_src.get("conflict_count"))
    gap_count = _count(safe_src.get("gap_count"))

    not_ready_reasons = _reasons(safe_src.get("not_ready_reasons"))
    if has_blocking_conflicts and "blocking_conflicts" not in not_ready_reasons:
        not_ready_reasons.append("blocking_conflicts")
    if has_blocking_gaps and "blocking_gaps" not in not_ready_reasons:
        not_ready_reasons.append("blocking_gaps")
    if evidence_warning_count and "evidence_warnings" not in not_ready_reasons:
        not_ready_reasons.append("evidence_warnings")
    if weak_evidence_count and "weak_or_missing_evidence" not in not_ready_reasons:
        not_ready_reasons.append("weak_or_missing_evidence")

    raw_readiness = str(safe_src.get("action_readiness") or "").strip().lower()
    if raw_readiness not in ACTION_READINESS_VALUES:
        raw_readiness = ""

    blocked = has_blocking_conflicts or has_blocking_gaps
    has_quality_warnings = bool(weak_evidence_count or evidence_warning_count)
    ready_for_decision = _bool(safe_src.get("ready_for_decision")) and not blocked and not has_quality_warnings
    if raw_readiness == "decision_ready" and not ready_for_decision:
        raw_readiness = "not_ready" if blocked else "review_only"
    action_readiness = raw_readiness or ("not_ready" if blocked else "decision_ready" if ready_for_decision else "review_only")
    if blocked:
        action_readiness = "not_ready"

    operator_review_possible = _bool(safe_src.get("operator_review_possible"), default=True) and not blocked
    ready_for_operator_review = _bool(
        safe_src.get("ready_for_operator_review"),
        default=operator_review_possible,
    ) and operator_review_possible

    out: dict[str, Any] = {
        "ready_for_decision": ready_for_decision,
        "operator_review_possible": operator_review_possible,
        "action_readiness": action_readiness,
        "not_ready_reasons": not_ready_reasons[:12],
        "weak_evidence_count": weak_evidence_count,
        "evidence_warning_count": evidence_warning_count,
        "conflict_count": conflict_count,
        "gap_count": gap_count,
        "has_blocking_conflicts": has_blocking_conflicts,
        "has_blocking_gaps": has_blocking_gaps,
    }
    if embedded:
        out["ready_for_operator_review"] = ready_for_operator_review
        out["source_diversity_count"] = _count(safe_src.get("source_diversity_count"))
        if "thread_has_unanswered_question" in safe_src:
            out["thread_has_unanswered_question"] = _bool(safe_src.get("thread_has_unanswered_question"))
        if "attachment_risk_count" in safe_src:
            out["attachment_risk_count"] = _count(safe_src.get("attachment_risk_count"))
    return out


def operator_feed_context_quality_view(row: dict[str, Any] | None) -> dict[str, Any]:
    """Projection-safe ContextQuality view for Daszek/operator surfaces."""

    return {key: normalize_context_quality(row, embedded=False)[key] for key in CONTEXT_QUALITY_CORE_KEYS}


__all__ = [
    "ACTION_READINESS_VALUES",
    "CONTEXT_QUALITY_CORE_KEYS",
    "CONTEXT_QUALITY_EMBEDDED_EXTRA_KEYS",
    "FORBIDDEN_CONTEXT_QUALITY_KEYS",
    "normalize_context_quality",
    "operator_feed_context_quality_view",
]
