"""Shared downstream stages for legacy process_snapshot tail and signal reconcile."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
import time

HotStateMode = Literal["none", "legacy_inject", "reconcile_signal_apply"]


# ── Stage timing helper (Faza 4a) ──────────────────────────────────────────────


def _timer_ms(start: float) -> float:
    """Zwraca czas trwania w ms od `start`."""
    return round((time.monotonic() - start) * 1000, 1)


@dataclass(slots=True)
class SharedDownstreamOptions:
    """Per-caller hooks; defaults match legacy process_snapshot tail."""

    resolve_effective_context_bundle: bool = False
    case_intelligence_guard_exceptions: bool = False
    hot_state_mode: HotStateMode = "none"
    entity_link_result: dict[str, Any] | None = None
    run_state: dict[str, Any] | None = None
    case_snapshot_hot_state_for_policy: dict[str, Any] | None = None
    # reconcile_signal_apply only
    signal: Any | None = None
    runtime_context: Any | None = None
    dry_run: bool = False
    skip_draft_reply: bool = False


@dataclass(slots=True)
class SharedDownstreamResult:
    case_link_result: dict[str, Any]
    business_result: dict[str, Any]
    reply_result: dict[str, Any]
    action_plan_result: dict[str, Any]
    case_intelligence_result: dict[str, Any]
    mailbox_memory_result: dict[str, Any]
    context_bundle: dict[str, Any]
    stage_config: dict[str, Any]
    policy_report: dict[str, Any] | None
    policy_action_proposal: dict[str, Any] | None
    hot_state_snapshot: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    stage_timings_ms: dict[str, float] = field(default_factory=dict)  # Faza 4a: metryki per-stage


def resolve_hot_state_case_id(
    *,
    signal: Any,
    runtime_context: Any,
    case_id: str,
    entity_link_dict: dict[str, Any],
) -> str:
    resolved = str(case_id or "").strip()
    if resolved:
        return resolved
    if str((entity_link_dict or {}).get("phase") or "").strip().lower() != "adjudication":
        return ""
    store = runtime_context.resolved_store
    fetch_override = getattr(store, "fetch_latest_adjudication_link_override", None) if store is not None else None
    if not callable(fetch_override):
        return ""
    override = fetch_override(signal.signal_id)
    if not isinstance(override, dict):
        return ""
    if str(override.get("override_kind") or "").strip() != "reject_same_case":
        return ""
    return str(override.get("rejected_case_id") or "").strip()


def fallback_case_intelligence_result(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    business_result: dict[str, Any],
    action_plan_result: dict[str, Any],
    error: Exception,
) -> dict[str, Any]:
    """Minimal projection-safe enrichment when case intelligence raises."""
    sm = snapshot.get("source_message") if isinstance(snapshot.get("source_message"), dict) else {}
    decision = intake_result.get("decision") if isinstance(intake_result.get("decision"), dict) else {}
    business_summary = str(business_result.get("business_summary_short") or "").strip()
    subject = str(sm.get("subject") or "").strip()
    essence = business_summary or subject or "Wiadomosc wymaga przegladu operatora; enrichment niedostepny."
    return {
        "case_understanding": {
            "case_id": str(case_link_result.get("case_id") or case_link_result.get("selected_case_id") or ""),
            "case_key": str(case_link_result.get("selected_case_key") or ""),
            "business_priority": str(decision.get("priority") or "unknown"),
            "confidence_overall": 0.0,
        },
        "desk_composition": {
            "presence_mode": "review",
            "fallback_mode": True,
        },
        "operator_explanation": {
            "essence_pl": essence[:500],
        },
        "next_best_action": {
            "primary_next_action": {
                "action_type": str(action_plan_result.get("primary_action") or decision.get("action") or "review"),
                "requires_operator_review": True,
            }
        },
        "execution_metadata": {
            "stage_name": "case_intelligence",
            "source_mode": "fallback",
            "fallback_reason": "case_intelligence_exception",
            "error_type": type(error).__name__,
        },
    }


_INVALID_FACT_STATUSES = {"superseded", "rejected", "stale", "invalidated", "disputed"}


def _case_id_from_context_pack(pack: dict[str, Any]) -> str:
    case_id = str(pack.get("case_id") or "").strip()
    if case_id:
        return case_id
    snapshot = pack.get("snapshot") if isinstance(pack.get("snapshot"), dict) else {}
    return str(snapshot.get("case_id") or snapshot.get("case_key") or "").strip()


def _source_message_id(snapshot: dict[str, Any], intake_result: dict[str, Any]) -> str:
    source = snapshot.get("source_message") if isinstance(snapshot.get("source_message"), dict) else {}
    return str(source.get("message_id") or intake_result.get("message_id") or "").strip()


def _trusted_prior_state_in_context_pack(pack: dict[str, Any], *, source_message_id: str) -> bool:
    facts = pack.get("active_facts") if isinstance(pack.get("active_facts"), list) else []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        status = str(fact.get("status") or "active").strip().lower()
        if status in _INVALID_FACT_STATUSES:
            continue
        key = str(fact.get("fact_key") or fact.get("key") or "").strip()
        if not key:
            continue
        source_ref = str(fact.get("source_ref") or "").strip()
        source_refs = [str(ref.get("source_id") or ref.get("message_id") or "").strip() for ref in fact.get("source_refs") or [] if isinstance(ref, dict)]
        evidence_refs = {ref for ref in [source_ref, *source_refs] if ref}
        if not evidence_refs:
            continue
        if source_message_id and source_message_id in evidence_refs:
            continue
        return True
    snapshot = pack.get("snapshot") if isinstance(pack.get("snapshot"), dict) else {}
    for key in ("key_facts", "open_questions", "latest_documents", "timeline"):
        values = snapshot.get(key)
        if isinstance(values, list) and values:
            return True
    for key in ("documents", "latest_documents"):
        values = pack.get(key)
        if isinstance(values, list) and values:
            return True
    return False


def trust_case_link_from_context_pack(
    *,
    case_link_result: dict[str, Any],
    mailbox_memory_result: dict[str, Any],
    context_pack: dict[str, Any] | None,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
) -> dict[str, Any]:
    """Upgrade an empty/no-link decision only when mailbox SoT already has prior case state."""
    from intake_schema import validate_case_link_result

    original = case_link_result if isinstance(case_link_result, dict) else {}
    decision = str(original.get("decision") or "no_link")
    if decision not in {"", "no_link"}:
        return original
    pack = context_pack if isinstance(context_pack, dict) else {}
    case_id = _case_id_from_context_pack(pack)
    mailbox_case_id = str((mailbox_memory_result or {}).get("case_id") or "").strip()
    if not case_id:
        return original
    if mailbox_case_id and mailbox_case_id != case_id:
        return original
    source_id = _source_message_id(snapshot, intake_result)
    if not _trusted_prior_state_in_context_pack(pack, source_message_id=source_id):
        return original

    confidence = max(float(original.get("confidence") or 0.0), 0.88)
    reasons = [*list(original.get("reasons") or []), "trusted_case_context_pack", "prior_case_state_present"]
    return validate_case_link_result({
        "selected_case_key": str(pack.get("case_key") or case_id),
        "selected_case_id": case_id,
        "case_id": case_id,
        "decision": "linked",
        "confidence": confidence,
        "source": "context_candidate",
        "reasons": reasons,
        "candidates": [{
            "case_key": str(pack.get("case_key") or case_id),
            "score": confidence,
            "source": "context_candidate",
            "reasons": reasons,
            "hard_match_count": 1,
            "soft_match_count": 0,
        }],
    })


def run_shared_downstream_stages(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    context_bundle: dict[str, Any],
    stage_config: dict[str, Any],
    options: SharedDownstreamOptions | None = None,
) -> SharedDownstreamResult:
    """link_case_context → mailbox → business → intelligence → policy (shared spine)."""
    opts = options or SharedDownstreamOptions()
    from policy_action_proposal import attach_policy_and_proposals

    from gmail_intake import (
        build_case_intelligence_layer,
        build_context_bundle,
        draft_reply,
        finalize_mailbox_memory,
        ingest_mailbox_memory,
        inject_latest_hot_state_for_resolved_case,
        link_case_context,
        load_hot_state_preflight_for_stage_config,
        merge_hot_state_into_mailbox_memory_result,
        plan_actions,
        run_business_reasoning,
    )

    warnings: list[str] = []
    entity_link_result = opts.entity_link_result
    if entity_link_result is None:
        entity_link_result = stage_config.get("entity_link_result")

    # Faza 4a: metryki per-stage z timerami — lokalny dict (thread-safe)
    stage_timings: dict[str, float] = {}

    _t = time.monotonic()
    case_link_result = link_case_context(snapshot, intake_result, context_bundle, stage_config)
    stage_timings["case_link"] = _timer_ms(_t)
    stage_config["case_link_result"] = case_link_result

    _t = time.monotonic()
    mailbox_memory_result = ingest_mailbox_memory(snapshot, intake_result, case_link_result, stage_config)
    stage_timings["mailbox_ingest"] = _timer_ms(_t)
    stage_config["mailbox_memory_result"] = mailbox_memory_result

    mailbox_context_pack = mailbox_memory_result.get("context_pack") if isinstance(mailbox_memory_result, dict) else None
    if isinstance(mailbox_context_pack, dict) and mailbox_context_pack:
        stage_config["mailbox_memory_context_pack"] = mailbox_context_pack
        context_bundle = build_context_bundle(snapshot, case_context_pack=mailbox_context_pack)
    load_hot_state_preflight_for_stage_config(
        mailbox_memory_result=mailbox_memory_result,
        config=stage_config,
    )
    if opts.resolve_effective_context_bundle:
        from gmail_intake import _resolve_effective_context_bundle

        context_bundle = _resolve_effective_context_bundle(snapshot, context_bundle, stage_config)
        effective_pack = context_bundle.get("case_context_pack") if isinstance(context_bundle, dict) else None
    else:
        effective_pack = mailbox_context_pack

    trusted_case_link_result = trust_case_link_from_context_pack(
        case_link_result=case_link_result,
        mailbox_memory_result=mailbox_memory_result,
        context_pack=effective_pack if isinstance(effective_pack, dict) else None,
        snapshot=snapshot,
        intake_result=intake_result,
    )
    if trusted_case_link_result != case_link_result:
        stage_config["case_link_result_before_context_trust"] = case_link_result
        case_link_result = trusted_case_link_result
        stage_config["case_link_result"] = case_link_result

    # Krok 3: LLM-intensive stages przez ThreadPoolExecutor.
    # RP-30: case_intelligence runs before action_plan so planning follows understanding.
    from concurrent.futures import ThreadPoolExecutor
    import os

    _EMPTY_ACTION_PLAN: dict[str, Any] = {}

    max_workers = int(os.getenv("INTAKE_SHARED_DOWNSTREAM_MAX_WORKERS", "2"))
    with ThreadPoolExecutor(max_workers=max_workers) as _pool:
        # Stage 1: business_reasoning (LLM) — niezalezne, startuje pierwsze
        _t = time.monotonic()
        biz_future = _pool.submit(run_business_reasoning, snapshot, intake_result, case_link_result, context_bundle, stage_config)
        business_result = biz_future.result()
        stage_timings["business_reasoning"] = _timer_ms(_t)

        # CanonicalActionDecision boundary — after BusinessReasoning, before any
        # downstream re-translation (draft / CI / ActionPlan / policy / planner).
        _t = time.monotonic()
        from canonical_action_decision import build_canonical_decision_for_stage

        understanding = stage_config.get("understanding_output")
        canonical_decision, canonicalization_failure = build_canonical_decision_for_stage(
            business_reasoning_result=business_result,
            situation_understanding=understanding if isinstance(understanding, dict) else None,
            case_context_pack=effective_pack if isinstance(effective_pack, dict) else None,
            intake_result=intake_result,
            case_id=str(
                ((intake_result.get("case_assessment") or {}).get("case_id") if isinstance(intake_result, dict) else "")
                or (intake_result.get("case_id") if isinstance(intake_result, dict) else "")
                or ""
            ),
            situation_version=str(
                (understanding.get("understanding_output_id") if isinstance(understanding, dict) else "")
                or (understanding.get("created_at") if isinstance(understanding, dict) else "")
                or ""
            ),
        )
        stage_config["canonical_decision"] = canonical_decision
        stage_config["canonicalization_failure"] = canonicalization_failure
        stage_timings["canonical_decision"] = _timer_ms(_t)

        # Stage 2: draft_reply (LLM) — zalezy od business_result
        if opts.skip_draft_reply:
            from reply_drafter import annotate_reply_causal_observability

            run_id = str(
                ((opts.run_state or {}).get("run_id") if isinstance(opts.run_state, dict) else "")
                or stage_config.get("run_id")
                or ""
            )
            reply_result = annotate_reply_causal_observability(
                {"draft_enabled": False, "drafts": [], "skipped": "drive_signal"},
                snapshot=snapshot,
                intake_result=intake_result,
                business_result=business_result,
                context_bundle=context_bundle,
                lane_plan=stage_config.get("lane_stage_plan")
                if isinstance(stage_config.get("lane_stage_plan"), dict)
                else {},
                skip_draft_reply=True,
                run_id=run_id,
            )
        else:
            _t = time.monotonic()
            reply_future = _pool.submit(draft_reply, snapshot, intake_result, business_result, context_bundle, stage_config)
            reply_result = reply_future.result()
            stage_timings["draft_reply"] = _timer_ms(_t)

        # Stage 3: case_intelligence (LLM) — before action_plan (RP-30)
        _t = time.monotonic()
        if opts.case_intelligence_guard_exceptions:
            try:
                ci_future = _pool.submit(
                    build_case_intelligence_layer,
                    snapshot,
                    intake_result,
                    case_link_result,
                    business_result,
                    reply_result,
                    _EMPTY_ACTION_PLAN,
                    stage_config,
                )
                case_intelligence_result = ci_future.result()
            except Exception as exc:  # noqa: BLE001 - enrichment must not block operator visibility
                warnings.append("case_intelligence_exception")
                case_intelligence_result = fallback_case_intelligence_result(
                    snapshot=snapshot,
                    intake_result=intake_result,
                    case_link_result=case_link_result,
                    business_result=business_result,
                    action_plan_result=_EMPTY_ACTION_PLAN,
                    error=exc,
                )
        else:
            ci_future = _pool.submit(
                build_case_intelligence_layer,
                snapshot,
                intake_result,
                case_link_result,
                business_result,
                reply_result,
                _EMPTY_ACTION_PLAN,
                stage_config,
            )
            case_intelligence_result = ci_future.result()
        stage_timings["case_intelligence"] = _timer_ms(_t)
        stage_config["case_intelligence_result"] = case_intelligence_result

        # Stage 4: action_plan — deterministic, after understanding is available
        _t = time.monotonic()
        action_plan_result = plan_actions(
            intake_result,
            case_link_result,
            business_result,
            reply_result,
            stage_config,
        )
        stage_timings["action_plan"] = _timer_ms(_t)
    stage_config["case_intelligence_result"] = case_intelligence_result

    _t = time.monotonic()
    mailbox_memory_result = finalize_mailbox_memory(
        snapshot=snapshot,
        business_result=business_result,
        reply_result=reply_result,
        action_plan_result=action_plan_result,
        case_intelligence_result=case_intelligence_result,
        config=stage_config,
    )
    stage_timings["mailbox_finalize"] = _timer_ms(_t)
    stage_config["mailbox_memory_result"] = mailbox_memory_result

    hot_state_snapshot: dict[str, Any] = {}
    if opts.hot_state_mode == "legacy_inject":
        mailbox_memory_result, case_intelligence_result = inject_latest_hot_state_for_resolved_case(
            mailbox_memory_result=mailbox_memory_result,
            case_intelligence_result=case_intelligence_result,
            mailbox_memory_runtime=stage_config.get("mailbox_memory_runtime"),
        )
        stage_config["mailbox_memory_result"] = mailbox_memory_result
    elif opts.hot_state_mode == "reconcile_signal_apply":
        from case_intelligence import apply_hot_state_to_case_intelligence
        from case_snapshot_manager import CaseSnapshotManager

        signal = opts.signal
        runtime_context = opts.runtime_context
        if signal is not None and runtime_context is not None:
            case_id = str(mailbox_memory_result.get("case_id") or "")
            hot_state_case_id = resolve_hot_state_case_id(
                signal=signal,
                runtime_context=runtime_context,
                case_id=case_id,
                entity_link_dict=entity_link_result or {},
            )
            if hot_state_case_id and not opts.dry_run:
                hot_state_snapshot = CaseSnapshotManager(store=runtime_context.resolved_store).apply_signal(
                    signal,
                    case_id_override=hot_state_case_id,
                    trace_id=str((runtime_context.run_state or {}).get("run_id") or ""),
                )
                mailbox_memory_result = merge_hot_state_into_mailbox_memory_result(
                    mailbox_memory_result,
                    hot_state_snapshot,
                )
                case_intelligence_result = apply_hot_state_to_case_intelligence(
                    case_intelligence_result,
                    hot_state_snapshot,
                )
                stage_config["mailbox_memory_result"] = mailbox_memory_result

    stage_config.pop("case_snapshot_hot_state_preflight", None)
    stage_config.pop("mailbox_memory_context_pack_preflight", None)
    stage_config["mailbox_memory_result"] = mailbox_memory_result

    hot_state_for_policy = opts.case_snapshot_hot_state_for_policy
    if hot_state_for_policy is None and hot_state_snapshot:
        hot_state_for_policy = hot_state_snapshot
    if hot_state_for_policy is None and isinstance(mailbox_memory_result, dict):
        hot_state_for_policy = mailbox_memory_result.get("case_snapshot_hot_state")

    settings = stage_config.get("settings")
    _t = time.monotonic()
    policy_report, policy_action_proposal = attach_policy_and_proposals(
        action_plan_result=action_plan_result,
        intake_result=intake_result,
        case_link_result=case_link_result,
        entity_link_result=entity_link_result,
        case_intelligence_result=case_intelligence_result,
        mailbox_memory_result=mailbox_memory_result,
        snapshot=snapshot,
        case_snapshot_hot_state=hot_state_for_policy,
        run_state=opts.run_state,
        settings=settings,
        stage_config=stage_config,
    )
    stage_timings["policy"] = _timer_ms(_t)

    return SharedDownstreamResult(
        case_link_result=case_link_result,
        business_result=business_result,
        reply_result=reply_result,
        action_plan_result=action_plan_result,
        case_intelligence_result=case_intelligence_result,
        mailbox_memory_result=mailbox_memory_result,
        context_bundle=context_bundle,
        stage_config=stage_config,
        policy_report=policy_report,
        policy_action_proposal=policy_action_proposal,
        hot_state_snapshot=hot_state_snapshot,
        warnings=warnings,
        stage_timings_ms=stage_timings,
    )


__all__ = [
    "SharedDownstreamOptions",
    "SharedDownstreamResult",
    "fallback_case_intelligence_result",
    "resolve_hot_state_case_id",
    "trust_case_link_from_context_pack",
    "run_shared_downstream_stages",
]
