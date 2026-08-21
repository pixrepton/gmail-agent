"""Canonical policy/action handoff from Brain 1 to Brain 2.

This module is deliberately narrow: it persists formal records, projects a
bounded read-only envelope, and reports consistency. It neither selects nor
blocks tools.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_runtime.tool_result import ToolCallPlan
from llm_contracts.engagement_snapshot_v2 import (
    PolicyActionEnvelopeV1,
    SemanticPolicyPlanConsistencyV1,
)

ACTION_INTENT_TOOL_MAPPING_CLASSIFICATION = "NO_SAFE_MAPPING_EXISTS"

# These tools are formally proven to materialize an operator/execution action.
# This is not an action_type -> tool mapping.
_ACTION_PRODUCING_TOOLS = frozenset({"generate_draft_reply", "propose_mutation"})
_SEMANTIC_ACTION_TOOLS = frozenset(
    {
        "generate_draft_reply",
        "request_operator_clarification",
        "call_kalk_top_quote",
        "propose_mutation",
        "propose_plan",
    }
)
_STALE_PROPOSAL_STATUSES = frozenset({"expired", "stale", "superseded"})
_CUSTOMER_MAIL_TOOLS = ("generate_draft_reply",)
_OPERATOR_CLARIFICATION_TOOL = "request_operator_clarification"


def _next_best_action_dict(candidate: dict[str, Any]) -> dict[str, Any]:
    value = candidate.get("next_best_action")
    return value if isinstance(value, dict) else {}


def _semantic_tool_constraints(
    *,
    proposal_action_type: str,
    candidate: dict[str, Any],
    canonical_decision_id: str = "",
    source_semantic_hash: str = "",
) -> dict[str, Any]:
    """Compile the narrow customer-mail action semantics currently proven by Brain 1.

    This is intentionally smaller than a general action-to-tool vocabulary. The
    first supported invariant is customer missing-data collection: asking the
    customer for missing data must materialize as a draft to the customer, never
    as an operator clarification request.
    """
    proposal_action = str(proposal_action_type or "").strip()
    nba = _next_best_action_dict(candidate)
    nba_action = str(nba.get("action_type") or candidate.get("next_best_action") or "").strip()
    channel = str(nba.get("suggested_channel") or "").strip()

    customer_missing_data = nba_action == "ask_for_missing_data" or proposal_action == "request_missing_info"
    if customer_missing_data:
        return {
            "canonical_decision_id": canonical_decision_id,
            "source_semantic_hash": str(source_semantic_hash or "").strip(),
            "action_target": "customer",
            "action_channel": channel or "mail",
            # allowed_tools is a full planner-turn whitelist. Keep it empty here
            # so safe read-only helpers are not accidentally hidden.
            "allowed_tools": [],
            "allowed_action_tools": list(_CUSTOMER_MAIL_TOOLS),
            "forbidden_tools": [_OPERATOR_CLARIFICATION_TOOL],
        }
    return {
        "canonical_decision_id": canonical_decision_id,
        "source_semantic_hash": str(source_semantic_hash or "").strip(),
        "action_target": "",
        "action_channel": channel,
        "allowed_tools": [],
        "allowed_action_tools": [],
        "forbidden_tools": [],
    }


def _proposal_raw_json(proposal: dict[str, Any]) -> dict[str, Any]:
    raw = proposal.get("raw_json")
    return raw if isinstance(raw, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _candidate_from_intelligence(case_intelligence_result: dict[str, Any]) -> dict[str, Any]:
    candidate = case_intelligence_result.get("decision_candidate")
    if isinstance(candidate, dict):
        return candidate
    pipeline = case_intelligence_result.get("decision_pipeline")
    if not isinstance(pipeline, dict):
        return {}
    outputs = pipeline.get("outputs")
    if not isinstance(outputs, dict):
        return {}
    nested = outputs.get("decision_candidate")
    return nested if isinstance(nested, dict) else {}


def _same(value: Any, expected: str) -> bool:
    return bool(expected) and str(value or "").strip() == expected


def _valid_spine_inputs(
    *,
    candidate: dict[str, Any],
    policy_decision: dict[str, Any],
    case_id: str,
    source_message_id: str,
) -> bool:
    candidate_id = str(candidate.get("decision_candidate_id") or "").strip()
    policy_candidate_id = str(policy_decision.get("decision_candidate_id") or "").strip()
    return bool(
        candidate_id
        and str(policy_decision.get("policy_decision_id") or "").strip()
        and _same(candidate.get("case_id"), case_id)
        and _same(candidate.get("source_signal_id"), source_message_id)
        and policy_candidate_id == candidate_id
    )


def persist_policy_action_spine(
    store: Any,
    *,
    case_intelligence_result: dict[str, Any] | None,
    case_id: str,
    source_signal_id: str,
    source_message_id: str,
) -> dict[str, int | bool]:
    """Append exact PolicyDecision/APv2 records to MailboxMemory.

    The current DecisionCandidate producer calls its message correlation
    ``source_signal_id``. Both that value and the actual CanonicalSignal ID are
    retained explicitly instead of laundering one into the other.
    """
    result: dict[str, int | bool] = {
        "policy_decision_inserted": False,
        "action_proposals_v2_inserted": 0,
    }
    if store is None:
        return result
    if not all(
        hasattr(store, name)
        for name in ("append_policy_decision", "append_action_proposal_v2")
    ):
        return result

    intelligence = (
        case_intelligence_result if isinstance(case_intelligence_result, dict) else {}
    )
    candidate = _candidate_from_intelligence(intelligence)
    policy_decision = intelligence.get("policy_decision")
    if not isinstance(policy_decision, dict):
        return result

    cid = str(case_id or "").strip()
    canonical_signal_id = str(source_signal_id or "").strip()
    message_id = str(source_message_id or "").strip()
    if (
        not cid
        or not canonical_signal_id
        or not message_id
        or not _valid_spine_inputs(
            candidate=candidate,
            policy_decision=policy_decision,
            case_id=cid,
            source_message_id=message_id,
        )
    ):
        return result

    candidate_id = str(candidate.get("decision_candidate_id") or "").strip()
    policy_id = str(policy_decision.get("policy_decision_id") or "").strip()
    evidence_refs = list(candidate.get("evidence_refs") or [])[:24]
    policy_row = {
        "policy_decision_id": policy_id,
        "decision_candidate_id": candidate_id,
        "case_id": cid,
        "source_signal_id": canonical_signal_id,
        "source_message_id": message_id,
        "schema_version": str(policy_decision.get("schema_version") or ""),
        "status": str(policy_decision.get("status") or ""),
        "allowed_actions": list(policy_decision.get("allowed_actions") or []),
        "requires_review": bool(policy_decision.get("requires_review")),
        "requires_human_approval": bool(
            policy_decision.get("requires_human_approval")
        ),
        "policy_basis": list(policy_decision.get("policy_basis") or []),
        "failed_rules": list(policy_decision.get("failed_rules") or []),
        "warnings": list(policy_decision.get("warnings") or []),
        "evidence_refs": evidence_refs,
        "generated_at": str(policy_decision.get("created_at") or ""),
        "raw_json": dict(policy_decision),
    }
    result["policy_decision_inserted"] = bool(
        store.append_policy_decision(policy_row)
    )

    proposals = intelligence.get("action_proposals_v2")
    if not isinstance(proposals, list):
        return result
    inserted = 0
    for raw in proposals:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("schema_version") or "") != "action_proposal.v2":
            continue
        if (
            not _same(raw.get("case_id"), cid)
            or str(raw.get("decision_candidate_id") or "").strip() != candidate_id
            or str(raw.get("policy_decision_id") or "").strip() != policy_id
            or not str(raw.get("proposal_id") or "").strip()
        ):
            continue
        semantic = _semantic_tool_constraints(
            proposal_action_type=str(raw.get("action_type") or ""),
            candidate=candidate,
            canonical_decision_id=str(raw.get("canonical_decision_id") or ""),
            source_semantic_hash=str(raw.get("semantic_hash") or ""),
        )
        proposal_row = {
            "proposal_id": str(raw.get("proposal_id") or "").strip(),
            "policy_decision_id": policy_id,
            "decision_candidate_id": candidate_id,
            "case_id": cid,
            "canonical_decision_id": str(raw.get("canonical_decision_id") or ""),
            "source_semantic_hash": str(raw.get("semantic_hash") or ""),
            "source_signal_id": canonical_signal_id,
            "source_message_id": message_id,
            "schema_version": str(raw.get("schema_version") or ""),
            "action_type": str(raw.get("action_type") or ""),
            "allowed_by_policy": bool(raw.get("allowed_by_policy")),
            "requires_operator_approval": bool(
                raw.get("requires_operator_approval")
            ),
            "status": str(raw.get("status") or ""),
            "action_mode": str(raw.get("action_mode") or ""),
            "blocked_reason": str(raw.get("blocked_reason") or ""),
            "evidence_refs": list(raw.get("evidence_refs") or [])[:24],
            "generated_at": str(raw.get("created_at") or ""),
            "expires_at": str(raw.get("expires_at") or ""),
            "raw_json": {**dict(raw), **semantic},
        }
        inserted += int(bool(store.append_action_proposal_v2(proposal_row)))
    result["action_proposals_v2_inserted"] = inserted
    return result


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_iso(value: Any) -> str:
    parsed = _as_datetime(value)
    if parsed is None:
        return str(value or "")
    return parsed.isoformat().replace("+00:00", "Z")


def _unavailable(reason_code: str) -> PolicyActionEnvelopeV1:
    return PolicyActionEnvelopeV1(
        freshness="unavailable",
        reason_codes=[reason_code],
    )


def project_policy_action_envelope(
    store: Any,
    *,
    case_id: str,
    source_signal_id: str,
    source_message_id: str,
    now: str | datetime | None = None,
) -> PolicyActionEnvelopeV1:
    """Project only records correlated to the exact current signal/message."""
    if store is None or not hasattr(store, "fetch_action_proposals_v2"):
        return _unavailable("canonical_action_proposal_v2_store_unavailable")
    cid = str(case_id or "").strip()
    signal_id = str(source_signal_id or "").strip()
    message_id = str(source_message_id or "").strip()
    if not cid or not signal_id or not message_id:
        return _unavailable("source_correlation_incomplete")

    proposals = store.fetch_action_proposals_v2(
        case_id=cid,
        source_signal_id=signal_id,
        source_message_id=message_id,
        limit=20,
    )
    if not proposals:
        return _unavailable("canonical_action_proposal_v2_not_found")
    proposal = proposals[0]
    policy_id = str(proposal.get("policy_decision_id") or "").strip()
    policy = (
        store.fetch_policy_decision(policy_id)
        if policy_id and hasattr(store, "fetch_policy_decision")
        else None
    )
    if not isinstance(policy, dict):
        return _unavailable("canonical_policy_decision_not_found")
    if (
        not _same(policy.get("case_id"), cid)
        or not _same(policy.get("source_signal_id"), signal_id)
        or not _same(policy.get("source_message_id"), message_id)
        or str(policy.get("decision_candidate_id") or "").strip()
        != str(proposal.get("decision_candidate_id") or "").strip()
    ):
        return _unavailable("canonical_policy_action_correlation_mismatch")

    reason_codes: list[str] = []
    freshness = "current"
    status = str(proposal.get("status") or "").strip().lower()
    if status in _STALE_PROPOSAL_STATUSES:
        freshness = "stale"
        reason_codes.append(f"proposal_status_{status}")
    expires_at = _as_datetime(proposal.get("expires_at"))
    now_at = _as_datetime(now) if now is not None else datetime.now(timezone.utc)
    if expires_at is not None and now_at is not None and expires_at <= now_at:
        freshness = "stale"
        if "proposal_expired" not in reason_codes:
            reason_codes.append("proposal_expired")

    raw_json = _proposal_raw_json(proposal)
    return PolicyActionEnvelopeV1(
        decision_candidate_id=str(proposal.get("decision_candidate_id") or ""),
        policy_decision_id=policy_id,
        action_proposal_id=str(proposal.get("proposal_id") or ""),
        source_signal_id=signal_id,
        source_message_id=message_id,
        policy_status=str(policy.get("status") or ""),
        action_intent=str(proposal.get("action_type") or ""),
        canonical_decision_id=str(raw_json.get("canonical_decision_id") or ""),
        source_semantic_hash=str(
            raw_json.get("source_semantic_hash")
            or proposal.get("source_semantic_hash")
            or ""
        ),
        action_target=str(raw_json.get("action_target") or ""),
        action_channel=str(raw_json.get("action_channel") or ""),
        allowed_tools=_string_list(raw_json.get("allowed_tools")),
        allowed_action_tools=_string_list(raw_json.get("allowed_action_tools")),
        forbidden_tools=_string_list(raw_json.get("forbidden_tools")),
        allowed_by_policy=bool(proposal.get("allowed_by_policy")),
        requires_operator_approval=bool(
            proposal.get("requires_operator_approval")
        ),
        freshness=freshness,
        proposal_status=str(proposal.get("status") or ""),
        reason_codes=reason_codes,
        generated_at=_as_iso(proposal.get("generated_at")),
        expires_at=_as_iso(proposal.get("expires_at")),
    )


def correlate_tool_plan(
    plan: ToolCallPlan,
    envelope: PolicyActionEnvelopeV1 | None,
) -> ToolCallPlan:
    """Attach input correlation to a plan without changing its tool or arguments."""
    if envelope is None or envelope.freshness == "unavailable":
        return plan.model_copy(
            update={
                "policy_decision_id": "",
                "action_proposal_id": "",
                "correlation_status": "missing_policy_envelope",
            }
        )
    policy_id = str(plan.policy_decision_id or envelope.policy_decision_id)
    proposal_id = str(plan.action_proposal_id or envelope.action_proposal_id)
    if envelope.freshness == "stale":
        correlation_status = "stale_policy_envelope"
    elif (
        policy_id == envelope.policy_decision_id
        and proposal_id == envelope.action_proposal_id
    ):
        correlation_status = "correlated"
    else:
        correlation_status = "conflicting"
    return plan.model_copy(
        update={
            "policy_decision_id": policy_id,
            "action_proposal_id": proposal_id,
            "correlation_status": correlation_status,
        }
    )


def evaluate_semantic_policy_plan_consistency(
    envelope: PolicyActionEnvelopeV1 | None,
    plan: ToolCallPlan,
) -> SemanticPolicyPlanConsistencyV1:
    """Observe policy/plan correlation; never mutate or authorize the plan."""
    if envelope is None or envelope.freshness == "unavailable":
        reasons = (
            list(envelope.reason_codes)
            if envelope is not None and envelope.reason_codes
            else ["policy_action_envelope_absent"]
        )
        return _consistency("missing_policy_envelope", reasons, envelope, plan)
    if envelope.freshness == "stale":
        return _consistency(
            "stale_policy_envelope",
            list(envelope.reason_codes) or ["policy_action_envelope_stale"],
            envelope,
            plan,
        )
    if not str(plan.policy_decision_id or "").strip() or not str(
        plan.action_proposal_id or ""
    ).strip():
        return _consistency(
            "missing_plan_correlation",
            ["planner_correlation_ids_missing"],
            envelope,
            plan,
        )

    mismatches: list[str] = []
    if plan.policy_decision_id != envelope.policy_decision_id:
        mismatches.append("policy_decision_id_mismatch")
    if plan.action_proposal_id != envelope.action_proposal_id:
        mismatches.append("action_proposal_id_mismatch")
    if mismatches:
        return _consistency("conflicting", mismatches, envelope, plan)

    expected_hash = str(getattr(envelope, "source_semantic_hash", "") or "").strip()
    observed_hash = str(getattr(plan, "semantic_hash", "") or "").strip()
    if expected_hash and observed_hash and expected_hash != observed_hash:
        return _consistency(
            "conflicting",
            ["canonical_semantic_drift"],
            envelope,
            plan,
        )

    forbidden_tools = {str(item).strip() for item in envelope.forbidden_tools if str(item).strip()}
    allowed_tools = {str(item).strip() for item in envelope.allowed_tools if str(item).strip()}
    allowed_action_tools = {
        str(item).strip()
        for item in envelope.allowed_action_tools
        if str(item).strip()
    }
    if plan.tool_name in forbidden_tools:
        reason_codes: list[str] = ["semantic_tool_forbidden_for_action_intent"]
        if str(getattr(envelope, "canonical_decision_id", "") or "").strip():
            reason_codes.append("canonical_semantic_drift")
        return _consistency(
            "conflicting",
            reason_codes,
            envelope,
            plan,
        )
    if allowed_tools:
        if plan.tool_name not in allowed_tools:
            return _consistency(
                "conflicting",
                ["semantic_tool_not_allowed_for_action_intent"],
                envelope,
                plan,
            )
        return _consistency(
            "consistent",
            ["semantic_tool_allowed_for_action_intent"],
            envelope,
            plan,
        )
    if allowed_action_tools and plan.tool_name in _SEMANTIC_ACTION_TOOLS:
        if plan.tool_name not in allowed_action_tools:
            return _consistency(
                "conflicting",
                ["semantic_tool_not_allowed_for_action_intent"],
                envelope,
                plan,
            )
        return _consistency(
            "consistent",
            ["semantic_action_tool_allowed_for_action_intent"],
            envelope,
            plan,
        )

    blocked = envelope.policy_status in {
        "blocked",
        "needs_human",
        "insufficient_context",
        "requires_adjudication",
        "dry_run_only",
    } or envelope.allowed_by_policy is False
    if blocked and plan.tool_name in _ACTION_PRODUCING_TOOLS:
        return _consistency(
            "conflicting",
            ["policy_blocks_actionable_tool"],
            envelope,
            plan,
        )

    return _consistency(
        "not_evaluable",
        ["no_formal_action_intent_tool_mapping"],
        envelope,
        plan,
    )


def _consistency(
    status: str,
    reason_codes: list[str],
    envelope: PolicyActionEnvelopeV1 | None,
    plan: ToolCallPlan,
) -> SemanticPolicyPlanConsistencyV1:
    return SemanticPolicyPlanConsistencyV1(
        status=status,
        reason_codes=[str(item)[:160] for item in reason_codes[:8]],
        policy_decision_id=str(
            (envelope.policy_decision_id if envelope is not None else "")
        ),
        action_proposal_id=str(
            (envelope.action_proposal_id if envelope is not None else "")
        ),
        tool_name=str(plan.tool_name or ""),
        mapping_classification=ACTION_INTENT_TOOL_MAPPING_CLASSIFICATION,
    )


def annotate_action_parent_refs(
    snapshot_delta: dict[str, Any],
    *,
    plan: ToolCallPlan,
    envelope: PolicyActionEnvelopeV1 | None,
) -> dict[str, Any]:
    """Add parent refs to newly materialized snapshot actions when IDs agree."""
    if (
        envelope is None
        or envelope.freshness != "current"
        or not envelope.policy_decision_id
        or not envelope.action_proposal_id
        or plan.policy_decision_id != envelope.policy_decision_id
        or plan.action_proposal_id != envelope.action_proposal_id
    ):
        return snapshot_delta
    actions = snapshot_delta.get("actions")
    if not isinstance(actions, list):
        return snapshot_delta
    annotated: list[Any] = []
    for item in actions:
        if not isinstance(item, dict):
            annotated.append(item)
            continue
        annotated.append(
            {
                **item,
                "parent_policy_decision_id": envelope.policy_decision_id,
                "parent_action_proposal_v2_id": envelope.action_proposal_id,
                "parent_decision_candidate_id": envelope.decision_candidate_id,
                "source_semantic_hash": getattr(
                    envelope, "source_semantic_hash", ""
                ),
                "source_signal_id": envelope.source_signal_id,
                # AI-OS-CANONICAL-DRAFT-IDENTITY-01: only ever set to "complete" here,
                # where real correlation to a fresh, id-matching envelope was just
                # proven above -- everywhere else this stays the honest default
                # ("identity_incomplete") rather than being guessed.
                "identity_state": "complete",
            }
        )
    return {**snapshot_delta, "actions": annotated}


__all__ = [
    "ACTION_INTENT_TOOL_MAPPING_CLASSIFICATION",
    "annotate_action_parent_refs",
    "correlate_tool_plan",
    "evaluate_semantic_policy_plan_consistency",
    "persist_policy_action_spine",
    "project_policy_action_envelope",
]
