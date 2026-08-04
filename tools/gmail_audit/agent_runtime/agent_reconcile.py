"""PR-D: reconcile via AgentRun (prep/primary) — no shared downstream."""

from __future__ import annotations

import uuid
from typing import Any

from agent_runtime.database_url import resolve_mailbox_memory_database_url
from agent_runtime.decision_divergence import build_decision_comparison_inputs
from agent_runtime.engagement_resolver import (
    EngagementResolution,
    extract_case_id_from_signal,
    resolve_engagement_for_case,
)
from agent_runtime.feed_projection import (
    build_canonical_operator_snapshot,
    build_operator_snapshot_from_engagement,
    build_v2_projection_from_engagement,
    projection_canonical_enabled,
)
from agent_runtime.jobs import build_agent_job_store
from agent_runtime.run import AgentRunResult, execute_agent_run
from agent_runtime.settings import AgentRuntimeSettings, load_agent_runtime_settings
from agent_runtime.signal_engagement import patch_signal_engagement
from case_intelligence_degradation import maybe_record_case_intelligence_degradation
from log_config import get_trace_id
from agent_runtime.store import (
    AgentConcurrencyError,
    InMemoryOperatorEngagementStore,
    OperatorEngagementStore,
    PostgresOperatorEngagementStore,
)
from agent_runtime.validate import AgentRuntimeConfigError, validate_agent_runtime_settings
from correlation_registry.service import CorrelationRegistryService, build_correlation_registry_service
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2
from log_config import get_logger
from preclassifier import contains_noise_keyword
from signal_contract import CanonicalSignal

logger = get_logger(__name__)


class AgentReconcileFailure(RuntimeError):
    """Typed reconcile failure that must stay visible end-to-end."""

    def __init__(
        self,
        *,
        failure_code: str,
        message: str,
        retryable: bool,
        severity: str = "attention_required",
        exception_class: str = "RuntimeError",
    ) -> None:
        super().__init__(message)
        self.failure_code = str(failure_code or "agent_execution_failed")
        self.retryable = bool(retryable)
        self.severity = str(severity or "attention_required")
        self.exception_class = str(exception_class or "RuntimeError")


def _raise_agent_reconcile_failure(
    *,
    failure_code: str,
    exc: Exception,
    retryable: bool,
    severity: str = "attention_required",
) -> None:
    raise AgentReconcileFailure(
        failure_code=failure_code,
        message=str(exc),
        retryable=retryable,
        severity=severity,
        exception_class=type(exc).__name__,
    ) from exc


def _failure_gap_message(failure_code: str) -> str:
    messages = {
        "case_intelligence_failed": "Case Intelligence nie zakonczyl przetwarzania sprawy. Wymagana uwaga operatora.",
        "agent_concurrency_conflict": "Engagement zostal zmieniony rownolegle. Odswiez stan i powtorz probe operatora.",
        "agent_execution_failed": "Agent Runtime nie zakonczyl wykonania sprawy. Wymagana uwaga operatora.",
    }
    return messages.get(str(failure_code or ""), "Agent Runtime zakonczyl sie bledem. Wymagana uwaga operatora.")


def _project_reconcile_failure_best_effort(
    operator_store: OperatorEngagementStore,
    snapshot: EngagementSnapshotV2,
    *,
    failure_code: str,
) -> EngagementSnapshotV2:
    from feed_visibility import mark_execution_attention
    from llm_contracts.engagement_snapshot_v2 import FeedVisibility, GapItem, OperationalStatus

    filtered_gaps = [gap for gap in (snapshot.gaps or []) if str(getattr(gap, "field", "") or "") != "agent_runtime_failure"]
    filtered_gaps.append(
        GapItem(
            field="agent_runtime_failure",
            severity="blocking",
            ask_pl=_failure_gap_message(failure_code),
        )
    )
    patched = snapshot.model_copy(
        update={
            "feed_visibility": FeedVisibility(
                **mark_execution_attention(snapshot.feed_visibility, reason=failure_code)
            ),
            "operational_status": OperationalStatus(
                code="pending_operator",
                steps_remaining=max(0, int(getattr(snapshot.operational_status, "steps_remaining", 0) or 0)),
                blocking=True,
            ),
            "gaps": filtered_gaps[:8],
        }
    )
    new_version = operator_store.save_snapshot(patched, expected_version=snapshot.version)
    return patched.model_copy(update={"version": new_version})


