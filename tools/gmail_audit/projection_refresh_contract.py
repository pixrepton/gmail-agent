"""Projection refresh metadata after operator adjudication (PR-6)."""

from __future__ import annotations

from typing import Any

from projection_refresh_rules import ProjectionRefreshDecision, decide_projection_refresh


def build_adjudication_projection_refresh(
    *,
    adjudication_kind: str,
    case_id: str = "",
    reconcile_result: Any | None = None,
) -> dict[str, Any]:
    """
    Every adjudication yields a refresh contract row for bridge/operator telemetry.
    Reconcile may be skipped (confirm) but UI still gets a refresh hint.
    """
    kind = str(adjudication_kind or "").strip()
    if reconcile_result is not None:
        prd = getattr(reconcile_result, "projection_refresh_decision", None)
        if prd is not None and hasattr(prd, "to_dict"):
            decision = prd.to_dict()
            return {
                "schema_version": "adjudication_projection_refresh.v1",
                "adjudication_kind": kind,
                "reconcile_ran": True,
                "projection_refresh_decision": decision,
                "should_refresh": bool(decision.get("should_refresh")),
            }
        if isinstance(reconcile_result, dict):
            nested = reconcile_result.get("projection_refresh_decision")
            if isinstance(nested, dict):
                return {
                    "schema_version": "adjudication_projection_refresh.v1",
                    "adjudication_kind": kind,
                    "reconcile_ran": True,
                    "projection_refresh_decision": nested,
                    "should_refresh": bool(nested.get("should_refresh")),
                }

    refresh = decide_projection_refresh(
        "operator_adjudication",
        source_kind="gmail",
        case_id=case_id,
        has_case_state=bool(case_id),
    )
    if kind == "confirm_same_case":
        refresh = ProjectionRefreshDecision(
            should_refresh=True,
            refresh_kind="case_and_note",
            reason="adjudication_confirm_refresh",
            trace_note_pl="Operator potwierdzil sprawe — odswiez projekcje bez pelnego reconcile.",
        )
    elif kind == "reject_same_case":
        refresh = ProjectionRefreshDecision(
            should_refresh=True,
            refresh_kind="case_and_note",
            reason="adjudication_reject_reconcile",
            trace_note_pl="Operator odrzucil powiazanie — reconcile zaktualizowal stan.",
        )
    decision = refresh.to_dict()
    return {
        "schema_version": "adjudication_projection_refresh.v1",
        "adjudication_kind": kind,
        "reconcile_ran": reconcile_result is not None and kind == "reject_same_case",
        "projection_refresh_decision": decision,
        "should_refresh": bool(decision.get("should_refresh")),
    }


__all__ = ["build_adjudication_projection_refresh"]
