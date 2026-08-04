"""Canonical planner/tool failure attribution (PLANNER-EXEC-FIDELITY-01).

Does not replace ToolResult.status; attaches a stable failure class so
configuration gaps are never scored as planner intelligence failures.
"""

from __future__ import annotations

from typing import Any, Literal

FailureClass = Literal[
    "TOOL_UNAVAILABLE",
    "TOOL_CONFIGURATION_MISSING",
    "TOOL_EXECUTION_FAILED",
    "TOOL_TIMEOUT",
    "TOOL_RATE_LIMITED",
    "TOOL_ARGUMENTS_INVALID",
    "PLANNER_WRONG_TOOL_CLASS",
    "PLANNER_KNOWN_FACT_REASK",
    "PLANNER_BUDGET_EXCEEDED",
    "POLICY_ENVELOPE_MISSING",
    "POLICY_TOOL_MISMATCH",
    "DOWNSTREAM_RESULT_INVALID",
    "DRAFT_SANITY_FAILED",
    "SAFE_ABSTENTION",
]

FailureOwner = Literal["infra", "capability", "policy", "quality", "planner"]


def attribution(
    *,
    failure_class: FailureClass,
    owner: FailureOwner,
    stage: str,
    retryable: bool,
    safe_next_step: str,
    correlation: dict[str, str] | None = None,
    detail: str = "",
) -> dict[str, Any]:
    """Build a machine-readable attribution payload for snapshot/journal."""
    payload: dict[str, Any] = {
        "failure_class": failure_class,
        "owner": owner,
        "stage": str(stage or "")[:80],
        "retryable": bool(retryable),
        "safe_next_step": str(safe_next_step or "")[:160],
        "detail": str(detail or "")[:320],
    }
    if correlation:
        payload["correlation"] = {
            str(k): str(v or "")[:120] for k, v in list(correlation.items())[:8]
        }
    return payload


def classify_tool_handler_error(
    *,
    tool_name: str,
    summary: str,
    status: str,
) -> dict[str, Any]:
    """Map common handler error text to a stable failure class."""
    text = str(summary or "").lower()
    name = str(tool_name or "").strip()
    if "not configured" in text or "is not configured" in text:
        return attribution(
            failure_class="TOOL_CONFIGURATION_MISSING",
            owner="infra",
            stage="tool_execution",
            retryable=False,
            safe_next_step="request_operator_clarification",
            detail=f"{name}: configuration missing",
        )
    if status == "node_a_error" or "niedostępny" in text or "unreachable" in text:
        return attribution(
            failure_class="TOOL_UNAVAILABLE",
            owner="infra",
            stage="tool_execution",
            retryable=True,
            safe_next_step="request_operator_clarification",
            detail=f"{name}: upstream unavailable",
        )
    if "timeout" in text or "timed out" in text:
        return attribution(
            failure_class="TOOL_TIMEOUT",
            owner="infra",
            stage="tool_execution",
            retryable=True,
            safe_next_step="retry_or_escalate",
            detail=f"{name}: timeout",
        )
    if "rate limit" in text or "429" in text:
        return attribution(
            failure_class="TOOL_RATE_LIMITED",
            owner="infra",
            stage="tool_execution",
            retryable=True,
            safe_next_step="backoff_then_retry",
            detail=f"{name}: rate limited",
        )
    if "wymagan" in text or "required" in text or "invalid" in text:
        return attribution(
            failure_class="TOOL_ARGUMENTS_INVALID",
            owner="planner",
            stage="tool_execution",
            retryable=True,
            safe_next_step="correct_arguments",
            detail=f"{name}: invalid arguments",
        )
    return attribution(
        failure_class="TOOL_EXECUTION_FAILED",
        owner="capability",
        stage="tool_execution",
        retryable=False,
        safe_next_step="request_operator_clarification",
        detail=f"{name}: execution failed",
    )


def attach_attribution(result: Any, payload: dict[str, Any]) -> Any:
    """Copy ToolResult with attribution in snapshot_delta + optional fields."""
    delta = dict(getattr(result, "snapshot_delta", None) or {})
    delta["execution_attribution"] = payload
    updates: dict[str, Any] = {"snapshot_delta": delta}
    if hasattr(result, "failure_class"):
        updates["failure_class"] = str(payload.get("failure_class") or "")
    if hasattr(result, "failure_owner"):
        updates["failure_owner"] = str(payload.get("owner") or "")
    if hasattr(result, "retryable"):
        updates["retryable"] = payload.get("retryable")
    return result.model_copy(update=updates)


__all__ = [
    "FailureClass",
    "FailureOwner",
    "attach_attribution",
    "attribution",
    "classify_tool_handler_error",
]
