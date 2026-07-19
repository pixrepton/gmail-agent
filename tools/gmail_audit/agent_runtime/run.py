
"""AgentRun entry — load snapshot, run graph, save (PR-C)."""

from __future__ import annotations

from log_config import get_logger
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from agent_runtime.checkpoint import (
    AgentRunCheckpointStore,
    InMemoryAgentRunCheckpointStore,
    new_run_id,
)
from agent_runtime.database_url import resolve_agent_runtime_database_url
from agent_runtime.constitution import AgentConstitution, load_live
from agent_runtime.graph import AgentGraphEngine, AgentGraphRunResult
from agent_runtime.planner import ToolPlanner
from agent_runtime.settings import AgentRuntimeSettings, load_agent_runtime_settings
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import OperatorEngagementStore
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tools_registry import AgentToolRegistry
from agent_runtime.turn_journal import AgentTurnJournal, InMemoryAgentTurnJournal, PostgresAgentTurnJournal
from agent_runtime.validate import AgentRuntimeConfigError, assert_agent_run_ready
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2
log = get_logger(__name__)


@dataclass
class AgentRunResult:
    snapshot: EngagementSnapshotV2
    graph: AgentGraphRunResult
    version: int
    warnings: list[str] = field(default_factory=list)


def build_planner(settings: AgentRuntimeSettings) -> ToolPlanner:
    from agent_runtime.openai_agent_client import OpenAIToolPlanner

    return OpenAIToolPlanner(settings=settings)


def build_turn_journal(
    settings: AgentRuntimeSettings,
    *,
    allow_in_memory: bool = False,
) -> AgentTurnJournal | None:
    url, url_source = resolve_agent_runtime_database_url(settings)
    if url:
        return PostgresAgentTurnJournal(url)
    if not settings.enabled:
        return None
    reason = "settings_not_loaded_or_no_db_url"
    if not allow_in_memory:
        raise AgentRuntimeConfigError(
            "Postgres agent turn journal required when AGENT_RUNTIME_ENABLED=1; "
            "set MAILBOX_MEMORY_DATABASE_URL (or pass allow_in_memory=True for isolated dev/test only)."
        )
    log.warning(
        "AGENT_RUN_TURN_JOURNAL_STORE_FALLBACK_TO_MEMORY",
        extra={"x": {"reason": reason, "url_source": url_source}},
    )
    return InMemoryAgentTurnJournal()


def load_run_constitution(settings: AgentRuntimeSettings) -> AgentConstitution:
    return load_live(
        rag_enabled=bool(settings.rag_enabled),
        database_url=settings.mailbox_database_url,
        constitution_path=settings.constitution_path or None,
    )


def _cap_steps_for_run(snapshot: EngagementSnapshotV2, settings: AgentRuntimeSettings) -> EngagementSnapshotV2:
    max_rounds = int(settings.max_rounds)
    remaining = int(snapshot.operational_status.steps_remaining)
    if remaining <= max_rounds:
        return snapshot
    return apply_snapshot_delta(
        snapshot,
        {"operational_status": {"steps_remaining": max_rounds}},
    )


def build_checkpoint_store(settings: AgentRuntimeSettings) -> Any | None:
    url = str(settings.mailbox_database_url or "").strip()
    if not url:
        return None
    store = AgentRunCheckpointStore(url)
    strict = str(__import__("os").environ.get("AGENT_CHECKPOINT_STRICT", "0")).strip() == "1"
    try:
        store.ensure_schema()
    except Exception as exc:
        if strict:
            raise RuntimeError(f"agent_run_checkpoints schema required (AGENT_CHECKPOINT_STRICT=1): {exc}") from exc
        return InMemoryAgentRunCheckpointStore()
    return store


def _resolve_agent_settings_for_signal(
    settings: AgentRuntimeSettings,
    signal: Mapping[str, Any] | None,
) -> AgentRuntimeSettings:
    """Strong-model tier for TUM deep_understand (W2.4)."""
    if not signal:
        return settings
    route = str(
        signal.get("tum_route") or signal.get("orchestrator_route") or ""
    ).strip().lower()
    if route != "deep_understand":
        return settings
    fallback = str(settings.model_fallback or "").strip()
    if fallback and fallback != settings.model:
        return replace(settings, model=fallback)
    return settings


