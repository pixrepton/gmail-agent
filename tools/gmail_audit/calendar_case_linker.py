"""Deterministic V1 linking between Google Calendar events and cases."""

from __future__ import annotations

import re
from typing import Any


def link_calendar_event_to_case(event: dict[str, Any], cases: list[dict[str, Any]]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for case in cases:
        score, reasons = score_calendar_case_link(event, case)
        if score <= 0.0:
            continue
        candidates.append(
            {
                "case_id": str(case.get("case_id") or ""),
                "link_confidence": round(score, 3),
                "match_reasons": reasons,
            }
        )
    candidates.sort(key=lambda item: float(item.get("link_confidence") or 0.0), reverse=True)
    best = candidates[0] if candidates else {"case_id": "", "link_confidence": 0.0, "match_reasons": []}
    second_score = float(candidates[1].get("link_confidence") or 0.0) if len(candidates) > 1 else 0.0
    best_score = float(best.get("link_confidence") or 0.0)
    delta = round(best_score - second_score, 3)
    reasons = list(best.get("match_reasons") or [])

    if best_score <= 0.0:
        status = "no_link"
    elif "proposal_source_case_id" in reasons or "attendee_email" in reasons or (best_score >= 0.55 and delta >= 0.08):
        status = "linked"
    elif best_score >= 0.35 and len(candidates) > 1 and delta < 0.08:
        status = "ambiguous"
    elif best_score >= 0.25:
        status = "candidate"
    else:
        status = "no_link"

    linked_case_id = str(best.get("case_id") or "") if status == "linked" else ""
    confidence = best_score if status != "no_link" else 0.0
    match_reasons = reasons if status != "no_link" else []
    return {
        "case_id": linked_case_id,
        "link_confidence": confidence,
        "match_reasons": match_reasons,
        "link_status": status,
        "candidates": candidates[:5],
        "top_score_delta": delta,
    }


def score_calendar_case_link(event: dict[str, Any], case: dict[str, Any]) -> tuple[float, list[str]]:
    haystack = " ".join(
        [
            str(event.get("summary") or ""),
            str(event.get("description") or ""),
            str(event.get("location") or ""),
        ]
    ).lower()
    reasons: list[str] = []
    score = 0.0

    case_id = str(case.get("case_id") or "").strip()
    if case_id and str(event.get("case_id") or "") == case_id:
        score += 0.6
        reasons.append("proposal_source_case_id")

    customer_email = str(case.get("customer_email") or "").strip().lower()
    attendees = " ".join(str(item.get("email") or item) for item in event.get("attendees") or []).lower()
    if customer_email and customer_email in attendees:
        score += 0.45
        reasons.append("attendee_email")

    customer_name = str(case.get("customer_name") or "").strip().lower()
    if customer_name and customer_name in haystack:
        score += 0.2
        reasons.append("customer_name")

    subject = str(case.get("subject") or "").strip().lower()
    overlap = _token_overlap(subject, haystack)
    if overlap >= 0.25:
        score += min(0.35, overlap * 0.35)
        reasons.append("summary_similarity")

    case_meta = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    address = str(case_meta.get("address") or case_meta.get("location") or "").strip().lower()
    if address and _token_overlap(address, haystack) >= 0.35:
        score += 0.2
        reasons.append("location_address")

    return min(1.0, score), reasons


def _token_overlap(a: str, b: str) -> float:
    ta = {t for t in re.split(r"\W+", a.lower()) if len(t) >= 3}
    tb = {t for t in re.split(r"\W+", b.lower()) if len(t) >= 3}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta))


__all__ = ["link_calendar_event_to_case", "score_calendar_case_link"]