def build_case_understanding_projection(
    case_intelligence_result: dict[str, Any] | None,
    *,
    message_id: str,
) -> dict[str, Any] | None:
    """Compact, projection-safe read of understanding_output for the CURRENT
    turn only (A1). Returns None (honest absence, never a guess) unless the
    understanding is genuinely correlated to the message being processed this
    turn and carries a real essence — never projects Understanding computed
    for a different/older signal as if it were current.
    """
    intel = case_intelligence_result if isinstance(case_intelligence_result, dict) else {}
    uo = intel.get("understanding_output")
    if not isinstance(uo, dict) or not uo:
        return None
    source_signal_id = str(uo.get("source_signal_id") or "").strip()
    mid = str(message_id or "").strip()
    if not source_signal_id or not mid or source_signal_id != mid:
        return None
    oe = uo.get("operator_explanation") if isinstance(uo.get("operator_explanation"), dict) else {}
    essence = str(oe.get("essence_pl") or uo.get("situation_summary_pl") or "").strip()
    if not essence:
        return None
    delta = uo.get("thread_delta") if isinstance(uo.get("thread_delta"), dict) else {}
    nba = uo.get("next_best_action_recommendation") if isinstance(uo.get("next_best_action_recommendation"), dict) else {}
    risks: list[dict[str, str]] = []
    for item in (uo.get("risks") or [])[:5]:
        if isinstance(item, dict) and str(item.get("summary_pl") or "").strip():
            risks.append(
                {
                    "risk_type": str(item.get("risk_type") or "")[:80],
                    "severity": str(item.get("severity") or "medium")[:40],
                    "summary_pl": str(item.get("summary_pl") or "")[:320],
                }
            )
    # Roadmap 1.3: sharpen vague NBA for planner (+ optional tool-class hint).
    from agent_runtime.recommended_next_step_quality import (
        planner_action_hint,
        sharpen_recommended_next_step,
    )

    nested = uo.get("case_understanding") if isinstance(uo.get("case_understanding"), dict) else {}
    ss = uo.get("situation_summary") if isinstance(uo.get("situation_summary"), dict) else {}
    case_kind_uo = str(
        nested.get("case_family")
        or ss.get("case_family")
        or uo.get("case_family")
        or ""
    )
    business_area_uo = str(ss.get("business_area") or nested.get("business_area") or "")
    what_changed = str(delta.get("operator_visible_delta_summary") or "")
    missing_fields = [str(x)[:240] for x in (uo.get("missing_critical_fields") or [])[:6]]
    # Prefer already-sharpened title from Understanding source; re-sharpen as defense.
    sharpened = sharpen_recommended_next_step(
        title_pl=str(nba.get("title_pl") or ""),
        reason_pl=str(nba.get("reason_pl") or ""),
        action_type=str(nba.get("action_type") or nba.get("recommended_action") or ""),
        case_kind=case_kind_uo,
        business_area=business_area_uo,
        case_family=case_kind_uo,
        missing_critical_fields=missing_fields,
        essence_pl=essence,
        what_changed_pl=what_changed,
    )
    quality = nba.get("quality") if isinstance(nba.get("quality"), dict) else {}
    hint = str(quality.get("planner_action_hint") or "").strip() or planner_action_hint(
        sharpened_pl=sharpened,
        case_kind=case_kind_uo,
        missing_critical_fields=missing_fields,
    )
    return {
        "source_signal_id": source_signal_id,
        "generated_at": str(uo.get("created_at") or ""),
        "essence_pl": essence[:700],
        "what_changed_pl": what_changed[:400],
        "why_pl": str(oe.get("why_pl") or "")[:600],
        "missing_critical_fields": missing_fields,
        "risks": risks,
        "recommended_next_step_pl": sharpened[:400],
        "planner_action_hint": hint[:80],
    }


def build_case_understanding_provenance_projection(
    case_intelligence_result: dict[str, Any] | None,
    *,
    message_id: str,
) -> dict[str, Any] | None:
    """SLICE-3A: the provenance envelope for the CURRENT turn's Understanding.

    Same freshness rule as `build_case_understanding_projection`, applied independently: the
    provenance is only returned when Brain 1 recorded it for THIS exact signal. The two are then
    set and cleared together by `graph._ground_current_signal`, so a snapshot can never carry a
    provenance describing one signal and an Understanding describing another.

    Returns None (honest absence) whenever Brain 1 did not record provenance -- for example when
    the Understanding stage was disabled entirely. No status is fabricated to fill the gap.
    """
    intel = case_intelligence_result if isinstance(case_intelligence_result, dict) else {}
    meta = intel.get("execution_metadata")
    if not isinstance(meta, dict):
        return None
    provenance = meta.get("case_understanding_provenance")
    if not isinstance(provenance, dict) or not provenance:
        return None
    source_signal_id = str(provenance.get("source_signal_id") or "").strip()
    mid = str(message_id or "").strip()
    if not source_signal_id or not mid or source_signal_id != mid:
        return None
    return provenance


def build_policy_action_envelope_handoff(
    *,
    store: Any,
    case_intelligence_result: dict[str, Any] | None,
    case_id: str,
    source_signal_id: str,
    source_message_id: str,
) -> tuple[dict[str, int | bool], dict[str, Any]]:
    """Persist canonical policy/APv2 records, then project the Brain 2 envelope."""
    from agent_runtime.policy_action_spine import (
        persist_policy_action_spine,
        project_policy_action_envelope,
    )

    persisted = persist_policy_action_spine(
        store,
        case_intelligence_result=case_intelligence_result,
        case_id=case_id,
        source_signal_id=source_signal_id,
        source_message_id=source_message_id,
    )
    envelope = project_policy_action_envelope(
        store,
        case_id=case_id,
        source_signal_id=source_signal_id,
        source_message_id=source_message_id,
    )
    return persisted, envelope.model_dump(mode="python")


def _current_trace_id(runtime_context: Any) -> str:
    run_state = getattr(runtime_context, "run_state", None)
    if isinstance(run_state, dict):
        trace_id = str(run_state.get("trace_id") or "").strip()
        if trace_id:
            return trace_id
    trace_id = str(get_trace_id() or "").strip()
    if trace_id:
        return trace_id
    return f"trace_{uuid.uuid4().hex[:16]}"


def _canonical_staging_payload(signal: CanonicalSignal, intake: dict[str, Any]) -> dict[str, Any]:
    return {**dict(intake or {}), "signal_id": signal.signal_id}


def _case_os_intelligence_downstream_active(settings: Any) -> bool:
    """True when Case OS intelligence flags require shared downstream on mailbox SoT."""
    return bool(
        getattr(settings, "case_intelligence_vnext_enabled", False)
        or getattr(settings, "understanding_output_enabled", False)
        or getattr(settings, "decision_pipeline_enabled", False)
        or getattr(settings, "action_proposal_v2_enabled", False)
    )


