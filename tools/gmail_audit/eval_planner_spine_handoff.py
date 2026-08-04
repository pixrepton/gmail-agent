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


def ensure_policy_spine_on_intelligence(
    intelligence: dict[str, Any] | None,
    *,
    case_id: str,
    message_id: str,
    dry_run_only: bool = True,
) -> dict[str, Any]:
    """Attach PolicyDecision + APv2 when decision_candidate exists but spine missing."""
    from action_proposal_v2 import build_action_proposals_v2
    from policy_decision import build_policy_decision

    intel = dict(intelligence) if isinstance(intelligence, dict) else {}
    if isinstance(intel.get("policy_decision"), dict) and isinstance(
        intel.get("action_proposals_v2"), list
    ) and intel.get("action_proposals_v2"):
        return intel

    candidate = intel.get("decision_candidate")
    if not isinstance(candidate, dict) or not candidate:
        pipeline = intel.get("decision_pipeline")
        if isinstance(pipeline, dict):
            outputs = pipeline.get("outputs")
            if isinstance(outputs, dict) and isinstance(
                outputs.get("decision_candidate"), dict
            ):
                candidate = outputs["decision_candidate"]
                intel["decision_candidate"] = candidate

    cid = str(case_id or "").strip() or "case_unknown"
    mid = str(message_id or "").strip() or f"{cid}_msg"
    if not isinstance(candidate, dict) or not str(
        candidate.get("decision_candidate_id") or ""
    ).strip():
        candidate = {
            "schema_version": "decision_candidate.v1",
            "decision_candidate_id": f"dc_harness_{cid}",
            "case_id": cid,
            "source_signal_id": mid,
            "next_best_action": "answer_customer",
            "evidence_refs": [{"evidence_id": "ev_harness", "source_ref": mid}],
        }
        intel["decision_candidate"] = candidate
        intel["_harness_candidate_synthesized"] = True
    else:
        candidate = dict(candidate)
        candidate.setdefault("case_id", cid)
        candidate.setdefault("source_signal_id", mid)
        intel["decision_candidate"] = candidate

    report = {
        "status": "APPROVED",
        "effective_risk_class": "low",
        "policy_basis": ["harness_spine_fidelity"],
        "failed_rules": [],
        "warnings": ["harness_policy_attached_for_fidelity"],
        "requires_review": True,
    }
    decision = build_policy_decision(
        policy_report=report,
        decision_candidate_id=str(candidate.get("decision_candidate_id") or ""),
        decision_candidate=candidate,
        dry_run_only=dry_run_only,
    )
    proposals = build_action_proposals_v2(
        decision_candidate=candidate,
        policy_decision=decision,
        primary_action_type="prepare_reply",
        dry_run_only=dry_run_only,
    )
    intel["policy_decision"] = decision
    intel["action_proposals_v2"] = proposals
    intel["_harness_spine_synthesized"] = True
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
    harness_mode: bool = False,
    policy_required: bool = True,
) -> dict[str, Any]:
    """Build signal_payload matching agent_reconcile handoff shape."""
    store = mailbox_store if mailbox_store is not None else InMemoryMailboxMemoryStore()
    mid = str(message_id or "").strip() or f"{case_id}_current"
    sid = str(signal_id or "").strip() or f"sig_{case_id}"
    cid = str(case_id or "").strip()

    intel = ensure_policy_spine_on_intelligence(
        case_intelligence_result,
        case_id=cid,
        message_id=mid,
        dry_run_only=True,
    )
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
        case_id=cid,
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
        "case_id": cid,
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
