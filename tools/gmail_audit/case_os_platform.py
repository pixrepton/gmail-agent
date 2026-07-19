"""Case OS platform helpers — shared projection rules (P2/P3)."""

from __future__ import annotations

from typing import Any

CORRELATION_KEYS = (
    "engagement_id",
    "case_id",
    "message_id",
    "workflow_id",
    "trace_id",
    "session_id",
    "request_id",
)


def normalize_os_correlation(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for key in CORRELATION_KEYS:
        val = str(src.get(key) or "").strip()
        if val:
            out[key] = val
    return out


def v2_action_proposal_to_feed_row(
    proposal: dict[str, Any],
    *,
    decision_view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map ActionProposal v2 + decision spine to feed V3 case/task row shape."""
    dv = decision_view if isinstance(decision_view, dict) else {}
    ap = proposal if isinstance(proposal, dict) else {}
    action_type = str(ap.get("action_type") or "").strip()
    summary = str(ap.get("summary_pl") or ap.get("summary") or "").strip()
    return {
        "proposal_id": str(ap.get("proposal_id") or "").strip(),
        "schema_version": "action_proposal.v2",
        "action_type": action_type,
        "status": str(ap.get("status") or "proposed").strip() or "proposed",
        "title": summary[:300] or action_type[:300] or "Propozycja działania",
        "summary": summary[:800],
        "reason": str(ap.get("blocked_reason") or "").strip()[:800],
        "requires_approval": bool(ap.get("requires_operator_approval", True)),
        "allowed_by_policy": ap.get("allowed_by_policy"),
        "policy_spine_ok": ap.get("policy_spine_ok"),
        "action_mode": str(ap.get("action_mode") or "").strip(),
        "decision_candidate_id": str(
            ap.get("decision_candidate_id") or dv.get("decision_candidate_id") or ""
        ).strip(),
        "policy_decision_id": str(ap.get("policy_decision_id") or dv.get("policy_decision_id") or "").strip(),
        "pipeline_run_id": str(dv.get("pipeline_run_id") or "").strip(),
        "proposal_summary_pl": str(dv.get("proposal_summary_pl") or "").strip()[:400],
        "why_pl": str(dv.get("why_pl") or "").strip()[:800],
        "source_spine": "decision_pipeline_v2",
        "feed_read_only": True,
    }


def resolve_feed_action_proposals(
    *,
    vnext_proposals: list[Any],
    case_intelligence: dict[str, Any] | None,
    decision_view: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """P2: prefer pipeline v2 proposals when present; else legacy v1 from pack."""
    ci = case_intelligence if isinstance(case_intelligence, dict) else {}
    dv = decision_view if isinstance(decision_view, dict) else {}
    v2_raw = ci.get("action_proposals_v2")
    if not isinstance(v2_raw, list):
        v2_raw = dv.get("action_proposals") if isinstance(dv.get("action_proposals"), list) else []
    if v2_raw:
        rows = [
            v2_action_proposal_to_feed_row(item, decision_view=dv)
            for item in v2_raw
            if isinstance(item, dict) and str(item.get("proposal_id") or item.get("action_type") or "").strip()
        ]
        if rows:
            return rows
    legacy: list[dict[str, Any]] = []
    for item in vnext_proposals or []:
        if isinstance(item, dict):
            legacy.append(dict(item))
    return legacy


def merge_decision_view_with_pipeline_proposals(
    decision_view: dict[str, Any] | None,
    pipeline_proposals: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """P2 product: surface pipeline v2 proposals inside decision_view for Daszek UI."""
    dv = dict(decision_view) if isinstance(decision_view, dict) else {}
    props = [dict(p) for p in pipeline_proposals if isinstance(p, dict)]
    if not props:
        return dv, []
    dv["action_proposals"] = props
    first = props[0]
    dv.setdefault("proposal_summary_pl", str(first.get("title") or first.get("summary") or "").strip())
    dv.setdefault("why_pl", str(first.get("why_pl") or first.get("reason") or "").strip())
    return dv, props


__all__ = [
    "CORRELATION_KEYS",
    "normalize_os_correlation",
    "merge_decision_view_with_pipeline_proposals",
    "resolve_feed_action_proposals",
    "v2_action_proposal_to_feed_row",
]