def _run_mailbox_intelligence_downstream(
    signal: CanonicalSignal,
    *,
    runtime_context: Any,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
    intake_output: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Run mailbox SoT + intelligence pipeline before agent execute (full Case OS profile)."""
    from event_memory import EventLog
    from gmail_intake import build_context_bundle, hydrate_intelligence_seam_config
    from intake_shared_downstream import SharedDownstreamOptions, run_shared_downstream_stages

    snapshot = dict(signal.payload.get("snapshot") or {})
    preclassification_result = dict(signal.payload.get("preclassification_result") or {"lane": "intake_llm"})
    lane_stage_plan = dict(signal.payload.get("lane_stage_plan") or {"run_case_linking": True})
    context_bundle = dict(signal.payload.get("context_bundle") or build_context_bundle(snapshot))
    stage_config = {
        "settings": runtime_context.settings,
        "model": getattr(runtime_context, "model", None) or getattr(runtime_context.settings, "groq_model", ""),
        "verbose": getattr(runtime_context, "verbose", False),
        "snapshot": snapshot,
        "preclassification_result": preclassification_result,
        "lane_stage_plan": lane_stage_plan,
        "event_log": EventLog(),
        "entity_link_result": entity_link_dict,
    }
    hydrate_intelligence_seam_config(runtime_context.run_state or {}, snapshot, stage_config)
    if dry_run:
        stage_config["mailbox_memory_runtime"] = None
        stage_config["daszek_client"] = None
    downstream = run_shared_downstream_stages(
        snapshot=snapshot,
        intake_result=intake_output,
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
    warnings = list(downstream.warnings)
    warnings.append("agent_runtime_with_mailbox_intelligence_downstream")
    return downstream.case_intelligence_result, downstream.mailbox_memory_result, warnings


def agent_runtime_reconcile_active(settings: AgentRuntimeSettings | None = None) -> bool:
    settings = settings or load_agent_runtime_settings()
    mode = str(settings.mode or "").strip().lower()
    return bool(settings.enabled) and mode in {"prep", "primary"}


def legacy_downstream_reconcile_active(settings: AgentRuntimeSettings | None = None) -> bool:
    """True when reconcile must use run_shared_downstream_stages (legacy path)."""
    settings = settings or load_agent_runtime_settings()
    if settings.enabled and str(settings.mode or "").strip().lower() == "legacy":
        raise AgentRuntimeConfigError(
            "agent runtime enabled with AGENT_RUNTIME_MODE=legacy is inconsistent; "
            "AGENT_RUNTIME_MODE is canonical — use legacy for rollback or prep|primary to run"
        )
    return not agent_runtime_reconcile_active(settings)


def assert_reconcile_mode_consistent(settings: AgentRuntimeSettings | None = None) -> None:
    settings = settings or load_agent_runtime_settings()
    if not settings.enabled:
        return
    issues = validate_agent_runtime_settings(settings)
    if issues:
        raise AgentRuntimeConfigError("; ".join(issues))


def _resolve_mailbox_memory_database_url(settings: Any) -> tuple[str, str]:
    return resolve_mailbox_memory_database_url(settings)


def _resolve_operator_engagement_database_url(settings: Any) -> tuple[str, str]:
    return resolve_mailbox_memory_database_url(settings)


def build_operator_engagement_store(
    settings: Any,
    *,
    allow_in_memory: bool = False,
) -> OperatorEngagementStore:
    url, url_source = _resolve_operator_engagement_database_url(settings)
    if url:
        return PostgresOperatorEngagementStore(url)
    reason = "settings_not_loaded_or_no_db_url"
    if not allow_in_memory:
        raise AgentRuntimeConfigError(
            "Postgres operator engagement store required; "
            "set MAILBOX_MEMORY_DATABASE_URL (call load_settings() so tools/gmail_audit/.env is loaded) "
            "or pass allow_in_memory=True for isolated dev/test only."
        )
    logger.warning(
        "OPERATOR_ENGAGEMENT_STORE_FALLBACK_TO_MEMORY",
        extra={"x": {"reason": reason, "url_source": url_source}},
    )
    return InMemoryOperatorEngagementStore()


def build_registry_for_reconcile(
    settings: Any,
    *,
    allow_in_memory: bool = False,
) -> CorrelationRegistryService | None:
    url, url_source = _resolve_mailbox_memory_database_url(settings)
    if url:
        registry = build_correlation_registry_service(url, in_memory=False)
        if registry is not None:
            registry.bootstrap()
        return registry
    reason = "settings_not_loaded_or_no_db_url"
    if not allow_in_memory:
        raise AgentRuntimeConfigError(
            "Postgres correlation registry required; "
            "set MAILBOX_MEMORY_DATABASE_URL (call load_settings() so tools/gmail_audit/.env is loaded) "
            "or pass allow_in_memory=True for isolated dev/test only."
        )
    logger.warning(
        "CORRELATION_REGISTRY_STORE_FALLBACK_TO_MEMORY",
        extra={"x": {"reason": reason, "url_source": url_source}},
    )
    registry = build_correlation_registry_service("", in_memory=True)
    if registry is not None:
        registry.bootstrap()
    return registry


def _message_id_from_signal(signal: CanonicalSignal, intake_output: dict[str, Any] | None = None) -> str:
    intake = dict(intake_output or signal.payload.get("intake_result_final") or signal.payload.get("intake_output") or {})
    message = dict(intake.get("message") or {})
    mid = str(message.get("message_id") or signal.source_ref.get("message_id") or "").strip()
    if mid:
        return mid
    snapshot = dict(signal.payload.get("snapshot") or {})
    return str((snapshot.get("source_message") or {}).get("message_id") or "").strip()


def resolve_case_id_for_agent(
    signal: CanonicalSignal,
    entity_link_dict: dict[str, Any],
    *,
    mailbox_store: Any | None = None,
    intake_output: dict[str, Any] | None = None,
) -> str:
    """case_id from entity link (VERIFIED), payload, signal hints, or mailbox message row."""
    for source in (entity_link_dict, dict(signal.payload or {})):
        case_id = str(source.get("case_id") or source.get("entity_link_case_id") or "").strip()
        if case_id:
            return case_id
        proposal = source.get("case_proposal")
        if isinstance(proposal, dict):
            proposed = str(proposal.get("case_id") or "").strip()
            if proposed:
                return proposed
    extracted = extract_case_id_from_signal(dict(signal.payload or {}))
    if extracted:
        return extracted
    if str(entity_link_dict.get("link_status") or "") == "VERIFIED":
        verified = str(entity_link_dict.get("case_id") or entity_link_dict.get("target_id") or "").strip()
        if verified:
            return verified
    if mailbox_store is not None:
        message_id = _message_id_from_signal(signal, intake_output)
        if message_id:
            fetch = getattr(mailbox_store, "fetch_case_by_message_id", None)
            if callable(fetch):
                row = fetch(message_id)
                if isinstance(row, dict):
                    case_id = str(row.get("case_id") or "").strip()
                    if case_id:
                        return case_id
    return ""


def _customer_email_from_signal(signal: CanonicalSignal, intake_output: dict[str, Any]) -> str:
    payload = dict(signal.payload or {})
    for key in ("customer_email", "from_email", "sender_email"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    message = dict(intake_output.get("message") or {})
    sender = str(message.get("sender") or message.get("from") or "").strip()
    if "<" in sender and ">" in sender:
        return sender.split("<", 1)[1].split(">", 1)[0].strip()
    if "@" in sender:
        return sender
    snapshot = dict(payload.get("snapshot") or {})
    source_message = dict(snapshot.get("source_message") or {})
    return str(source_message.get("from_email") or source_message.get("customer_email") or "").strip()


def _intake_case_family(intake_output: dict[str, Any]) -> str:
    decision = dict(intake_output.get("decision") or {})
    direct = str(intake_output.get("case_family") or decision.get("case_family") or "").strip()
    if direct and direct.lower() != "unknown":
        return direct
    assessment = intake_output.get("case_assessment")
    if isinstance(assessment, dict):
        family = str(assessment.get("case_family") or "").strip()
        if family and family.lower() != "unknown":
            return family
    return direct


def signal_payload_for_agent(signal: CanonicalSignal, intake_output: dict[str, Any]) -> dict[str, Any]:
    payload = dict(signal.payload or {})
    snapshot = dict(payload.get("snapshot") or {})
    source_message = dict(snapshot.get("source_message") or {})
    message = dict(intake_output.get("message") or {})
    decision = dict(intake_output.get("decision") or {})
    preclass = dict(payload.get("preclassification_result") or {})
    document_row = dict(payload.get("document_row") or {})
    agent_payload = {
        "signal_id": signal.signal_id,
        "source_kind": str(signal.source_kind or ""),
        "case_id": str(signal.payload.get("case_id") or ""),
        "subject": str(message.get("subject") or source_message.get("subject") or signal.signal_summary_pl or ""),
        "snippet": str(message.get("snippet") or source_message.get("snippet") or ""),
        "body_text": str(message.get("body_text") or source_message.get("body_text") or ""),
        "message_id": str(message.get("message_id") or source_message.get("message_id") or ""),
        "customer_email": _customer_email_from_signal(signal, intake_output),
        # Klasyfikacja case_kind (F1): szeroki podział z intake — patrz handlers._classify_case_kind.
        "business_area": str(
            intake_output.get("business_area")
            or decision.get("business_area")
            or preclass.get("business_area")
            or ""
        ),
        "case_family": _intake_case_family(intake_output),
        "decision_action": str(decision.get("action") or intake_output.get("decision_action") or ""),
    }
    if str(signal.source_kind or "").strip().lower() == "drive":
        file_id = str(document_row.get("drive_item_id") or (signal.source_ref or {}).get("file_id") or "")
        agent_payload.update(
            {
                "channel": "drive",
                "drive_file_id": file_id,
                "drive_file_name": str(document_row.get("file_name") or signal.signal_summary_pl or ""),
                "document_kind": str(document_row.get("document_kind") or ""),
                "document_lane": str(document_row.get("lane") or ""),
                "signal_kind": str(signal.signal_kind or ""),
            }
        )
    return agent_payload


def _feed_visibility_for_signal(signal: CanonicalSignal) -> Any | None:
    """SLICE-2B: decide MAIN-feed membership routing once, at snapshot creation.

    Pure delegation to `feed_visibility.classify_signal_for_feed`; the executive-state override is
    applied later at read time. Returns None (legacy main_feed fallback) if classification data is
    unavailable, so a gap can only over-show, never silently hide.
    """
    try:
        from feed_visibility import classify_signal_for_feed
        from llm_contracts.engagement_snapshot_v2 import FeedVisibility

        payload = dict(getattr(signal, "payload", None) or {})
        decision = classify_signal_for_feed(
            preclassification_result=payload.get("preclassification_result"),
            triage_result=payload.get("triage_result"),
        )
        return FeedVisibility(**decision)
    except Exception:  # noqa: BLE001 - visibility metadata must never break signal processing
        logger.warning("FEED_VISIBILITY_CLASSIFY_FAILED", extra={"x": {
            "signal_id": str(getattr(signal, "signal_id", "") or ""),
        }})
        return None


def _refresh_feed_visibility(
    operator_store: OperatorEngagementStore,
    snapshot: EngagementSnapshotV2,
    *,
    signal: CanonicalSignal,
    dry_run: bool,
) -> EngagementSnapshotV2:
    """SLICE-2B1: re-evaluate feed membership for an EXISTING snapshot under a new signal.

    An engagement outlives the signal that created it. Without this, the first message's routing
    verdict was permanent, so a newsletter arriving before a real enquiry on the same engagement
    would keep the case out of the operator's feed indefinitely.

    Promotion only (`merge_feed_visibility`); a later low-value signal can never demote. Failure
    is non-fatal and never blocks signal processing.
    """
    try:
        from feed_visibility import merge_feed_visibility
        from llm_contracts.engagement_snapshot_v2 import FeedVisibility

        incoming = _feed_visibility_for_signal(signal)
        if incoming is None:
            return snapshot
        merged = merge_feed_visibility(
            snapshot.feed_visibility,
            incoming.model_dump(mode="python"),
        )
        if merged is None:
            return snapshot
        promoted = snapshot.model_copy(update={"feed_visibility": FeedVisibility(**merged)})
        logger.info("FEED_VISIBILITY_PROMOTED", extra={"x": {
            "engagement_id": str(snapshot.engagement_id or ""),
            "from": str(getattr(snapshot.feed_visibility, "mode", "") or ""),
            "to": str(merged.get("mode") or ""),
            "signal_id": str(getattr(signal, "signal_id", "") or ""),
        }})
        if dry_run:
            return promoted
        new_version = operator_store.save_snapshot(promoted, expected_version=snapshot.version)
        return promoted.model_copy(update={"version": new_version})
    except Exception:  # noqa: BLE001 - visibility metadata must never break signal processing
        logger.warning("FEED_VISIBILITY_REFRESH_FAILED", extra={"x": {
            "engagement_id": str(getattr(snapshot, "engagement_id", "") or ""),
            "signal_id": str(getattr(signal, "signal_id", "") or ""),
        }})
        return snapshot


def ensure_engagement_snapshot(
    operator_store: OperatorEngagementStore,
    *,
    signal: CanonicalSignal,
    runtime_context: Any,
    case_id: str,
    engagement_id: str,
    intake_output: dict[str, Any],
    dry_run: bool,
) -> EngagementSnapshotV2:
    existing = operator_store.load_snapshot(engagement_id)
    if existing is not None:
        return _refresh_feed_visibility(
            operator_store, existing, signal=signal, dry_run=dry_run
        )
    agent_signal = signal_payload_for_agent(signal, intake_output)
    if dry_run:
        from agent_runtime.store import build_initial_snapshot

        return build_initial_snapshot(
            case_id=case_id,
            engagement_id=engagement_id,
            signal_id=signal.signal_id,
            trace_id=_current_trace_id(runtime_context),
            feed_visibility=_feed_visibility_for_signal(signal),
        )
    from agent_runtime.store import build_snapshot_from_signal

    built = build_snapshot_from_signal(
        signal=agent_signal,
        case_id=case_id,
        engagement_id=engagement_id,
        signal_id=signal.signal_id,
        trace_id=_current_trace_id(runtime_context),
        feed_visibility=_feed_visibility_for_signal(signal),
    )
    return operator_store.insert_snapshot(built)


def run_agent_reconcile(
    signal: CanonicalSignal,
    *,
    runtime_context: Any,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
    intake_output: dict[str, Any] | None = None,
) -> tuple[EngagementSnapshotV2, AgentRunResult | None, EngagementResolution, list[str]]:
    """
    Resolve engagement, run agent (unless dry_run), patch signal link, audit job.
    Returns (final_snapshot, run_result|None, resolution, warnings).
    """
    warnings: list[str] = []
    settings = load_agent_runtime_settings()
    assert_reconcile_mode_consistent(settings)
    intake = dict(intake_output or signal.payload.get("intake_result_final") or signal.payload.get("intake_output") or {})
    case_id = resolve_case_id_for_agent(
        signal,
        entity_link_dict,
        mailbox_store=getattr(runtime_context, "resolved_store", None),
        intake_output=intake,
    )
    if not case_id:
        raise ValueError("agent reconcile requires case_id on signal or entity link")

    registry = build_registry_for_reconcile(runtime_context.settings)
    if registry is None:
        raise RuntimeError("correlation registry required for agent reconcile")

    resolution = resolve_engagement_for_case(
        case_id,
        registry=registry,
        customer_email=_customer_email_from_signal(signal, intake),
        message_id=str(intake.get("message", {}).get("message_id") or signal.source_ref.get("message_id") or ""),
    )
    operator_store = build_operator_engagement_store(runtime_context.settings)
    snapshot = ensure_engagement_snapshot(
        operator_store,
        signal=signal,
        runtime_context=runtime_context,
        case_id=case_id,
        engagement_id=resolution.engagement_id,
        intake_output=intake,
        dry_run=dry_run,
    )

    case_intelligence_result: dict[str, Any] = {}
    mailbox_memory_result: dict[str, Any] = {}
    if _case_os_intelligence_downstream_active(runtime_context.settings):
        try:
            case_intelligence_result, mailbox_memory_result, intel_warnings = _run_mailbox_intelligence_downstream(
                signal,
                runtime_context=runtime_context,
                dry_run=dry_run,
                entity_link_dict=entity_link_dict,
                intake_output=intake,
            )
            warnings.extend(intel_warnings)
            resolved_case = str(mailbox_memory_result.get("case_id") or case_id).strip()
            if resolved_case:
                case_id = resolved_case
            # DQ-18: same durable degradation record as the signal_reconciler.py
            # live path, keyed by the same case row, so both entrypoints converge
            # on one degradation state per case rather than two.
            if not dry_run and case_id:
                warnings.extend(
                    maybe_record_case_intelligence_degradation(
                        runtime_context.resolved_store,
                        case_id,
                        case_intelligence_result,
                        signal_id=str(signal.signal_id or ""),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            try:
                snapshot = _project_reconcile_failure_best_effort(
                    operator_store,
                    snapshot,
                    failure_code="case_intelligence_failed",
                )
            except Exception:  # noqa: BLE001 - visibility projection must never hide the root failure
                logger.warning("RECONCILE_FAILURE_PROJECTION_FAILED", extra={"x": {
                    "engagement_id": str(resolution.engagement_id or ""),
                    "failure_code": "case_intelligence_failed",
                }})
            _raise_agent_reconcile_failure(
                failure_code="case_intelligence_failed",
                exc=exc,
                retryable=True,
            )

    run_result: AgentRunResult | None = None
    if dry_run:
        warnings.append("agent_dry_run_skipped_execute")
    else:
        mailbox_store = runtime_context.resolved_store
        try:
            agent_signal = signal_payload_for_agent(signal, intake)
            try:
                _, policy_action_envelope = build_policy_action_envelope_handoff(
                    store=mailbox_store,
                    case_intelligence_result=case_intelligence_result,
                    case_id=case_id,
                    source_signal_id=str(signal.signal_id or ""),
                    source_message_id=str(agent_signal.get("message_id") or ""),
                )
            except Exception as exc:  # detection-only handoff cannot suppress Brain 2
                from llm_contracts.engagement_snapshot_v2 import PolicyActionEnvelopeV1

                warnings.append(
                    f"policy_action_spine_handoff_failed:{type(exc).__name__}:{exc}"
                )
                policy_action_envelope = PolicyActionEnvelopeV1(
                    freshness="unavailable",
                    reason_codes=["canonical_policy_action_projection_failed"],
                ).model_dump(mode="python")
            agent_signal["policy_action_envelope"] = policy_action_envelope
            agent_signal["understanding_brief_pl"] = str(
                (case_intelligence_result.get("operator_brief") or {}).get("brief_pl") or ""
            )
            agent_signal["case_understanding_projection"] = build_case_understanding_projection(
                case_intelligence_result,
                message_id=str(agent_signal.get("message_id") or ""),
            )
            agent_signal["case_understanding_provenance"] = build_case_understanding_provenance_projection(
                case_intelligence_result,
                message_id=str(agent_signal.get("message_id") or ""),
            )
            agent_signal["decision_comparison_inputs"] = build_decision_comparison_inputs(
                case_intelligence_result,
                message_id=str(agent_signal.get("message_id") or ""),
            )
            run_result = execute_agent_run(
                resolution.engagement_id,
                store=operator_store,
                signal=agent_signal,
                settings=settings,
                mailbox_store=mailbox_store,
                require_enabled=False,
            )
            snapshot = run_result.snapshot
            if run_result.warnings:
                warnings.extend(run_result.warnings)
        except AgentConcurrencyError as exc:
            try:
                snapshot = _project_reconcile_failure_best_effort(
                    operator_store,
                    snapshot,
                    failure_code="agent_concurrency_conflict",
                )
            except Exception:  # noqa: BLE001 - visibility projection must never hide the root failure
                logger.warning("RECONCILE_FAILURE_PROJECTION_FAILED", extra={"x": {
                    "engagement_id": str(resolution.engagement_id or ""),
                    "failure_code": "agent_concurrency_conflict",
                }})
            _raise_agent_reconcile_failure(
                failure_code="agent_concurrency_conflict",
                exc=exc,
                retryable=True,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                snapshot = _project_reconcile_failure_best_effort(
                    operator_store,
                    snapshot,
                    failure_code="agent_execution_failed",
                )
            except Exception:  # noqa: BLE001 - visibility projection must never hide the root failure
                logger.warning("RECONCILE_FAILURE_PROJECTION_FAILED", extra={"x": {
                    "engagement_id": str(resolution.engagement_id or ""),
                    "failure_code": "agent_execution_failed",
                }})
            _raise_agent_reconcile_failure(
                failure_code="agent_execution_failed",
                exc=exc,
                retryable=False,
            )

        if mailbox_store is not None:
            patch_signal_engagement(
                mailbox_store,
                signal_id=signal.signal_id,
                engagement_id=resolution.engagement_id,
            )

        job_store = build_agent_job_store(runtime_context.settings)
        job_store.record_completed(
            engagement_id=resolution.engagement_id,
            signal_id=signal.signal_id,
            case_id=case_id,
        )

    return snapshot, run_result, resolution, warnings, case_intelligence_result, mailbox_memory_result


def _evaluate_cost_gate(signal: CanonicalSignal, intake: dict[str, Any]) -> dict[str, Any]:
    """
    Evaluate if agent run is justified for this signal (W0.4).
    Returns dict with 'skip' boolean and 'reason' string.
    """
    # Check signal source/kind for obvious low-value types
    source_kind = str(signal.source_kind or "").strip().lower()
    signal_kind = str(signal.signal_kind or "").strip().lower()

    # Auto-reply / bounce / notification sources — always skip
    if source_kind in {"auto_reply", "bounce", "notification", "system"}:
        return {"skip": True, "reason": f"low_value_source:{source_kind}"}

    # Spam indicators in subject/body
    subject = str(intake.get("message", {}).get("subject") or "").lower()
    body = str(intake.get("message", {}).get("body_text") or "").lower()

    spam_indicators = [
        "unsubscribe", "wyrejestruj", "newsletter", "promocja", "oferta specjalna",
        "spam", "marketing", "reklama", "viagra", "casino", "loteria",
        "auto-reply", "automatyczna odpowiedź", "brak dostępu", "delivery failed",
        "returned mail", "undeliverable", "bounced",
    ]

    combined = f"{subject} {body}"
    for indicator in spam_indicators:
        if contains_noise_keyword(combined, indicator):
            return {"skip": True, "reason": f"spam_indicator:{indicator}"}

    # Empty or near-empty content — skip if too short to be actionable
    if len(body.strip()) < 20 and len(subject.strip()) < 5:
        return {"skip": True, "reason": "empty_content"}

    # Flag signals without clear business intent (for future filtering, not blocking)
    if not intake.get("case_id") and not _customer_email_from_signal(signal, intake):
        # Still allow the agent to run - it can create new cases/identities
        # Only skip if clearly spam or low-value above
        return {"skip": False, "reason": "justified (creates new identity)"}

    return {"skip": False, "reason": "justified"}


def _check_cieplo_dedup(signal: CanonicalSignal, intake: dict[str, Any]) -> dict[str, Any]:
    """Dedup check: skip TUM for cieplo-orchestrator signals with completed pipeline.

    Prevents duplicate processing when Cieplo already completed the workflow.
    """
    source_repo = str(
        intake.get("message", {}).get("source_repo")
        or signal.payload.get("source_repo")
        or ""
    ).strip().lower()
    if source_repo != "cieplo-orchestrator":
        return {"skip": False, "reason": "not_cieplo"}

    # Check if lead has completed pipeline via subject/body indicators
    subject = str(intake.get("message", {}).get("subject") or "").lower()
    has_completed_indicators = any(
        kw in subject for kw in ["pdf gotowy", "pdf_ready", "completed", "zakończono"]
    )
    body = str(intake.get("message", {}).get("body_text") or "").lower()
    if not has_completed_indicators:
        has_completed_indicators = any(
            kw in body for kw in ["pdf gotowy", "pdf_ready", "completed", "zakończono", "status: done"]
        )

    if has_completed_indicators:
        return {"skip": True, "reason": "cieplo_already_completed"}

    return {"skip": False, "reason": "cieplo_in_progress"}


def run_agent_reconcile_staging(
    signal: CanonicalSignal,
    *,
    runtime_context: Any,
    dry_run: bool,
    intake_output: dict[str, Any] | None = None,
) -> tuple[EngagementSnapshotV2, AgentRunResult | None, EngagementResolution, list[str]]:
    """TUM deep_understand path — staging engagement without case_id (RFC E2)."""
    from agent_runtime.engagement_resolver import resolve_staging_engagement
    from agent_runtime.store import build_staging_snapshot

    warnings: list[str] = ["agent_staging_reconcile"]
    settings = load_agent_runtime_settings()
    if not dry_run:
        assert_reconcile_mode_consistent(settings)
    intake = dict(intake_output or signal.payload.get("intake_output") or {})

    # W0.4: Cost gate — skip agent run for low-value signals (spam, auto-reply, empty)
    cieplo_skip = _check_cieplo_dedup(signal, intake)
    if cieplo_skip.get("skip", False):
        warnings.append(f"cieplo_dedup_skip:{cieplo_skip.get('reason', 'completed')}")
        resolution = resolve_staging_engagement(
            _canonical_staging_payload(signal, intake),
            signal_id=signal.signal_id,
        )
        operator_store = build_operator_engagement_store(runtime_context.settings)
        snapshot = build_staging_snapshot(
            engagement_id=resolution.engagement_id,
            signal_id=signal.signal_id,
            trace_id=_current_trace_id(runtime_context),
            feed_visibility=_feed_visibility_for_signal(signal),
        )
        if not dry_run:
            snapshot = operator_store.insert_snapshot(snapshot)
        return snapshot, None, resolution, warnings

    if not dry_run:
        cost_gate_result = _evaluate_cost_gate(signal, intake)
        if cost_gate_result.get("skip", False):
            warnings.append(f"cost_gate_skip:{cost_gate_result.get('reason', 'low_value')}")
            # Create minimal staging snapshot without agent run
            resolution = resolve_staging_engagement(
                _canonical_staging_payload(signal, intake),
                signal_id=signal.signal_id,
            )
            operator_store = build_operator_engagement_store(runtime_context.settings)
            snapshot = build_staging_snapshot(
                engagement_id=resolution.engagement_id,
                signal_id=signal.signal_id,
                trace_id=_current_trace_id(runtime_context),
            feed_visibility=_feed_visibility_for_signal(signal),
            )
            if not dry_run:
                snapshot = operator_store.insert_snapshot(snapshot)
            return snapshot, None, resolution, warnings

    resolution = resolve_staging_engagement(
        _canonical_staging_payload(signal, intake),
        signal_id=signal.signal_id,
    )
    operator_store = build_operator_engagement_store(runtime_context.settings)
    existing = operator_store.load_snapshot(resolution.engagement_id)
    if existing is None:
        snapshot = build_staging_snapshot(
            engagement_id=resolution.engagement_id,
            signal_id=signal.signal_id,
            trace_id=_current_trace_id(runtime_context),
            feed_visibility=_feed_visibility_for_signal(signal),
        )
        if not dry_run:
            snapshot = operator_store.insert_snapshot(snapshot)
    else:
        snapshot = existing
        current_trace_id = _current_trace_id(runtime_context)
        current_signal_id = str(signal.signal_id or "").strip()
        updates: dict[str, Any] = {}
        if current_signal_id and str(snapshot.signal_id or "").strip() != current_signal_id:
            updates["signal_id"] = current_signal_id
        if current_trace_id and str(snapshot.trace_id or "").strip() != current_trace_id:
            updates["trace_id"] = current_trace_id
        # SLICE-2B1: the same monotonic promotion as the case-bound path. Reachable whenever a
        # staging engagement_id is reused (`stg_<signal_id[:12]>` prefix reuse, or a replay of the
        # same signal after its classification inputs changed).
        try:
            from feed_visibility import merge_feed_visibility
            from llm_contracts.engagement_snapshot_v2 import FeedVisibility

            _incoming = _feed_visibility_for_signal(signal)
            _merged = (
                merge_feed_visibility(snapshot.feed_visibility, _incoming.model_dump(mode="python"))
                if _incoming is not None
                else None
            )
            if _merged is not None:
                updates["feed_visibility"] = FeedVisibility(**_merged)
        except Exception:  # noqa: BLE001 - visibility metadata must never break signal processing
            logger.warning("FEED_VISIBILITY_REFRESH_FAILED", extra={"x": {
                "engagement_id": str(snapshot.engagement_id or ""),
                "signal_id": current_signal_id,
            }})
        if updates:
            snapshot = snapshot.model_copy(update=updates)
            if not dry_run:
                new_version = operator_store.save_snapshot(snapshot, expected_version=existing.version)
                snapshot = snapshot.model_copy(update={"version": new_version})

    run_result: AgentRunResult | None = None
    if dry_run:
        warnings.append("agent_staging_dry_run_skipped_execute")
    else:
        mailbox_store = runtime_context.resolved_store
        try:
            agent_signal = signal_payload_for_agent(signal, intake)
            agent_signal["tum_route"] = "deep_understand"
            agent_signal["orchestrator_route"] = "deep_understand"
            run_result = execute_agent_run(
                resolution.engagement_id,
                store=operator_store,
                signal=agent_signal,
                settings=settings,
                mailbox_store=mailbox_store,
                require_enabled=False,
                operator_scope=getattr(runtime_context, "operator_scope", ""),
            )
            snapshot = run_result.snapshot
            if run_result.warnings:
                warnings.extend(run_result.warnings)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"agent_staging_run_failed:{type(exc).__name__}:{exc}")

        if mailbox_store is not None:
            patch_signal_engagement(
                mailbox_store,
                signal_id=signal.signal_id,
                engagement_id=resolution.engagement_id,
            )

    return snapshot, run_result, resolution, warnings


def build_agent_reconcile_result(
    signal: CanonicalSignal,
    *,
    runtime_context: Any,
    dry_run: bool,
    entity_link_dict: dict[str, Any],
    snapshot_eng: EngagementSnapshotV2,
    resolution: EngagementResolution,
    run_result: AgentRunResult | None,
    warnings: list[str],
    intake_output: dict[str, Any] | None = None,
    fact_updates: Any | None = None,
    source_kind_override: str | None = None,
    case_intelligence_result: dict[str, Any] | None = None,
    downstream_mailbox_result: dict[str, Any] | None = None,
) -> Any:
    """Build ReconcileResult for agent-centric reconcile (lazy import signal_reconciler types)."""
    from projection_refresh_rules import decide_projection_refresh
    from signal_reconciler import (
        CaseMutationPlan,
        FactUpdates,
        ReconcileResult,
        SnapshotRefreshDecision,
        _stamp_case_runtime_state,
    )

    intake_result = dict(
        intake_output
        or signal.payload.get("intake_result_final")
        or signal.payload.get("intake_output")
        or {}
    )
    case_id = snapshot_eng.case_id
    case_key = str(
        signal.case_key_hint
        or entity_link_dict.get("case_key")
        or ""
    ).strip()
    run_id = str(((runtime_context.run_state or {}).get("run_id") or "agent-runtime"))
    v2_projection = build_v2_projection_from_engagement(
        snapshot_eng,
        signal=signal,
        intake_output=intake_result,
        case_key=case_key,
    )
    # FAZA 3: za flagą AGENT_PROJECTION_CANONICAL przepuść projekcję przez kanoniczny kompozytor
    # LLM (ContextTraySet -> ProjectionEnvelope). Fallback do cienkiego snapshotu przy każdym błędzie
    # — zero regresji, gdy pack/transport nie domkną się dla wejścia agentowego.
    if projection_canonical_enabled():
        try:
            operator_snapshot = build_canonical_operator_snapshot(
                engagement=snapshot_eng,
                signal=signal,
                intake_output=intake_result,
                run_id=run_id,
                store=getattr(runtime_context, "resolved_store", None),
                settings=getattr(runtime_context, "settings", None),
                warnings=warnings,
            )
            warnings.append("agent_projection_canonical")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"agent_projection_canonical_failed:{type(exc).__name__}:{exc}")
            operator_snapshot = build_operator_snapshot_from_engagement(
                snapshot_eng,
                signal=signal,
                intake_output=intake_result,
                case_key=case_key,
                run_id=run_id,
            )
    else:
        operator_snapshot = build_operator_snapshot_from_engagement(
            snapshot_eng,
            signal=signal,
            intake_output=intake_result,
            case_key=case_key,
            run_id=run_id,
        )
    effective_source_kind = str(source_kind_override or signal.source_kind or "gmail")
    projection_decision = decide_projection_refresh(
        signal.signal_kind,
        source_kind=effective_source_kind,
        case_id=case_id,
        has_case_state=True,
    )
    if not dry_run and case_id:
        _stamp_case_runtime_state(
            runtime_context.resolved_store,
            case_id=case_id,
            signal=signal,
            projection_decision=projection_decision,
        )
    intel = dict(case_intelligence_result or {})
    downstream_mm = dict(downstream_mailbox_result or {})
    mailbox_memory_result: dict[str, Any] = {
        "case_id": case_id,
        "engagement_id": resolution.engagement_id,
        "agent_runtime": True,
        "snapshot_version": snapshot_eng.version,
        "policy_report": downstream_mm.get("policy_report") or {"status": "AGENT_RUNTIME"},
    }
    if intel:
        mailbox_memory_result["case_intelligence_result"] = intel
    if downstream_mm:
        mailbox_memory_result.update({k: v for k, v in downstream_mm.items() if k not in mailbox_memory_result})
    if run_result is not None:
        mailbox_memory_result["agent_turns"] = len(run_result.graph.turns)
    merged_warnings = list(warnings)
    if not intel:
        merged_warnings.append("agent_runtime_reconcile_no_shared_downstream")
    facts = fact_updates if fact_updates is not None else FactUpdates()
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
            mutation_kind="agent_runtime",
            reasons=["agent_centric_reconcile"],
        ),
        fact_updates=facts,
        snapshot_refresh_decision=SnapshotRefreshDecision(
            should_refresh=True,
            mode="agent_runtime",
            reason=f"{signal.signal_kind}_agent",
        ),
        projection_refresh_decision=projection_decision,
        mailbox_memory_result=mailbox_memory_result,
        rebuild_result={
            "case_id": case_id,
            "engagement_id": resolution.engagement_id,
            "rebuild_mode": "agent_runtime",
            "update_reasons": ["agent_run_completed"] if run_result else ["agent_dry_run"],
        },
        preview={
            "signal_id": signal.signal_id,
            "case_id": case_id,
            "engagement_id": resolution.engagement_id,
            "operational_status": snapshot_eng.operational_status.code,
            "agent_run_version": snapshot_eng.version,
            "reconcile_path": "agent_runtime",
        },
        v2_projection=v2_projection,
        stage_outputs={
            "canonical_signal_id": signal.signal_id,
            "entity_link_result": entity_link_dict,
            "operator_projection_snapshot": operator_snapshot,
            "agent_engagement_snapshot": snapshot_eng.model_dump(mode="python"),
            "agent_runtime_mode": str(load_agent_runtime_settings().mode or "prep"),
            "reconcile_path": "agent_runtime",
            "case_intelligence_result": intel,
        },
        warnings=merged_warnings,
    )
