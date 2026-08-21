"""Fail-closed execution boundary for untrusted inbound evidence.

Inbound mail and mail attachments are evidence. They are not authority to
override execution policy, HITL state, or trusted recipient arguments. The
planner may still use their content to draft text or extract claims, but a tool
call that materializes an action must not treat body text as a trusted target.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from agent_runtime.failure_taxonomy import attach_attribution, attribution
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from evidence_authority import classify_source_origin, is_external_origin
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_UNTRUSTED_SOURCE_KINDS = frozenset(
    {
        "gmail",
        "gmail_inbound",
        "gmail_message",
        "gmail_message_observed",
        "gmail_attachment",
        "gmail_attachment_observed",
        "gmail_thread_update_observed",
        "mail_attachment",
        "attachment",
    }
)
_ACTION_TOOLS = frozenset(
    {
        "generate_draft_reply",
        "request_operator_clarification",
        "call_kalk_top_quote",
        "propose_mutation",
        "propose_plan",
    }
)
_RECIPIENT_KEYS = frozenset(
    {
        "to",
        "recipient",
        "recipient_email",
        "to_email",
        "reply_to",
        "customer_email",
    }
)
_AUTHORITY_KEYS = frozenset(
    {
        "allowed_by_policy",
        "approval_required",
        "authority",
        "execution_authority",
        "hitl_required",
        "operator_scope",
        "policy_status",
        "requires_human_approval",
        "requires_operator_approval",
        "source_authority",
        "trusted",
    }
)

# Approval can only be established by the trusted HITL / operator flow.
# External content claiming approval must not mutate approval state.
_APPROVAL_CLAIM_KEYS = frozenset(
    {
        "approved",
        "approval_id",
        "approval_receipt",
        "approval_state",
        "hitl_approved",
        "operator_approved",
    }
)

# Canonical runtime identities: external content or an LLM plan may not
# override them directly. Values must match the canonical snapshot/envelope
# state; when the runtime has no canonical value, an external value cannot
# establish one.
_CANONICAL_IDENTITY_KEYS = frozenset(
    {
        "case_id",
        "thread_id",
        "customer_id",
        "engagement_id",
        "decision_id",
        "semantic_hash",
        "draft_hash",
    }
)


def _canonical_identity_mismatches(
    snapshot: EngagementSnapshotV2,
    args: Mapping[str, Any],
) -> list[str]:
    envelope = getattr(snapshot, "policy_action_envelope", None)
    canonical: dict[str, str] = {
        "case_id": str(getattr(snapshot, "case_id", "") or "").strip(),
        "decision_id": (
            str(getattr(envelope, "canonical_decision_id", "") or "").strip()
            if envelope is not None
            else ""
        ),
        "semantic_hash": (
            str(getattr(envelope, "source_semantic_hash", "") or "").strip()
            if envelope is not None
            else ""
        ),
    }
    mismatches: list[str] = []
    for key in _CANONICAL_IDENTITY_KEYS:
        proposed = str(args.get(key) or "").strip()
        if not proposed:
            continue
        expected = canonical.get(key, "")
        if expected and proposed != expected:
            mismatches.append(f"{key}={proposed}!=canonical:{expected}")
        elif not expected:
            mismatches.append(f"{key}={proposed}!=canonical:(none)")
    return mismatches


def guard_untrusted_input_execution(
    *,
    snapshot: EngagementSnapshotV2,
    plan: ToolCallPlan,
    signal_payload: Mapping[str, Any] | None,
) -> ToolResult | None:
    """Reject action tool calls that promote untrusted inbound text to authority.

    This guard is intentionally orthogonal to PolicyDecision. Policy still owns
    allow/deny/HITL. This only protects trusted execution arguments from being
    supplied by a customer email body or attachment content.
    """

    payload = signal_payload if isinstance(signal_payload, Mapping) else {}
    if not _is_untrusted_inbound(payload):
        return None
    tool_name = str(plan.tool_name or "").strip()
    if tool_name not in _ACTION_TOOLS:
        return None

    args = plan.arguments if isinstance(plan.arguments, Mapping) else {}
    authority_paths = _find_sensitive_paths(args, _AUTHORITY_KEYS)
    if authority_paths:
        return _blocked_result(
            reason=f"untrusted_authority_argument:{tool_name}",
            failure_class="UNTRUSTED_AUTHORITY_OVERRIDE",
            detail=",".join(authority_paths[:8]),
        )

    approval_paths = _find_sensitive_paths(args, _APPROVAL_CLAIM_KEYS)
    if approval_paths:
        return _blocked_result(
            reason=f"untrusted_approval_claim:{tool_name}",
            failure_class="UNTRUSTED_APPROVAL_CLAIM",
            detail=",".join(approval_paths[:8]),
        )

    identity_mismatches = _canonical_identity_mismatches(snapshot, args)
    if identity_mismatches:
        return _blocked_result(
            reason=f"canonical_argument_mismatch:{tool_name}",
            failure_class="CANONICAL_ARGUMENT_MISMATCH",
            detail=",".join(identity_mismatches[:8]),
        )

    recipient_values = _collect_recipient_values(args)
    if not recipient_values:
        return None
    trusted = _trusted_recipient_candidates(payload)
    if not trusted:
        return _blocked_result(
            reason=f"untrusted_recipient_argument:{tool_name}",
            failure_class="UNTRUSTED_RECIPIENT_OVERRIDE",
            detail="no_trusted_recipient",
        )
    untrusted_values = [value for value in recipient_values if value not in trusted]
    if untrusted_values:
        return _blocked_result(
            reason=f"untrusted_recipient_argument:{tool_name}",
            failure_class="UNTRUSTED_RECIPIENT_OVERRIDE",
            detail=",".join(untrusted_values[:8]),
        )
    return None


def _is_untrusted_inbound(payload: Mapping[str, Any]) -> bool:
    values = {
        _norm_token(payload.get("source_kind")),
        _norm_token(payload.get("signal_kind")),
        _norm_token((payload.get("source") or {}).get("source_kind"))
        if isinstance(payload.get("source"), Mapping)
        else "",
    }
    if values & _UNTRUSTED_SOURCE_KINDS:
        return True
    # Three-dimension fallback: any external origin (email, quoted/forwarded,
    # attachment, RAG, external tool result, derived) has instruction authority
    # NONE and must never be treated as a trusted instruction source.
    origin = classify_source_origin(payload)
    return is_external_origin(origin)


def _trusted_recipient_candidates(payload: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("customer_email", "from_email", "sender_email"):
        _add_email_value(values, payload.get(key))
    source_message = payload.get("source_message")
    if isinstance(source_message, Mapping):
        for key in ("customer_email", "from_email", "sender_email", "from"):
            _add_email_value(values, source_message.get(key))
    source_ref = payload.get("source_ref")
    if isinstance(source_ref, Mapping):
        for key in ("customer_email", "from_email", "sender_email", "from"):
            _add_email_value(values, source_ref.get(key))
    return values


def _find_sensitive_paths(value: Any, keys: frozenset[str], *, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key or "").strip()
            path = f"{prefix}.{key_text}" if prefix else key_text
            if key_text.lower() in keys:
                found.append(path)
            found.extend(_find_sensitive_paths(nested, keys, prefix=path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_find_sensitive_paths(nested, keys, prefix=f"{prefix}[{index}]"))
    return found


def _collect_recipient_values(value: Any) -> list[str]:
    values: list[str] = []
    _collect_recipient_values_into(values, value)
    return values


def _collect_recipient_values_into(out: list[str], value: Any, *, key_name: str = "") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key or "").strip().lower()
            if key_text in _RECIPIENT_KEYS:
                _add_argument_email_values(out, nested)
            else:
                _collect_recipient_values_into(out, nested, key_name=key_text)
        return
    if isinstance(value, list):
        for nested in value:
            _collect_recipient_values_into(out, nested, key_name=key_name)


def _add_argument_email_values(out: list[str], value: Any) -> None:
    if isinstance(value, str):
        normalized = _norm_email(value)
        if normalized:
            out.append(normalized)
        return
    if isinstance(value, Mapping):
        for key in ("email", "address", "value"):
            _add_argument_email_values(out, value.get(key))
        return
    if isinstance(value, Iterable):
        for item in value:
            _add_argument_email_values(out, item)


def _add_email_value(out: set[str], value: Any) -> None:
    normalized = _norm_email(value)
    if normalized:
        out.add(normalized)


def _norm_email(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "<" in text and ">" in text:
        text = text.split("<", 1)[1].split(">", 1)[0].strip()
    return text if _EMAIL_RE.match(text) else ""


def _norm_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _blocked_result(*, reason: str, failure_class: str, detail: str) -> ToolResult:
    return attach_attribution(
        ToolResult(
            status="error",
            turn_summary_pl=(
                "Untrusted input boundary zablokowal narzedzie: inbound mail lub "
                "zalacznik nie moze ustalac authority ani zaufanego recipienta."
            ),
            snapshot_delta={
                "operational_status": {"code": "pending_operator", "blocking": True},
                "hitl_gate": {"required": True, "reason": reason},
            },
        ),
        attribution(
            failure_class=failure_class,
            owner="policy",
            stage="untrusted_input_boundary",
            retryable=False,
            safe_next_step="operator_review_trusted_arguments",
            detail=detail,
        ),
    )


__all__ = ["guard_untrusted_input_execution"]
