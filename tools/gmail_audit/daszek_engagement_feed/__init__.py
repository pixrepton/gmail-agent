"""Thin Daszek operational feed from EngagementSnapshot.v2 (PR-E package)."""

from __future__ import annotations

import os
from typing import Any

from agent_runtime.agent_reconcile import build_operator_engagement_store
from agent_runtime.database_url import resolve_mailbox_memory_database_url
from agent_runtime.turn_journal import AgentTurnJournal, InMemoryAgentTurnJournal, PostgresAgentTurnJournal
from agent_runtime.validate import AgentRuntimeConfigError
from log_config import get_logger
from daszek_engagement_feed.build import (
    build_engagement_feed_envelope,
    build_feed_from_engagement_snapshots,
    build_operational_feed_from_engagement_store,
)
from daszek_engagement_feed.case import (
    build_case_detail_from_engagement,
    operator_essence_pl_from_snapshot,
    snapshot_to_feed_case,
)
from daszek_engagement_feed.day import compose_day_sections
from daszek_engagement_feed.desk import DESK_OPERATIONAL_CODES, snapshot_to_desk_item
from daszek_engagement_feed.tasks import snapshot_to_feed_tasks

ENGAGEMENT_FEED_SCHEMA_VERSION = "2"

logger = get_logger(__name__)


def resolve_reconcile_case_id_for_feed(reconcile_result: Any) -> str:
    case_id = str(getattr(reconcile_result, "case_id", "") or "").strip()
    if case_id:
        return case_id
    stage = getattr(reconcile_result, "stage_outputs", None)
    if not isinstance(stage, dict):
        return ""
    agent_snap = stage.get("agent_engagement_snapshot")
    if isinstance(agent_snap, dict):
        case_id = str(agent_snap.get("case_id") or "").strip()
        if case_id:
            return case_id
    op_snap = stage.get("operator_projection_snapshot")
    if isinstance(op_snap, dict):
        envelope = op_snap.get("projection_envelope")
        if isinstance(envelope, dict):
            return str(envelope.get("case_id") or "").strip()
    return ""


def engagement_feed_source_enabled(settings: Any | None = None) -> bool:
    from agent_runtime.agent_reconcile import agent_runtime_reconcile_active
    from agent_runtime.primary_cutover import agent_runtime_primary_active, legacy_feed_explicitly_requested

    if legacy_feed_explicitly_requested():
        return False
    if agent_runtime_primary_active():
        return True
    raw = str(os.getenv("DASZEK_FEED_SOURCE", "") or "").strip().lower()
    if not raw:
        return agent_runtime_reconcile_active()
    return raw in {"engagement_snapshot_v2", "engagement_v2", "agent_runtime"}


def build_turn_journal_for_settings(
    settings: Any,
    *,
    allow_in_memory: bool = False,
) -> AgentTurnJournal:
    url, url_source = resolve_mailbox_memory_database_url(settings)
    if url:
        return PostgresAgentTurnJournal(url)
    reason = "settings_not_loaded_or_no_db_url"
    if not allow_in_memory:
        raise AgentRuntimeConfigError(
            "Postgres agent turn journal required; "
            "set MAILBOX_MEMORY_DATABASE_URL (call load_settings() so tools/gmail_audit/.env is loaded) "
            "or pass allow_in_memory=True for isolated dev/test only."
        )
    logger.warning(
        "AGENT_TURN_JOURNAL_STORE_FALLBACK_TO_MEMORY",
        extra={"x": {"reason": reason, "url_source": url_source}},
    )
    return InMemoryAgentTurnJournal()


def build_engagement_feed_for_cel(
    mailbox_store: Any,
    settings: Any,
    *,
    case_limit: int = 50,
    snapshot_id: str | None = None,
    trigger_message_id: str = "",
    run_id: str = "",
    extra_case_ids: list[str] | None = None,
) -> dict[str, Any]:
    from daszek_engagement_feed.build import _is_excluded_case

    operator_store = build_operator_engagement_store(settings)
    journal = build_turn_journal_for_settings(settings)
    case_ids: list[str] = []
    list_fn = getattr(mailbox_store, "list_cases", None)
    if callable(list_fn):
        try:
            rows = list_fn(limit=case_limit) or []
        except TypeError:
            rows = list_fn() or []
        for row in rows:
            if isinstance(row, dict):
                cid = str(row.get("case_id") or "").strip()
                if cid and not _is_excluded_case(cid):
                    case_ids.append(cid)
    elif hasattr(mailbox_store, "cases"):
        for cid in list(getattr(mailbox_store, "cases", {}).keys())[:case_limit]:
            if cid and not _is_excluded_case(str(cid)):
                case_ids.append(str(cid))
    for cid in extra_case_ids or []:
        cid_s = str(cid or "").strip()
        if cid_s and cid_s not in case_ids and not _is_excluded_case(cid_s):
            case_ids.append(cid_s)
    return build_operational_feed_from_engagement_store(
        operator_store,
        case_ids=case_ids or None,
        journal=journal,
        mailbox_store=mailbox_store,
        case_limit=case_limit,
        snapshot_id=snapshot_id,
        source={
            "source_run_id": run_id,
            "trigger_message_id": trigger_message_id,
            "cel_path": "engagement_snapshot_v2",
        },
    )


def build_daszek_feed_doctor_check(settings: Any | None = None) -> dict[str, Any]:
    from agent_runtime.agent_reconcile import agent_runtime_reconcile_active
    from agent_runtime.settings import load_agent_runtime_settings

    agent_settings = load_agent_runtime_settings()
    source_raw = str(os.getenv("DASZEK_FEED_SOURCE", "") or "").strip().lower()
    engagement_on = engagement_feed_source_enabled(settings)
    agent_on = agent_runtime_reconcile_active(agent_settings)
    issues: list[str] = []
    if source_raw in {"legacy", "mailbox_memory", "projection_v3"} and agent_on:
        issues.append("DASZEK_FEED_SOURCE=legacy conflicts with active agent runtime")
    if source_raw in {"engagement_snapshot_v2", "engagement_v2", "agent_runtime"} and not agent_on:
        issues.append("DASZEK_FEED_SOURCE requests engagement feed but agent runtime is inactive")
    status = "failed" if issues else ("ok" if engagement_on or source_raw else "skipped")
    return {
        "status": status,
        "feed_source_env": source_raw or "(auto)",
        "engagement_feed_enabled": engagement_on,
        "agent_runtime_active": agent_on,
        "issues": issues,
    }


__all__ = [
    "DESK_OPERATIONAL_CODES",
    "ENGAGEMENT_FEED_SCHEMA_VERSION",
    "build_case_detail_from_engagement",
    "build_daszek_feed_doctor_check",
    "build_engagement_feed_envelope",
    "build_engagement_feed_for_cel",
    "build_feed_from_engagement_snapshots",
    "build_operational_feed_from_engagement_store",
    "compose_day_sections",
    "engagement_feed_source_enabled",
    "operator_essence_pl_from_snapshot",
    "resolve_reconcile_case_id_for_feed",
    "snapshot_to_desk_item",
    "snapshot_to_feed_case",
    "snapshot_to_feed_tasks",
]
