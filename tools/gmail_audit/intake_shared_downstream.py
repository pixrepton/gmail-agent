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

    # Krok 3: LLM-intensive stages przez ThreadPoolExecutor.
    # Uwaga: case_intelligence zalezy od business_result, reply_result i action_plan_result,
    # wiec pelna rownoleglosc nie jest mozliwa bez refaktora architektury.
    # Uzywamy executors dla przejrzystosci kodu i gotowosci na przyszle zmiany.
    from concurrent.futures import ThreadPoolExecutor
    import os

    max_workers = int(os.getenv("INTAKE_SHARED_DOWNSTREAM_MAX_WORKERS", "2"))
    with ThreadPoolExecutor(max_workers=max_workers) as _pool:
        # Stage 1: business_reasoning (LLM) — niezalezne, startuje pierwsze
        _t = time.monotonic()
        biz_future = _pool.submit(run_business_reasoning, snapshot, intake_result, case_link_result, context_bundle, stage_config)
        business_result = biz_future.result()
        stage_timings["business_reasoning"] = _timer_ms(_t)

        # Stage 2: draft_reply (LLM) — zalezy od business_result
        if opts.skip_draft_reply:
            reply_result = {"draft_enabled": False, "drafts": [], "skipped": "drive_signal"}
        else:
            _t = time.monotonic()
            reply_future = _pool.submit(draft_reply, snapshot, intake_result, business_result, context_bundle, stage_config)
            reply_result = reply_future.result()
            stage_timings["draft_reply"] = _timer_ms(_t)

        # Stage 3: action_plan (deterministyczne, szybkie)
        _t = time.monotonic()
        action_plan_result = plan_actions(intake_result, case_link_result, business_result, reply_result, stage_config)
        stage_timings["action_plan"] = _timer_ms(_t)

        # Stage 4: case_intelligence (LLM) — zalezy od business_result, reply_result, action_plan_result
        _t = time.monotonic()
        if opts.case_intelligence_guard_exceptions:
            try:
                ci_future = _pool.submit(build_case_intelligence_layer, snapshot, intake_result, case_link_result, business_result, reply_result, action_plan_result, stage_config)
                case_intelligence_result = ci_future.result()
            except Exception as exc:  # noqa: BLE001 - enrichment must not block operator visibility
                warnings.append("case_intelligence_exception")
                case_intelligence_result = fallback_case_intelligence_result(
                    snapshot=snapshot,
                    intake_result=intake_result,
                    case_link_result=case_link_result,
                    business_result=business_result,
                    action_plan_result=action_plan_result,
                    error=exc,
                )
        else:
            ci_future = _pool.submit(build_case_intelligence_layer, snapshot, intake_result, case_link_result, business_result, reply_result, action_plan_result, stage_config)
            case_intelligence_result = ci_future.result()
        stage_timings["case_intelligence"] = _timer_ms(_t)
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
    "run_shared_downstream_stages",
]
