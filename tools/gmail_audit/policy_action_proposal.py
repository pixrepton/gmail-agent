"""
Normalized action proposal for PolicyEngine — single schema for Gmail intake + signal runtime.

Maps existing action_plan_result / intake / linker outputs to policy_engine.action_class and flags.
Does not compute HVAC, pricing, or OfferDTO; see policy_engine module boundary.
"""

from __future__ import annotations

import hashlib
from typing import Any

from case_snapshot_hot_state_contract import CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION
from policy_engine import PolicyContext, PolicyEngine, PolicyReport

POLICY_ACTION_PROPOSAL_SCHEMA_VERSION = "policy_action_proposal.v1"
EXECUTION_ACTION_PROPOSAL_SCHEMA_VERSION = "action_proposal.v1"

EXECUTION_ACTION_TYPES = {
    "set_case_status",
    "mark_attention_required",
    "prepare_reply_draft",
    "apply_gmail_label",
    "archive_gmail",
    "create_calendar_event",
}

# primary_action -> policy action_class (stable vocabulary for rules in policy_engine)
_PRIMARY_TO_ACTION_CLASS: dict[str, str] = {
    "prepare_reply": "LIVE_REPLY",  # client reply pathway; same policy guards as eventual send
    "create_task": "CREATE_TASK",
    "update_case": "UPDATE_CASE",
    "create_review": "REVIEW_ESCALATION",
    "ignore": "OBSERVE",
    "hold": "HOLD",
}


def _proposal_id(*, run_id: str, message_id: str, primary_action: str) -> str:
    base = f"{run_id}|{message_id}|{primary_action}"
    return f"prop_{hashlib.sha256(base.encode('utf-8')).hexdigest()[:20]}"


def build_execution_action_proposal_v1(
    *,
    case_id: str,
    action_type: str,
    payload: dict[str, Any] | None = None,
    source_signal_id: str = "",
    proposed_by: str = "ai",
    confidence: float = 0.0,
    created_at: str = "",
) -> dict[str, Any]:
    """Build the supervised V1 proposal contract consumed by execution_runtime."""
    from execution_runtime import action_risk_class, now_iso

    action = str(action_type or "").strip()
    risk, basis = action_risk_class(action, payload or {})
    ts = created_at or now_iso()
    proposal_id = f"ap_{hashlib.sha256(f'{case_id}|{source_signal_id}|{action}|{payload}|{ts}'.encode('utf-8')).hexdigest()[:24]}"
    return {
        "schema_version": EXECUTION_ACTION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "case_id": str(case_id or ""),
        "source_signal_id": str(source_signal_id or ""),
        "action_type": action,
        "payload": dict(payload or {}),
        "proposed_by": str(proposed_by or "ai"),
        "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
        "risk_class": risk,
        "requires_review": True,
        "policy_basis": basis,
        "created_at": ts,
        "status": "proposed",
    }


