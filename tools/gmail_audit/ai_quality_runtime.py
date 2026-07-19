"""AI quality summary V1 from proposals, outcomes, and operator feedback."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


WINDOWS = ("last_7_days", "last_30_days", "all_time")


def build_ai_quality_summary(store: Any, *, window: str = "all_time", now: datetime | None = None) -> dict[str, Any]:
    window = window if window in WINDOWS else "all_time"
    cutoff = _window_cutoff(window, now=now)
    proposals = _filter_by_time(store.fetch_action_proposals(limit=5000), cutoff, "created_at")
    results = _filter_by_time(store.fetch_execution_results(limit=5000), cutoff, "executed_at")
    feedback_events = _feedback_events(store, cutoff)

    accepted = [p for p in proposals if str(p.get("status") or "") in {"approved", "executed"}]
    rejected = [p for p in proposals if str(p.get("status") or "") == "rejected"]
    ratings = [str(_payload(ev).get("rating") or ev.get("rating") or "").strip() for ev in feedback_events]
    tags = [tag for ev in feedback_events for tag in list(_payload(ev).get("tags") or ev.get("tags") or [])]
    target_types = [str(_payload(ev).get("target_type") or ev.get("target_type") or "") for ev in feedback_events]
    return {
        "window": window,
        "total_ai_suggestions": len(proposals),
        "accepted_suggestions": len(accepted),
        "rejected_suggestions": len(rejected),
        "execution_results": len(results),
        "feedback_count": len(feedback_events),
        "accurate_rate": _rate(ratings, "accurate"),
        "partially_accurate_rate": _rate(ratings, "partially_accurate"),
        "inaccurate_rate": _rate(ratings, "inaccurate"),
        "wrong_source_count": tags.count("wrong_source"),
        "wrong_priority_count": tags.count("wrong_priority"),
        "bad_draft_count": tags.count("bad_draft"),
        "missing_info_count": tags.count("missing_important_info"),
        "document_intelligence_feedback_count": target_types.count("document_intelligence"),
        "calendar_suggestion_feedback_count": target_types.count("calendar_suggestion"),
        "recent_feedback": feedback_events[:20],
        "top_problem_tags": _tag_counts(tags),
        "by_target_type": _feedback_by_target_type(feedback_events),
        "by_action_type": _suggestions_by_action_type(proposals),
        "by_rating": _simple_counts(ratings),
        "problem_tags_by_target_type": _problem_tags_by_target_type(feedback_events),
        "rejected_by_action_type": _rejected_by_action_type(proposals),
        "document_feedback_by_document_type": _document_feedback_by_document_type(feedback_events),
        "calendar_feedback_count_by_risk": _calendar_feedback_count_by_risk(feedback_events),
    }


def _feedback_events(store: Any, cutoff: datetime | None) -> list[dict[str, Any]]:
    if hasattr(store, "fetch_events"):
        rows = store.fetch_events(event_types=("v2_1_feedback_calibration",), limit=5000)
    else:
        rows = []
    rows = _filter_by_time(rows, cutoff, "occurred_at")
    rows.sort(key=lambda item: str(item.get("occurred_at") or item.get("created_at") or ""), reverse=True)
    return rows


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _rate(values: list[str], target: str) -> float:
    if not values:
        return 0.0
    return round(sum(1 for value in values if value == target) / len(values), 4)


def _tag_counts(tags: list[str]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for tag in tags:
        counts[str(tag)] = counts.get(str(tag), 0) + 1
    return [{"tag": key, "count": value} for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _simple_counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def _feedback_by_target_type(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = _payload(event)
        target_type = str(payload.get("target_type") or event.get("target_type") or "unknown") or "unknown"
        rating = str(payload.get("rating") or event.get("rating") or "")
        entry = grouped.setdefault(target_type, {"feedback_count": 0, "ratings": {}})
        entry["feedback_count"] += 1
        if rating:
            entry["ratings"][rating] = entry["ratings"].get(rating, 0) + 1
    return grouped


def _suggestions_by_action_type(proposals: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    for proposal in proposals:
        action_type = str(proposal.get("action_type") or "unknown") or "unknown"
        status = str(proposal.get("status") or "")
        entry = grouped.setdefault(action_type, {"total": 0, "accepted": 0, "rejected": 0, "executed": 0})
        entry["total"] += 1
        if status in {"approved", "executed"}:
            entry["accepted"] += 1
        if status == "rejected":
            entry["rejected"] += 1
        if status == "executed":
            entry["executed"] += 1
    return grouped


def _rejected_by_action_type(proposals: list[dict[str, Any]]) -> dict[str, int]:
    return {
        action_type: data["rejected"]
        for action_type, data in _suggestions_by_action_type(proposals).items()
        if data.get("rejected")
    }


def _problem_tags_by_target_type(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[str]] = {}
    for event in events:
        payload = _payload(event)
        target_type = str(payload.get("target_type") or event.get("target_type") or "unknown") or "unknown"
        grouped.setdefault(target_type, []).extend(str(tag) for tag in list(payload.get("tags") or event.get("tags") or []))
    return {target: _tag_counts(tags) for target, tags in grouped.items()}


def _document_feedback_by_document_type(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        payload = _payload(event)
        if str(payload.get("target_type") or event.get("target_type") or "") != "document_intelligence":
            continue
        metadata = payload.get("target_metadata") if isinstance(payload.get("target_metadata"), dict) else {}
        document_type = str(payload.get("document_type") or metadata.get("document_type") or "").strip()
        if not document_type:
            document_type = "unknown"
        counts[document_type] = counts.get(document_type, 0) + 1
    return counts


def _calendar_feedback_count_by_risk(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        payload = _payload(event)
        if str(payload.get("target_type") or event.get("target_type") or "") != "calendar_suggestion":
            continue
        metadata = payload.get("target_metadata") if isinstance(payload.get("target_metadata"), dict) else {}
        risk = str(payload.get("calendar_risk") or metadata.get("calendar_risk") or "").strip()
        if not risk:
            risk = "unknown"
        counts[risk] = counts.get(risk, 0) + 1
    return counts


def _window_cutoff(window: str, *, now: datetime | None = None) -> datetime | None:
    if window == "all_time":
        return None
    base = now or datetime.now().astimezone()
    if window == "last_7_days":
        return base - timedelta(days=7)
    if window == "last_30_days":
        return base - timedelta(days=30)
    return None


def _filter_by_time(rows: list[dict[str, Any]], cutoff: datetime | None, field: str) -> list[dict[str, Any]]:
    if cutoff is None:
        return list(rows)
    filtered: list[dict[str, Any]] = []
    for row in rows:
        parsed = _parse_time(row.get(field) or row.get("created_at"))
        if parsed is None or parsed >= cutoff:
            filtered.append(row)
    return filtered


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


__all__ = ["WINDOWS", "build_ai_quality_summary"]
