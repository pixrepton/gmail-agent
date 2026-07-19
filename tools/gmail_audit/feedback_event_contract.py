"""V2.1 split feedback: calibration-only vs truth-affecting adjudication."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

CalibrationCategory = Literal[
    "too_strong",
    "too_weak",
    "accurate",
    "wrong_priority",
    "wrong_routing",
    "wrong_topic",
    "wrong_case",
    "wrong_draft_tone",
    "wrong_attention_weight",
    "quality_other",
    "partially_accurate",
    "inaccurate",
    "wrong_source",
    "bad_draft",
    "accepted_draft",
    "rejected_draft",
    "edited_decision",
    "rejected_fact_claim",  # calibration only — truth invalidation uses AdjudicationEvent.invalidate_fact
    "policy_block",
    "operator_correction",
    "missing_important_info",
    "needs_manual_review",
    "other",
]

AdjudicationKind = Literal[
    "confirm_same_case",
    "reject_same_case",
    "resolve_conflict",
    "invalidate_fact",
    "confirm_authoritative_source",
    "mark_source_non_authoritative",
    "confirm_merge_split",
    "truth_other",
]

EVENT_TYPE_FEEDBACK_CALIBRATION = "v2_1_feedback_calibration"
EVENT_TYPE_ADJUDICATION = "v2_1_adjudication"


@dataclass(slots=True)
class FeedbackEvent:
    """Calibration-only: model quality / routing / tone — must not mutate operational truth.

    Hard invariant: ``calibration_category == "rejected_fact_claim"`` (and any calibration)
    is a **signal** for eval / learning — it never invalidates mailbox facts by itself.
    Truth changes require ``AdjudicationEvent`` with ``adjudication_kind="invalidate_fact"``
    (or other adjudication kinds), not FeedbackEvent.
    """

    event_id: str
    occurred_at: str
    case_id: str
    trace_id: str = ""
    calibration_category: str = "quality_other"
    detail: str = ""
    target_refs: dict[str, Any] = field(default_factory=dict)
    source_surface: str = "operator"
    operator_id: str = ""
    target_type: str = ""
    target_id: str = ""
    rating: str = ""
    tags: list[str] = field(default_factory=list)
    submitted_by: str = ""
    submitted_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_class"] = "FeedbackEvent"
        d["schema_version"] = "feedback_event.v1"
        return d


@dataclass(slots=True)
class AdjudicationEvent:
    """Truth-affecting operator decisions — may change identity, facts, conflicts."""

    event_id: str
    occurred_at: str
    case_id: str
    adjudication_kind: str = "truth_other"
    trace_id: str = ""
    detail: str = ""
    target_refs: dict[str, Any] = field(default_factory=dict)
    source_surface: str = "operator"
    operator_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_class"] = "AdjudicationEvent"
        d["schema_version"] = "adjudication_event.v1"
        return d


def validate_feedback_event(d: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    if str(d.get("event_class") or "") != "FeedbackEvent" and "calibration_category" in d:
        pass  # allow construction from partial dict without event_class
    if not str(d.get("event_id") or "").strip():
        errs.append("event_id required")
    if not str(d.get("case_id") or "").strip():
        errs.append("case_id required")
    cat = str(d.get("calibration_category") or "")
    if cat and cat not in set(_calibration_values()):
        errs.append(f"unknown calibration_category: {cat!r}")
    rating = str(d.get("rating") or "")
    if rating and rating not in {"accurate", "partially_accurate", "inaccurate"}:
        errs.append(f"unknown rating: {rating!r}")
    return errs


def validate_adjudication_event(d: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    if not str(d.get("event_id") or "").strip():
        errs.append("event_id required")
    if not str(d.get("case_id") or "").strip():
        errs.append("case_id required")
    kind = str(d.get("adjudication_kind") or "")
    if kind and kind not in set(_adjudication_values()):
        errs.append(f"unknown adjudication_kind: {kind!r}")
    return errs


def _calibration_values() -> tuple[str, ...]:
    return (
        "too_strong",
        "too_weak",
        "accurate",
        "wrong_priority",
        "wrong_routing",
        "wrong_topic",
        "wrong_case",
        "wrong_draft_tone",
        "wrong_attention_weight",
        "quality_other",
        "partially_accurate",
        "inaccurate",
        "wrong_source",
        "bad_draft",
        "accepted_draft",
        "rejected_draft",
        "edited_decision",
        "rejected_fact_claim",
        "policy_block",
        "operator_correction",
        "missing_important_info",
        "needs_manual_review",
        "other",
    )


def _adjudication_values() -> tuple[str, ...]:
    return (
        "confirm_same_case",
        "reject_same_case",
        "resolve_conflict",
        "invalidate_fact",
        "confirm_authoritative_source",
        "mark_source_non_authoritative",
        "confirm_merge_split",
        "truth_other",
    )


def feedback_event_from_dict(d: dict[str, Any]) -> FeedbackEvent:
    return FeedbackEvent(
        event_id=str(d.get("event_id") or ""),
        occurred_at=str(d.get("occurred_at") or datetime.now().astimezone().isoformat()),
        case_id=str(d.get("case_id") or ""),
        trace_id=str(d.get("trace_id") or ""),
        calibration_category=str(d.get("calibration_category") or "quality_other"),
        detail=str(d.get("detail") or ""),
        target_refs=dict(d.get("target_refs") or {}) if isinstance(d.get("target_refs"), dict) else {},
        source_surface=str(d.get("source_surface") or "operator"),
        operator_id=str(d.get("operator_id") or ""),
        target_type=str(d.get("target_type") or ""),
        target_id=str(d.get("target_id") or ""),
        rating=str(d.get("rating") or ""),
        tags=list(d.get("tags") or []) if isinstance(d.get("tags"), list) else [],
        submitted_by=str(d.get("submitted_by") or d.get("operator_id") or ""),
        submitted_at=str(d.get("submitted_at") or d.get("occurred_at") or datetime.now().astimezone().isoformat()),
        payload=dict(d.get("payload") or {}) if isinstance(d.get("payload"), dict) else {},
    )


def adjudication_event_from_dict(d: dict[str, Any]) -> AdjudicationEvent:
    return AdjudicationEvent(
        event_id=str(d.get("event_id") or ""),
        occurred_at=str(d.get("occurred_at") or datetime.now().astimezone().isoformat()),
        case_id=str(d.get("case_id") or ""),
        adjudication_kind=str(d.get("adjudication_kind") or "truth_other"),
        trace_id=str(d.get("trace_id") or ""),
        detail=str(d.get("detail") or ""),
        target_refs=dict(d.get("target_refs") or {}) if isinstance(d.get("target_refs"), dict) else {},
        source_surface=str(d.get("source_surface") or "operator"),
        operator_id=str(d.get("operator_id") or ""),
        payload=dict(d.get("payload") or {}) if isinstance(d.get("payload"), dict) else {},
    )


# --- Feedback analytics grouping (QualityHub-ready; no metrics engine) ---

FeedbackAnalyticsGroup = Literal[
    "routing_quality",
    "priority_quality",
    "case_link_quality",
    "draft_quality",
    "decision_quality",
    "policy_quality",
    "evidence_quality",
    "truth_adjudication",
    "operator_correction",
    "unknown",
]

_FEEDBACK_ANALYTICS_GROUPS: frozenset[str] = frozenset(
    {
        "routing_quality",
        "priority_quality",
        "case_link_quality",
        "draft_quality",
        "decision_quality",
        "policy_quality",
        "evidence_quality",
        "truth_adjudication",
        "operator_correction",
        "unknown",
    }
)

_CALIBRATION_CATEGORY_TO_ANALYTICS_GROUP: dict[str, FeedbackAnalyticsGroup] = {
    "wrong_topic": "routing_quality",
    "wrong_routing": "routing_quality",
    "wrong_priority": "priority_quality",
    "wrong_attention_weight": "priority_quality",
    "wrong_case": "case_link_quality",
    "accepted_draft": "draft_quality",
    "rejected_draft": "draft_quality",
    "bad_draft": "draft_quality",
    "wrong_draft_tone": "draft_quality",
    "edited_decision": "decision_quality",
    "rejected_fact_claim": "evidence_quality",
    "wrong_source": "evidence_quality",
    "policy_block": "policy_quality",
    "operator_correction": "operator_correction",
    "too_strong": "operator_correction",
    "too_weak": "operator_correction",
    "accurate": "operator_correction",
    "partially_accurate": "operator_correction",
    "inaccurate": "evidence_quality",
    "quality_other": "operator_correction",
    "missing_important_info": "evidence_quality",
    "needs_manual_review": "operator_correction",
    "other": "operator_correction",
}

_ADJUDICATION_KIND_TO_ANALYTICS_GROUP: dict[str, FeedbackAnalyticsGroup] = {
    "confirm_same_case": "case_link_quality",
    "reject_same_case": "truth_adjudication",
    "resolve_conflict": "truth_adjudication",
    "invalidate_fact": "truth_adjudication",
    "confirm_authoritative_source": "evidence_quality",
    "mark_source_non_authoritative": "evidence_quality",
    "confirm_merge_split": "truth_adjudication",
    "truth_other": "truth_adjudication",
}

_TRUTH_AFFECTING_ADJUDICATION_KINDS: frozenset[str] = frozenset(
    {
        "reject_same_case",
        "resolve_conflict",
        "invalidate_fact",
        "confirm_merge_split",
        "truth_other",
    }
)

_CORRELATION_REF_KEYS: tuple[str, ...] = (
    "case_id",
    "source_signal_id",
    "signal_id",
    "decision_candidate_id",
    "policy_decision_id",
    "proposal_id",
    "action_proposal_id",
    "feedback_event_id",
    "adjudication_event_id",
    "event_id",
    "trace_id",
    "message_id",
    "thread_id",
)

_FORBIDDEN_ANALYTICS_EXPORT_KEYS: frozenset[str] = frozenset(
    {
        "body",
        "snippet",
        "raw_body",
        "prompt",
        "detail",
        "note",
        "summary_text",
        "payload",
        "tags",
    }
)


def normalize_feedback_analytics_group(group: str) -> FeedbackAnalyticsGroup:
    key = str(group or "").strip()
    if key in _FEEDBACK_ANALYTICS_GROUPS:
        return key  # type: ignore[return-value]
    return "unknown"


def feedback_category_to_analytics_group(category: str) -> FeedbackAnalyticsGroup:
    """Map calibration-only category to a stable analytics dimension."""
    key = str(category or "").strip()
    if not key:
        return "unknown"
    return _CALIBRATION_CATEGORY_TO_ANALYTICS_GROUP.get(key, "operator_correction")


def adjudication_kind_to_analytics_group(kind: str) -> FeedbackAnalyticsGroup:
    """Map truth-affecting adjudication kind to analytics dimension."""
    key = str(kind or "").strip()
    if not key:
        return "unknown"
    return _ADJUDICATION_KIND_TO_ANALYTICS_GROUP.get(key, "operator_correction")


def is_truth_affecting_adjudication(kind: str) -> bool:
    return str(kind or "").strip() in _TRUTH_AFFECTING_ADJUDICATION_KINDS


def calibration_signal_mutates_truth(category: str) -> bool:
    """Explicit invariant: calibration categories never mutate operational truth."""
    _ = category
    return False


def extract_feedback_correlation_refs(event: dict[str, Any]) -> dict[str, str]:
    """
    Normalize correlation IDs when present. Missing IDs are omitted.
    Never copies free-text or forbidden raw keys.
    """
    if not isinstance(event, dict):
        return {}
    refs = dict(event.get("target_refs") or {}) if isinstance(event.get("target_refs"), dict) else {}
    out: dict[str, str] = {}
    case_id = str(event.get("case_id") or refs.get("case_id") or "").strip()
    if case_id:
        out["case_id"] = case_id
    for key in _CORRELATION_REF_KEYS:
        if key == "case_id":
            continue
        value = str(event.get(key) or refs.get(key) or "").strip()
        if value:
            out[key] = value
    proposal = str(out.get("proposal_id") or out.get("action_proposal_id") or "").strip()
    if proposal:
        out["proposal_id"] = proposal
    event_id = str(event.get("event_id") or "").strip()
    event_class = str(event.get("event_class") or "")
    if event_id:
        if event_class == "AdjudicationEvent":
            out.setdefault("adjudication_event_id", event_id)
        else:
            out.setdefault("feedback_event_id", event_id)
        out.setdefault("event_id", event_id)
    source_signal = str(out.get("source_signal_id") or out.get("signal_id") or "").strip()
    if source_signal:
        out["source_signal_id"] = source_signal
    return {k: v for k, v in out.items() if k not in _FORBIDDEN_ANALYTICS_EXPORT_KEYS and v}


def build_feedback_analytics_key(
    *,
    analytics_group: str,
    event_domain: str,
    category_or_kind: str,
    correlation_refs: dict[str, str] | None = None,
) -> str:
    """Stable, code-like aggregation key (no customer text)."""
    group = normalize_feedback_analytics_group(analytics_group)
    domain = str(event_domain or "unknown").strip() or "unknown"
    kind = str(category_or_kind or "").strip() or "unspecified"
    parts = [group, domain, kind]
    refs = correlation_refs or {}
    for key in sorted(refs):
        value = str(refs.get(key) or "").strip()
        if value and key not in _FORBIDDEN_ANALYTICS_EXPORT_KEYS:
            parts.append(f"{key}={value[:128]}")
    return "|".join(parts)


def build_feedback_analytics_record(event: dict[str, Any]) -> dict[str, Any]:
    """
    Projection-safe analytics slice for eval / future QualityHub export.
    Does not include detail, body, snippet, prompt, or payload blobs.
    """
    if not isinstance(event, dict):
        return {
            "analytics_group": "unknown",
            "event_domain": "unknown",
            "category_or_kind": "",
            "mutates_truth": False,
            "correlation_refs": {},
            "analytics_key": build_feedback_analytics_key(
                analytics_group="unknown",
                event_domain="unknown",
                category_or_kind="",
            ),
        }
    event_class = str(event.get("event_class") or "")
    is_adjudication = event_class == "AdjudicationEvent" or bool(str(event.get("adjudication_kind") or "").strip())
    if is_adjudication:
        kind = str(event.get("adjudication_kind") or "truth_other")
        group = adjudication_kind_to_analytics_group(kind)
        domain = "adjudication"
        mutates_truth = is_truth_affecting_adjudication(kind)
        category_or_kind = kind
    else:
        cat = str(event.get("calibration_category") or "quality_other")
        group = feedback_category_to_analytics_group(cat)
        domain = "calibration"
        mutates_truth = calibration_signal_mutates_truth(cat)
        category_or_kind = cat
    correlation_refs = extract_feedback_correlation_refs(event)
    analytics_group = normalize_feedback_analytics_group(group)
    return {
        "analytics_group": analytics_group,
        "event_domain": domain,
        "category_or_kind": category_or_kind,
        "mutates_truth": mutates_truth,
        "correlation_refs": correlation_refs,
        "analytics_key": build_feedback_analytics_key(
            analytics_group=analytics_group,
            event_domain=domain,
            category_or_kind=category_or_kind,
            correlation_refs=correlation_refs,
        ),
    }


__all__ = [
    "EVENT_TYPE_ADJUDICATION",
    "EVENT_TYPE_FEEDBACK_CALIBRATION",
    "AdjudicationEvent",
    "AdjudicationKind",
    "CalibrationCategory",
    "FeedbackAnalyticsGroup",
    "FeedbackEvent",
    "adjudication_event_from_dict",
    "adjudication_kind_to_analytics_group",
    "build_feedback_analytics_key",
    "build_feedback_analytics_record",
    "calibration_signal_mutates_truth",
    "extract_feedback_correlation_refs",
    "feedback_category_to_analytics_group",
    "feedback_event_from_dict",
    "is_truth_affecting_adjudication",
    "normalize_feedback_analytics_group",
    "validate_adjudication_event",
    "validate_feedback_event",
]