def build_policy_action_proposal(
    *,
    action_plan_result: dict[str, Any] | None,
    intake_result: dict[str, Any] | None,
    case_link_result: dict[str, Any] | None,
    case_intelligence_result: dict[str, Any] | None,
    entity_link_result: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    run_id: str = "",
    message_id: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    """Stable proposal dict consumed by PolicyEngine (not OfferDTO, not kalk-top)."""
    plan = action_plan_result or {}
    intake = intake_result or {}
    business = {}
    if isinstance(case_intelligence_result, dict):
        business = case_intelligence_result.get("case_understanding") or {}
    primary = str(plan.get("primary_action") or "").strip() or "hold"
    action_class = _PRIMARY_TO_ACTION_CLASS.get(primary, "HOLD")

    safe_live = bool(plan.get("safe_for_live_push"))
    review_mode = str((case_intelligence_result or {}).get("review_routing", {}).get("review_mode") or "")
    if not review_mode and isinstance(case_intelligence_result, dict):
        rr = case_intelligence_result.get("review_routing") or {}
        if isinstance(rr, dict):
            review_mode = str(rr.get("review_mode") or "")

    inference_only = review_mode in {"suggest_only"} or bool(
        (case_intelligence_result or {}).get("case_understanding", {}).get("inference_only_proposal")
    )

    # Technical / compliance hints — must come from authoritative tags elsewhere; flags only
    requires_technical = bool(business.get("requires_technical_authority")) or bool(
        (intake.get("metadata") or {}).get("requires_technical_authority")
        if isinstance(intake.get("metadata"), dict)
        else False
    )
    requires_compliance = bool(business.get("requires_compliance_content")) or bool(
        (intake.get("metadata") or {}).get("requires_compliance_content")
        if isinstance(intake.get("metadata"), dict)
        else False
    )

    link_decision = str((case_link_result or {}).get("decision") or "")
    el_status = ""
    if isinstance(entity_link_result, dict):
        el_status = str(entity_link_result.get("link_status") or entity_link_result.get("phase") or "")

    changes_external = primary in {"prepare_reply", "create_task", "update_case"}

    comm_intent = str(business.get("communication_intent") or "").strip().lower()
    if not comm_intent and primary == "prepare_reply":
        comm_intent = "client_reply"

    src = snapshot or {}
    thread = src.get("thread") if isinstance(src.get("thread"), dict) else {}
    is_first_in_thread = str(thread.get("thread_position") or "").lower() in {"first", "only", "latest"}
    # treat latest-only inbound as potentially first operator-facing reply context
    first_contact_hint = is_first_in_thread or bool(thread.get("message_count", 0) in (0, 1))

    return {
        "schema_version": POLICY_ACTION_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": _proposal_id(run_id=run_id, message_id=message_id, primary_action=primary),
        "trace_id": trace_id or run_id,
        "canonical_decision_id": str(plan.get("canonical_decision_id") or ""),
        "primary_action": primary,
        "action_class": action_class,
        "safe_for_live_push": safe_live,
        "is_live": safe_live,
        "external_send": safe_live and primary == "prepare_reply",
        "changes_external_state": changes_external,
        "requires_technical_authority": requires_technical,
        "requires_compliance_content": requires_compliance,
        "inference_only": inference_only,
        "communication_intent": comm_intent,
        "case_link_decision": link_decision,
        "entity_link_status": el_status,
        "first_contact_hint": first_contact_hint,
    }


def build_fallback_case_snapshot_hot_state(
    *,
    mailbox_memory_result: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    case_link_result: dict[str, Any] | None,
    intake_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """When CaseSnapshotManager hot state is absent, provide minimal shape for policy rules."""
    mb = mailbox_memory_result or {}
    snap = snapshot or {}
    intake = intake_result or {}
    case_id = str(mb.get("case_id") or "").strip() or "unknown_case"
    context_pack = mb.get("context_pack") if isinstance(mb.get("context_pack"), dict) else {}
    ctx_snap = context_pack.get("snapshot") if isinstance(context_pack.get("snapshot"), dict) else {}
    if not ctx_snap:
        ctx_snap = mb.get("snapshot") if isinstance(mb.get("snapshot"), dict) else {}
    open_q = list((ctx_snap.get("open_questions") or [])[:5])
    conflicts: list[dict[str, Any]] = []
    if open_q:
        conflicts.append({"severity": "medium", "summary": "open_questions_present"})

    review_required = bool(intake.get("review_required"))
    conf = float(mb.get("confidence") or ctx_snap.get("confidence") or 0.5)

    return {
        "schema_version": CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION,
        "snapshot_id": f"policy_fallback_{case_id}",
        "case": {
            "case_id": case_id,
            "operational_status": "OK",
            "summary_text": str(ctx_snap.get("summary") or "")[:2000],
            "metadata": dict(ctx_snap.get("metadata") or {}) if isinstance(ctx_snap.get("metadata"), dict) else {},
        },
        "key_facts": [],
        "active_conflicts": conflicts,
        "snapshot_meta": {
            "version": 1,
            "confidence": conf,
            "review_required": review_required,
            "entity_link_status": str((case_link_result or {}).get("decision") or ""),
        },
        "latest_activity": {
            "thread_message_count": int(snap.get("thread", {}).get("message_count") or 1)
            if isinstance(snap.get("thread"), dict)
            else 1,
        },
    }


def build_policy_context_for_intake(
    *,
    policy_action_proposal: dict[str, Any],
    case_snapshot_hot_state: dict[str, Any],
    mailbox_memory_result: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    case_link_result: dict[str, Any] | None,
    entity_link_result: dict[str, Any] | None,
    case_intelligence_result: dict[str, Any] | None,
    run_state: dict[str, Any] | None,
    trace_id: str = "",
    proposal_id: str = "",
) -> PolicyContext:
    """Fill PolicyContext from mailbox + snapshot (no new rules — data plumbing only)."""
    mb = mailbox_memory_result or {}
    src = snapshot or {}
    thread = src.get("thread") if isinstance(src.get("thread"), dict) else {}
    pos = str(thread.get("thread_position") or "").lower()
    is_first_contact = bool(policy_action_proposal.get("first_contact_hint")) or pos in {"first", "only"}

    md = {}
    case_block = case_snapshot_hot_state.get("case") if isinstance(case_snapshot_hot_state.get("case"), dict) else {}
    if isinstance(case_block.get("metadata"), dict):
        md = case_block["metadata"]

    ctx = PolicyContext(
        trace_id=trace_id or str(policy_action_proposal.get("trace_id") or ""),
        proposal_id=proposal_id or str(policy_action_proposal.get("proposal_id") or ""),
        is_first_contact=is_first_contact,
        has_approved_communication_state=bool(md.get("approved_outreach") or md.get("communication_approved")),
        relationship_debtor=bool(md.get("debtor")),
        relationship_complaint_active=bool(md.get("complaint_active")),
        relationship_escalation_open=bool(md.get("service_escalation_open")),
        entity_link_status=str(
            (entity_link_result or {}).get("link_status")
            or (entity_link_result or {}).get("phase")
            or policy_action_proposal.get("entity_link_status")
            or ""
        ),
        source_trust_score=float(mb.get("source_trust_score") or 0.85),
        snapshot_confidence=float(
            (case_snapshot_hot_state.get("snapshot_meta") or {}).get("confidence")
            or mb.get("confidence")
            or 0.5
        ),
        event_timeline=list(mb.get("events") or []) if isinstance(mb.get("events"), list) else [],
    )

    if isinstance(case_intelligence_result, dict):
        cd = case_intelligence_result.get("confidence_domains")
        if isinstance(cd, dict):
            ctx.snapshot_confidence = float(cd.get("confidence_case_link") or ctx.snapshot_confidence)

    if isinstance(run_state, dict) and not ctx.event_timeline:
        # optional: future timeline from run_state
        pass

    sm = case_snapshot_hot_state.get("snapshot_meta")
    if isinstance(sm, dict) and sm.get("review_required"):
        pass

    av = {}
    if isinstance(case_snapshot_hot_state.get("snapshot_meta"), dict):
        av = (case_snapshot_hot_state["snapshot_meta"].get("authoritative_verdicts") or {})
    if isinstance(av, dict):
        ctx.authoritative_verdicts.update({k: v for k, v in av.items() if v})

    return ctx


def evaluate_policy_for_intake_stage(
    *,
    action_plan_result: dict[str, Any] | None,
    intake_result: dict[str, Any] | None,
    case_link_result: dict[str, Any] | None,
    entity_link_result: dict[str, Any] | None,
    case_intelligence_result: dict[str, Any] | None,
    mailbox_memory_result: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    case_snapshot_hot_state: dict[str, Any] | None,
    run_state: dict[str, Any] | None,
    policy_engine: PolicyEngine | None = None,
) -> tuple[PolicyReport, dict[str, Any]]:
    """Run PolicyEngine on the unified proposal; returns report + proposal dict for artifacts."""
    run_id = str((run_state or {}).get("run_id") or "")
    src = snapshot or {}
    sm = src.get("source_message") if isinstance(src.get("source_message"), dict) else {}
    message_id = str(sm.get("message_id") or "")
    trace_id = run_id

    proposal = build_policy_action_proposal(
        action_plan_result=action_plan_result,
        intake_result=intake_result,
        case_link_result=case_link_result,
        case_intelligence_result=case_intelligence_result,
        entity_link_result=entity_link_result,
        snapshot=snapshot,
        run_id=run_id,
        message_id=message_id,
        trace_id=trace_id,
    )

    hot = case_snapshot_hot_state if isinstance(case_snapshot_hot_state, dict) and case_snapshot_hot_state else None
    if not hot and isinstance(mailbox_memory_result, dict):
        nested = mailbox_memory_result.get("case_snapshot_hot_state")
        if isinstance(nested, dict) and nested:
            hot = nested
    if not hot:
        hot = build_fallback_case_snapshot_hot_state(
            mailbox_memory_result=mailbox_memory_result,
            snapshot=snapshot,
            case_link_result=case_link_result,
            intake_result=intake_result,
        )

    ctx = build_policy_context_for_intake(
        policy_action_proposal=proposal,
        case_snapshot_hot_state=hot,
        mailbox_memory_result=mailbox_memory_result,
        snapshot=snapshot,
        case_link_result=case_link_result,
        entity_link_result=entity_link_result,
        case_intelligence_result=case_intelligence_result,
        run_state=run_state,
        trace_id=trace_id,
        proposal_id=str(proposal.get("proposal_id") or ""),
    )

    engine = policy_engine or PolicyEngine()
    report = engine.evaluate(proposal, hot, ctx)
    return report, proposal


def attach_policy_evaluation_to_results(
    *,
    mailbox_memory_result: dict[str, Any] | None,
    case_intelligence_result: dict[str, Any] | None,
    policy_report: PolicyReport,
    policy_action_proposal: dict[str, Any],
) -> None:
    """Mutate mailbox + merge automation gates in-place (additive)."""
    from automation_gates import merge_policy_report_into_automation_policy

    pr_dict = policy_report.to_dict()
    if isinstance(mailbox_memory_result, dict):
        mailbox_memory_result["policy_report"] = pr_dict
        mailbox_memory_result["policy_action_proposal"] = dict(policy_action_proposal)

    if isinstance(case_intelligence_result, dict):
        case_intelligence_result["policy_report"] = pr_dict
        case_intelligence_result["policy_action_proposal"] = dict(policy_action_proposal)
        ap = case_intelligence_result.get("automation_policy")
        if isinstance(ap, dict):
            case_intelligence_result["automation_policy"] = merge_policy_report_into_automation_policy(
                ap, pr_dict
            )


def _flag_from_settings_or_config(
    *,
    settings: Any | None,
    stage_config: dict[str, Any] | None,
    settings_attr: str,
    config_key: str,
    default: bool = False,
) -> bool:
    cfg = stage_config if isinstance(stage_config, dict) else {}
    if config_key in cfg:
        return bool(cfg.get(config_key))
    if settings is not None:
        return bool(getattr(settings, settings_attr, default))
    return default


def attach_policy_and_proposals(
    *,
    action_plan_result: dict[str, Any] | None,
    intake_result: dict[str, Any] | None,
    case_link_result: dict[str, Any] | None,
    entity_link_result: dict[str, Any] | None,
    case_intelligence_result: dict[str, Any] | None,
    mailbox_memory_result: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    case_snapshot_hot_state: dict[str, Any] | None,
    run_state: dict[str, Any] | None,
    settings: Any | None = None,
    stage_config: dict[str, Any] | None = None,
    policy_engine: PolicyEngine | None = None,
) -> tuple[PolicyReport, dict[str, Any]]:
    """Single policy evaluate + attach (+ optional ActionProposal v2 bundle on intelligence)."""
    policy_report, policy_action_proposal = evaluate_policy_for_intake_stage(
        action_plan_result=action_plan_result,
        intake_result=intake_result,
        case_link_result=case_link_result,
        entity_link_result=entity_link_result,
        case_intelligence_result=case_intelligence_result,
        mailbox_memory_result=mailbox_memory_result,
        snapshot=snapshot,
        case_snapshot_hot_state=case_snapshot_hot_state,
        run_state=run_state,
        policy_engine=policy_engine,
    )
    attach_policy_evaluation_to_results(
        mailbox_memory_result=mailbox_memory_result,
        case_intelligence_result=case_intelligence_result,
        policy_report=policy_report,
        policy_action_proposal=policy_action_proposal,
    )

    action_v2_enabled = _flag_from_settings_or_config(
        settings=settings,
        stage_config=stage_config,
        settings_attr="action_proposal_v2_enabled",
        config_key="action_proposal_v2_enabled",
    )
    if not action_v2_enabled or not isinstance(case_intelligence_result, dict):
        return policy_report, policy_action_proposal

    cand = case_intelligence_result.get("decision_candidate")
    if not isinstance(cand, dict) or not cand.get("decision_candidate_id"):
        dp = case_intelligence_result.get("decision_pipeline")
        if isinstance(dp, dict):
            outputs = dp.get("outputs") if isinstance(dp.get("outputs"), dict) else {}
            cand = outputs.get("decision_candidate") if isinstance(outputs.get("decision_candidate"), dict) else {}
    if not isinstance(cand, dict) or not cand.get("decision_candidate_id"):
        return policy_report, policy_action_proposal

    from action_proposal_v2 import build_policy_gated_action_proposals_v2_bundle
    from policy_decision import build_policy_decision

    dry_run_only = _flag_from_settings_or_config(
        settings=settings,
        stage_config=stage_config,
        settings_attr="decision_pipeline_dry_run_only",
        config_key="decision_pipeline_dry_run_only",
        default=True,
    )
    policy_decision = build_policy_decision(
        policy_report=policy_report.to_dict(),
        decision_candidate_id=str(cand.get("decision_candidate_id") or ""),
        decision_candidate=cand,
        case_link_result=case_link_result,
        dry_run_only=dry_run_only,
    )
    v2_bundle = build_policy_gated_action_proposals_v2_bundle(
        decision_candidate=cand,
        policy_decision=policy_decision,
        planner_primary_action=str((action_plan_result or {}).get("primary_action") or "hold"),
        dry_run_only=dry_run_only,
    )
    canonical_decision_id = str((action_plan_result or {}).get("canonical_decision_id") or "")
    if canonical_decision_id:
        for raw_proposal in v2_bundle.get("action_proposals_v2") or []:
            if isinstance(raw_proposal, dict):
                raw_proposal["canonical_decision_id"] = canonical_decision_id
    case_intelligence_result["policy_decision"] = policy_decision
    case_intelligence_result["action_proposals_v2"] = v2_bundle["action_proposals_v2"]
    return policy_report, policy_action_proposal


__all__ = [
    "POLICY_ACTION_PROPOSAL_SCHEMA_VERSION",
    "EXECUTION_ACTION_PROPOSAL_SCHEMA_VERSION",
    "attach_policy_and_proposals",
    "attach_policy_evaluation_to_results",
    "build_execution_action_proposal_v1",
    "build_fallback_case_snapshot_hot_state",
    "build_policy_action_proposal",
    "build_policy_context_for_intake",
    "evaluate_policy_for_intake_stage",
]
