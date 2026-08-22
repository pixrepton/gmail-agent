"""P1.2B: canonical write-execution argument binding for the post-HITL boundary.

Answers mechanically, before any manual-delivery/write authorization:

    What exactly was approved?
    For which decision revision?
    For which draft?
    For which case/thread?
    For which recipient?

This module reuses the P1.2A ``ArgumentConstraint`` contract (EXACT/ABSENT/...)
instead of introducing a new constraint framework. It never authorizes a
mutation; it only returns a deterministic PASS/DENY verdict with reason codes
so the caller can fail closed. Planner output never establishes canonical
execution state here -- the canonical reference is projected from the approved
snapshot (action draft identity + communication receipt) and the durable
DecisionRevisionLedger (P1.1P).
"""

from __future__ import annotations

import re
from typing import Any

from agent_runtime.tool_argument_constraints import (
    REASON_ARGUMENT_NOT_ALLOWED,
    REASON_ARGUMENT_OUTSIDE_CANONICAL_SET,
    REASON_CANONICAL_ARGUMENT_MISMATCH,
    REASON_MISSING_REQUIRED_CANONICAL_ARGUMENT,
    REASON_STALE_DECISION_REVISION,
    REASON_UNBOUND_EXECUTION_ARGUMENT,
    build_argument_constraint,
    constraint_violations,
    violations_reason_codes,
)

# Minimal additions to the existing failure taxonomy: approval presence and
# approval-artifact identity. Everything else reuses P1.2A reason codes.
REASON_APPROVAL_MISSING = "APPROVAL_MISSING"
REASON_APPROVAL_ARTIFACT_MISMATCH = "APPROVAL_ARTIFACT_MISMATCH"

VERDICT_READY = "WRITE_BOUNDARY_READY"
VERDICT_DENIED = "WRITE_BOUNDARY_DENIED"

_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)


class WriteBoundaryDeniedError(RuntimeError):
    """Fail-closed write-boundary denial carrying the deterministic verdict.

    Raised by the production seam so the bridge queue records a failed row and
    never authorizes manual delivery of an artifact that does not match the
    approved snapshot / current durable decision revision.
    """

    def __init__(self, verdict: dict[str, Any]) -> None:
        self.verdict = dict(verdict or {})
        codes = self.verdict.get("reason_codes") or []
        super().__init__("WRITE_BOUNDARY_DENIED:" + ",".join(str(c) for c in codes))


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _norm_email(value: Any) -> str:
    match = _EMAIL_RE.search(_text(value))
    return str(match.group(0) if match else "").strip().lower()


def find_approved_action(snapshot: Any, action_id: str) -> Any | None:
    """Find the enabled action with the requested id on the snapshot."""
    if snapshot is None:
        return None
    for action in getattr(snapshot, "actions", None) or []:
        if str(getattr(action, "id", "") or "") == str(action_id or ""):
            return action
    return None


def approved_write_reference(snapshot: Any, action_id: str) -> dict[str, Any]:
    """Canonical approved-artifact reference projected from the snapshot.

    Pure projection; never guesses values and never takes planner/row values.
    Empty fields are honest defaults ("not bound"), which the evaluator treats
    as UNBOUND_EXECUTION_ARGUMENT (fail closed).
    """
    action = find_approved_action(snapshot, action_id)
    receipt = getattr(snapshot, "communication_receipt", None)
    envelope = getattr(snapshot, "policy_action_envelope", None)
    action_version = (
        _text(getattr(action, "decision_version_id", "")) if action is not None else ""
    )
    envelope_version = (
        _text(getattr(envelope, "decision_version_id", "")) if envelope is not None else ""
    )
    action_hash = (
        _text(getattr(action, "source_semantic_hash", "")) if action is not None else ""
    )
    envelope_hash = (
        _text(getattr(envelope, "source_semantic_hash", "")) if envelope is not None else ""
    )
    return {
        "action_id": _text(action_id),
        "case_id": _text(getattr(snapshot, "case_id", "")),
        "draft_id": _text(getattr(action, "draft_id", "") if action is not None else ""),
        "body_hash": _text(getattr(action, "body_hash", "") if action is not None else ""),
        "revision": int(getattr(action, "revision", 1) or 1) if action is not None else 0,
        "decision_id": _text(
            getattr(envelope, "canonical_decision_id", "") if envelope is not None else ""
        ),
        "decision_version_id": action_version or envelope_version,
        "semantic_hash": action_hash or envelope_hash,
        "target_email": _norm_email(
            getattr(receipt, "target_email", "") if receipt is not None else ""
        ),
        "thread_id": _text(getattr(receipt, "thread_id", "") if receipt is not None else ""),
        "receipt_state": _text(getattr(receipt, "state", "") if receipt is not None else ""),
        "receipt_draft_id": _text(
            getattr(receipt, "draft_id", "") if receipt is not None else ""
        ),
        "receipt_body_hash": _text(
            getattr(receipt, "body_hash", "") if receipt is not None else ""
        ),
        "action_present": bool(action is not None and getattr(action, "enabled", False)),
    }


