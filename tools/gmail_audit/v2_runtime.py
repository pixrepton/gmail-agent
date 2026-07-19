"""Canonical lightweight helpers for the Daszek v2 runtime seam."""

from __future__ import annotations
from log_config import get_logger

from datetime import datetime
from typing import Any

from artifact_io import append_jsonl
from dash_projection_v2 import validate_v2_shadow_projection
from daszek_client import DaszekClientError
from daszek_push_policy import evaluate_operator_projection_policy
from event_memory import EventLog
from mailbox_v2_desk_note import persist_open_desk_note_id_from_v2_projection
from redaction import sanitize_for_storage

log = get_logger(__name__)


def extract_v2_projection_from_stage_record(stage_record: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(stage_record, dict):
        return None

    projection = {
        "signal_projection": stage_record.get("signal_projection"),
        "case_patch": stage_record.get("case_patch"),
        "desk_note_patch": stage_record.get("desk_note_patch"),
        "decision_trace": stage_record.get("decision_trace"),
    }
    if not all(isinstance(value, dict) and value for value in projection.values()):
        return None
    return validate_v2_shadow_projection(projection)


def build_v2_ingest_payload(
    *,
    run_id: str,
    message_key: str,
    v2_projection: dict[str, Any],
    thread_memory: dict[str, Any] | None = None,
    operational_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    projection = validate_v2_shadow_projection(v2_projection)
    payload: dict[str, Any] = {
        "projection_version": "1.0",
        "run_id": str(run_id or "").strip(),
        "message_key": str(message_key or "").strip(),
        "emitted_at": datetime.now().astimezone().isoformat(),
        "signal_projection": projection["signal_projection"],
        "case_patch": projection["case_patch"],
        "desk_note_patch": projection["desk_note_patch"],
        "decision_trace": projection["decision_trace"],
    }
    if thread_memory and isinstance(thread_memory, dict) and str(thread_memory.get("thread_id") or "").strip():
        payload["thread_memory"] = thread_memory
    if operational_events:
        payload["operational_events"] = operational_events
    return payload


def push_v2_projection_to_daszek(
    *,
    run_state: dict[str, Any],
    message_id: str,
    v2_projection: dict[str, Any] | None,
    case_intelligence_result: dict[str, Any] | None = None,
    event_log: EventLog | None = None,
    action_plan_result: dict[str, Any] | None = None,
    intake_result_final: dict[str, Any] | None = None,
    policy_report: dict[str, Any] | None = None,
) -> None:
    client = run_state.get("daszek_client")
    manifest = run_state["manifest"]
    v2_enabled = bool(manifest.get("daszek_v2_push_enabled"))

    if client is None:
        log.warning("v2_push_skip: daszek_client is None for message_id=%s", message_id)
        return

    try:
        from config import load_settings
        from daszek_engagement_feed import engagement_feed_source_enabled

        settings = run_state.get("settings")
        if settings is None:
            settings = load_settings(require_groq=False, require_google=False)
        if engagement_feed_source_enabled(settings):
            log.warning("v2_push_skip_engagement_feed_source message_id=%s", message_id)
            append_jsonl(
                run_state["daszek_v2_push_path"],
                sanitize_for_storage(
                    {
                        "record_type": "projection_skip",
                        "surface": "v2",
                        "message_id": message_id,
                        "reason": "skipped_engagement_feed_source",
                        "push_policy_reason": "skipped_engagement_feed_v2",
                        "push_policy_detail": "DASZEK_FEED_SOURCE=engagement_snapshot_v2 — v2 live push wyłączony (Move 5).",
                        "daszek_v2_push_enabled": v2_enabled,
                    }
                ),
            )
            return
    except Exception:  # noqa: BLE001 — guard must not break v2 push path
        pass

    if not isinstance(v2_projection, dict) or not v2_projection:
        append_jsonl(
            run_state["daszek_v2_push_path"],
            sanitize_for_storage(
                {
                    "record_type": "projection_skip",
                    "surface": "v2",
                    "message_id": message_id,
                    "reason": "skipped_missing_shadow_projection",
                    "push_policy_reason": "skipped_missing_v2_projection",
                    "push_policy_detail": "v2_projection was empty or None before ingest (build/extract failed or incomplete stage); see stage_records / errors JSONL.",
                    "daszek_v2_push_enabled": True,
                }
            ),
        )
        run_state["summary"]["items_v2_push_skipped"] += 1
        return

    if not v2_enabled:
        log.warning("v2_push_skip_config_disabled message_id=%s", message_id)
        append_jsonl(
            run_state["daszek_v2_push_path"],
            sanitize_for_storage(
                {
                    "record_type": "projection_skip",
                    "surface": "v2",
                    "message_id": message_id,
                    "reason": "skipped_v2_config_disabled",
                    "push_policy_reason": "skipped_v2_disabled",
                    "push_policy_detail": "daszek_v2_push_enabled is false; set DASZEK_V2_PUSH=1 to POST v2 ingest.",
                    "daszek_v2_push_enabled": False,
                }
            ),
        )
        run_state["summary"]["items_v2_push_skipped"] += 1
        return

    policy = evaluate_operator_projection_policy(
        manifest=manifest,
        action_plan_result=action_plan_result,
        intake_result_final=intake_result_final,
        policy_report=policy_report,
    )
    append_jsonl(
        run_state["daszek_v2_push_path"],
        sanitize_for_storage(
            {
                "record_type": "push_policy",
                "surface": "v2_operator_projection",
                "message_id": message_id,
                "allowed": policy.allowed,
                "push_policy_reason": policy.push_policy_reason,
                "push_policy_detail": policy.push_policy_detail,
            }
        ),
    )
    if not policy.allowed:
        run_state["summary"]["items_v2_push_blocked_by_policy"] += 1
        return

    thread_mem = None
    op_events: list[dict[str, Any]] | None = None
    if isinstance(case_intelligence_result, dict):
        candidate = case_intelligence_result.get("thread_memory")
        if isinstance(candidate, dict):
            thread_mem = candidate
    if isinstance(event_log, EventLog) and event_log.events():
        op_events = sanitize_for_storage(event_log.events())

    try:
        payload = build_v2_ingest_payload(
            run_id=run_state["run_id"],
            message_key=message_id,
            v2_projection=v2_projection,
            thread_memory=thread_mem,
            operational_events=op_events,
        )
        result = client.push_v2_projection(payload)
    except (DaszekClientError, ValueError) as exc:
        append_jsonl(
            run_state["daszek_v2_push_path"],
            sanitize_for_storage(
                {
                    "record_type": "projection_failure",
                    "surface": "v2",
                    "message_id": message_id,
                    "error": str(exc),
                    "push_policy_reason": policy.push_policy_reason,
                    "push_policy_detail": policy.push_policy_detail,
                }
            ),
        )
        run_state["summary"]["items_v2_push_failed"] += 1
        error_handler = run_state.get("_record_error")
        if callable(error_handler):
            error_handler(
                run_state,
                stage="daszek_v2_push",
                message_id=message_id,
                error=str(exc),
                details={"shadow_contract": "daszek_v2_ingest"},
            )
        return

    append_jsonl(
        run_state["daszek_v2_push_path"],
        {
            "status": result.status,
            "message_id": result.message_id,
            "signal_id": result.signal_id,
            "trace_id": result.trace_id,
            "push_policy_reason": policy.push_policy_reason,
            "details": sanitize_for_storage(result.details),
        },
    )
    run_state["summary"]["items_v2_pushed"] += 1

    mb_runtime = run_state.get("mailbox_memory_runtime")
    store = getattr(mb_runtime, "store", None) if mb_runtime is not None else None
    if store is not None:
        try:
            if persist_open_desk_note_id_from_v2_projection(store, v2_projection):
                run_state["summary"]["items_v2_desk_note_persisted"] = (
                    int(run_state["summary"].get("items_v2_desk_note_persisted", 0) or 0) + 1
                )
        except Exception as exc:  # noqa: BLE001
            append_jsonl(
                run_state["daszek_v2_push_path"],
                sanitize_for_storage(
                    {
                        "record_type": "desk_note_persist_warning",
                        "message_id": message_id,
                        "error": str(exc)[:500],
                    }
                ),
            )

    readback_enabled = bool(manifest.get("daszek_v2_readback_enabled"))
    if readback_enabled:
        rb = client.readback_v2_projection(payload=payload, ingest_details=sanitize_for_storage(result.details))
        append_jsonl(
            run_state["daszek_v2_push_path"],
            sanitize_for_storage(
                {
                    "record_type": "v2_readback",
                    "message_id": message_id,
                    "signal_id": result.signal_id,
                    "trace_id": result.trace_id,
                    **rb,
                }
            ),
        )


__all__ = [
    "build_v2_ingest_payload",
    "extract_v2_projection_from_stage_record",
    "push_v2_projection_to_daszek",
]
