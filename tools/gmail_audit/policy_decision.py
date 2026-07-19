"""PolicyDecision v1 bridge over PolicyReport for Decision Pipeline P0."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

POLICY_DECISION_SCHEMA_VERSION = "policy_decision.v1"
POLICY_ENGINE_VERSION = "policy_engine.v2_1"

_SAFE_P0_ACTIONS = {
    "prepare_reply_draft",
    "request_missing_info",
    "mark_attention_required",
    "ask_for_operator_adjudication",
    "no_action",
}
_FORBIDDEN_EXTERNAL_ACTIONS = [
    "send_email",
    "auto_send",
    "archive_gmail",
    "apply_gmail_label",
    "create_calendar_event",
    "calendar_live_write",
    "handoff_to_kalk_top",
    "create_offerdto",
]


def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_policy_decision(
    *,
    policy_report: dict[str, Any],
    decision_candidate_id: str,
    decision_candidate: dict[str, Any] | None = None,
    case_link_result: dict[str, Any] | None = None,
    dry_run_only: bool = True,
) -> dict[str, Any]:
    """Map PolicyReport to the formal P0 pipeline gate."""
    report = policy_report if isinstance(policy_report, dict) else {}
    candidate = decision_candidate if isinstance(decision_candidate, dict) else {}
    link = case_link_result if isinstance(case_link_result, dict) else {}
    cid = str(decision_candidate_id or candidate.get("decision_candidate_id") or "").strip()

    status = _status(report, candidate, link, dry_run_only=dry_run_only)
    allowed_actions = _allowed_actions(status)
    blocked_actions = list(_FORBIDDEN_EXTERNAL_ACTIONS)
    for action in _SAFE_P0_ACTIONS:
        if action not in allowed_actions:
            blocked_actions.append(action)

    requires_review = bool(report.get("requires_review")) or status != "allowed"
    requires_human = status in {"needs_human", "blocked", "insufficient_context", "requires_adjudication", "dry_run_only"}
    if dry_run_only:
        requires_review = True
        requires_human = True

    seed = {
        "candidate": cid,
        "status": status,
        "failed_rules": list(report.get("failed_rules") or []),
        "basis": list(report.get("policy_basis") or []),
    }
    pdec_id = "pdec_" + hashlib.sha256(repr(seed).encode("utf-8")).hexdigest()[:22]
    return {
        "schema_version": POLICY_DECISION_SCHEMA_VERSION,
        "policy_decision_id": pdec_id,
        "decision_candidate_id": cid,
        "status": status,
        "risk_class": _risk_class(report.get("effective_risk_class")),
        "allowed_actions": allowed_actions,
        "blocked_actions": blocked_actions,
        "requires_review": requires_review,
        "requires_human_approval": requires_human,
        "dry_run_only": bool(dry_run_only),
        "policy_basis": [str(x)[:240] for x in (report.get("policy_basis") or [])],
        "failed_rules": [str(x)[:160] for x in (report.get("failed_rules") or [])],
        "warnings": [str(x)[:240] for x in (report.get("warnings") or [])],
        "policy_engine_version": POLICY_ENGINE_VERSION,
        "created_at": _utc(),
    }


def _status(
    report: dict[str, Any],
    candidate: dict[str, Any],
    link: dict[str, Any],
    *,
    dry_run_only: bool,
) -> str:
    rec_mode = str(candidate.get("recommended_mode") or "").strip()
    if rec_mode == "not_ready":
        return "insufficient_context"
    link_decision = str(link.get("decision") or "").strip().lower()
    link_status = str(link.get("case_link_status") or "").strip().lower()
    if link_decision in {"pending_adjudication", "competing_links", "link_conflict"} or link_status == "pending_or_conflict":
        return "requires_adjudication"

    raw = str(report.get("status") or "").strip().upper()
    if raw == "REJECTED":
        return "blocked"
    if raw == "NEEDS_HUMAN":
        return "needs_human"
    if raw == "APPROVED":
        if dry_run_only or bool(report.get("requires_review")):
            return "allowed_with_review"
        return "allowed"
    return "needs_human"


def _allowed_actions(status: str) -> list[str]:
    if status in {"allowed", "allowed_with_review"}:
        return ["prepare_reply_draft", "request_missing_info", "mark_attention_required"]
    if status == "insufficient_context":
        return ["request_missing_info", "mark_attention_required"]
    if status in {"needs_human", "requires_adjudication"}:
        return ["ask_for_operator_adjudication", "mark_attention_required"]
    if status == "dry_run_only":
        return ["mark_attention_required", "no_action"]
    return ["no_action"]


def _risk_class(value: Any) -> str:
    v = str(value or "medium").strip().lower()
    if v == "critical":
        return "high"
    return v if v in {"low", "medium", "high"} else "medium"


__all__ = [
    "POLICY_DECISION_SCHEMA_VERSION",
    "POLICY_ENGINE_VERSION",
    "build_policy_decision",
]