def execute_agent_run(
    engagement_id: str,
    *,
    store: OperatorEngagementStore,
    signal: Mapping[str, Any] | None = None,
    planner: ToolPlanner | None = None,
    constitution: AgentConstitution | None = None,
    settings: AgentRuntimeSettings | None = None,
    mailbox_store: Any | None = None,
    turn_journal: AgentTurnJournal | None = None,
    require_enabled: bool = True,
    resume_from: str | None = None,
    run_id: str | None = None,
    operator_scope: str = "",
) -> AgentRunResult:
    """
    Run one agent graph pass: plan → tool → delta → CAS save.
    Persists episodic turns when Postgres journal is configured.
    """
    settings = settings or load_agent_runtime_settings()
    settings = _resolve_agent_settings_for_signal(settings, signal)
    warnings: list[str] = []
    if require_enabled and not settings.enabled:
        raise AgentRuntimeConfigError("AGENT_RUNTIME_ENABLED must be 1 for execute_agent_run")
    if str(settings.mode or "").strip().lower() == "legacy":
        raise AgentRuntimeConfigError("execute_agent_run must not run in AGENT_RUNTIME_MODE=legacy")
    if require_enabled:
        assert_agent_run_ready(settings)

    constitution = constitution or load_run_constitution(settings)
    # Routing constitution wg source_kind sygnału (PR-separation)
    if signal is not None:
        source = str(signal.get("source_kind", "") or signal.get("signal_kind", "") or "").strip()
        if source:
            from agent_runtime.constitution import get_constitution_for_signal
            from dataclasses import replace
            allowlist, budget, system_note = get_constitution_for_signal(source)
            constitution = replace(
                constitution,
                tool_allowlist=allowlist,
                tool_budget=dict(budget),
            )
    checkpoint_store = build_checkpoint_store(settings)
    active_run_id = str(run_id or resume_from or new_run_id())
    start_turn_idx = 0
    snapshot = store.load_snapshot(engagement_id)
    store_snapshot = snapshot
    if resume_from and checkpoint_store is not None:
        loaded = checkpoint_store.load_latest(resume_from)
        if loaded is not None:
            snapshot = loaded.snapshot
            start_turn_idx = int(loaded.turn_idx) + 1
            active_run_id = resume_from
            warnings.append("agent_run_resumed_from_checkpoint")
            if store_snapshot is not None:
                snapshot = snapshot.model_copy(update={"version": store_snapshot.version})
    if snapshot is None:
        raise ValueError(f"No engagement snapshot for {engagement_id!r}")

    snapshot = _cap_steps_for_run(snapshot, settings)
    planner = planner or build_planner(settings)
    journal = turn_journal
    if journal is None and settings.enabled:
        journal = build_turn_journal(settings)

    engine = AgentGraphEngine(
        planner=planner,
        constitution=constitution,
        tool_registry=AgentToolRegistry(),
        turn_journal=journal,
        checkpoint_store=checkpoint_store,
        run_id=active_run_id,
    )
    ctx = ToolExecutionContext.from_snapshot(
        snapshot,
        settings=settings,
        mailbox_store=mailbox_store,
        signal_payload=dict(signal or {}),
        constitution=constitution,
    )

    # P7: Event Spine — agent.run.started
    _db_url = str(getattr(settings, "mailbox_database_url", "") or "").strip()
    if _db_url:
        try:
            from event_spine.emitter import publish_os_event
            _case_id = str(getattr(snapshot, "case_id", "") or "")
            _trace_id = f"agent_{uuid.uuid4().hex[:12]}"
            publish_os_event(
                database_url=_db_url,
                event_type="agent.run.started",
                engagement_id=engagement_id,
                case_id=_case_id or None,
                trace_id=_trace_id,
                severity="info",
                payload={"mode": str(settings.mode or ""), "max_rounds": settings.max_rounds},
            )
        except Exception as exc:
            log.warning("agent.run.started event_publish_failed engagement_id=%s exc=%s", engagement_id, exc)
            _trace_id = None
    else:
        _trace_id = None

    graph_result = engine.run(
        snapshot,
        context=ctx,
        turn_journal=journal,
        start_turn_idx=start_turn_idx,
        operator_scope=operator_scope,
    )
    new_version = store.save_snapshot(graph_result.snapshot, expected_version=snapshot.version)
    final = graph_result.snapshot.model_copy(update={"version": new_version})

    # P7: Event Spine — agent.run.completed
    if _db_url and _trace_id:
        try:
            from event_spine.emitter import publish_os_event
            _proposal_count = sum(
                1 for p in (getattr(graph_result.snapshot, "staging_proposals", []) or [])
                if getattr(p, "status", "") != "rejected"
            )
            publish_os_event(
                database_url=_db_url,
                event_type="agent.run.completed",
                engagement_id=engagement_id,
                case_id=_case_id or None,
                trace_id=_trace_id,
                severity="info",
                success=not graph_result.snapshot.hitl_gate.required,
                payload={
                    "turns": len(graph_result.turns),
                    "proposals": _proposal_count,
                    "hitl_required": graph_result.snapshot.hitl_gate.required,
                },
            )
        except Exception as exc:
            log.warning("agent.run.completed event_publish_failed engagement_id=%s exc=%s", engagement_id, exc)

    return AgentRunResult(
        snapshot=final,
        graph=graph_result,
        version=new_version,
        warnings=warnings,
    )
