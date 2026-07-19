"""Deterministic case-link mini-engine for Gmail Intake v2 shadow mode."""

from __future__ import annotations

from typing import Any

from intake_schema import validate_case_link_result


def link_case(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    context_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect, score, and normalize a case-link decision."""
    candidates = collect_case_candidates(snapshot, intake_result, context_bundle or {})
    scored = [score_case_candidate(candidate, snapshot, intake_result) for candidate in candidates]
    decision = pick_case_decision(scored)
    return validate_case_link_result(decision)


def build_no_link_case_result(*, reason: str) -> dict[str, Any]:
    """Return a deterministic no-link result when case linking is intentionally skipped."""
    return validate_case_link_result(
        {
            "selected_case_key": "",
            "decision": "no_link",
            "confidence": 0.0,
            "source": "none",
            "reasons": [reason],
            "candidates": [],
        }
    )


def collect_case_candidates(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    context_bundle: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Collect deterministic case candidates from explicit references and bounded context."""
    context_bundle = context_bundle or {}
    message = snapshot.get("source_message") or {}
    context_messages = snapshot.get("context_messages") or []
    snapshot_candidates = snapshot.get("case_link_candidates") or []
    intake_candidates = intake_result.get("thread", {}).get("linked_case_candidates") or []
    reference_tokens = (message.get("reference_tokens") or {}).get("case") or []

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_candidate(case_key: str, *, source: str, reasons: list[str], base_score: float) -> None:
        key = str(case_key or "").strip()
        if not key or key in seen:
            return
        seen.add(key)
        candidates.append(
            {
                "case_key": key,
                "source": source,
                "reasons": reasons,
                "base_score": round(base_score, 4),
            }
        )

    for token in reference_tokens:
        add_candidate(
            _normalize_explicit_case_key(token),
            source="explicit_reference",
            reasons=["explicit_case_reference_in_message"],
            base_score=0.94,
        )

    for candidate in snapshot_candidates:
        if not isinstance(candidate, dict):
            continue
        add_candidate(
            candidate.get("case_key") or "",
            source=infer_case_link_source(candidate),
            reasons=_normalize_reason_list(candidate.get("evidence")) or ["snapshot_case_link_candidate"],
            base_score=_candidate_score(candidate.get("match_confidence"), default=0.6),
        )

    for candidate in intake_candidates:
        if not isinstance(candidate, dict):
            continue
        add_candidate(
            candidate.get("case_key") or "",
            source=infer_case_link_source(candidate),
            reasons=["intake_linked_case_candidate"],
            base_score=_candidate_score(candidate.get("match_confidence"), default=0.58),
        )

    normalized_subject = str(message.get("normalized_subject") or "").strip()
    participants = _participants_from_snapshot(snapshot)
    for item in context_messages:
        if not isinstance(item, dict):
            continue
        context_case_key = _context_case_key(item)
        if not context_case_key:
            continue
        reasons: list[str] = []
        base_score = 0.35
        if str(item.get("thread_id") or "").strip() and str(item.get("thread_id") or "").strip() == str(message.get("thread_id") or "").strip():
            reasons.append("same_message_thread")
            base_score += 0.35
        if normalized_subject and normalized_subject == str(item.get("normalized_subject") or "").strip():
            reasons.append("subject_continuity")
            base_score += 0.15
        overlap = participants.intersection(_participants_from_message(item))
        if overlap:
            reasons.append("participant_overlap")
            base_score += min(0.15, 0.05 * len(overlap))
        add_candidate(
            context_case_key,
            source="context_candidate",
            reasons=reasons or ["context_message_reference"],
            base_score=base_score,
        )

    bundle_candidates = context_bundle.get("case_link_candidates") or []
    for candidate in bundle_candidates:
        if not isinstance(candidate, dict):
            continue
        add_candidate(
            candidate.get("case_key") or "",
            source=infer_case_link_source(candidate),
            reasons=_normalize_reason_list(candidate.get("reasons")) or ["context_bundle_candidate"],
            base_score=_candidate_score(candidate.get("score"), default=0.45),
        )

    return candidates


def score_case_candidate(candidate: dict[str, Any], snapshot: dict[str, Any], intake_result: dict[str, Any]) -> dict[str, Any]:
    """Assign a deterministic score to a candidate using bounded Gmail evidence."""
    message = snapshot.get("source_message") or {}
    intake_confidence = intake_result.get("confidence", {}) if isinstance(intake_result.get("confidence"), dict) else {}
    reasons = _normalize_reason_list(candidate.get("reasons"))
    score = float(candidate.get("base_score") or 0.0)

    if candidate.get("source") == "explicit_reference":
        score += 0.08
    if candidate.get("source") == "thread":
        score += 0.02
    if str(message.get("thread_id") or "").strip() and str(candidate.get("case_key") or "").startswith("thread:"):
        reasons.append("thread_key_selected")
        score += 0.01
    if intake_result.get("review_required"):
        score -= 0.05
    score += min(0.05, float(intake_confidence.get("case_link_confidence") or 0.0) * 0.05)
    score = max(0.0, min(1.0, round(score, 4)))

    hard_match_count = sum(1 for reason in reasons if any(token in reason for token in ("explicit", "thread", "same_message_thread")))
    soft_match_count = max(0, len(reasons) - hard_match_count)
    return {
        "case_key": str(candidate.get("case_key") or "").strip(),
        "score": score,
        "source": infer_case_link_source(candidate),
        "reasons": reasons,
        "hard_match_count": hard_match_count,
        "soft_match_count": soft_match_count,
    }


def pick_case_decision(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose a deterministic case-link decision from scored candidates."""
    ranked = [candidate for candidate in candidates if str(candidate.get("case_key") or "").strip()]
    ranked.sort(key=lambda item: (float(item.get("score") or 0.0), int(item.get("hard_match_count") or 0)), reverse=True)

    if not ranked:
        return {
            "selected_case_key": "",
            "decision": "no_link",
            "confidence": 0.0,
            "source": "none",
            "reasons": ["no_case_candidates"],
            "candidates": [],
        }

    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    confidence = float(best.get("score") or 0.0)
    explicit_candidate = next((candidate for candidate in ranked if candidate.get("source") == "explicit_reference"), None)
    explicit_score = float(explicit_candidate.get("score") or 0.0) if explicit_candidate is not None else 0.0
    if explicit_candidate is not None and confidence >= 0.85 and confidence - explicit_score <= 0.05:
        best = explicit_candidate
        confidence = explicit_score
    if second is not None:
        second_score = float(second.get("score") or 0.0)
        second_hard = int(second.get("hard_match_count") or 0)
        best_hard = int(best.get("hard_match_count") or 0)
        if confidence - second_score <= 0.08 and second_hard > best_hard:
            best = second
            confidence = second_score

    reasons = list(best.get("reasons") or [])

    second_score = float(second.get("score") or 0.0) if second is not None else 0.0
    if best.get("source") == "explicit_reference" and confidence >= 0.85:
        second = None
        second_score = 0.0
    if second is not None and abs(confidence - second_score) <= 0.08 and confidence >= 0.72 and second_score >= 0.72:
        return {
            "selected_case_key": str(best.get("case_key") or "").strip(),
            "decision": "competing_links",
            "confidence": round(confidence, 4),
            "source": infer_case_link_source(best),
            "reasons": reasons + ["multiple_similar_candidates"],
            "candidates": ranked[:5],
        }

    if confidence >= 0.72:
        decision = "linked"
    elif confidence >= 0.45:
        decision = "weak_link"
    else:
        decision = "no_link"

    if decision == "no_link":
        selected_case_key = ""
        source = "none"
    else:
        selected_case_key = str(best.get("case_key") or "").strip()
        source = infer_case_link_source(best)

    return {
        "selected_case_key": selected_case_key,
        "decision": decision,
        "confidence": round(confidence, 4),
        "source": source,
        "reasons": reasons or [f"{decision}_selected"],
        "candidates": ranked[:5],
    }


def infer_case_link_source(candidate: dict[str, Any] | None) -> str:
    """Map candidate evidence into the public case-link source vocabulary."""
    if not isinstance(candidate, dict):
        return "none"
    source = str(candidate.get("source") or candidate.get("case_type") or "").strip()
    if source in {"thread", "thread_context"}:
        return "thread"
    if source in {"explicit_reference", "reference_context"}:
        return "explicit_reference"
    if source in {"subject_continuity", "subject_context"}:
        return "subject_continuity"
    if source in {"entity_match"}:
        return "entity_match"
    if source in {"context_candidate", "message_context"}:
        return "context_candidate"
    return "none"


def _context_case_key(context_message: dict[str, Any]) -> str:
    reference_tokens = context_message.get("reference_tokens") or {}
    case_tokens = reference_tokens.get("case") or []
    if case_tokens:
        return str(case_tokens[0]).strip()
    thread_id = str(context_message.get("thread_id") or "").strip()
    if thread_id:
        return f"thread:{thread_id}"
    return ""


def _participants_from_snapshot(snapshot: dict[str, Any]) -> set[str]:
    message = snapshot.get("source_message") or {}
    return _participants_from_message(message)


def _participants_from_message(message: dict[str, Any]) -> set[str]:
    participants = {
        str(message.get("sender_email") or "").strip().lower(),
        *[str(item).strip().lower() for item in message.get("to") or [] if str(item).strip()],
        *[str(item).strip().lower() for item in message.get("cc") or [] if str(item).strip()],
    }
    return {item for item in participants if item}


def _normalize_reason_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def _candidate_score(value: Any, *, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _normalize_explicit_case_key(value: Any) -> str:
    token = str(value or "").strip().upper()
    if token and not token.startswith("CASE-") and any(character.isdigit() for character in token):
        return f"CASE-{token}"
    return token


__all__ = [
    "build_no_link_case_result",
    "collect_case_candidates",
    "infer_case_link_source",
    "link_case",
    "pick_case_decision",
    "score_case_candidate",
]