def project_write_execution_constraints(
    reference: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deterministic EXACT/ABSENT constraint projection for the write artifact.

    Every execution-critical field must equal the approved canonical value;
    thread_id is deliberately NOT part of this projection: the write boundary
    derives the thread exclusively from the runtime-owned canonical resolution
    (see evaluate_write_execution_binding), so no artifact input can claim one.
    """
    common = {
        "source_kind": "approved_hitl_artifact",
        "source_ref": _text(reference.get("decision_version_id")),
        "decision_id": _text(reference.get("decision_id")),
        "decision_version_id": _text(reference.get("decision_version_id")),
        "semantic_hash": _text(reference.get("semantic_hash")),
    }
    return [
        build_argument_constraint(
            argument_name="case_id",
            constraint_mode="EXACT",
            expected_value=reference.get("case_id"),
            **common,
        ),
        build_argument_constraint(
            argument_name="draft_id",
            constraint_mode="EXACT",
            expected_value=reference.get("draft_id"),
            **common,
        ),
        build_argument_constraint(
            argument_name="body_hash",
            constraint_mode="EXACT",
            expected_value=reference.get("body_hash"),
            **common,
        ),
        build_argument_constraint(
            argument_name="revision",
            constraint_mode="EXACT",
            expected_value=int(reference.get("revision") or 0),
            **common,
        ),
        build_argument_constraint(
            argument_name="decision_version_id",
            constraint_mode="EXACT",
            expected_value=reference.get("decision_version_id"),
            **common,
        ),
        build_argument_constraint(
            argument_name="semantic_hash",
            constraint_mode="EXACT",
            expected_value=reference.get("semantic_hash"),
            **common,
        ),
        build_argument_constraint(
            argument_name="recipient",
            constraint_mode="EXACT",
            expected_value=reference.get("target_email"),
            **common,
        ),
    ]


def evaluate_write_execution_binding(
    *,
    snapshot: Any,
    action_id: str,
    proposed: dict[str, Any] | None = None,
    resolved_target: dict[str, Any] | None = None,
    ledger: Any | None = None,
    expected_body_hash: str | None = None,
) -> dict[str, Any]:
    """Deterministic write-boundary authorization verdict.

    Args:
        snapshot: approved EngagementSnapshotV2 (hitl_gate cleared, receipt set).
        action_id: action whose draft is being delivered.
        proposed: execution artifact fields the caller wants to authorize
            (case_id, draft_id, body_hash, revision, decision_version_id,
            semantic_hash, recipient, thread_id). No planner values are
            accepted as canonical state.
        resolved_target: runtime-owned recipient/thread resolution
            ({"to": ..., "thread_id": ...}); merged into proposed.
        ledger: store-backed DecisionRevisionLedger (P1.1P) used to resolve the
            current durable revision. None => current revision unavailable =>
            UNBOUND_EXECUTION_ARGUMENT (fail closed).
        expected_body_hash: caller's approve-packet body hash (what the operator
            saw). When provided it must equal the approved action body_hash;
            a stale preview packet is denied even if the body itself matches.
    """
    reference = approved_write_reference(snapshot, action_id)
    base = {
        "source_kind": "approved_hitl_artifact",
        "source_ref": reference["decision_version_id"],
        "decision_id": reference["decision_id"],
        "decision_version_id": reference["decision_version_id"],
        "semantic_hash": reference["semantic_hash"],
    }
    violations: list[dict[str, Any]] = []
    reason_codes: list[str] = []

    hitl = getattr(snapshot, "hitl_gate", None)
    if hitl is not None and bool(getattr(hitl, "required", False)):
        violations.append(
            {
                "argument_name": "approval",
                "constraint_mode": "PRESENT",
                "reason_code": REASON_APPROVAL_MISSING,
                "expected": "operator approval granted",
                "proposed": "hitl_gate.required=true",
                **base,
            }
        )
        reason_codes.append(REASON_APPROVAL_MISSING)

    if reference["receipt_state"] != "ready_for_manual_send" or not reference["body_hash"]:
        violations.append(
            {
                "argument_name": "approval_receipt",
                "constraint_mode": "EXACT",
                "reason_code": REASON_APPROVAL_ARTIFACT_MISMATCH,
                "expected": "ready_for_manual_send with body_hash",
                "proposed": reference["receipt_state"],
                **base,
            }
        )
        reason_codes.append(REASON_APPROVAL_ARTIFACT_MISMATCH)

    if (
        reference["receipt_draft_id"]
        and reference["draft_id"]
        and reference["receipt_draft_id"] != reference["draft_id"]
    ):
        violations.append(
            {
                "argument_name": "draft_id",
                "constraint_mode": "EXACT",
                "reason_code": REASON_APPROVAL_ARTIFACT_MISMATCH,
                "expected": reference["draft_id"],
                "proposed": reference["receipt_draft_id"],
                **base,
            }
        )
        reason_codes.append(REASON_APPROVAL_ARTIFACT_MISMATCH)

    if (
        reference["receipt_body_hash"]
        and reference["body_hash"]
        and reference["receipt_body_hash"] != reference["body_hash"]
    ):
        violations.append(
            {
                "argument_name": "body_hash",
                "constraint_mode": "EXACT",
                "reason_code": REASON_APPROVAL_ARTIFACT_MISMATCH,
                "expected": reference["body_hash"],
                "proposed": reference["receipt_body_hash"],
                **base,
            }
        )
        reason_codes.append(REASON_APPROVAL_ARTIFACT_MISMATCH)

    if not reference["action_present"] or not reference["body_hash"]:
        violations.append(
            {
                "argument_name": "approved_action",
                "constraint_mode": "PRESENT",
                "reason_code": REASON_UNBOUND_EXECUTION_ARGUMENT,
                "expected": "enabled action with draft identity",
                "proposed": "missing",
                **base,
            }
        )
        reason_codes.append(REASON_UNBOUND_EXECUTION_ARGUMENT)

    if not reference["decision_id"] or not reference["decision_version_id"]:
        violations.append(
            {
                "argument_name": "decision_version_id",
                "constraint_mode": "EXACT",
                "reason_code": REASON_UNBOUND_EXECUTION_ARGUMENT,
                "expected": "canonical decision version binding",
                "proposed": reference["decision_version_id"],
                **base,
            }
        )
        reason_codes.append(REASON_UNBOUND_EXECUTION_ARGUMENT)
    elif ledger is None:
        violations.append(
            {
                "argument_name": "decision_version_id",
                "constraint_mode": "EXACT",
                "reason_code": REASON_UNBOUND_EXECUTION_ARGUMENT,
                "expected": "current durable revision",
                "proposed": reference["decision_version_id"],
                **base,
            }
        )
        reason_codes.append(REASON_UNBOUND_EXECUTION_ARGUMENT)
    else:
        current = ledger.current_cad(reference["decision_id"])
        durable_version = (
            _text(current.get("decision_version_id")) if current is not None else ""
        )
        if not durable_version:
            violations.append(
                {
                    "argument_name": "decision_version_id",
                    "constraint_mode": "EXACT",
                    "reason_code": REASON_UNBOUND_EXECUTION_ARGUMENT,
                    "expected": "current durable revision",
                    "proposed": reference["decision_version_id"],
                    **base,
                }
            )
            reason_codes.append(REASON_UNBOUND_EXECUTION_ARGUMENT)
        elif durable_version != reference["decision_version_id"]:
            violations.append(
                {
                    "argument_name": "decision_version_id",
                    "constraint_mode": "EXACT",
                    "reason_code": REASON_STALE_DECISION_REVISION,
                    "expected": durable_version,
                    "proposed": reference["decision_version_id"],
                    **base,
                }
            )
            reason_codes.append(REASON_STALE_DECISION_REVISION)

    if not reference["semantic_hash"]:
        violations.append(
            {
                "argument_name": "semantic_hash",
                "constraint_mode": "EXACT",
                "reason_code": REASON_UNBOUND_EXECUTION_ARGUMENT,
                "expected": "canonical semantic hash",
                "proposed": "",
                **base,
            }
        )
        reason_codes.append(REASON_UNBOUND_EXECUTION_ARGUMENT)

    if not reference["target_email"]:
        violations.append(
            {
                "argument_name": "recipient",
                "constraint_mode": "EXACT",
                "reason_code": REASON_UNBOUND_EXECUTION_ARGUMENT,
                "expected": "approved recipient",
                "proposed": "",
                **base,
            }
        )
        reason_codes.append(REASON_UNBOUND_EXECUTION_ARGUMENT)

    merged_proposed = dict(proposed or {})
    canonical_thread = ""
    if isinstance(resolved_target, dict):
        merged_proposed["recipient"] = _norm_email(resolved_target.get("to"))
        thread = _text(resolved_target.get("thread_id"))
        canonical_thread = thread
        if thread:
            # Runtime-owned canonical resolution fills the thread only when the
            # artifact did not claim one; an explicit artifact thread claim is
            # compared (and denied on mismatch) below.
            merged_proposed.setdefault("thread_id", thread)

    # Thread binding: the canonical thread is runtime-owned (mailbox context
    # pack), never an artifact/planner input. A proposed thread that differs
    # from the canonical resolution, or a thread claim when no canonical thread
    # exists, is denied instead of guessed.
    proposed_thread = _text(merged_proposed.get("thread_id"))
    if canonical_thread:
        if proposed_thread != canonical_thread:
            violations.append(
                {
                    "argument_name": "thread_id",
                    "constraint_mode": "EXACT",
                    "reason_code": REASON_CANONICAL_ARGUMENT_MISMATCH,
                    "expected": canonical_thread,
                    "proposed": proposed_thread,
                    **base,
                }
            )
            reason_codes.append(REASON_CANONICAL_ARGUMENT_MISMATCH)
    elif proposed_thread:
        violations.append(
            {
                "argument_name": "thread_id",
                "constraint_mode": "ABSENT",
                "reason_code": REASON_ARGUMENT_NOT_ALLOWED,
                "expected": "(absent - no canonical thread)",
                "proposed": proposed_thread,
                **base,
            }
        )
        reason_codes.append(REASON_ARGUMENT_NOT_ALLOWED)

    expected_hash = _text(expected_body_hash)
    if expected_hash and reference["body_hash"] and expected_hash != reference["body_hash"]:
        violations.append(
            {
                "argument_name": "body_hash",
                "constraint_mode": "EXACT",
                "reason_code": REASON_CANONICAL_ARGUMENT_MISMATCH,
                "expected": reference["body_hash"],
                "proposed": expected_hash,
                **base,
            }
        )
        reason_codes.append(REASON_CANONICAL_ARGUMENT_MISMATCH)

    # thread_id is validated by the explicit runtime-owned check above, not by
    # the constraint projection (the projection intentionally has no thread
    # constraint, so a thread claim can never be smuggled past it as an
    # "allowed" argument).
    constraint_input = {
        key: value for key, value in merged_proposed.items() if key != "thread_id"
    }
    arg_violations = constraint_violations(
        constraint_input,
        project_write_execution_constraints(reference),
    )
    violations.extend(arg_violations)
    reason_codes.extend(violations_reason_codes(arg_violations))

    ordered: list[str] = []
    seen: set[str] = set()
    for code in reason_codes:
        code = _text(code)
        if code and code not in seen:
            seen.add(code)
            ordered.append(code)

    status = "deny" if ordered else "pass"
    canonical_thread_display = (
        canonical_thread or reference["thread_id"] or ""
    )
    return {
        "status": status,
        "verdict": VERDICT_READY if status == "pass" else VERDICT_DENIED,
        "reason_codes": ordered,
        "violations": violations,
        "approved_action_id": reference["action_id"],
        "canonical": {
            "case_id": reference["case_id"],
            "draft_id": reference["draft_id"],
            "body_hash": reference["body_hash"],
            "revision": reference["revision"],
            "decision_id": reference["decision_id"],
            "decision_version_id": reference["decision_version_id"],
            "semantic_hash": reference["semantic_hash"],
            "recipient": reference["target_email"],
            "thread_id": canonical_thread_display,
        },
        "proposed": merged_proposed,
    }


__all__ = [
    "REASON_APPROVAL_ARTIFACT_MISMATCH",
    "REASON_APPROVAL_MISSING",
    "VERDICT_DENIED",
    "VERDICT_READY",
    "WriteBoundaryDeniedError",
    "approved_write_reference",
    "evaluate_write_execution_binding",
    "find_approved_action",
    "project_write_execution_constraints",
]
