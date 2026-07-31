"""Shared downstream state engine for canonical signals."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_runtime.signal_registry import register_signal_handler
from case_intelligence import apply_hot_state_to_case_intelligence
from case_routing import enrich_case_row_before_upsert
from case_snapshot_manager import CaseSnapshotManager
from entity_linker import EntityLinker, apply_entity_link
from mailbox_memory_store import InMemoryMailboxMemoryStore, MailboxMemoryStore
from projection_refresh_rules import ProjectionRefreshDecision, decide_projection_refresh
from signal_contract import CanonicalSignal
from signal_journal import SignalJournal
from exceptions import StagingDeduplicationError, WriteTransactionError
from log_config import get_logger

from config import Settings
from graph_store import GraphStore
from mailbox_memory_runtime import MailboxMemoryRuntime

logger = get_logger("signal_reconciler")


@dataclass(slots=True)
class SignalRuntimeContext:
    settings: 'Settings'
    journal: SignalJournal
    mailbox_memory_runtime: 'MailboxMemoryRuntime | None' = None
    store: 'MailboxMemoryStore | None' = None
    graph_store: 'GraphStore | None' = None
    run_state: dict[str, Any] | None = None
    model: str | None = None
    verbose: bool = False
    mode: str = "active"
    persist_entity_links: bool = True
    operator_scope: str = "operator"
    trace_id: str = ""  # Faza 0c: distributed tracing — propagowany z workera

    @property
    def resolved_store(self) -> 'MailboxMemoryStore | None':
        if self.store is not None:
            return self.store
        if self.mailbox_memory_runtime is not None:
            return self.mailbox_memory_runtime.store
        return None

    def for_replay(self) -> "SignalRuntimeContext":
        if self.mailbox_memory_runtime is not None and not isinstance(
            self.mailbox_memory_runtime.store, InMemoryMailboxMemoryStore
        ):
            return replace(
                self,
                persist_entity_links=False,
                run_state=self.run_state,
            )
        try:
            from graph_store import InMemoryGraphStore
        except Exception:  # pragma: no cover - optional
            graph_store = None
        else:
            graph_store = InMemoryGraphStore()
        from mailbox_memory_runtime import MailboxMemoryRuntime
        blob_root = Path(getattr(self.settings, "mailbox_memory_blob_root", Path.cwd() / "data" / "mailbox_memory" / "blobs"))
        runtime = MailboxMemoryRuntime(
            store=InMemoryMailboxMemoryStore(),
            blob_root=blob_root,
            stage_mode="shadow",
            graph_store=graph_store,
        )
        runtime.bootstrap()
        return SignalRuntimeContext(
            settings=self.settings,
            journal=self.journal,
            mailbox_memory_runtime=runtime,
            graph_store=graph_store,
            model=self.model,
            verbose=self.verbose,
            mode="active",
            persist_entity_links=False,
        )


@dataclass(slots=True, frozen=True)
class CaseMutationPlan:
    case_id: str
    case_key: str
    mutation_kind: str
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class FactUpdates:
    facts_upserted: int = 0
    documents_upserted: int = 0
    events_appended: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class SnapshotRefreshDecision:
    should_refresh: bool
    mode: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReconcileResult:
    signal_id: str
    source_kind: str
    signal_kind: str
    processing_state: str
    case_id: str = ""
    case_key: str = ""
    linked_entity_id: str = ""
    linked_entity_kind: str = "case"
    entity_link: dict[str, Any] = field(default_factory=dict)
    case_mutation_plan: CaseMutationPlan | None = None
    fact_updates: FactUpdates = field(default_factory=FactUpdates)
    snapshot_refresh_decision: SnapshotRefreshDecision | None = None
    projection_refresh_decision: ProjectionRefreshDecision | None = None
    mailbox_memory_result: dict[str, Any] = field(default_factory=dict)
    rebuild_result: dict[str, Any] = field(default_factory=dict)
    preview: dict[str, Any] | None = None
    v2_projection: dict[str, Any] | None = None
    stage_outputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        linked_id = self.linked_entity_id or self.case_id
        linked_kind = self.linked_entity_kind or ("engagement" if str(linked_id).startswith("stg_") else "case")
        return {
            "signal_id": self.signal_id,
            "source_kind": self.source_kind,
            "signal_kind": self.signal_kind,
            "processing_state": self.processing_state,
            "case_id": self.case_id,
            "case_key": self.case_key,
            "linked_entity_id": linked_id,
            "linked_entity_kind": linked_kind,
            "entity_link": self.entity_link,
            "case_mutation_plan": self.case_mutation_plan.to_dict() if self.case_mutation_plan else {},
            "fact_updates": self.fact_updates.to_dict(),
            "snapshot_refresh_decision": self.snapshot_refresh_decision.to_dict() if self.snapshot_refresh_decision else {},
            "projection_refresh_decision": self.projection_refresh_decision.to_dict() if self.projection_refresh_decision else {},
            "mailbox_memory_result": self.mailbox_memory_result,
            "rebuild_result": self.rebuild_result,
            "preview": self.preview,
            "v2_projection": self.v2_projection,
            "stage_outputs": self.stage_outputs,
            "warnings": list(self.warnings),
        }


def _maybe_cleanup_staging_engagements(runtime_context: SignalRuntimeContext) -> None:
    """TTL cleanup for stale stg_* rows without case_id (MAX-STACK slice E)."""
    try:
        from agent_runtime.agent_reconcile import build_operator_engagement_store
        from agent_runtime.settings import load_agent_runtime_settings
        from agent_runtime.staging_ttl import cleanup_stale_staging_engagements

        settings = getattr(runtime_context, "settings", None)
        if settings is None:
            return
        agent_settings = load_agent_runtime_settings()
        if not agent_settings.enabled:
            return
        operator_store = build_operator_engagement_store(settings)
        max_hours = int(getattr(agent_settings, "staging_ttl_hours", 72) or 72)
        cleanup_stale_staging_engagements(operator_store, max_age_hours=max_hours)
        logger.info("STAGING_CLEANUP_COMPLETED", extra={"x": {"max_hours": max_hours}})
    except Exception as exc:
        raise StagingDeduplicationError(
            "TTL cleanup failed — staging may accumulate",
            context={"component": "_maybe_cleanup_staging"}
        ) from exc


def reconcile_signal(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool = False,
) -> ReconcileResult:
    _maybe_cleanup_staging_engagements(runtime_context)
    logger.info("SIGNAL_RECEIVED", extra={"x": {
        "signal_id": signal.signal_id,
        "source_kind": signal.source_kind,
        "sender": getattr(signal, "sender_email", "") or "",
    }})
    runtime_context.journal.record_processing_attempt(
        signal=signal,
        status="started",
        details={"dry_run": dry_run, "mode": runtime_context.mode},
    )
    try:
        entity_link_dict: dict[str, Any] = {}
        if runtime_context.resolved_store is not None:
            if runtime_context.persist_entity_links:
                signal, link_result = apply_entity_link(
                    signal,
                    store=runtime_context.resolved_store,
                    graph_store=runtime_context.graph_store,
                    run_state=runtime_context.run_state,
                )
            else:
                link_result = EntityLinker(runtime_context.resolved_store).find_case(signal)
                entity_payload = dict(signal.payload or {})
                entity_artifacts = dict(signal.artifacts or {})
                merged = link_result.to_dict()
                entity_payload["_entity_link"] = merged
                entity_artifacts["entity_link"] = merged
                if link_result.link_status == "VERIFIED" and link_result.case_id:
                    entity_payload["case_id"] = link_result.case_id
                    entity_payload["entity_link_case_id"] = link_result.case_id
                    entity_payload["entity_link_case_key"] = link_result.case_key
                signal = replace(signal, payload=entity_payload, artifacts=entity_artifacts)
            entity_link_dict = link_result.to_dict()
        # PR-Signal: registry pattern zamiast if/elif
        from agent_runtime.signal_registry import SIGNAL_HANDLERS

        handler = SIGNAL_HANDLERS.get(signal.source_kind)
        if handler is None:
            raise ValueError(f"Unsupported signal source_kind: {signal.source_kind}")
        result = handler(
            signal,
            runtime_context=runtime_context,
            dry_run=dry_run,
            entity_link_dict=entity_link_dict,
        )
        runtime_context.journal.record_processing_attempt(
            signal=signal,
            status=result.processing_state,
            details={
                "case_id": result.case_id,
                "projection_refresh": result.projection_refresh_decision.to_dict() if result.projection_refresh_decision else {},
                "warnings": list(result.warnings),
            },
        )
        logger.info("SIGNAL_RECONCILED", extra={"x": {
            "signal_id": signal.signal_id,
            "case_id": result.case_id,
            "processing_state": result.processing_state,
        }})
        return result
    except Exception as exc:
        failure_code = str(getattr(exc, "failure_code", "") or "signal_reconcile_failed")
        retryable = bool(getattr(exc, "retryable", False))
        severity = str(getattr(exc, "severity", "") or "attention_required")
        exception_class = str(getattr(exc, "exception_class", "") or type(exc).__name__)
        logger.error("SIGNAL_FAILED", extra={"x": {
            "signal_id": getattr(signal, "signal_id", ""),
            "source_kind": getattr(signal, "source_kind", ""),
        }})
        runtime_context.journal.record_processing_attempt(
            signal=signal,
            status="failed",
            error_text=str(exc),
            details={
                "dry_run": dry_run,
                "failure_code": failure_code,
                "retryable": retryable,
                "severity": severity,
                "exception_class": exception_class,
            },
        )
        raise


def reconcile_signal_batch(
    signals: list[CanonicalSignal],
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool = False,
) -> list[ReconcileResult]:
    return [
        reconcile_signal(signal, runtime_context=runtime_context, dry_run=dry_run)
        for signal in signals
    ]


def replay_signal(
    signal_id: str,
    *,
    runtime_context: SignalRuntimeContext,
) -> ReconcileResult:
    signal = runtime_context.journal.fetch_signal(signal_id)
    if signal is None:
        raise ValueError(f"Unknown signal_id: {signal_id}")
    return reconcile_signal(signal, runtime_context=runtime_context.for_replay(), dry_run=False)


@register_signal_handler("gmail")
def _reconcile_gmail_signal(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
) -> ReconcileResult:
    from agent_runtime.agent_reconcile import (
        agent_runtime_reconcile_active,
        legacy_downstream_reconcile_active,
        resolve_case_id_for_agent,
    )
    from agent_runtime.orchestrator import route_signal

    if signal.signal_kind in {"gmail_case_proposal", "CASE_PROPOSAL"}:
        if agent_runtime_reconcile_active():
            return _reconcile_gmail_signal_staging_agent(
                signal,
                runtime_context=runtime_context,
                dry_run=dry_run,
                entity_link_dict=entity_link_dict,
            )
    if signal.signal_kind == "gmail_message_observed":
        if agent_runtime_reconcile_active():
            payload = dict(signal.payload or {})
            intake_result = dict(payload.get("intake_result_final") or payload.get("intake_output") or {})
            case_id = resolve_case_id_for_agent(
                signal,
                entity_link_dict,
                intake_output=intake_result,
            )
            route = route_signal(
                signal,
                entity_link=entity_link_dict,
                case_id=case_id,
                link_confidence=float(entity_link_dict.get("link_confidence") or 0.0),
            )
            if not case_id and route.route == "deep_understand":
                return _reconcile_gmail_signal_staging_agent(
                    signal,
                    runtime_context=runtime_context,
                    dry_run=dry_run,
                    entity_link_dict=entity_link_dict,
                )
            return _reconcile_gmail_signal_agent(
                signal,
                runtime_context=runtime_context,
                dry_run=dry_run,
                entity_link_dict=entity_link_dict,
            )
        if not legacy_downstream_reconcile_active():
            raise RuntimeError("reconcile mode misconfigured: neither agent nor legacy path active")
    if signal.signal_kind != "gmail_message_observed":
        if signal.signal_kind in {"gmail_attachment_observed", "gmail_aux_attachment"} and agent_runtime_reconcile_active():
            case_id = str(signal.payload.get("case_id") or entity_link_dict.get("case_id") or "").strip()
            if not case_id:
                return _reconcile_gmail_signal_staging_agent(
                    signal,
                    runtime_context=runtime_context,
                    dry_run=dry_run,
                    entity_link_dict=entity_link_dict,
                )
        projection_decision = decide_projection_refresh(
            signal.signal_kind,
            source_kind="gmail",
            case_id=str(signal.payload.get("case_id") or ""),
            has_case_state=False,
        )
        return ReconcileResult(
            signal_id=signal.signal_id,
            source_kind=signal.source_kind,
            signal_kind=signal.signal_kind,
            processing_state="shadowed" if dry_run else "reconciled",
            case_id=str(signal.payload.get("case_id") or ""),
            case_key=str(signal.case_key_hint or ""),
            entity_link=entity_link_dict,
            projection_refresh_decision=projection_decision,
            snapshot_refresh_decision=SnapshotRefreshDecision(
                should_refresh=False,
                mode="incremental_refresh",
                reason="gmail_aux_signal_noop",
            ),
            warnings=["gmail auxiliary signal recorded without full downstream refresh"],
        )

    return _reconcile_gmail_signal_legacy(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        entity_link_dict=entity_link_dict,
    )


def _check_cieplo_staging_dedup(
    signal: CanonicalSignal,
    runtime_context: SignalRuntimeContext,
) -> dict[str, Any]:
    """Check if Cieplo signal already has a staging engagement (message_id dedup).

    Prevents duplicate TUM staging engagements for the same Cieplo message.
    """
    source_repo = str(
        signal.payload.get("source_repo")
        or signal.artifacts.get("source_repo")
        or ""
    ).strip().lower()
    if source_repo != "cieplo-orchestrator":
        return {"skip": False, "reason": "not_cieplo"}

    try:
        from agent_runtime.agent_reconcile import build_operator_engagement_store
        from agent_runtime.settings import load_agent_runtime_settings

        settings = getattr(runtime_context, "settings", None)
        if settings is None:
            return {"skip": False, "reason": "no_settings"}
        agent_settings = load_agent_runtime_settings()
        if not agent_settings.enabled:
            return {"skip": False, "reason": "agent_runtime_disabled"}
        operator_store = build_operator_engagement_store(settings)

        # Check existing staging engagements by trace_id
        staging_ids = getattr(operator_store, "list_staging_engagement_ids", None)
        if callable(staging_ids):
            for sid in staging_ids() or []:
                snap = operator_store.load_snapshot(sid)
                if snap is not None and str(getattr(snap, "trace_id", "") or "") == signal.signal_id:
                    return {"skip": True, "reason": f"cieplo_staging_already_exists:{signal.signal_id}", "engagement_id": sid}
    except Exception as exc:
        raise StagingDeduplicationError(
            "Cieplo dedup check failed — duplicates may pass",
            context={"component": "_check_cieplo_staging_dedup"}
        ) from exc
    return {"skip": False, "reason": "cieplo_no_duplicate"}


def _reconcile_gmail_signal_staging_agent(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
) -> ReconcileResult:
    from agent_runtime.agent_reconcile import build_agent_reconcile_result, run_agent_reconcile_staging

    # P2-8: Cieplo dedup — skip if staging engagement already exists for this message
    dedup = _check_cieplo_staging_dedup(signal, runtime_context)
    if dedup.get("skip", False):
        eid = str(dedup.get("engagement_id") or "").strip()
        return ReconcileResult(
            signal_id=signal.signal_id,
            source_kind=signal.source_kind,
            signal_kind=signal.signal_kind,
            processing_state="skipped_duplicate",
            case_id="",
            case_key=signal.case_key_hint or "",
            linked_entity_id=eid,
            linked_entity_kind="engagement",
            entity_link=entity_link_dict,
            warnings=[f"cieplo_staging_dedup:{dedup.get('reason', 'already_exists')}"],
        )

    payload = dict(signal.payload or {})
    intake_result = dict(payload.get("intake_result_final") or payload.get("intake_output") or payload)
    synthetic_intake = {
        **intake_result,
        "message": dict(intake_result.get("message") or {"message_id": signal.signal_id}),
        "staging": True,
    }
    snapshot_eng, run_result, resolution, warnings = run_agent_reconcile_staging(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        intake_output=synthetic_intake,
    )
    return build_agent_reconcile_result(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        entity_link_dict=entity_link_dict,
        snapshot_eng=snapshot_eng,
        resolution=resolution,
        run_result=run_result,
        warnings=[*warnings, "gmail_staging_agent_path"],
        intake_output=synthetic_intake,
        source_kind_override="gmail",
    )


def _reconcile_gmail_signal_agent(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
) -> ReconcileResult:
    from agent_runtime.agent_reconcile import build_agent_reconcile_result, run_agent_reconcile

    payload = dict(signal.payload or {})
    intake_result = dict(payload.get("intake_result_final") or payload.get("intake_output") or {})
    snapshot_eng, run_result, resolution, warnings, case_intelligence_result, mailbox_intel_result = run_agent_reconcile(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        entity_link_dict=entity_link_dict,
        intake_output=intake_result,
    )
    return build_agent_reconcile_result(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        entity_link_dict=entity_link_dict,
        snapshot_eng=snapshot_eng,
        resolution=resolution,
        run_result=run_result,
        warnings=warnings,
        intake_output=intake_result,
        case_intelligence_result=case_intelligence_result,
        downstream_mailbox_result=mailbox_intel_result,
    )


def _load_agent_runtime_mode_label() -> str:
    from agent_runtime.settings import load_agent_runtime_settings

    return str(load_agent_runtime_settings().mode or "prep")


# ---- split from _reconcile_gmail_signal_legacy ----
def _reconcile_gmail_legacy_prepare(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
) -> dict[str, Any]:
    """Phase 1: extract payload, build stage config, run downstream stages."""
    from event_memory import EventLog
    from gmail_intake import (
        build_context_bundle,
        build_projection_preview,
        build_v2_projection,
        hydrate_intelligence_seam_config,
    )
    from intake_shared_downstream import SharedDownstreamOptions, run_shared_downstream_stages

    snapshot = dict(signal.payload.get("snapshot") or {})
    intake_result = dict(signal.payload.get("intake_result_final") or signal.payload.get("intake_output") or {})
    preclassification_result = dict(signal.payload.get("preclassification_result") or {"lane": "intake_llm"})
    lane_stage_plan = dict(signal.payload.get("lane_stage_plan") or _default_lane_stage_plan())
    context_bundle = dict(signal.payload.get("context_bundle") or build_context_bundle(snapshot))

    stage_config = {
        "settings": runtime_context.settings,
        "model": runtime_context.model or getattr(runtime_context.settings, "groq_model", ""),
        "verbose": runtime_context.verbose,
        "snapshot": snapshot,
        "preclassification_result": preclassification_result,
        "lane_stage_plan": lane_stage_plan,
        "event_log": EventLog(),
    }
    hydrate_intelligence_seam_config(runtime_context.run_state or {}, snapshot, stage_config)
    if dry_run:
        stage_config["mailbox_memory_runtime"] = None
        stage_config["daszek_client"] = None
    stage_config["entity_link_result"] = entity_link_dict

    downstream = run_shared_downstream_stages(
        snapshot=snapshot,
        intake_result=intake_result,
        context_bundle=context_bundle,
        stage_config=stage_config,
        options=SharedDownstreamOptions(
            case_intelligence_guard_exceptions=True,
            hot_state_mode="reconcile_signal_apply",
            entity_link_result=entity_link_dict,
            run_state=runtime_context.run_state,
            signal=signal,
            runtime_context=runtime_context,
            dry_run=dry_run,
        ),
    )
    case_link_result = downstream.case_link_result
    business_result = downstream.business_result
    reply_result = downstream.reply_result
    action_plan_result = downstream.action_plan_result
    case_intelligence_result = downstream.case_intelligence_result
    mailbox_memory_result = downstream.mailbox_memory_result
    context_bundle = downstream.context_bundle
    stage_config = downstream.stage_config
    warnings = list(downstream.warnings)
    hot_state_snapshot = downstream.hot_state_snapshot
    case_id = str(mailbox_memory_result.get("case_id") or "")
    case_key = str(case_link_result.get("selected_case_key") or signal.case_key_hint or "")
    hot_state_case_id = _resolve_hot_state_case_id(
        signal=signal,
        runtime_context=runtime_context,
        case_id=case_id,
        entity_link_dict=entity_link_dict,
    )
    return {
        "snapshot": snapshot,
        "intake_result": intake_result,
        "preclassification_result": preclassification_result,
        "lane_stage_plan": lane_stage_plan,
        "context_bundle": context_bundle,
        "stage_config": stage_config,
        "downstream": downstream,
        "case_link_result": case_link_result,
        "business_result": business_result,
        "reply_result": reply_result,
        "action_plan_result": action_plan_result,
        "case_intelligence_result": case_intelligence_result,
        "mailbox_memory_result": mailbox_memory_result,
        "warnings": warnings,
        "hot_state_snapshot": hot_state_snapshot,
        "case_id": case_id,
        "case_key": case_key,
        "hot_state_case_id": hot_state_case_id,
    }


def _reconcile_gmail_legacy_execute(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
    pr: dict[str, Any],
) -> ReconcileResult:
    """Phase 2: build projection and assemble result."""
    snapshot = pr["snapshot"]
    intake_result = pr["intake_result"]
    preclassification_result = pr["preclassification_result"]
    lane_stage_plan = pr["lane_stage_plan"]
    context_bundle = pr["context_bundle"]
    stage_config = pr["stage_config"]
    downstream = pr["downstream"]
    case_link_result = pr["case_link_result"]
    business_result = pr["business_result"]
    reply_result = pr["reply_result"]
    action_plan_result = pr["action_plan_result"]
    case_intelligence_result = pr["case_intelligence_result"]
    mailbox_memory_result = pr["mailbox_memory_result"]
    warnings = pr["warnings"]
    hot_state_snapshot = pr["hot_state_snapshot"]
    case_id = pr["case_id"]
    case_key = pr["case_key"]
    hot_state_case_id = pr["hot_state_case_id"]
    from gmail_intake import build_projection_preview
    from projection_snapshot_transport import build_operator_projection_snapshot, v2_projection_from_snapshot

    stage_outputs_for_projection = {
        "preclassification_result": preclassification_result,
        "case_link_result": case_link_result,
        "entity_link_result": entity_link_dict,
        "business_reasoning_result": business_result,
        "reply_draft_result": reply_result,
        "action_plan_result": action_plan_result,
        "case_intelligence_result": case_intelligence_result,
        "mailbox_memory_result": mailbox_memory_result,
        "canonical_signal_id": signal.signal_id,
    }
    operator_snapshot = build_operator_projection_snapshot(
        intake_result,
        stage_outputs=stage_outputs_for_projection,
        run_id=str(((runtime_context.run_state or {}).get("run_id") or "signal-runtime")),
        settings=runtime_context.settings,
    )
    projection_validation = operator_snapshot.get("projection_validation") if isinstance(operator_snapshot, dict) else {}
    if isinstance(projection_validation, dict) and not projection_validation.get("ok", True):
        validation_errors = list(projection_validation.get("errors") or [])
        warnings.append("projection_validation_failed: " + "; ".join(str(e) for e in validation_errors[:6]))
        if not dry_run:
            return ReconcileResult(
                signal_id=signal.signal_id,
                source_kind=signal.source_kind,
                signal_kind=signal.signal_kind,
                processing_state="failed",
                warnings=warnings,
            )
    v2_projection = v2_projection_from_snapshot(operator_snapshot)
    preview = build_projection_preview(
        intake_result,
        preclassification_result=preclassification_result,
        case_link_result=case_link_result,
        business_result=business_result,
        reply_result=reply_result,
        action_plan_result=action_plan_result,
        case_intelligence_result=case_intelligence_result,
        mailbox_memory_result=mailbox_memory_result,
        canonical_signal_id=signal.signal_id,
    )

    projection_decision = decide_projection_refresh(
        signal.signal_kind,
        source_kind="gmail",
        case_id=case_id or hot_state_case_id,
        has_case_state=bool(mailbox_memory_result.get("snapshot")),
    )
    if not dry_run:
        _stamp_case_runtime_state(
            runtime_context.resolved_store,
            case_id=case_id or hot_state_case_id,
            signal=signal,
            projection_decision=projection_decision,
        )

    rebuild_result = {
        "case_id": case_id,
        "snapshot": mailbox_memory_result.get("snapshot") or {},
        "hot_state_snapshot": hot_state_snapshot,
        "context_pack": mailbox_memory_result.get("context_pack") or {},
        "next_action": mailbox_memory_result.get("next_action") or {},
        "rebuild_mode": "incremental_refresh",
        "source_refs_used": list((mailbox_memory_result.get("context_pack") or {}).get("source_refs") or []),
        "update_reasons": ["gmail_signal_reconciled"],
    }
    return ReconcileResult(
        signal_id=signal.signal_id,
        source_kind=signal.source_kind,
        signal_kind=signal.signal_kind,
        processing_state="shadowed" if dry_run else "reconciled",
        case_id=case_id,
        case_key=case_key,
        entity_link=entity_link_dict,
        case_mutation_plan=CaseMutationPlan(
            case_id=case_id,
            case_key=case_key,
            mutation_kind=str(case_link_result.get("decision") or "relinked"),
            reasons=list(case_link_result.get("reasons") or []),
        ),
        fact_updates=FactUpdates(
            facts_upserted=len(mailbox_memory_result.get("facts") or []),
            documents_upserted=len(mailbox_memory_result.get("documents") or []),
            events_appended=len(mailbox_memory_result.get("events") or []),
        ),
        snapshot_refresh_decision=SnapshotRefreshDecision(
            should_refresh=True,
            mode="incremental_refresh",
            reason="gmail_message_observed",
        ),
        projection_refresh_decision=projection_decision,
        mailbox_memory_result=mailbox_memory_result,
        rebuild_result=rebuild_result,
        preview=preview,
        v2_projection=v2_projection,
        stage_outputs={
            "preclassification_result": preclassification_result,
            "case_link_result": case_link_result,
            "entity_link_result": entity_link_dict,
            "business_reasoning_result": business_result,
            "reply_draft_result": reply_result,
            "action_plan_result": action_plan_result,
            "case_intelligence_result": case_intelligence_result,
            "mailbox_memory_result": mailbox_memory_result,
            "operator_projection_snapshot": operator_snapshot,
        },
        warnings=warnings,
    )


def _reconcile_gmail_signal_legacy(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
) -> ReconcileResult:
    """Reconcile Gmail signal (prepare + execute)."""
    pr = _reconcile_gmail_legacy_prepare(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        entity_link_dict=entity_link_dict,
    )
    return _reconcile_gmail_legacy_execute(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        entity_link_dict=entity_link_dict,
        pr=pr,
    )


def _fallback_case_intelligence_result(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    business_result: dict[str, Any],
    action_plan_result: dict[str, Any],
    error: Exception,
) -> dict[str, Any]:
    from intake_shared_downstream import fallback_case_intelligence_result

    return fallback_case_intelligence_result(
        snapshot=snapshot,
        intake_result=intake_result,
        case_link_result=case_link_result,
        business_result=business_result,
        action_plan_result=action_plan_result,
        error=error,
    )


def _resolve_hot_state_case_id(
    *,
    signal: CanonicalSignal,
    runtime_context: SignalRuntimeContext,
    case_id: str,
    entity_link_dict: dict[str, Any],
) -> str:
    from intake_shared_downstream import resolve_hot_state_case_id

    return resolve_hot_state_case_id(
        signal=signal,
        runtime_context=runtime_context,
        case_id=case_id,
        entity_link_dict=entity_link_dict,
    )


@register_signal_handler("calendar")
def _reconcile_calendar_signal(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
) -> ReconcileResult:
    from agent_runtime.agent_reconcile import agent_runtime_reconcile_active, run_agent_reconcile_staging, build_agent_reconcile_result
    from agent_runtime.orchestrator import route_signal

    payload = dict(signal.payload or {})
    case_id = str(payload.get("case_id") or entity_link_dict.get("case_id") or "").strip()
    route = route_signal(
        signal,
        entity_link=entity_link_dict,
        case_id=case_id,
        link_confidence=float(entity_link_dict.get("link_confidence") or payload.get("link_confidence") or 0.0),
    )
    if agent_runtime_reconcile_active() and not case_id and route.route == "deep_understand":
        synthetic_intake = {
            "event_id": str(payload.get("event_id") or signal.source_ref.get("event_id") or signal.signal_id),
            "summary": str(payload.get("summary") or signal.signal_summary_pl or ""),
            "staging": True,
        }
        snapshot_eng, run_result, resolution, warnings = run_agent_reconcile_staging(
            signal,
            runtime_context=runtime_context,
            dry_run=dry_run,
            intake_output=synthetic_intake,
        )
        return build_agent_reconcile_result(
            signal,
            runtime_context=runtime_context,
            dry_run=dry_run,
            entity_link_dict=entity_link_dict,
            snapshot_eng=snapshot_eng,
            resolution=resolution,
            run_result=run_result,
            warnings=[*warnings, "calendar_staging_agent_path"],
            intake_output=synthetic_intake,
            source_kind_override="calendar",
        )
    projection_decision = decide_projection_refresh(
        signal.signal_kind,
        source_kind="calendar",
        case_id=case_id,
        has_case_state=bool(case_id),
    )
    return ReconcileResult(
        signal_id=signal.signal_id,
        source_kind=signal.source_kind,
        signal_kind=signal.signal_kind,
        processing_state="shadowed" if dry_run else "reconciled",
        case_id=case_id,
        case_key=str(signal.case_key_hint or ""),
        entity_link=entity_link_dict,
        projection_refresh_decision=projection_decision,
        snapshot_refresh_decision=SnapshotRefreshDecision(
            should_refresh=bool(case_id),
            mode="incremental_refresh",
            reason="calendar_signal_refresh",
        ),
        warnings=["calendar_spine_reconcile"],
    )


@register_signal_handler("operator_command")
def _reconcile_operator_command(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
) -> ReconcileResult:
    """Reconcile an operator command signal via staging agent path.

    Generic Hands / Agent-as-Gateway (I4):
    - Gdy case_id w payload jest pusty — czat ogólny.
    - W czacie ogólnym agent korzysta z wyspecjalizowanych read tools do znalezienia kontekstu.
    - NIE klasyfikuj intencji przez if/elif — agent sam decyduje.
    """
    from agent_runtime.agent_reconcile import (
        agent_runtime_reconcile_active,
        build_agent_reconcile_result,
        run_agent_reconcile_staging,
    )

    if not agent_runtime_reconcile_active():
        raise RuntimeError("agent runtime required for operator_command")

    payload = dict(signal.payload or {})
    case_id = str(payload.get("case_id") or "").strip()
    is_general_chat = not bool(case_id)

    synthetic_intake = {
        "message": {
            "message_id": str(payload.get("message_id") or signal.source_ref.get("message_id") or signal.signal_id),
            "subject": str(payload.get("subject") or signal.signal_summary_pl or "Polecenie operatora"),
            "body_text": str(payload.get("user_input") or payload.get("body_text") or ""),
        },
        "staging": True,
        "is_general_chat": is_general_chat,
    }

    if is_general_chat:
        # I4.1: Czat ogólny — nie ma docelowej sprawy
        # Agent sam wybierze dopuszczalne narzedzie read do znalezienia kontekstu
        synthetic_intake["case_id"] = ""
        # Wstrzyknij system note dla czatu ogólnego
        try:
            from agent_runtime.constitution import GENERAL_GATEWAY_SYSTEM_NOTE
            synthetic_intake["system_note_extra"] = GENERAL_GATEWAY_SYSTEM_NOTE
        except ImportError:
            logger.debug("GENERAL_GATEWAY_SYSTEM_NOTE not available — general chat gateway note skipped")

    # Put user instruction into payload for the agent to see
    signal_with_instruction = signal
    user_input = str(payload.get("user_input") or "").strip()
    if user_input:
        from dataclasses import replace
        updated_payload = dict(signal.payload or {})
        updated_payload["user_instruction"] = user_input
        signal_with_instruction = replace(signal, payload=updated_payload)

    snapshot_eng, run_result, resolution, warnings = run_agent_reconcile_staging(
        signal_with_instruction,
        runtime_context=runtime_context,
        dry_run=dry_run,
        intake_output=synthetic_intake,
    )

    # Inject user_instruction into snapshot so planner sees it
    if user_input and snapshot_eng is not None and not snapshot_eng.user_instruction:
        from agent_runtime.snapshot_delta import apply_snapshot_delta
        snapshot_eng = apply_snapshot_delta(snapshot_eng, {"user_instruction": user_input})

    return build_agent_reconcile_result(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        entity_link_dict=entity_link_dict,
        snapshot_eng=snapshot_eng,
        resolution=resolution,
        run_result=run_result,
        warnings=[*warnings, "operator_command_staging_agent_path"],
        intake_output=synthetic_intake,
        source_kind_override="operator_command",
    )


def _reconcile_drive_signal_staging_agent(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
    document_row: dict[str, Any],
) -> ReconcileResult:
    from agent_runtime.agent_reconcile import build_agent_reconcile_result, run_agent_reconcile_staging

    file_id = str(signal.source_ref.get("file_id") or signal.payload.get("file_id") or "")
    synthetic_intake = {
        "message": {"message_id": signal.signal_id},
        "file_id": file_id,
        "file_name": str(document_row.get("file_name") or ""),
        "drive_document_id": str(document_row.get("document_id") or ""),
        "staging": True,
    }
    snapshot_eng, run_result, resolution, warnings = run_agent_reconcile_staging(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        intake_output=synthetic_intake,
    )
    return build_agent_reconcile_result(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        entity_link_dict=entity_link_dict,
        snapshot_eng=snapshot_eng,
        resolution=resolution,
        run_result=run_result,
        warnings=[*warnings, "drive_staging_agent_path"],
        intake_output=synthetic_intake,
        fact_updates=FactUpdates(documents_upserted=1 if document_row else 0),
        source_kind_override="drive",
    )


def _reconcile_drive_signal_agent(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
    case_id: str,
    case_key: str,
    document_row: dict[str, Any],
) -> ReconcileResult:
    from agent_runtime.agent_reconcile import build_agent_reconcile_result, run_agent_reconcile

    synthetic_intake = _build_drive_intake_result(
        signal,
        document_row=document_row,
        case_id=case_id,
        case_key=case_key,
        snapshot={"case_id": case_id, "status": "open"},
    )
    snapshot_eng, run_result, resolution, warnings, case_intelligence_result, mailbox_intel_result = run_agent_reconcile(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        entity_link_dict={**entity_link_dict, "case_id": case_id},
        intake_output=synthetic_intake,
    )
    return build_agent_reconcile_result(
        signal,
        runtime_context=runtime_context,
        dry_run=dry_run,
        entity_link_dict=entity_link_dict,
        snapshot_eng=snapshot_eng,
        resolution=resolution,
        run_result=run_result,
        warnings=warnings,
        intake_output=synthetic_intake,
        fact_updates=FactUpdates(documents_upserted=1 if document_row else 0),
        source_kind_override="drive",
        case_intelligence_result=case_intelligence_result,
        downstream_mailbox_result=mailbox_intel_result,
    )


@register_signal_handler("drive")
def _reconcile_drive_signal(
    signal: CanonicalSignal,
    *,
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
) -> ReconcileResult:
    from case_guidance_reasoner import fallback_case_guidance
    from case_intelligence import build_case_intelligence
    from gmail_intake import merge_hot_state_into_mailbox_memory_result

    store = runtime_context.resolved_store
    if store is None:
        raise RuntimeError("Drive signal reconciliation requires mailbox/shared memory store.")

    payload = dict(signal.payload or {})
    document_row = dict(payload.get("document_row") or {})
    fact_rows = [dict(item) for item in payload.get("fact_rows") or []]
    event_rows = [dict(item) for item in payload.get("event_rows") or []]
    graph_upsert = dict(payload.get("graph_upsert") or {})
    case_seed_row = dict(payload.get("case_seed_row") or {})
    case_id = str(payload.get("case_id") or document_row.get("case_id") or case_seed_row.get("case_id") or "")
    case_key = str(payload.get("case_key") or document_row.get("probable_case_key") or case_seed_row.get("case_key") or signal.case_key_hint or "")
    raw_observation_id = str((signal.artifacts or {}).get("raw_observation_id") or "").strip()
    if raw_observation_id:
        normalized_fact_rows: list[dict[str, Any]] = []
        for row in fact_rows:
            item = dict(row)
            metadata = dict(item.get("metadata") or {})
            original_source_ref = str(item.get("source_ref") or "")
            if original_source_ref:
                metadata["original_source_ref"] = original_source_ref
            metadata["raw_observation_id"] = raw_observation_id
            item["source_ref"] = raw_observation_id
            item["metadata"] = metadata
            normalized_fact_rows.append(item)
        fact_rows = normalized_fact_rows
    if case_seed_row and not dry_run:
        # CONC-01: case_seed_row used to be written via a plain, unconditional
        # store.upsert_case(enriched) -- a full-row overwrite with no read and
        # no lock. Any concurrent contribution to the same brand-new case_id
        # (e.g. a Gmail signal's _stamp_case_runtime_state call) landing
        # around this write was silently discarded. Route through the same
        # atomic mutate_case contract instead, merging case_seed_row's own
        # fields onto the row read under lock so a concurrent contribution
        # already committed (or committed while this call waits for the
        # lock) survives.
        seed_case_id = str(case_id or case_seed_row.get("case_id") or "").strip()
        mutate = getattr(type(store), "mutate_case", None)
        if seed_case_id and callable(mutate):
            def _mutate_case_seed_row(row: dict[str, Any]) -> dict[str, Any]:
                merged = dict(row)
                merged.update(case_seed_row)
                merged["case_id"] = seed_case_id
                enriched, routing = enrich_case_row_before_upsert(
                    merged,
                    source_kind=str(signal.source_kind or "drive"),
                )
                return enriched if routing.upsert_allowed else dict(row)

            store.mutate_case(seed_case_id, _mutate_case_seed_row, create_if_missing=True)
        else:
            enriched, routing = enrich_case_row_before_upsert(
                case_seed_row,
                source_kind=str(signal.source_kind or "drive"),
            )
            if routing.upsert_allowed:
                store.upsert_case(enriched)
    chunk_rows = [dict(item) for item in payload.get("chunk_rows") or []]
    drive_write_ok = True
    if not dry_run and (document_row or fact_rows or event_rows):
        try:
            if document_row and signal.signal_kind not in {"drive_document_link_candidate", "drive_document_removed"}:
                store.upsert_drive_document(document_row)
                document_id = str(document_row.get("document_id") or payload.get("document_id") or "").strip()
                if chunk_rows and document_id and hasattr(store, "replace_drive_document_chunks"):
                    store.replace_drive_document_chunks(document_id=document_id, rows=chunk_rows)
            if fact_rows:
                store.replace_drive_document_facts(
                    document_id=str(document_row.get("document_id") or payload.get("document_id") or ""),
                    rows=fact_rows,
                )
            if event_rows:
                for row in event_rows:
                    store.append_event(row)
        except Exception as exc:
            raise WriteTransactionError(
                "Drive signal reconcile write failed — partial state possible",
                context={"case_id": case_id, "signal_id": signal.signal_id}
            ) from exc
    if graph_upsert and runtime_context.graph_store is not None and not dry_run and drive_write_ok:
        nodes = list(graph_upsert.get("nodes") or [])
        edges = list(graph_upsert.get("edges") or [])
        if nodes or edges:
            runtime_context.graph_store.upsert_many(nodes=nodes, edges=edges)

    if not case_id and case_key and document_row:
        from mailbox_memory_runtime import stable_id
        case_id = stable_id("case", case_key)

    from agent_runtime.agent_reconcile import agent_runtime_reconcile_active
    from agent_runtime.orchestrator import route_signal

    link_confidence = float(
        document_row.get("link_confidence")
        or payload.get("link_confidence")
        or entity_link_dict.get("link_confidence")
        or 0.0
    )
    linkage_status = str(
        document_row.get("linkage_status")
        or payload.get("linkage_status")
        or entity_link_dict.get("linkage_status")
        or ""
    )
    route = route_signal(
        signal,
        entity_link=entity_link_dict,
        case_id=case_id,
        link_confidence=link_confidence,
        linkage_status=linkage_status,
    )

    # Drive intelligence = agent reasoning layer (LLM + tools: read_google_drive_file, …).
    # Nie shared downstream spine — operator vision 2026-06-20 (RFC E1 §korekta).
    # Drive-as-Gateway (2026-06-24): when agent runtime is active, always route
    # through agent runtime regardless of route.route. Legacy inline is only for
    # agent-runtime-disabled fallback.
    if case_id and agent_runtime_reconcile_active():
        return _reconcile_drive_signal_agent(
            signal,
            runtime_context=runtime_context,
            dry_run=dry_run,
            entity_link_dict=entity_link_dict,
            case_id=case_id,
            case_key=case_key,
            document_row=document_row,
        )

    if not case_id and agent_runtime_reconcile_active():
        return _reconcile_drive_signal_staging_agent(
            signal,
            runtime_context=runtime_context,
            dry_run=dry_run,
            entity_link_dict=entity_link_dict,
            document_row=document_row,
        )

    logger.warning("drive_legacy_fallback case_id=%s route=%s agent_active=%s",
                case_id, route.route, agent_runtime_reconcile_active())
    return _reconcile_drive_signal_legacy_inline(
        signal,
        case_id=case_id,
        case_key=case_key,
        document_row=document_row,
        fact_rows=fact_rows,
        event_rows=event_rows,
        payload=payload,
        entity_link_dict=entity_link_dict,
        runtime_context=runtime_context,
        dry_run=dry_run,
        store=store,
    )


def _reconcile_drive_legacy_prepare_case_state(
    signal: CanonicalSignal,
    *,
    case_id: str,
    case_key: str,
    document_row: dict[str, Any],
    fact_rows: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
    payload: dict[str, Any],
    entity_link_dict: dict[str, Any],
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    store: Any,
) -> dict[str, Any]:
    """Prepare case state for Drive signal (if case_id exists)."""
    from case_guidance_reasoner import fallback_case_guidance
    from case_intelligence import build_case_intelligence
    from gmail_intake import merge_hot_state_into_mailbox_memory_result
    from policy_action_proposal import attach_policy_and_proposals
    from projection_snapshot_transport import build_operator_projection_snapshot, v2_projection_from_snapshot
    preview = None

    case_record = store.fetch_case(case_id) or {
        "case_id": case_id,
        "case_key": case_key,
        "case_family": str(payload.get("case_family") or "unknown"),
        "mailbox": "drive",
        "subject": str(document_row.get("file_name") or signal.signal_summary_pl or "Drive case"),
        "status": "open",
        "customer_name": "",
        "customer_email": "",
        "metadata": {"source": "drive_signal_runtime"},
    }
    from mailbox_memory_runtime import build_case_snapshot
    snapshot = build_case_snapshot(
        case_id=case_id,
        case_record=case_record,
        messages=store.fetch_messages_for_case(case_id, limit=10),
        facts=store.fetch_facts_for_case(case_id),
        documents=store.fetch_documents_for_case(case_id, limit=8),
        events=store.fetch_events_for_case(case_id, limit=20),
        next_action=store.fetch_next_action(case_id) or {},
        drive_enrichment=_collect_drive_enrichment(runtime_context, case_id),
    )
    from mailbox_memory_runtime import build_case_context_pack
    context_pack_obj = build_case_context_pack(
        store=store,
        case_id=case_id,
        query_text=str(document_row.get("file_name") or signal.signal_summary_pl or ""),
        graph_store=runtime_context.graph_store,
        retrieval_runtime=runtime_context.mailbox_memory_runtime,
    )
    context_pack = context_pack_obj.to_dict()

    synthetic_intake = _build_drive_intake_result(signal, document_row=document_row, case_id=case_id, case_key=case_key, snapshot=snapshot)
    business_result = _build_drive_business_result(signal, document_row=document_row, snapshot=snapshot)
    action_plan_result = _build_drive_action_plan(signal, snapshot=snapshot)
    case_link_result = {
        "selected_case_key": case_key,
        "case_id": case_id,
        "decision": str(payload.get("linkage_status") or document_row.get("linkage_status") or "deterministic"),
        "reasons": list(payload.get("link_reasons") or (document_row.get("metadata") or {}).get("link_reasons") or []),
    }
    case_intelligence_result = build_case_intelligence(
        snapshot={"source_message": {"message_id": str(signal.source_ref.get("file_id") or signal.signal_id)}, "mailbox": "drive"},
        intake_result=synthetic_intake,
        case_link_result=case_link_result,
        business_result=business_result,
        reply_result={"draft_enabled": False, "drafts": []},
        action_plan_result=action_plan_result,
        case_context_pack=context_pack,
    )
    case_intelligence_result["case_guidance"] = fallback_case_guidance(
        reason="drive_signal_refresh",
        base_intelligence=case_intelligence_result,
    )
    next_action = {
        "case_id": case_id,
        "next_action": str(action_plan_result.get("primary_action") or "review"),
        "rationale": str(action_plan_result.get("why_this_action") or ""),
        "source_stage": "signal_reconciler",
        "payload": action_plan_result,
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    if not dry_run:
        store.upsert_snapshot(
            case_id,
            {
                "status": str(snapshot.get("status") or "open"),
                "customer_name": str((snapshot.get("customer") or {}).get("name") or ""),
                "customer_email": str((snapshot.get("customer") or {}).get("email") or ""),
                "recommended_next_action": str(next_action.get("next_action") or ""),
                "snapshot_json": snapshot,
                "updated_at": next_action["updated_at"],
            },
        )
        store.upsert_next_action(case_id, next_action)
        hot_state_snapshot = CaseSnapshotManager(store=store).apply_signal(
            signal,
            case_id_override=case_id,
            trace_id=str((runtime_context.run_state or {}).get("run_id") or ""),
        )
        case_intelligence_result = apply_hot_state_to_case_intelligence(
            case_intelligence_result,
            hot_state_snapshot,
        )
    mailbox_memory_result = {
        "enabled": not dry_run,
        "case_id": case_id,
        "snapshot": snapshot,
        "case_snapshot_hot_state": hot_state_snapshot,
        "context_pack": context_pack,
        "next_action": next_action,
        "facts": fact_rows,
        "documents": [document_row] if document_row else [],
        "events": event_rows,
    }
    if isinstance(hot_state_snapshot, dict) and hot_state_snapshot:
        mailbox_memory_result = merge_hot_state_into_mailbox_memory_result(mailbox_memory_result, hot_state_snapshot)
    _drive_snap = {
        "mailbox": "drive",
        "source_message": {"message_id": str((signal.source_ref or {}).get("file_id") or signal.signal_id)},
    }
    _hot_for_policy = (
        hot_state_snapshot
        if isinstance(hot_state_snapshot, dict) and hot_state_snapshot
        else mailbox_memory_result.get("case_snapshot_hot_state")
    )
    attach_policy_and_proposals(
        action_plan_result=action_plan_result,
        intake_result=synthetic_intake,
        case_link_result=case_link_result,
        entity_link_result=entity_link_dict,
        case_intelligence_result=case_intelligence_result,
        mailbox_memory_result=mailbox_memory_result,
        snapshot=_drive_snap,
        case_snapshot_hot_state=_hot_for_policy if isinstance(_hot_for_policy, dict) else None,
        run_state=runtime_context.run_state,
        settings=runtime_context.settings,
    )
    operator_snapshot = build_operator_projection_snapshot(
        synthetic_intake,
        stage_outputs={
            "preclassification_result": {"lane": "drive_signal"},
            "case_link_result": case_link_result,
            "entity_link_result": entity_link_dict,
            "business_reasoning_result": business_result,
            "reply_draft_result": {"draft_enabled": False, "drafts": []},
            "action_plan_result": action_plan_result,
            "case_intelligence_result": case_intelligence_result,
            "mailbox_memory_result": mailbox_memory_result,
            "canonical_signal_id": signal.signal_id,
        },
        settings=runtime_context.settings,
        run_id=str(((runtime_context.run_state or {}).get("run_id") or "signal-runtime")),
    )
    v2_projection = v2_projection_from_snapshot(operator_snapshot)
    return {
        "case_record": case_record,
        "snapshot": snapshot,
        "context_pack": context_pack,
        "synthetic_intake": synthetic_intake,
        "business_result": business_result,
        "action_plan_result": action_plan_result,
        "case_link_result": case_link_result,
        "case_intelligence_result": case_intelligence_result,
        "next_action": next_action,
        "mailbox_memory_result": mailbox_memory_result,
        "operator_snapshot": operator_snapshot,
        "v2_projection": v2_projection,
        "preview": preview,
        "hot_state_snapshot": hot_state_snapshot,
    }


def _reconcile_drive_legacy_prepare_no_case(
    signal: CanonicalSignal,
    *,
    case_id: str,
    case_key: str,
    document_row: dict[str, Any],
    fact_rows: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
    payload: dict[str, Any],
    entity_link_dict: dict[str, Any],
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    store: Any,
) -> dict[str, Any]:
    """Prepare minimal result when no case_id exists."""
    mailbox_memory_result = {
        "enabled": False,
        "case_id": "",
        "snapshot": {},
        "context_pack": {},
        "next_action": {},
        "facts": fact_rows,
        "documents": [document_row] if document_row else [],
        "events": event_rows,
    }
    return {
        "mailbox_memory_result": mailbox_memory_result,
        "snapshot": {},
        "context_pack": {},
        "next_action": {},
        "hot_state_snapshot": {},
        "operator_snapshot": None,
        "v2_projection": None,
        "preview": None,
        "synthetic_intake": {},
        "business_result": {},
        "action_plan_result": {},
        "case_link_result": {},
        "case_intelligence_result": {},
        "case_record": {},
    }


def _reconcile_drive_legacy_assemble_result(
    signal: CanonicalSignal,
    *,
    case_id: str,
    case_key: str,
    payload: dict[str, Any],
    document_row: dict[str, Any],
    entity_link_dict: dict[str, Any],
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    store: Any,
    prepared: dict[str, Any],
) -> ReconcileResult:
    """Build projection decision and assemble final result."""
    snapshot = prepared["snapshot"]
    context_pack = prepared["context_pack"]
    next_action = prepared["next_action"]
    hot_state_snapshot = prepared["hot_state_snapshot"]
    operator_snapshot = prepared["operator_snapshot"]
    v2_projection = prepared["v2_projection"]
    preview = prepared["preview"]
    synthetic_intake = prepared["synthetic_intake"]
    business_result = prepared["business_result"]
    action_plan_result = prepared["action_plan_result"]
    case_link_result = prepared["case_link_result"]
    case_intelligence_result = prepared["case_intelligence_result"]
    mailbox_memory_result = prepared["mailbox_memory_result"]
    fact_rows = list(mailbox_memory_result.get("facts") or [])
    event_rows = list(mailbox_memory_result.get("events") or [])
    projection_decision = decide_projection_refresh(
        signal.signal_kind,
        source_kind="drive",
        case_id=case_id,
        has_case_state=bool(snapshot)
        or (isinstance(mailbox_memory_result.get("case_snapshot_hot_state"), dict) and bool(mailbox_memory_result.get("case_snapshot_hot_state"))),
    )
    if case_id and not dry_run:
        _stamp_case_runtime_state(store, case_id=case_id, signal=signal, projection_decision=projection_decision)
    return ReconcileResult(
        signal_id=signal.signal_id,
        source_kind=signal.source_kind,
        signal_kind=signal.signal_kind,
        processing_state="shadowed" if dry_run else "reconciled",
        case_id=case_id,
        case_key=case_key,
        entity_link=entity_link_dict,
        case_mutation_plan=CaseMutationPlan(
            case_id=case_id, case_key=case_key,
            mutation_kind=str(payload.get("linkage_status") or document_row.get("linkage_status") or "updated"),
            reasons=list(payload.get("link_reasons") or (document_row.get("metadata") or {}).get("link_reasons") or []),
        ),
        fact_updates=FactUpdates(
            facts_upserted=len(fact_rows),
            documents_upserted=1 if document_row else 0,
            events_appended=len(event_rows),
        ),
        snapshot_refresh_decision=SnapshotRefreshDecision(should_refresh=bool(case_id), mode="incremental_refresh", reason="drive_signal_refresh"),
        projection_refresh_decision=projection_decision,
        mailbox_memory_result=mailbox_memory_result,
        rebuild_result={
            "case_id": case_id, "snapshot": snapshot, "hot_state_snapshot": hot_state_snapshot,
            "context_pack": context_pack, "next_action": next_action,
            "rebuild_mode": "incremental_refresh",
            "source_refs_used": list((context_pack or {}).get("source_refs") or []),
            "update_reasons": ["drive_signal_reconciled"],
        },
        preview=preview,
        v2_projection=v2_projection,
        stage_outputs={
            "intake_result_final": synthetic_intake if case_id else {},
            "case_link_result": case_link_result if case_id else {},
            "entity_link_result": entity_link_dict,
            "business_reasoning_result": business_result,
            "action_plan_result": action_plan_result,
            "case_intelligence_result": case_intelligence_result,
            "mailbox_memory_result": mailbox_memory_result,
            "operator_projection_snapshot": operator_snapshot,
        },
    )


# ---- wrapper calling split helpers ----
def _reconcile_drive_signal_legacy_inline(
    signal: CanonicalSignal,
    *,
    case_id: str,
    case_key: str,
    document_row: dict[str, Any],
    fact_rows: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
    payload: dict[str, Any],
    entity_link_dict: dict[str, Any],
    runtime_context: SignalRuntimeContext,
    dry_run: bool,
    store: Any,
) -> ReconcileResult:
    """Legacy fallback for Drive signals (dispatches to split helpers)."""
    if case_id:
        prepared = _reconcile_drive_legacy_prepare_case_state(
            signal, case_id=case_id, case_key=case_key,
            document_row=document_row, fact_rows=fact_rows, event_rows=event_rows,
            payload=payload, entity_link_dict=entity_link_dict,
            runtime_context=runtime_context, dry_run=dry_run, store=store,
        )
    else:
        prepared = _reconcile_drive_legacy_prepare_no_case(
            signal, case_id=case_id, case_key=case_key,
            document_row=document_row, fact_rows=fact_rows, event_rows=event_rows,
            payload=payload, entity_link_dict=entity_link_dict,
            runtime_context=runtime_context, dry_run=dry_run, store=store,
        )
    return _reconcile_drive_legacy_assemble_result(
        signal, case_id=case_id, case_key=case_key,
        payload=payload, document_row=document_row,
        entity_link_dict=entity_link_dict,
        runtime_context=runtime_context, dry_run=dry_run,
        store=store, prepared=prepared,
    )

def _collect_drive_enrichment(runtime_context: SignalRuntimeContext, case_id: str) -> dict[str, Any]:
    from mailbox_memory_runtime import collect_drive_case_enrichment

    return collect_drive_case_enrichment(
        store=runtime_context.resolved_store,
        case_id=case_id,
        graph_store=runtime_context.graph_store,
    )


def _build_drive_intake_result(
    signal: CanonicalSignal,
    *,
    document_row: dict[str, Any],
    case_id: str,
    case_key: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    document_kind = str(document_row.get("document_kind") or "")
    business_area = "finance" if "invoice" in document_kind else "operations"
    action = "update_case_state" if case_id else "review"
    return {
        "source": {
            "channel": "drive",
            "mailbox": "google_drive",
            "observed_at": signal.observed_at,
        },
        "message": {
            "message_id": str(document_row.get("drive_item_id") or signal.signal_id),
            "date": signal.observed_at,
            "sender": "Google Drive",
            "to": [],
            "subject": str(document_row.get("file_name") or signal.signal_summary_pl),
            "has_attachments": False,
        },
        "thread": {
            "thread_id": case_key or case_id or str(document_row.get("drive_item_id") or ""),
            "thread_position": "signal_update",
            "is_reply_or_forward": False,
            "thread_summary": signal.signal_summary_pl,
            "linked_case_candidates": [],
        },
        "business_area": business_area,
        "primary_signal": {
            "code": signal.signal_kind,
            "name": signal.signal_kind,
            "description": signal.signal_summary_pl,
            "business_significance": "operational",
        },
        "secondary_signals": [],
        "case_assessment": {
            "case_family": str(document_row.get("lane") or "unknown"),
            "is_new_case": not bool(case_id),
            "state_change": {"detected": True},
            "state_detected": str(snapshot.get("status") or "open"),
        },
        "decision": {
            "action": action,
            "action_rationale": signal.signal_summary_pl,
        },
        "priority": "medium",
        "confidence": {
            "signal_confidence": 0.82,
            "case_link_confidence": float(document_row.get("link_confidence") or 0.0),
            "decision_confidence": 0.7,
            "extraction_confidence": float(document_row.get("extraction_confidence") or 0.0),
        },
        "review": {
            "required": bool(signal.signal_kind == "drive_conflict_detected"),
            "flags": ["multiple_competing_signals"] if signal.signal_kind == "drive_conflict_detected" else [],
        },
        "reason": signal.signal_summary_pl,
    }


def _build_drive_business_result(signal: CanonicalSignal, *, document_row: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "business_area": "finance" if "invoice" in str(document_row.get("document_kind") or "") else "operations",
        "business_interpretation": signal.signal_summary_pl,
        "business_summary_short": str(document_row.get("summary_text") or signal.signal_summary_pl),
        "confidence": {
            "action_confidence": 0.7,
            "business_confidence": 0.75,
        },
        "customer_state_guess": "active_case" if snapshot else "unclear",
        "human_review_bias": "medium",
        "missing_information": list(snapshot.get("open_questions") or []),
        "operator_note": signal.signal_summary_pl,
        "recommended_action_reason": signal.signal_summary_pl,
        "recommended_next_action": "review" if snapshot.get("open_questions") else "update_case",
        "reply_recommended": False,
        "risks": ["Drive signal requires operator verification."] if signal.signal_kind == "drive_conflict_detected" else [],
        "safety_notes": ["No execution plane side effects."],
        "urgency": "high" if signal.signal_kind == "drive_conflict_detected" else "medium",
    }


def _build_drive_action_plan(signal: CanonicalSignal, *, snapshot: dict[str, Any]) -> dict[str, Any]:
    needs_review = bool(snapshot.get("open_questions")) or signal.signal_kind == "drive_conflict_detected"
    return {
        "primary_action": "review" if needs_review else "update_case",
        "why_this_action": signal.signal_summary_pl,
        "safe_for_live_push": False,
        "safe_for_operator_projection": True,
    }


def _default_lane_stage_plan() -> dict[str, bool]:
    return {
        "run_case_linking": True,
        "run_business_reasoning": True,
        "run_reply_drafter": True,
    }


def _stamp_case_runtime_state(
    store: Any,
    *,
    case_id: str,
    signal: CanonicalSignal,
    projection_decision: ProjectionRefreshDecision,
) -> None:
    if store is None or not case_id:
        return
    mutate = getattr(type(store), "mutate_case", None)
    if callable(mutate):
        # CONC-01: route both existing-case updates AND first-time case
        # materialization through the same atomic contract (advisory lock +
        # SELECT ... FOR UPDATE + single transaction). Probing via an
        # unlocked fetch_case() first (as this used to do) reopens the exact
        # TOCTOU window mutate_case exists to close -- two concurrent
        # callers could both observe "not found" and both fall through to
        # an unlocked upsert_case, silently losing one contribution.
        # create_if_missing=True makes mutate_case hand the mutator an
        # explicit empty-row state under the same lock instead.
        def _mutate(row: dict[str, Any]) -> dict[str, Any]:
            case_row = dict(row)
            source_kinds = list(case_row.get("last_source_kinds_seen") or [])
            if signal.source_kind not in source_kinds:
                source_kinds.append(signal.source_kind)
            now_iso = datetime.now().astimezone().isoformat()
            case_row["latest_signal_id"] = signal.signal_id
            case_row["latest_signal_at"] = signal.observed_at
            case_row["last_rebuild_at"] = now_iso
            case_row["last_source_kinds_seen"] = source_kinds
            case_row["updated_at"] = now_iso
            if projection_decision.should_refresh:
                case_row["last_projection_refresh_at"] = now_iso
            enriched, routing = enrich_case_row_before_upsert(
                case_row,
                source_kind=str(signal.source_kind or "gmail"),
            )
            return enriched if routing.upsert_allowed else dict(row)

        store.mutate_case(case_id, _mutate, create_if_missing=True)
        return
    case_row = store.fetch_case(case_id) or {"case_id": case_id, "metadata": {}}
    source_kinds = list(case_row.get("last_source_kinds_seen") or [])
    if signal.source_kind not in source_kinds:
        source_kinds.append(signal.source_kind)
    now_iso = datetime.now().astimezone().isoformat()
    case_row["latest_signal_id"] = signal.signal_id
    case_row["latest_signal_at"] = signal.observed_at
    case_row["last_rebuild_at"] = now_iso
    case_row["last_source_kinds_seen"] = source_kinds
    case_row["updated_at"] = now_iso
    if projection_decision.should_refresh:
        case_row["last_projection_refresh_at"] = now_iso
    enriched, routing = enrich_case_row_before_upsert(
        case_row,
        source_kind=str(signal.source_kind or "gmail"),
    )
    if routing.upsert_allowed:
        store.upsert_case(enriched)


__all__ = [
    "CaseMutationPlan",
    "FactUpdates",
    "ReconcileResult",
    "SignalRuntimeContext",
    "SnapshotRefreshDecision",
    "reconcile_signal",
    "reconcile_signal_batch",
    "replay_signal",
]
