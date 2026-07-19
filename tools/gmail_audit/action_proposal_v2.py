"""ActionProposal v2 — produced after PolicyDecision (action_proposal.v2)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

ACTION_PROPOSAL_V2_SCHEMA_VERSION = "action_proposal.v2"
POLICY_GATED_ACTION_PROPOSALS_V2_BUNDLE_VERSION = "policy_gated_action_proposals_v2.bundle.v1"
P0_SAFE_ACTION_TYPES = {
    "prepare_reply_draft",
    "request_missing_info",
    "mark_attention_required",
    "ask_for_operator_adjudication",
    "no_action",
}

# Deprecated strings at v2 boundary only — canonical planner primaries in action_planner / glossary.
_PRIMARY_ACTION_DEPRECATED_ALIASES: dict[str, str] = {
    "reply": "prepare_reply",
    "collect_data": "prepare_reply",
}


def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_planner_primary_for_v2(primary_action_type: str) -> str:
    """Map deprecated planner-side aliases before policy/v2 matching (glossary deprecated_aliases)."""
    p = str(primary_action_type or "").strip()
    return _PRIMARY_ACTION_DEPRECATED_ALIASES.get(p, p)


def _v2_reply_intent_tokens(*, planner_primary: str, decision_candidate: dict[str, Any]) -> tuple[str, str]:
    """Planner primary (normalized) + DecisionCandidate.next_best_action (NBA string spine)."""
    p_norm = normalize_planner_primary_for_v2(planner_primary)
    nba = str(decision_candidate.get("next_best_action") or "").strip()
    return p_norm, nba


def action_proposal_v2_correlation_ids_present(raw: dict[str, Any]) -> bool:
    """True when record is not v2, or v2 carries both formal spine ids."""
    if str(raw.get("schema_version") or "") != ACTION_PROPOSAL_V2_SCHEMA_VERSION:
        return True
    cid = str(raw.get("decision_candidate_id") or "").strip()
    pid = str(raw.get("policy_decision_id") or "").strip()
    return bool(cid and pid)


def action_proposal_v2_policy_cleared_envelope(raw: dict[str, Any]) -> bool:
    """True when v2 shows policy-cleared envelope (allowed_by_policy); requires both spine ids."""
    if str(raw.get("schema_version") or "") != ACTION_PROPOSAL_V2_SCHEMA_VERSION:
        return False
    if not action_proposal_v2_correlation_ids_present(raw):
        return False
    return bool(raw.get("allowed_by_policy"))


def build_policy_gated_action_proposals_v2_bundle(
    *,
    decision_candidate: dict[str, Any],
    policy_decision: dict[str, Any],
    planner_primary_action: str,
    dry_run_only: bool = True,
) -> dict[str, Any]:
    """Glue spine inputs to v2 proposals only — no policy evaluation, no candidate construction.

    Callers must supply a real ``DecisionCandidate`` dict and a real ``PolicyDecision`` dict
    (e.g. from ``build_policy_decision`` after ``PolicyEngine``). This helper exists so
    production and offline fixtures share one path and do not hand-roll v2 payloads.
    """
    cand = decision_candidate if isinstance(decision_candidate, dict) else {}
    pd = policy_decision if isinstance(policy_decision, dict) else {}
    cid = str(cand.get("decision_candidate_id") or "").strip()
    pid = str(pd.get("policy_decision_id") or "").strip()
    policy_spine_ok = bool(cid and pid)
    primary_norm = normalize_planner_primary_for_v2(planner_primary_action)
    proposals = build_action_proposals_v2(
        decision_candidate=cand,
        policy_decision=pd,
        primary_action_type=primary_norm,
        dry_run_only=dry_run_only,
    )
    diagnostics = ""
    if not policy_spine_ok:
        missing: list[str] = []
        if not cid:
            missing.append("decision_candidate_id")
        if not pid:
            missing.append("policy_decision_id")
        diagnostics = "missing_spine:" + ",".join(missing)
    elif not proposals:
        diagnostics = "empty_proposals_after_spine"
    return {
        "schema_version": POLICY_GATED_ACTION_PROPOSALS_V2_BUNDLE_VERSION,
        "decision_candidate_id": cid,
        "policy_decision_id": pid,
        "policy_spine_ok": policy_spine_ok,
        "v2_proposals_built": bool(proposals),
        "planner_primary_normalized": primary_norm,
        "action_proposals_v2": proposals,
        "bundle_diagnostics": diagnostics,
    }


def build_action_proposals_v2(
    *,
    decision_candidate: dict[str, Any],
    policy_decision: dict[str, Any],
    primary_action_type: str,
    dry_run_only: bool = True,
) -> list[dict[str, Any]]:
    """One primary proposal derived from candidate + policy (bounded, review-first)."""
    cid = str(decision_candidate.get("decision_candidate_id") or "").strip()
    pid = str(policy_decision.get("policy_decision_id") or "").strip()
    if not cid or not pid:
        return []
    case_id = str(decision_candidate.get("case_id") or "")
    allowed = list(policy_decision.get("allowed_actions") or [])
    status = str(policy_decision.get("status") or "needs_human")

    p_norm, nba_code = _v2_reply_intent_tokens(planner_primary=primary_action_type, decision_candidate=decision_candidate)
    # Planner primaries prepare_reply/hold/ignore plus NBA strings that may only appear on DecisionCandidate.
    wants_prepare = p_norm in {"prepare_reply", "answer_customer", "wait"} or nba_code in {
        "answer_customer",
        "wait",
        "prepare_reply",
    }
    wants_request_info = p_norm in {"request_missing_info", "hold"} or nba_code in {"ask_for_missing_data"}

    action_type = "no_action"
    if status == "requires_adjudication":
        action_type = "ask_for_operator_adjudication"
    elif status in {"needs_human", "blocked"}:
        action_type = "ask_for_operator_adjudication" if "ask_for_operator_adjudication" in allowed else "no_action"
    elif status == "insufficient_context":
        action_type = "request_missing_info" if "request_missing_info" in allowed else "mark_attention_required"
    elif "prepare_reply_draft" in allowed and wants_prepare:
        action_type = "prepare_reply_draft"
    elif "request_missing_info" in allowed and wants_request_info:
        action_type = "request_missing_info"
    elif "mark_attention_required" in allowed:
        action_type = "mark_attention_required"

    if action_type not in P0_SAFE_ACTION_TYPES:
        action_type = "no_action"

    allowed_by = status in {"allowed", "allowed_with_review"} and action_type in allowed
    proposal_id = f"apv2_{hashlib.sha256(f'{cid}|{action_type}'.encode()).hexdigest()[:22]}"

    mode = "dry_run" if dry_run_only else "preview"

    return [
        {
            "schema_version": ACTION_PROPOSAL_V2_SCHEMA_VERSION,
            "proposal_id": proposal_id,
            "decision_candidate_id": cid,
            "policy_decision_id": pid,
            "case_id": case_id,
            "action_type": action_type,
            "action_mode": mode,
            "summary": "Review-first proposal from policy envelope.",
            "recommended_action": str(decision_candidate.get("next_best_action") or action_type),
            "draft_payload": {},
            "risk_class": str(policy_decision.get("risk_class") or "medium"),
            "requires_operator_approval": True,
            "allowed_by_policy": allowed_by,
            "blocked_reason": "" if allowed_by else "policy_status_or_action_not_allowed",
            "evidence_refs": list(decision_candidate.get("evidence_refs") or [])[:16],
            "expires_at": "",
            "status": "proposed",
            "operator_decision": "",
            "execution_result_ref": "",
            "created_at": _utc(),
        }
    ]


def assert_action_proposal_v2_execution_allowed(
    raw: dict[str, Any],
    *,
    dry_run_only: bool = True,
) -> tuple[bool, str]:
    """Guard: v2 proposals require policy linkage; live blocked when dry_run_only."""
    if str(raw.get("schema_version") or "") != ACTION_PROPOSAL_V2_SCHEMA_VERSION:
        return True, ""
    if not str(raw.get("policy_decision_id") or "").strip():
        return False, "policy_decision_id_required"
    if not str(raw.get("decision_candidate_id") or "").strip():
        return False, "decision_candidate_id_required"
    if not bool(raw.get("allowed_by_policy")):
        return False, "not_allowed_by_policy"
    mode = str(raw.get("action_mode") or "preview")
    if dry_run_only and mode == "approved_live":
        return False, "dry_run_only"
    return True, ""
