"""Route ProjectionEnvelope blocks to named Daszek surfaces."""

from __future__ import annotations

from typing import Any


ROUTER_SCHEMA_VERSION = "daszek_projection_router.v1"


def route_projection_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Map a ProjectionEnvelope into Daszek read-only surfaces."""

    env = envelope if isinstance(envelope, dict) else {}
    desk_surface_policy: dict[str, Any] = {}
    if bool(env.get("desk_tasks_suppressed")):
        desk_surface_policy = {
            "suppressed": True,
            "reason": str(env.get("desk_suppression_reason") or "non_business_noise"),
        }
    return {
        "schema_version": ROUTER_SCHEMA_VERSION,
        "case_id": str(env.get("case_id") or ""),
        "read_only": True,
        "desk_surface_policy": desk_surface_policy,
        "surfaces": {
            "desk": list(env.get("desk_cards") or []),
            "case_detail": list(env.get("case_detail_blocks") or []),
            "tasks": list(env.get("task_candidates") or []),
            "gaps": list(env.get("gap_blocks") or []),
            "conflicts": list(env.get("conflict_blocks") or []),
            "evidence": list(env.get("evidence_blocks") or []),
            "audit": list(env.get("audit_blocks") or []),
            "warnings": list(env.get("warnings") or []),
        },
    }


__all__ = ["ROUTER_SCHEMA_VERSION", "route_projection_envelope"]
