"""Production-faithful planner spine handoff for eval / recovery harness.

Bridges Brain 1 case_intelligence → PolicyDecision/APv2 persist → envelope +
Understanding projection into AgentGraphEngine signal_payload.

This is the missing link that made Fresh 38 report missing_policy_envelope on
every full planner case when run_recovery_pf skipped the reconcile handoff.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.agent_reconcile import (
    build_case_understanding_projection,
    build_case_understanding_provenance_projection,
    build_policy_action_envelope_handoff,
)
from agent_runtime.envelope_presence import classify_envelope_presence
from llm_contracts.engagement_snapshot_v2 import PolicyActionEnvelopeV1
from mailbox_memory import InMemoryMailboxMemoryStore


def _candidate_from_intelligence(intelligence: dict[str, Any]) -> dict[str, Any]:
    candidate = intelligence.get("decision_candidate")
    if isinstance(candidate, dict):
        return candidate
    pipeline = intelligence.get("decision_pipeline")
    if not isinstance(pipeline, dict):
        return {}
    outputs = pipeline.get("outputs")
    if not isinstance(outputs, dict):
        return {}
    nested = outputs.get("decision_candidate")
    return nested if isinstance(nested, dict) else {}


def ensure_policy_spine_on_intelligence(
    intelligence: dict[str, Any] | None,
    *,
    case_id: str,
    message_id: str,
    dry_run_only: bool = True,
    action_plan_result: dict[str, Any] | None = None,
    intake_result: dict[str, Any] | None = None,
    case_link_result: dict[str, Any] | None = None,
    entity_link_result: dict[str, Any] | None = None,
    mailbox_memory_result: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
    case_snapshot_hot_state: dict[str, Any] | None = None,
    run_state: dict[str, Any] | None = None,
    settings: Any | None = None,
    stage_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach real PolicyDecision + APv2 when a real DecisionCandidate exists."""
    from policy_action_proposal import attach_policy_and_proposals

    intel = dict(intelligence) if isinstance(intelligence, dict) else {}
    if isinstance(intel.get("policy_decision"), dict) and isinstance(
        intel.get("action_proposals_v2"), list
    ) and intel.get("action_proposals_v2"):
        return intel

    candidate = _candidate_from_intelligence(intel)
    if not isinstance(candidate, dict) or not str(
        candidate.get("decision_candidate_id") or ""
    ).strip():
        intel["_policy_spine_attach_status"] = "missing_decision_candidate"
        return intel

    candidate = dict(candidate)
    cid = str(candidate.get("case_id") or "").strip()
    mid = str(candidate.get("source_signal_id") or "").strip()
    if not cid or not mid:
        intel["decision_candidate"] = candidate
        intel["_policy_spine_attach_status"] = "decision_candidate_correlation_incomplete"
        return intel
    intel["decision_candidate"] = candidate

    snap = dict(snapshot or {})
    source_message = snap.get("source_message")
    if not isinstance(source_message, dict):
        source_message = {}
    source_message.setdefault("message_id", mid)
    snap["source_message"] = source_message

    mb = dict(mailbox_memory_result or {})
    mb.setdefault("case_id", cid)
    context_pack = intel.get("mailbox_memory_context_pack")
    if isinstance(context_pack, dict) and context_pack:
        mb.setdefault("context_pack", context_pack)

    stage = dict(stage_config or {})
    stage.setdefault("action_proposal_v2_enabled", True)
    stage.setdefault("decision_pipeline_dry_run_only", bool(dry_run_only))

    attach_policy_and_proposals(
        action_plan_result=action_plan_result or {},
        intake_result=intake_result or {},
        case_link_result=case_link_result or {},
        entity_link_result=entity_link_result or {},
        case_intelligence_result=intel,
        mailbox_memory_result=mb,
        snapshot=snap,
        case_snapshot_hot_state=case_snapshot_hot_state,
        run_state=run_state or {"run_id": f"policy_spine_{cid}_{mid}"},
        settings=settings,
        stage_config=stage,
    )
    if isinstance(intel.get("policy_decision"), dict) and isinstance(
        intel.get("action_proposals_v2"), list
    ) and intel.get("action_proposals_v2"):
        intel["_policy_spine_attach_status"] = "attached_from_policy_engine"
    else:
        intel["_policy_spine_attach_status"] = "policy_attach_produced_no_v2"
    return intel


