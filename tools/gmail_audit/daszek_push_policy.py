"""Single place of truth for Daszek live mutation vs operator projection policy.

- **v1 /tasks preview push** uses ``safe_for_live_push`` (extremely conservative).
- **v2 /ingest operator projection** uses ``safe_for_operator_projection`` plus a broader PolicyEngine gate.

Live pushes must not rely only on metadata in artifacts: the same rules gate actual HTTP calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

LivePushSurface = Literal["v1_preview_tasks"]

# PolicyEngine statuses that may receive an operator-visible v2 projection (not autonomous live actions).
POLICY_ENGINE_OPERATOR_PROJECTION_ALLOWED = frozenset({"APPROVED", "NEEDS_HUMAN"})


@dataclass(frozen=True, slots=True)
class LivePushPolicyResult:
    """Outcome of :func:`evaluate_live_push_policy` / :func:`evaluate_operator_projection_policy`."""

    allowed: bool
    push_policy_reason: str
    push_policy_detail: str


def evaluate_live_push_policy(
    *,
    surface: LivePushSurface,
    manifest: dict[str, Any],
    action_plan_result: dict[str, Any] | None,
    intake_result_final: dict[str, Any] | None,
    policy_report: dict[str, Any] | None = None,
) -> LivePushPolicyResult:
    """Return whether a live Daszek **v1 /tasks** mutation is allowed.

    Uses ``safe_for_live_push`` from the action plan (see ``action_planner.plan_actions``), which
    is intentionally conservative (today: mostly ``ignore`` + high confidence + stable case link).

    When ``policy_report`` is provided (from :mod:`policy_engine`), only ``status == APPROVED`` may
    proceed — backend policy before projection push (V2.1).
    """
    intake = intake_result_final or {}
    plan = action_plan_result or {}

    if policy_report is not None:
        st = str(policy_report.get("status") or "").strip()
        if st != "APPROVED":
            risk = policy_report.get("effective_risk_class")
            return LivePushPolicyResult(
                False,
                "blocked_policy_engine",
                f"PolicyEngine status={st!r} effective_risk_class={risk!r}; live push not allowed.",
            )

    safe = bool(plan.get("safe_for_live_push"))
    primary_action = str(plan.get("primary_action") or "")
    review_required = bool(intake.get("review_required"))

    if surface == "v1_preview_tasks":
        if not bool(manifest.get("daszek_push_requested")):
            return LivePushPolicyResult(
                False,
                "skipped_v1_not_requested",
                "Run manifest has daszek_push_requested=false; no v1 /tasks live push.",
            )
        if review_required:
            return LivePushPolicyResult(
                False,
                "blocked_review_required",
                "Intake review_required is set; v1 live mutations are blocked.",
            )
        if not safe:
            return LivePushPolicyResult(
                False,
                "blocked_not_safe_for_live_push",
                f"safe_for_live_push is false (primary_action={primary_action!r}).",
            )
        return LivePushPolicyResult(
            True,
            "allowed_safe_for_live_push",
            f"primary_action={primary_action!r}, safe_for_live_push=true.",
        )

    raise ValueError(f"Unknown live push surface: {surface!r}")


def evaluate_operator_projection_policy(
    *,
    manifest: dict[str, Any],
    action_plan_result: dict[str, Any] | None,
    intake_result_final: dict[str, Any] | None,
    policy_report: dict[str, Any] | None = None,
) -> LivePushPolicyResult:
    """Return whether a **v2 ingest** operator projection POST is allowed.

    Separates operator-visible projection from ``safe_for_live_push``. PolicyEngine allows
    reviewable outcomes (``APPROVED``, ``NEEDS_HUMAN``); ``REJECTED`` and unknown statuses block
    unless the manifest enables desk relax flags (projection ingest is not autonomous execution).
    """
    plan = action_plan_result or {}
    primary_action = str(plan.get("primary_action") or "")
    safe_projection = bool(plan.get("safe_for_operator_projection"))

    if not bool(manifest.get("daszek_v2_push_enabled")):
        return LivePushPolicyResult(
            False,
            "skipped_v2_disabled",
            "daszek_v2_push_enabled is false in manifest; no v2 ingest push.",
        )

    if policy_report is not None:
        st = str(policy_report.get("status") or "").strip().upper()
        if st == "REJECTED":
            risk = policy_report.get("effective_risk_class")
            if bool(manifest.get("daszek_v2_desk_relax_rejected")):
                return LivePushPolicyResult(
                    True,
                    "allowed_operator_projection_desk_relax_rejected",
                    "PolicyEngine REJECTED relaxed for v2 ingest: operator desk is projection-only (not autonomous execution). "
                    f"effective_risk_class={risk!r}.",
                )
            return LivePushPolicyResult(
                False,
                "blocked_policy_engine_rejected",
                f"PolicyEngine status=REJECTED effective_risk_class={risk!r}; operator projection blocked.",
            )
        if st and st not in POLICY_ENGINE_OPERATOR_PROJECTION_ALLOWED:
            risk = policy_report.get("effective_risk_class")
            return LivePushPolicyResult(
                False,
                "blocked_policy_engine_status",
                f"PolicyEngine status={st!r} effective_risk_class={risk!r}; operator projection blocked.",
            )

    if not safe_projection:
        intake = intake_result_final or {}
        intake_action = str((intake.get("decision") or {}).get("action") or "").strip().lower()
        primary_lower = primary_action.strip().lower()
        if bool(manifest.get("daszek_v2_desk_include_ignore")) and (
            primary_lower == "ignore" or intake_action == "ignore"
        ):
            return LivePushPolicyResult(
                True,
                "allowed_operator_projection_desk_include_ignore",
                "safe_for_operator_projection relaxed: desk_include_ignore enabled for ignore-classified mail.",
            )
        return LivePushPolicyResult(
            False,
            "blocked_not_safe_for_operator_projection",
            f"safe_for_operator_projection is false (primary_action={primary_action!r}).",
        )

    return LivePushPolicyResult(
        True,
        "allowed_operator_projection",
        f"primary_action={primary_action!r}, safe_for_operator_projection=true.",
    )


__all__ = [
    "POLICY_ENGINE_OPERATOR_PROJECTION_ALLOWED",
    "LivePushPolicyResult",
    "LivePushSurface",
    "evaluate_live_push_policy",
    "evaluate_operator_projection_policy",
]
