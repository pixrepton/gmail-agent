"""Conservative downstream action planning for Gmail Intake v2 shadow mode."""

from __future__ import annotations

import re
from typing import Any

from intake_schema import validate_action_plan_result


# BusinessReasoning is prompted to put "niesprawdzone twierdzenia" (unverified,
# calibrated-uncertainty assertions) into unsupported_claims -- honesty about what
# it could not confirm, not necessarily a dangerous promise. Only a categorical
# guarantee/commitment the system can never actually support deterministically is
# treated as genuinely unsafe here. Mirrors the categorical-guarantee vocabulary
# already used for draft rewriting in reply_drafter.py's _COMMITMENT_PATTERNS
# (kept as a separate, smaller pattern: that gate rewrites draft body text against
# case_state; this one classifies BusinessReasoning's own claim list before it can
# become a planner blocker).
_UNSAFE_CLAIM_MARKERS = re.compile(
    r"gwarantuj\w*|na pewno|sto procent pewn\w*|obiecuj\w*|zapewniamy",
    re.IGNORECASE,
)


def _is_unsafe_claim(text: str) -> bool:
    return bool(_UNSAFE_CLAIM_MARKERS.search(text))


def _is_blocking(item: dict[str, Any]) -> bool:
    """A 'low' severity item (currently: a calibrated-uncertainty unsupported claim)
    is surfaced to the operator but must not reduce confidence or block execution
    on its own -- only genuinely risky blockers do."""
    return str(item.get("severity") or "").lower() != "low"


