"""Unified operator projection snapshot for v2 push and v3 operational feed (PR-7/PR-8)."""

from __future__ import annotations

from typing import Any

from config import Settings

OPERATOR_PROJECTION_SNAPSHOT_SCHEMA_VERSION = "operator_projection_snapshot.v1"


def build_operator_projection_snapshot(
    intake_output: dict[str, Any],
    *,
    stage_outputs: dict[str, Any] | None = None,
    run_id: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """
  Canonical Node B → Node A projection payload: v2 shadow blocks + decision_view.
  v3 feed and v2 push both consume this shape (PR-8).
    """
    from context_tray_set import build_context_tray_set
    from dash_projection_v2 import build_v2_shadow_projection
    from daszek_projection_router import route_projection_envelope
    from decision_projection_blocks import build_decision_view_blocks
    from llm_projection_composer import compose_projection_from_trays
    from projection_quality_metrics import build_projection_quality_metrics

    stage_outputs = stage_outputs if isinstance(stage_outputs, dict) else {}
    mailbox_memory_result = stage_outputs.get("mailbox_memory_result")
    case_intelligence_result = stage_outputs.get("case_intelligence_result")
    mb = mailbox_memory_result if isinstance(mailbox_memory_result, dict) else {}
    ci = case_intelligence_result if isinstance(case_intelligence_result, dict) else {}
    pack = mb.get("context_pack") if isinstance(mb.get("context_pack"), dict) else {}
    vnext = pack.get("vnext") if isinstance(pack.get("vnext"), dict) else {}

    decision_view = build_decision_view_blocks(
        case_intelligence=ci if ci else None,
        mailbox_context={"vnext": vnext, "pack": pack},
    )
    v2_projection = build_v2_shadow_projection(
        intake_output,
        stage_outputs=stage_outputs,
        run_id=run_id,
    )
    if isinstance(decision_view, dict) and decision_view:
        v2_projection = dict(v2_projection)
        v2_projection["decision_view"] = decision_view
    context_source = vnext if isinstance(vnext, dict) and vnext else pack
    context_tray_set = build_context_tray_set(context_source, generated_at=str(stage_outputs.get("generated_at") or ""))
    projection_envelope, projection_composer_decision = compose_projection_from_trays(
        context_tray_set,
        decision_view=decision_view if isinstance(decision_view, dict) else {},
        v2_projection=v2_projection if isinstance(v2_projection, dict) else {},
        generated_at=str(stage_outputs.get("generated_at") or ""),
        settings=settings,
        stage_outputs=stage_outputs,
    )
    projection_validation = projection_envelope.get("projection_validation")
    if not isinstance(projection_validation, dict):
        from projection_validator import validate_projection_envelope

        projection_validation = validate_projection_envelope(projection_envelope, context_tray_set=context_tray_set)
    business_result = stage_outputs.get("business_reasoning_result")
    preclassification_result = stage_outputs.get("preclassification_result")
    from operator_visibility_policy import should_suppress_desk_and_tasks, suppress_projection_envelope_surfaces

    if should_suppress_desk_and_tasks(
        business_result=business_result if isinstance(business_result, dict) else None,
        preclassification_result=preclassification_result if isinstance(preclassification_result, dict) else None,
    ):
        projection_envelope = suppress_projection_envelope_surfaces(projection_envelope)
    daszek_routes = route_projection_envelope(projection_envelope)
    projection_quality_metrics = build_projection_quality_metrics(
        projection_envelope,
        generated_at=str(stage_outputs.get("generated_at") or ""),
    )

    return {
        "schema_version": OPERATOR_PROJECTION_SNAPSHOT_SCHEMA_VERSION,
        "run_id": str(run_id or "").strip(),
        "v2_projection": v2_projection,
        "decision_view": decision_view if isinstance(decision_view, dict) else {},
        "context_tray_set": context_tray_set,
        "projection_envelope": projection_envelope,
        "projection_composer_decision": projection_composer_decision,
        "projection_validation": projection_validation,
        "daszek_routes": daszek_routes,
        "projection_quality_metrics": projection_quality_metrics,
        "canonical_signal_id": str(stage_outputs.get("canonical_signal_id") or "").strip(),
    }


def v2_projection_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Extract v2 shadow projection from operator snapshot (backward compatible)."""
    v2 = snapshot.get("v2_projection")
    return v2 if isinstance(v2, dict) else {}


__all__ = [
    "OPERATOR_PROJECTION_SNAPSHOT_SCHEMA_VERSION",
    "build_operator_projection_snapshot",
    "v2_projection_from_snapshot",
]
