"""Supervised execution loop for Daszek AI-native V1.

Python owns the proposal, policy gate, execution result, and audit trail.
Daszek may transport an owner decision, but it is not the business source of
truth.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from permissions import PermissionDenied, actor_role, require_owner


ACTION_TYPES = (
    "set_case_status",
    "mark_attention_required",
    "prepare_reply_draft",
    "apply_gmail_label",
    "archive_gmail",
    "create_calendar_event",
)
CASE_STATUSES = ("new", "needs_review", "waiting_for_customer", "ready_to_reply", "blocked", "done")
RISK_CLASSES = ("R0", "R1", "R2", "R3", "R4")
PROPOSAL_STATUSES = ("proposed", "approved", "rejected", "executed", "failed", "blocked")
EXECUTION_STATUSES = ("executed", "failed", "blocked", "skipped")

EXTERNAL_WRITE_ACTIONS = {"apply_gmail_label", "archive_gmail", "create_calendar_event"}
FORBIDDEN_ACTION_TYPES = {"send_email", "auto_send", "delete_email", "auto_delete", "auto_close_case"}


@dataclass(slots=True)
class ActionProposal:
    proposal_id: str
    case_id: str
    action_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    source_signal_id: str = ""
    proposed_by: str = "ai"
    confidence: float = 0.0
    risk_class: str = "R1"
    requires_review: bool = True
    policy_basis: list[str] = field(default_factory=list)
    created_at: str = ""
    status: str = "proposed"
    decision_reason: str = ""
    decided_by: str = ""
    decided_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExecutionResult:
    execution_id: str
    proposal_id: str
    case_id: str
    action_type: str
    approved_by: str = ""
    approved_at: str = ""
    executed_by: str = "agent_service"
    executed_at: str = ""
    execution_status: str = "skipped"
    error_code: str = ""
    error_message: str = ""
    result_payload: dict[str, Any] = field(default_factory=dict)
    audit_trace_id: str = ""
    policy_result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def stable_id(prefix: str, *parts: Any) -> str:
    raw = json.dumps([str(part) for part in parts], ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def action_risk_class(action_type: str, payload: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    action = str(action_type or "").strip()
    payload = payload or {}
    if action in FORBIDDEN_ACTION_TYPES:
        return "R4", ["forbidden_in_v1"]
    if action == "prepare_reply_draft":
        return "R1", ["draft_only_no_send"]
    if action == "mark_attention_required":
        return "R1", ["internal_annotation"]
    if action == "set_case_status":
        target = str(payload.get("status") or "").strip()
        if target == "done":
            return "R2", ["case_done_requires_owner_review"]
        return "R1", ["internal_case_state_update"]
    if action in {"apply_gmail_label", "archive_gmail"}:
        return "R2", ["gmail_external_write_requires_owner_review", "live_write_not_proven_in_repo"]
    if action == "create_calendar_event":
        return "R4", ["calendar_write_disabled_read_only"]
    return "R4", ["unknown_action_type_forbidden"]


def normalize_action_proposal(raw: dict[str, Any]) -> ActionProposal:
    payload = dict(raw.get("payload") or {}) if isinstance(raw.get("payload"), dict) else {}
    action_type = str(raw.get("action_type") or raw.get("type") or "").strip()
    risk, basis = action_risk_class(action_type, payload)
    created_at = str(raw.get("created_at") or now_iso())
    case_id = str(raw.get("case_id") or "").strip()
    proposal_id = str(raw.get("proposal_id") or "").strip()
    if not proposal_id:
        proposal_id = stable_id("proposal", case_id, action_type, payload, created_at)
    status = str(raw.get("status") or "proposed").strip()
    if status not in PROPOSAL_STATUSES:
        status = "proposed"
    return ActionProposal(
        proposal_id=proposal_id,
        case_id=case_id,
        source_signal_id=str(raw.get("source_signal_id") or ""),
        action_type=action_type,
        payload=payload,
        proposed_by=str(raw.get("proposed_by") or "ai"),
        confidence=max(0.0, min(1.0, float(raw.get("confidence") or 0.0))),
        risk_class=str(raw.get("risk_class") or risk),
        requires_review=bool(raw.get("requires_review", True)),
        policy_basis=list(raw.get("policy_basis") or basis),
        created_at=created_at,
        status=status,
        decision_reason=str(raw.get("decision_reason") or ""),
        decided_by=str(raw.get("decided_by") or ""),
        decided_at=str(raw.get("decided_at") or ""),
    )


def policy_gate(proposal: ActionProposal, *, approved_by: str = "") -> dict[str, Any]:
    risk, basis = action_risk_class(proposal.action_type, proposal.payload)
    approved = bool(approved_by)
    allowed = risk in {"R0", "R1"} or (risk == "R2" and approved)
    if risk in {"R3", "R4"}:
        allowed = False
    return {
        "allowed": allowed,
        "risk_class": risk,
        "requires_review": risk in {"R2", "R3", "R4"} or proposal.requires_review,
        "approved_by_required": risk == "R2",
        "policy_basis": list(dict.fromkeys([*proposal.policy_basis, *basis])),
        "forbidden": risk == "R4",
    }


def create_action_proposal(store: Any, raw: dict[str, Any]) -> ActionProposal:
    proposal = normalize_action_proposal(raw)
    if not proposal.case_id:
        raise ValueError("case_id is required")
    if proposal.action_type not in ACTION_TYPES and proposal.risk_class != "R4":
        raise ValueError(f"unsupported action_type: {proposal.action_type}")
    store.upsert_action_proposal(proposal.to_dict())
    _append_execution_event(
        store,
        case_id=proposal.case_id,
        event_type="action_proposal_created",
        summary=f"Action proposal created: {proposal.action_type}",
        payload=proposal.to_dict(),
    )
    return proposal


def approve_action_proposal(store: Any, proposal_id: str, *, approved_by: str, reason: str = "") -> ActionProposal:
    require_owner(approved_by)
    proposal = _load_proposal(store, proposal_id)
    if proposal.status not in {"proposed", "blocked", "failed"}:
        raise ValueError(f"proposal is not approvable from status={proposal.status}")
    proposal.status = "approved"
    proposal.decided_by = approved_by
    proposal.decided_at = now_iso()
    proposal.decision_reason = str(reason or "")
    store.upsert_action_proposal(proposal.to_dict())
    _append_execution_event(
        store,
        case_id=proposal.case_id,
        event_type="action_proposal_approved",
        summary=f"Owner approved action proposal: {proposal.action_type}",
        payload=proposal.to_dict(),
    )
    return proposal


def _rejected_execution_result(
    proposal: ActionProposal,
    *,
    rejected_by: str,
    decision_key: str = "",
) -> ExecutionResult:
    decided_at = str(proposal.decided_at or now_iso())
    resolved_decision_key = str(decision_key or stable_id("decision", proposal.proposal_id, "reject"))
    return ExecutionResult(
        execution_id=stable_id("execution", proposal.proposal_id, "decision_rejected"),
        proposal_id=proposal.proposal_id,
        case_id=proposal.case_id,
        action_type=proposal.action_type,
        approved_by="",
        approved_at="",
        executed_by=rejected_by,
        executed_at=decided_at,
        execution_status="skipped",
        error_code="decision_rejected",
        error_message="proposal rejected by owner",
        result_payload={
            "decision_status": "rejected",
            "decision_key": resolved_decision_key,
            "proposal_status": "rejected",
        },
        audit_trace_id=stable_id("audit", proposal.proposal_id, "decision_rejected"),
        policy_result={},
    )


def reject_action_proposal(
    store: Any,
    proposal_id: str,
    *,
    rejected_by: str,
    reason: str = "",
    decision_key: str = "",
) -> ActionProposal:
    require_owner(rejected_by)
    proposal = _load_proposal(store, proposal_id)
    if proposal.status == "rejected":
        result = _rejected_execution_result(proposal, rejected_by=rejected_by, decision_key=decision_key)
        store.upsert_execution_result(result.to_dict())
        return proposal
    if proposal.status in {"approved", "executed"}:
        raise ValueError(f"cannot reject proposal from status={proposal.status}")
    if proposal.status not in {"proposed", "blocked", "failed"}:
        raise ValueError(f"proposal is not rejectable from status={proposal.status}")
    proposal.status = "rejected"
    proposal.decided_by = rejected_by
    proposal.decided_at = now_iso()
    proposal.decision_reason = str(reason or "")
    store.upsert_action_proposal(proposal.to_dict())
    store.upsert_execution_result(
        _rejected_execution_result(proposal, rejected_by=rejected_by, decision_key=decision_key).to_dict()
    )
    _append_execution_event(
        store,
        case_id=proposal.case_id,
        event_type="action_proposal_rejected",
        summary=f"Owner rejected action proposal: {proposal.action_type}",
        payload=proposal.to_dict(),
        event_id=stable_id("event", "action_proposal_rejected", proposal.case_id, proposal.proposal_id, "rejected"),
    )
    return proposal


def execute_action_proposal(
    store: Any,
    proposal_id: str,
    *,
    executed_by: str,
    dry_run: bool = True,
    calendar_client: Any = None,
) -> ExecutionResult:
    require_owner(executed_by)
    proposal = _load_proposal(store, proposal_id)
    if proposal.status != "approved":
        result = _execution_result(
            proposal,
            approved_by=proposal.decided_by,
            approved_at=proposal.decided_at,
            executed_by=executed_by,
            execution_status="blocked",
            error_code="not_approved",
            error_message="proposal must be approved before execution",
        )
        _persist_execution_result(store, proposal, result)
        return result

    gate = policy_gate(proposal, approved_by=proposal.decided_by)
    if not gate["allowed"]:
        proposal.status = "blocked"
        store.upsert_action_proposal(proposal.to_dict())
        result = _execution_result(
            proposal,
            approved_by=proposal.decided_by,
            approved_at=proposal.decided_at,
            executed_by=executed_by,
            execution_status="blocked",
            error_code="policy_blocked",
            error_message="policy gate blocked this action",
            policy_result=gate,
        )
        _persist_execution_result(store, proposal, result)
        return result

    result_payload: dict[str, Any] = {}
    status = "executed"
    error_code = ""
    error_message = ""
    try:
        if proposal.action_type == "set_case_status":
            result_payload = _execute_set_case_status(store, proposal)
        elif proposal.action_type == "mark_attention_required":
            result_payload = {"attention_required": True, "case_id": proposal.case_id}
        elif proposal.action_type == "prepare_reply_draft":
            result_payload = {"draft": proposal.payload, "no_send": True}
        elif proposal.action_type in {"apply_gmail_label", "archive_gmail"}:
            status = "skipped" if dry_run else "blocked"
            error_code = "not_proven_live" if dry_run else "missing_safe_gmail_write_wrapper"
            error_message = "repo-local Gmail helper is read-only; no live Gmail write was executed"
            result_payload = {"dry_run": dry_run, "not_proven_live": True}
        elif proposal.action_type == "create_calendar_event":
            status = "blocked"
            error_code = "calendar_write_disabled_read_only"
            error_message = "Node B never creates, updates or deletes Google Calendar events"
            result_payload = {"manual_operator_delivery": True, "calendar_write_attempted": False}
        else:
            status = "blocked"
            error_code = "unsupported_action"
            error_message = f"unsupported action_type={proposal.action_type}"
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error_code = "execution_error"
        error_message = str(exc)

    proposal.status = "executed" if status == "executed" else ("blocked" if status == "blocked" else proposal.status)
    store.upsert_action_proposal(proposal.to_dict())
    result = _execution_result(
        proposal,
        approved_by=proposal.decided_by,
        approved_at=proposal.decided_at,
        executed_by=executed_by,
        execution_status=status,
        error_code=error_code,
        error_message=error_message,
        result_payload=result_payload,
        policy_result=gate,
    )
    _persist_execution_result(store, proposal, result)
    return result


def _execute_set_case_status(store: Any, proposal: ActionProposal) -> dict[str, Any]:
    target = str(proposal.payload.get("status") or "").strip()
    if target not in CASE_STATUSES:
        raise ValueError(f"unsupported case status: {target}")
    case = store.fetch_case(proposal.case_id) or {"case_id": proposal.case_id}
    previous = str(case.get("status") or "")
    case["status"] = target
    case["updated_at"] = now_iso()
    metadata = dict(case.get("metadata") or {}) if isinstance(case.get("metadata"), dict) else {}
    metadata["status_updated_by_execution"] = True
    metadata["last_status_proposal_id"] = proposal.proposal_id
    case["metadata"] = metadata
    store.upsert_case(case)
    return {"previous_status": previous, "new_status": target}


def _load_proposal(store: Any, proposal_id: str) -> ActionProposal:
    row = store.fetch_action_proposal(str(proposal_id or "").strip())
    if not row:
        raise KeyError(f"ActionProposal not found: {proposal_id}")
    return normalize_action_proposal(row)


def _execution_result(
    proposal: ActionProposal,
    *,
    approved_by: str,
    approved_at: str,
    executed_by: str,
    execution_status: str,
    error_code: str = "",
    error_message: str = "",
    result_payload: dict[str, Any] | None = None,
    policy_result: dict[str, Any] | None = None,
) -> ExecutionResult:
    executed_at = now_iso()
    return ExecutionResult(
        execution_id=stable_id("execution", proposal.proposal_id, execution_status, executed_at),
        proposal_id=proposal.proposal_id,
        case_id=proposal.case_id,
        action_type=proposal.action_type,
        approved_by=approved_by,
        approved_at=approved_at,
        executed_by=executed_by,
        executed_at=executed_at,
        execution_status=execution_status if execution_status in EXECUTION_STATUSES else "failed",
        error_code=error_code,
        error_message=error_message,
        result_payload=result_payload or {},
        audit_trace_id=stable_id("audit", proposal.proposal_id, executed_at),
        policy_result=policy_result or policy_gate(proposal, approved_by=approved_by),
    )


def _persist_execution_result(store: Any, proposal: ActionProposal, result: ExecutionResult) -> None:
    store.upsert_execution_result(result.to_dict())
    _append_execution_event(
        store,
        case_id=proposal.case_id,
        event_type="action_execution_result",
        summary=f"Action execution {result.execution_status}: {proposal.action_type}",
        payload=result.to_dict(),
    )


def _append_execution_event(
    store: Any,
    *,
    case_id: str,
    event_type: str,
    summary: str,
    payload: dict[str, Any],
    event_id: str = "",
) -> None:
    occurred_at = now_iso()
    store.append_event(
        {
            "event_id": event_id or stable_id("event", event_type, case_id, payload.get("proposal_id"), occurred_at),
            "case_id": case_id,
            "message_id": "",
            "thread_id": "",
            "event_type": event_type,
            "occurred_at": occurred_at,
            "summary_text": summary,
            "payload": payload,
            "source_refs": [{"type": "case", "case_id": case_id}],
        }
    )


__all__ = [
    "ACTION_TYPES",
    "CASE_STATUSES",
    "EXECUTION_STATUSES",
    "PROPOSAL_STATUSES",
    "RISK_CLASSES",
    "ActionProposal",
    "ExecutionResult",
    "PermissionDenied",
    "action_risk_class",
    "approve_action_proposal",
    "create_action_proposal",
    "execute_action_proposal",
    "normalize_action_proposal",
    "policy_gate",
    "reject_action_proposal",
]
