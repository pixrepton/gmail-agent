"""CEL Radlin Digital Twin Definition of Done (PR-F).

Validates that a reconcile + snapshot + optional feed envelope satisfy the
agent-centric product contract (no shared downstream as SoT).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

RADLIN_CEL_AREA_M2 = 128
RADLIN_CEL_CITY = "radlin"
AGENT_RUNTIME_WARNING = "agent_runtime_reconcile_no_shared_downstream"


@dataclass(frozen=True)
class DigitalTwinDodReport:
    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": dict(self.checks), "failures": list(self.failures)}


def _city_matches_radlin(snapshot: EngagementSnapshotV2) -> bool:
    city = str(snapshot.hvac_profile.location.city or "").strip().lower()
    return RADLIN_CEL_CITY in city


def _reconcile_path_agent(reconcile_result: Any | None) -> bool:
    if reconcile_result is None:
        return False
    stage = getattr(reconcile_result, "stage_outputs", None)
    if isinstance(stage, dict) and str(stage.get("reconcile_path") or "") == "agent_runtime":
        return True
    preview = getattr(reconcile_result, "preview", None)
    if isinstance(preview, dict) and str(preview.get("reconcile_path") or "") == "agent_runtime":
        return True
    return False


def _no_shared_downstream(reconcile_result: Any | None) -> bool:
    if reconcile_result is None:
        return True
    warnings = getattr(reconcile_result, "warnings", None) or []
    return any(AGENT_RUNTIME_WARNING in str(w) for w in warnings)


def _v2_projection_agent_marker(reconcile_result: Any | None) -> bool:
    if reconcile_result is None:
        return False
    v2 = getattr(reconcile_result, "v2_projection", None)
    if not isinstance(v2, dict):
        return False
    signal_proj = v2.get("signal_projection")
    if isinstance(signal_proj, dict) and isinstance(signal_proj.get("agent_runtime"), dict):
        return True
    trace = v2.get("decision_trace")
    return isinstance(trace, dict) and str(trace.get("decision_type") or "") == "agent_runtime"


def _feed_case_visible(envelope: dict[str, Any] | None, case_id: str) -> bool:
    if not envelope or not case_id:
        return False
    feed = envelope.get("feed") if isinstance(envelope.get("feed"), dict) else {}
    cases = feed.get("cases") or []
    if isinstance(cases, list):
        for row in cases:
            if isinstance(row, dict) and str(row.get("case_id") or "") == case_id:
                return True
    details = feed.get("case_details") if isinstance(feed.get("case_details"), dict) else {}
    return case_id in details


def _feed_has_agent_turns(envelope: dict[str, Any] | None, case_id: str) -> bool:
    if not envelope or not case_id:
        return False
    feed = envelope.get("feed") if isinstance(envelope.get("feed"), dict) else {}
    details = feed.get("case_details") if isinstance(feed.get("case_details"), dict) else {}
    detail = details.get(case_id)
    if not isinstance(detail, dict):
        return False
    turns = detail.get("agent_turns")
    return isinstance(turns, list) and len(turns) > 0


def _feed_from_engagement(envelope: dict[str, Any] | None) -> bool:
    if not envelope or not isinstance(envelope, dict):
        return False
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    if str(source.get("feed_source") or "") == "engagement_snapshot_v2":
        return True
    feed = envelope.get("feed") if isinstance(envelope.get("feed"), dict) else {}
    meta = feed.get("feed_meta") if isinstance(feed.get("feed_meta"), dict) else {}
    return bool(meta.get("agent_runtime"))


def evaluate_digital_twin_dod(
    snapshot: EngagementSnapshotV2,
    *,
    reconcile_result: Any | None = None,
    feed_envelope: dict[str, Any] | None = None,
    require_feed: bool = False,
) -> DigitalTwinDodReport:
    """CEL Radlin DoD: 128 m², Radlin, agent reconcile, HITL, engagement feed."""
    checks: dict[str, bool] = {
        "heated_area_128_m2": snapshot.hvac_profile.heated_area_m2 == RADLIN_CEL_AREA_M2,
        "city_radlin": _city_matches_radlin(snapshot),
        "engagement_id_present": bool(str(snapshot.engagement_id or "").strip()),
        "case_id_present": bool(str(snapshot.case_id or "").strip()),
        "hitl_or_operator_stop": bool(snapshot.hitl_gate.required)
        or snapshot.operational_status.code in {"pending_operator", "ready_for_quote", "node_a_error"},
        "agent_reconcile_path": _reconcile_path_agent(reconcile_result),
        "no_shared_downstream": _no_shared_downstream(reconcile_result),
        "agent_engagement_in_stage": bool(
            isinstance(getattr(reconcile_result, "stage_outputs", None), dict)
            and (reconcile_result.stage_outputs or {}).get("agent_engagement_snapshot")
        )
        if reconcile_result is not None
        else True,
    }
    if require_feed or feed_envelope is not None:
        cid = str(snapshot.case_id or "").strip()
        checks["feed_engagement_snapshot_v2"] = _feed_from_engagement(feed_envelope)
        checks["feed_case_visible"] = _feed_case_visible(feed_envelope, cid)
        checks["feed_agent_turns_present"] = _feed_has_agent_turns(feed_envelope, cid)
    if reconcile_result is not None:
        checks["v2_projection_agent_runtime"] = _v2_projection_agent_marker(reconcile_result)

    failures: list[str] = []
    labels = {
        "heated_area_128_m2": f"hvac_profile.heated_area_m2 must be {RADLIN_CEL_AREA_M2}",
        "city_radlin": f"hvac_profile.location.city must mention {RADLIN_CEL_CITY!r}",
        "engagement_id_present": "engagement_id required",
        "case_id_present": "case_id required",
        "hitl_or_operator_stop": "hitl_gate or terminal operational_status required",
        "agent_reconcile_path": "reconcile_path must be agent_runtime",
        "no_shared_downstream": f"warnings must include {AGENT_RUNTIME_WARNING!r}",
        "agent_engagement_in_stage": "stage_outputs.agent_engagement_snapshot required",
        "feed_engagement_snapshot_v2": "feed must be built from engagement_snapshot_v2",
        "feed_case_visible": "feed must list the case_id in cases or case_details",
        "feed_agent_turns_present": "feed.case_details must include agent_turns",
        "v2_projection_agent_runtime": "v2_projection must carry agent_runtime marker",
    }
    for key, passed in checks.items():
        if not passed:
            failures.append(labels.get(key, key))

    return DigitalTwinDodReport(ok=not failures, checks=checks, failures=failures)


def assert_digital_twin_dod(
    snapshot: EngagementSnapshotV2,
    *,
    reconcile_result: Any | None = None,
    feed_envelope: dict[str, Any] | None = None,
    require_feed: bool = False,
) -> DigitalTwinDodReport:
    report = evaluate_digital_twin_dod(
        snapshot,
        reconcile_result=reconcile_result,
        feed_envelope=feed_envelope,
        require_feed=require_feed,
    )
    if not report.ok:
        raise AssertionError("Digital Twin CEL Radlin DoD failed: " + "; ".join(report.failures))
    return report


def build_digital_twin_doctor_check(settings: Any | None = None) -> dict[str, Any]:
    """Doctor slice: primary cutover readiness + pytest gate reference."""
    from agent_runtime.primary_cutover import build_primary_cutover_doctor_check
    from agent_runtime.settings import load_agent_runtime_settings

    agent = load_agent_runtime_settings()
    primary = build_primary_cutover_doctor_check(agent)
    if not agent.enabled:
        return {
            "status": "skipped",
            "enabled": False,
            "mode": agent.mode,
            "cel_radlin_pytest": "tests/test_digital_twin_cel_radlin_dod.py",
            "issues": [],
        }
    issues = list(primary.get("issues") or [])
    status = "failed" if issues else "ok"
    if agent.mode == "prep":
        status = "ok" if not issues else "failed"
    return {
        "status": status,
        "enabled": agent.enabled,
        "mode": agent.mode,
        "primary_active": primary.get("primary_active"),
        "cel_radlin_pytest": "tests/test_digital_twin_cel_radlin_dod.py",
        "primary_cutover": primary,
        "issues": issues,
    }


__all__ = [
    "AGENT_RUNTIME_WARNING",
    "RADLIN_CEL_AREA_M2",
    "RADLIN_CEL_CITY",
    "DigitalTwinDodReport",
    "assert_digital_twin_dod",
    "build_digital_twin_doctor_check",
    "evaluate_digital_twin_dod",
]
