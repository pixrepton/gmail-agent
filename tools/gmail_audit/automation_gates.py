"""Action-level automation gating: what may run without human confirmation."""

from __future__ import annotations

from typing import Any


def build_automation_policy(
    *,
    review_routing: dict[str, Any],
    confidence_domains: dict[str, float],
    thresholds: dict[str, float],
    action_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive explicit automation gates from review mode and per-domain confidence."""
    mode = str(review_routing.get("review_mode") or "auto_safe")
    action_plan = action_plan or {}
    safe_for_push = bool(action_plan.get("safe_for_live_push"))

    auto_safe = mode == "auto_safe" and not review_routing.get("review_required")
    next_ok = float(confidence_domains.get("confidence_next_action") or 0.0) >= float(
        thresholds.get("confidence_next_action", 0.65)
    )
    attach_ok = float(confidence_domains.get("confidence_attachment_extraction") or 0.0) >= float(
        thresholds.get("confidence_attachment_extraction", 0.5)
    )
    surface_ok = float(confidence_domains.get("confidence_surface_decision") or 0.0) >= float(
        thresholds.get("confidence_surface_decision", 0.5)
    )

    policy: dict[str, Any] = {
        "allow_automated_intake_record": auto_safe,
        "allow_automated_desk_projection": auto_safe and surface_ok and attach_ok,
        "allow_automated_v1_task_push": auto_safe and safe_for_push,
        "allow_automated_client_reply": False,
        "allow_automated_supplier_touch": auto_safe and next_ok and mode in {"auto_safe", "suggest_only"},
        "allow_background_attachment_ocr": attach_ok or mode == "suggest_only",
        "primary_block_reason": "",
    }

    reasons: list[str] = []
    if not auto_safe:
        reasons.append(f"review_mode:{mode}")
    if not attach_ok:
        reasons.append("low_attachment_confidence")
    if not surface_ok:
        reasons.append("low_surface_confidence")
    if not next_ok:
        reasons.append("low_next_action_confidence")
    policy["blocked_automation_reasons"] = reasons
    if reasons:
        policy["primary_block_reason"] = reasons[0]

    return policy


def merge_policy_report_into_automation_policy(
    base_policy: dict[str, Any],
    policy_report: dict[str, Any] | None,
) -> dict[str, Any]:
    """If PolicyEngine did not APPROVE, turn off automated allow_* flags (additive; V2.1)."""
    if not policy_report:
        return base_policy
    if str(policy_report.get("status") or "").strip() == "APPROVED":
        return base_policy
    out = dict(base_policy)
    reasons = list(out.get("blocked_automation_reasons") or [])
    reasons.append("policy_engine_not_approved")
    out["blocked_automation_reasons"] = reasons
    out["primary_block_reason"] = out.get("primary_block_reason") or "policy_engine_not_approved"
    for key in list(out.keys()):
        if key.startswith("allow_automated_") and isinstance(out[key], bool):
            out[key] = False
    return out