def build_production_faithful_planner_signal(
    *,
    case_id: str,
    signal_id: str,
    message_id: str,
    subject: str,
    body: str,
    case_intelligence_result: dict[str, Any] | None,
    case_kind: str | None = None,
    extraction: dict[str, Any] | None = None,
    mailbox_store: Any | None = None,
    action_plan_result: dict[str, Any] | None = None,
    intake_result: dict[str, Any] | None = None,
    case_link_result: dict[str, Any] | None = None,
    entity_link_result: dict[str, Any] | None = None,
    mailbox_memory_result: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
    case_snapshot_hot_state: dict[str, Any] | None = None,
    run_state: dict[str, Any] | None = None,
    settings: Any | None = None,
    stage_config: dict[str, Any] | None = None,
    harness_mode: bool = False,
    policy_required: bool = True,
) -> dict[str, Any]:
    """Build signal_payload matching agent_reconcile handoff shape."""
    store = mailbox_store if mailbox_store is not None else InMemoryMailboxMemoryStore()
    source_intel = dict(case_intelligence_result) if isinstance(case_intelligence_result, dict) else {}
    source_candidate = _candidate_from_intelligence(source_intel)
    source_mid = str(source_candidate.get("source_signal_id") or "").strip()
    mid = str(message_id or "").strip() or source_mid or f"{case_id}_current"
    sid = str(signal_id or "").strip() or f"sig_{case_id}"
    requested_cid = str(case_id or "").strip()

    intel = ensure_policy_spine_on_intelligence(
        case_intelligence_result,
        case_id=requested_cid,
        message_id=mid,
        dry_run_only=True,
        action_plan_result=action_plan_result,
        intake_result=intake_result,
        case_link_result=case_link_result,
        entity_link_result=entity_link_result,
        mailbox_memory_result=mailbox_memory_result,
        snapshot=snapshot,
        case_snapshot_hot_state=case_snapshot_hot_state,
        run_state=run_state,
        settings=settings,
        stage_config=stage_config,
    )
    candidate = _candidate_from_intelligence(intel)
    spine_cid = str(candidate.get("case_id") or "").strip() or requested_cid
    # Align Understanding source_signal_id with message_id for projection.
    uo = intel.get("understanding_output")
    if isinstance(uo, dict) and uo and not str(uo.get("source_signal_id") or "").strip():
        uo = dict(uo)
        uo["source_signal_id"] = mid
        intel["understanding_output"] = uo
    elif isinstance(uo, dict) and str(uo.get("source_signal_id") or "").strip() != mid:
        # Projection requires exact match — rewrite for harness fidelity when
        # Understanding was keyed differently in eval fixtures.
        uo = dict(uo)
        uo["source_signal_id"] = mid
        intel["understanding_output"] = uo

    persisted, envelope = build_policy_action_envelope_handoff(
        store=store,
        case_intelligence_result=intel,
        case_id=spine_cid,
        source_signal_id=sid,
        source_message_id=mid,
    )

    projection = build_case_understanding_projection(intel, message_id=mid)
    provenance = build_case_understanding_provenance_projection(intel, message_id=mid)

    envelope_model = (
        PolicyActionEnvelopeV1.model_validate(envelope)
        if isinstance(envelope, dict)
        else None
    )
    presence = classify_envelope_presence(
        envelope_model,
        case_understanding_present=projection is not None,
        policy_required=policy_required,
        harness_mode=harness_mode,
    )

    signal: dict[str, Any] = {
        "signal_id": sid,
        "source_kind": "gmail",
        "case_id": spine_cid,
        "planner_case_id_requested": requested_cid,
        "message_id": mid,
        "subject": str(subject or ""),
        "snippet": str(body or "")[:500],
        "body_text": str(body or ""),
        "policy_action_envelope": envelope,
        "policy_required": policy_required,
        "harness_mode": harness_mode,
        "envelope_presence": presence,
        "spine_persist": persisted,
    }
    if case_kind:
        signal["case_kind"] = case_kind
    if projection:
        signal["case_understanding_projection"] = projection
        brief = str(projection.get("essence_pl") or "")[:400]
        if brief:
            signal["understanding_brief_pl"] = brief
    if provenance:
        signal["case_understanding_provenance"] = provenance

    # Hydrate structured facts for known-fact guards / planner view.
    profile_delta: dict[str, Any] = {}
    if isinstance(extraction, dict):
        heated = extraction.get("heated_area_m2") or extraction.get("area_m2")
        city = extraction.get("city") or extraction.get("location_city")
        if heated is not None:
            profile_delta["heated_area_m2"] = heated
        if city:
            profile_delta["location"] = {"city": str(city)}
    if profile_delta:
        signal["hvac_profile_seed"] = profile_delta

    return {
        "signal_payload": signal,
        "mailbox_store": store,
        "case_intelligence_result": intel,
        "envelope_presence": presence,
    }


def apply_hvac_seed_to_snapshot(snapshot: Any, signal_payload: dict[str, Any]) -> Any:
    """Apply optional hvac_profile_seed onto the engagement snapshot before run."""
    seed = signal_payload.get("hvac_profile_seed")
    if not isinstance(seed, dict) or not seed:
        return snapshot
    from llm_contracts.engagement_snapshot_v2 import HvacLocation, HvacProfile

    current = snapshot.hvac_profile
    city = None
    loc = seed.get("location")
    if isinstance(loc, dict):
        city = loc.get("city")
    elif getattr(current.location, "city", None):
        city = current.location.city
    profile = HvacProfile(
        heated_area_m2=seed.get("heated_area_m2", current.heated_area_m2),
        location=HvacLocation(
            city=city,
            postal_code=getattr(current.location, "postal_code", None),
        ),
        building_type=seed.get("building_type", current.building_type),
        thermal_demand_kw=seed.get("thermal_demand_kw", current.thermal_demand_kw),
    )
    return snapshot.model_copy(update={"hvac_profile": profile})


__all__ = [
    "apply_hvac_seed_to_snapshot",
    "build_production_faithful_planner_signal",
    "ensure_policy_spine_on_intelligence",
]
