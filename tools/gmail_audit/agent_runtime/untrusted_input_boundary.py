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
            failure_class="UNTRUSTED_AUTHORITY_ARGUMENT",
            detail=",".join(authority_paths[:8]),
        )

    recipient_values = _collect_recipient_values(args)
    if not recipient_values:
        return None
    trusted = _trusted_recipient_candidates(payload)
    if not trusted:
        return _blocked_result(
            reason=f"untrusted_recipient_argument:{tool_name}",
            failure_class="UNTRUSTED_RECIPIENT_ARGUMENT",
            detail="no_trusted_recipient",
        )
    untrusted_values = [value for value in recipient_values if value not in trusted]
    if untrusted_values:
        return _blocked_result(
            reason=f"untrusted_recipient_argument:{tool_name}",
            failure_class="UNTRUSTED_RECIPIENT_ARGUMENT",
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
    return bool(values & _UNTRUSTED_SOURCE_KINDS)


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
