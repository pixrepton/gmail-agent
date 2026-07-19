"""Route operator-facing feedback into calibration vs adjudication durable events."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

from feedback_event_contract import (
    EVENT_TYPE_ADJUDICATION,
    EVENT_TYPE_FEEDBACK_CALIBRATION,
    AdjudicationEvent,
    FeedbackEvent,
    adjudication_event_from_dict,
    feedback_event_from_dict,
    validate_adjudication_event,
    validate_feedback_event,
)

Domain = Literal["calibration", "adjudication", "unknown"]


def stable_feedback_event_id(*, parts: list[str]) -> str:
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"fb_{h}"


def classify_operator_payload(raw: dict[str, Any]) -> Domain:
    """Classify incoming operator payload. Prefer explicit event_domain."""
    if not isinstance(raw, dict):
        return "unknown"
    explicit = str(raw.get("event_domain") or raw.get("v2_1_event_domain") or "").strip().lower()
    if explicit in {"calibration", "adjudication"}:
        return explicit  # type: ignore[return-value]
    if raw.get("adjudication_kind") or raw.get("event_class") == "AdjudicationEvent":
        return "adjudication"
    if raw.get("calibration_category") or raw.get("rating") or raw.get("target_type") or raw.get("event_class") == "FeedbackEvent":
        return "calibration"
    return "unknown"


def route_operator_payload(raw: dict[str, Any]) -> tuple[Domain, dict[str, Any]]:
    """
    Normalize raw dict into either FeedbackEvent or AdjudicationEvent shape.
    Unknown domain defaults to calibration (does not mutate truth by default).
    """
    domain = classify_operator_payload(raw)
    if domain == "unknown":
        domain = "calibration"
    eid = str(raw.get("event_id") or "").strip() or stable_feedback_event_id(
        parts=[
            str(raw.get("case_id") or ""),
            str(raw.get("trace_id") or ""),
            str(raw.get("detail") or raw.get("note") or ""),
            domain,
        ]
    )
    if domain == "adjudication":
        ev = AdjudicationEvent(
            event_id=eid,
            occurred_at=str(raw.get("occurred_at") or raw.get("created_at") or ""),
            case_id=str(raw.get("case_id") or ""),
            adjudication_kind=str(raw.get("adjudication_kind") or "truth_other"),
            trace_id=str(raw.get("trace_id") or ""),
            detail=str(raw.get("detail") or raw.get("note") or raw.get("body") or ""),
            target_refs=_operator_target_refs(raw),
            source_surface=str(raw.get("source_surface") or raw.get("surface") or "operator"),
            operator_id=str(raw.get("operator_id") or ""),
            payload={k: v for k, v in raw.items() if k not in {"event_domain", "v2_1_event_domain"}},
        )
        if not str(ev.occurred_at or "").strip():
            ev = replace(ev, occurred_at=datetime.now().astimezone().isoformat())
        return "adjudication", ev.to_dict()
    ev = FeedbackEvent(
        event_id=eid,
        occurred_at=str(raw.get("occurred_at") or raw.get("created_at") or ""),
        case_id=str(raw.get("case_id") or ""),
        trace_id=str(raw.get("trace_id") or ""),
        calibration_category=str(raw.get("calibration_category") or "quality_other"),
        detail=str(raw.get("detail") or raw.get("note") or raw.get("body") or ""),
        target_refs=_operator_target_refs(raw),
        source_surface=str(raw.get("source_surface") or raw.get("surface") or "operator"),
        operator_id=str(raw.get("operator_id") or ""),
        target_type=str(raw.get("target_type") or ""),
        target_id=str(raw.get("target_id") or ""),
        rating=str(raw.get("rating") or ""),
        tags=list(raw.get("tags") or []) if isinstance(raw.get("tags"), list) else [],
        submitted_by=str(raw.get("submitted_by") or raw.get("operator_id") or ""),
        submitted_at=str(raw.get("submitted_at") or raw.get("occurred_at") or raw.get("created_at") or ""),
        payload={k: v for k, v in raw.items() if k not in {"event_domain", "v2_1_event_domain"}},
    )
    if not str(ev.occurred_at or "").strip():
        ev = replace(ev, occurred_at=datetime.now().astimezone().isoformat())
    return "calibration", ev.to_dict()


def _operator_target_refs(raw: dict[str, Any]) -> dict[str, Any]:
    refs = dict(raw.get("target_refs") or {}) if isinstance(raw.get("target_refs"), dict) else {}
    for key in ("decision_candidate_id", "policy_decision_id", "action_proposal_id"):
        value = str(raw.get(key) or refs.get(key) or "").strip()
        if value:
            refs[key] = value
    source_signal_id = str(raw.get("source_signal_id") or refs.get("source_signal_id") or refs.get("signal_id") or "").strip()
    if source_signal_id:
        refs["source_signal_id"] = source_signal_id
        refs["signal_id"] = str(refs.get("signal_id") or source_signal_id)
    return refs


def persist_routed_event(store: Any, domain: Domain, event_dict: dict[str, Any]) -> str:
    """Append to mailbox_memory_events with unambiguous event_type + full contract in payload."""
    if domain == "adjudication":
        row = _row_for_adjudication(event_dict)
    else:
        row = _row_for_feedback(event_dict)
    store.append_event(row)
    return str(row.get("event_id") or "")


def _row_for_feedback(d: dict[str, Any]) -> dict[str, Any]:
    fe = feedback_event_from_dict(d)
    err = validate_feedback_event(fe.to_dict())
    if err:
        raise ValueError(f"invalid FeedbackEvent: {err}")
    return {
        "event_id": fe.event_id,
        "case_id": fe.case_id,
        "message_id": str(fe.target_refs.get("message_id") or ""),
        "thread_id": str(fe.target_refs.get("thread_id") or ""),
        "event_type": EVENT_TYPE_FEEDBACK_CALIBRATION,
        "occurred_at": fe.occurred_at,
        "summary_text": fe.detail[:2000] if fe.detail else fe.calibration_category,
        "payload": fe.to_dict(),
        "source_refs": list(fe.target_refs.get("source_refs") or []) if isinstance(fe.target_refs.get("source_refs"), list) else [],
    }


def _row_for_adjudication(d: dict[str, Any]) -> dict[str, Any]:
    ae = adjudication_event_from_dict(d)
    err = validate_adjudication_event(ae.to_dict())
    if err:
        raise ValueError(f"invalid AdjudicationEvent: {err}")
    return {
        "event_id": ae.event_id,
        "case_id": ae.case_id,
        "message_id": str(ae.target_refs.get("message_id") or ""),
        "thread_id": str(ae.target_refs.get("thread_id") or ""),
        "event_type": EVENT_TYPE_ADJUDICATION,
        "occurred_at": ae.occurred_at,
        "summary_text": ae.detail[:2000] if ae.detail else ae.adjudication_kind,
        "payload": ae.to_dict(),
        "source_refs": list(ae.target_refs.get("source_refs") or []) if isinstance(ae.target_refs.get("source_refs"), list) else [],
    }


def calibration_cannot_mutate_truth(_event: FeedbackEvent) -> bool:
    """Explicit guard: calibration events are not applied to fact/snapshot mutation paths."""
    return True


def adjudication_may_affect_state_inputs(_event: AdjudicationEvent) -> bool:
    """Adjudication is allowed to feed rebuild / linker / fact validity (future hooks)."""
    return True


__all__ = [
    "calibration_cannot_mutate_truth",
    "adjudication_may_affect_state_inputs",
    "classify_operator_payload",
    "persist_routed_event",
    "route_operator_payload",
    "stable_feedback_event_id",
]