def plan_actions(
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any] | None,
    business_result: dict[str, Any] | None,
    reply_result: dict[str, Any] | None,
    case_context_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a conservative downstream action plan without changing live behavior."""
    primary_action = select_primary_action(intake_result, case_link_result, business_result, reply_result)
    projection_mode = infer_projection_mode(intake_result, primary_action)
    checklist = build_operator_checklist(
        intake_result,
        case_link_result,
        business_result,
        reply_result,
        primary_action,
        case_context_pack=case_context_pack,
    )
    blockers = _reasoning_blockers(business_result, case_context_pack)
    confidence, confidence_components = _action_confidence(intake_result, business_result, case_link_result, blockers=blockers)

    result = validate_action_plan_result(
        {
            "primary_action": primary_action,
            "secondary_actions": _secondary_actions(primary_action, reply_result),
            "operator_checklist": checklist,
            "daszek_projection_mode": projection_mode,
            "safe_for_live_push": is_safe_for_live_push(intake_result, case_link_result, business_result, primary_action, confidence, blockers=blockers),
            "safe_for_operator_projection": is_safe_for_operator_projection(
                intake_result, primary_action, projection_mode
            ),
            "confidence": confidence,
            "why_this_action": _why_this_action(primary_action, intake_result, business_result),
            "why_not_other_actions": _why_not_other_actions(primary_action, intake_result, case_link_result),
            "review_priority": _review_priority(intake_result, business_result, blockers=blockers),
            "requires_case_confirmation": str((case_link_result or {}).get("decision") or "") in {"weak_link", "competing_links"} or bool(blockers),
        }
    )
    result["execution_metadata"] = {
        "shadow_only": True,
        "input_decision_action": str(intake_result.get("decision", {}).get("action") or ""),
        "business_next_action": str((business_result or {}).get("recommended_next_action") or ""),
        "confidence_components": confidence_components,
        "reasoning_blockers": blockers,
    }
    return result


def select_primary_action(
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any] | None,
    business_result: dict[str, Any] | None,
    reply_result: dict[str, Any] | None,
) -> str:
    """Pick the safest operator-facing action bundle."""
    intake_action = str(intake_result.get("decision", {}).get("action") or "")
    business_action = str((business_result or {}).get("recommended_next_action") or "")
    case_link_decision = str((case_link_result or {}).get("decision") or "")

    if intake_result.get("review_required"):
        return "create_review"
    if intake_action == "ignore":
        return "ignore"
    if business_action in {"reply", "collect_data"} and bool((reply_result or {}).get("draft_enabled")):
        return "prepare_reply"
    if intake_action in {"append_to_existing_case", "update_case_state"} and case_link_decision in {"linked", "weak_link"}:
        return "update_case"
    if intake_action in {"create_case", "create_case_and_task", "create_task"}:
        return "create_task"
    return "hold"


def build_operator_checklist(
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any] | None,
    business_result: dict[str, Any] | None,
    reply_result: dict[str, Any] | None,
    primary_action: str,
    case_context_pack: dict[str, Any] | None = None,
) -> list[str]:
    """Return an operator-facing checklist for the chosen action."""
    checklist: list[str] = []
    case_link_decision = str((case_link_result or {}).get("decision") or "")
    if case_link_decision in {"weak_link", "competing_links"}:
        checklist.append("verify case link before updating downstream objects")
    for item in (business_result or {}).get("missing_information") or []:
        checklist.append(f"confirm missing data: {item}")
    if primary_action == "prepare_reply":
        checklist.append("review draft before sending")
    if intake_result.get("review_required"):
        checklist.append("review intake flags and rationale")
    if bool((reply_result or {}).get("unsafe_claims_detected")):
        checklist.append("remove unsafe claims from the draft")
    snapshot = case_context_pack if isinstance(case_context_pack, dict) else {}
    for item in (snapshot.get("snapshot") or {}).get("open_questions") or []:
        checklist.append(f"resolve open question: {item}")
    for blocker in _reasoning_blockers(business_result, case_context_pack):
        kind = str(blocker.get("kind") or "")
        label = str(blocker.get("label") or kind)
        checklist.append(f"review {kind}: {label}")
    return checklist or ["operator confirmation required before any live action"]


def infer_projection_mode(intake_result: dict[str, Any], primary_action: str) -> str:
    """Map action-plan intent to preview mode while keeping v1 projection authoritative."""
    intake_action = str(intake_result.get("decision", {}).get("action") or "")
    if primary_action == "ignore" or intake_action == "ignore":
        return "ignore"
    if primary_action == "create_review":
        return "review"
    if primary_action == "update_case":
        return "case_update"
    if intake_action in {"mark_reference", "mark_watchlist"}:
        return "reference"
    return "task"


def is_safe_for_operator_projection(
    intake_result: dict[str, Any],
    primary_action: str,
    projection_mode: str,
) -> bool:
    """Gate Daszek v2 operator-visible projection (not live mailbox/Daszek v1 actions).

    Conservative default: suppress pure noise (ignore / silent projection). Everything else may be
    projected for operator review when upstream PolicyEngine and v2 ingest policy allow it.
    """
    intake_action = str(intake_result.get("decision", {}).get("action") or "")
    if primary_action == "ignore" or intake_action == "ignore":
        return False
    if projection_mode == "ignore":
        return False
    return True


def is_safe_for_live_push(
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any] | None,
    business_result: dict[str, Any] | None,
    primary_action: str,
    confidence: float,
    blockers: list[dict[str, Any]] | None = None,
) -> bool:
    """Keep live-push safety extremely conservative at the start of v2."""
    if any(_is_blocking(item) for item in blockers or []):
        return False
    if primary_action != "ignore":
        return False
    if intake_result.get("review_required"):
        return False
    if str((case_link_result or {}).get("decision") or "") not in {"linked", "no_link"}:
        return False
    if str((business_result or {}).get("urgency") or "") == "high":
        return False
    return confidence >= 0.85


def _secondary_actions(primary_action: str, reply_result: dict[str, Any] | None) -> list[str]:
    actions: list[str] = []
    if primary_action == "prepare_reply":
        actions.append("log_reply_draft_in_preview")
    if bool((reply_result or {}).get("draft_enabled")) and primary_action != "prepare_reply":
        actions.append("keep_reply_draft_available")
    return actions


def _action_confidence(
    intake_result: dict[str, Any],
    business_result: dict[str, Any] | None,
    case_link_result: dict[str, Any] | None,
    blockers: list[dict[str, Any]] | None = None,
) -> tuple[float, dict[str, float]]:
    intake_confidence = intake_result.get("confidence", {}) if isinstance(intake_result.get("confidence"), dict) else {}
    business_confidence = (business_result or {}).get("confidence", {}) if isinstance((business_result or {}).get("confidence"), dict) else {}
    intake_decision = _clamp01(intake_confidence.get("decision_confidence"))
    intake_case_link = _clamp01(intake_confidence.get("case_link_confidence"))
    deterministic_case_link = _clamp01((case_link_result or {}).get("confidence"))
    case_link = max(intake_case_link, deterministic_case_link)
    business_action = _clamp01(business_confidence.get("action_confidence"))
    evidence_ledger = _evidence_ledger_confidence(business_result or {})
    weighted = (intake_decision * 0.30) + (case_link * 0.25) + (business_action * 0.25) + (evidence_ledger * 0.20)
    blocking_count = sum(1 for item in blockers or [] if _is_blocking(item))
    blocker_penalty = min(0.45, 0.12 * blocking_count)
    score = max(0.0, min(1.0, weighted - blocker_penalty))
    components = {
        "intake_decision": round(intake_decision, 4),
        "case_link": round(case_link, 4),
        "business_action": round(business_action, 4),
        "evidence_ledger": round(evidence_ledger, 4),
        "blocker_penalty": round(blocker_penalty, 4),
    }
    return round(score, 4), components


def _clamp01(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _evidence_ledger_confidence(business_result: dict[str, Any]) -> float:
    evidence = business_result.get("evidence_refs")
    unsupported = business_result.get("unsupported_claims")
    conflicts = business_result.get("conflict_refs")
    has_unsafe_claim = isinstance(unsupported, list) and any(
        _is_unsafe_claim(str(claim or "")) for claim in unsupported
    )
    if isinstance(evidence, list) and evidence:
        score = 0.85
    elif has_unsafe_claim:
        score = 0.2
    else:
        score = 0.65
    if isinstance(conflicts, list) and conflicts:
        score = min(score, 0.45)
    return score


def _reasoning_blockers(
    business_result: dict[str, Any] | None,
    case_context_pack: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    cp = case_context_pack if isinstance(case_context_pack, dict) else {}
    doc = cp.get("document_intelligence") if isinstance(cp.get("document_intelligence"), dict) else {}
    for conflict in doc.get("document_conflicts") or []:
        if isinstance(conflict, dict):
            blockers.append(
                {
                    "kind": "document conflict",
                    "label": str(conflict.get("field_name") or conflict.get("conflict_type") or "document_conflict"),
                    "severity": str(conflict.get("severity") or "medium"),
                }
            )
    for field in doc.get("fields_requiring_review") or []:
        if isinstance(field, dict):
            blockers.append(
                {
                    "kind": "document review",
                    "label": str(field.get("field_name") or "field_requires_review"),
                    "severity": "medium",
                }
            )
    cal = cp.get("calendar") if isinstance(cp.get("calendar"), dict) else {}
    risk = str(cal.get("calendar_risk") or "")
    if risk in {"possible_conflict", "needs_scheduling_review"}:
        blockers.append({"kind": "calendar risk", "label": risk, "severity": "high"})
    for claim in (business_result or {}).get("unsupported_claims") or []:
        text = str(claim or "").strip()
        if not text:
            continue
        if _is_unsafe_claim(text):
            blockers.append({"kind": "unsupported claim", "label": text[:120], "severity": "high"})
        else:
            blockers.append({"kind": "unconfirmed claim", "label": text[:120], "severity": "low"})
    return blockers


def _why_this_action(primary_action: str, intake_result: dict[str, Any], business_result: dict[str, Any] | None) -> str:
    if primary_action == "create_review":
        return "Review is already required by the intake control plane."
    if primary_action == "prepare_reply":
        return "Business reasoning recommends a reply or data collection and a draft is available."
    if primary_action == "update_case":
        return "The mail looks like a continuation of an existing case."
    if primary_action == "create_task":
        return "The message remains actionable without forcing an unsafe automation jump."
    if primary_action == "ignore":
        return "Both intake and business reasoning indicate that no operator action is needed."
    return str((business_result or {}).get("operator_note") or "Conservative hold because confidence is limited.")


def _why_not_other_actions(primary_action: str, intake_result: dict[str, Any], case_link_result: dict[str, Any] | None) -> list[str]:
    reasons: list[str] = []
    if primary_action != "update_case" and str((case_link_result or {}).get("decision") or "") in {"weak_link", "competing_links"}:
        reasons.append("case link is not stable enough for a confident case update")
    if primary_action != "prepare_reply" and intake_result.get("review_required"):
        reasons.append("review gate takes precedence over reply preparation")
    if primary_action != "ignore" and str(intake_result.get("decision", {}).get("action") or "") == "ignore":
        reasons.append("intake control plane already marked the message as ignore")
    return reasons


def _review_priority(
    intake_result: dict[str, Any],
    business_result: dict[str, Any] | None,
    blockers: list[dict[str, Any]] | None = None,
) -> str:
    if any(str(item.get("severity") or "").lower() == "high" for item in blockers or []):
        return "high"
    if blockers and any(str(item.get("label") or "").lower() in {"amount_total", "address", "service_date", "customer", "calendar_risk"} for item in blockers):
        return "high"
    if blockers:
        return "normal"
    if str((business_result or {}).get("urgency") or "") == "high":
        return "high"
    if intake_result.get("review_required"):
        return "normal"
    return "low"


__all__ = [
    "build_operator_checklist",
    "infer_projection_mode",
    "is_safe_for_live_push",
    "is_safe_for_operator_projection",
    "plan_actions",
    "select_primary_action",
]
