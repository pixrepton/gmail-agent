"""Gmail-agent os_event telemetry (W1+): feed push, reconcile."""

from __future__ import annotations

import os
from typing import Any

from event_spine.emitter import publish_os_event


def _database_url(settings: Any | None) -> str:
    if settings is not None:
        raw = str(getattr(settings, "mailbox_memory_database_url", "") or "").strip()
        if raw:
            return raw
    return str(os.environ.get("MAILBOX_MEMORY_DATABASE_URL") or "").strip()


def _base_payload(*, summary_pl: str, status: str = "ok", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": "topinstal.os_event.v1",
        "summary_pl": str(summary_pl or "").strip(),
        "status": str(status or "ok").strip() or "ok",
    }
    for key, value in extra.items():
        if value is not None and str(value).strip() != "":
            body[key] = value
    return body


def publish_gmail_feed_push_event(
    settings: Any | None,
    *,
    ok: bool,
    snapshot_id: str = "",
    error: str = "",
    engagement_id: str = "",
    case_id: str = "",
    trigger: str = "",
    message_id: str = "",
) -> str | None:
    db_url = _database_url(settings)
    if not db_url:
        return None
    event_type = "gmail.feed.pushed" if ok else "gmail.feed.push_failed"
    summary = (
        "Operational feed wypchnięty do Daszka"
        if ok
        else f"Nie udało się wypchnąć feedu do Daszka: {str(error or 'błąd')[:120]}"
    )
    return publish_os_event(
        database_url=db_url,
        event_type=event_type,
        engagement_id=str(engagement_id or "").strip(),
        source_repo="gmail-agent",
        payload=_base_payload(
            summary_pl=summary,
            status="ok" if ok else "error",
            snapshot_id=str(snapshot_id or "").strip(),
            trigger=str(trigger or "").strip(),
        ),
        correlation={
            "case_id": str(case_id or "").strip(),
            "message_id": str(message_id or "").strip(),
            "surface": "v3_operational_feed",
        },
    )


def publish_gmail_reconcile_completed(
    settings: Any | None,
    reconcile_result: Any,
    *,
    trigger_message_id: str = "",
) -> str | None:
    if reconcile_result is None:
        return None
    processing_state = str(getattr(reconcile_result, "processing_state", "") or "").strip()
    if processing_state == "skipped_duplicate":
        return None
    db_url = _database_url(settings)
    if not db_url:
        return None
    case_id = str(getattr(reconcile_result, "case_id", "") or "").strip()
    signal_id = str(getattr(reconcile_result, "signal_id", "") or "").strip()
    engagement_id = ""
    stage_outputs = getattr(reconcile_result, "stage_outputs", None)
    if isinstance(stage_outputs, dict):
        agent_snap = stage_outputs.get("agent_engagement_snapshot")
        if isinstance(agent_snap, dict):
            engagement_id = str(agent_snap.get("engagement_id") or "").strip()
    summary = f"Reconcile zakończony ({processing_state or 'ok'})"
    if case_id:
        summary = f"Reconcile sprawy {case_id} zakończony ({processing_state or 'ok'})"
    status = "ok"
    if processing_state.startswith("failed") or processing_state == "error":
        status = "error"
    elif processing_state in {"shadowed", "skipped"}:
        status = "warning"
    return publish_os_event(
        database_url=db_url,
        event_type="gmail.reconcile.completed",
        engagement_id=engagement_id,
        source_repo="gmail-agent",
        payload=_base_payload(
            summary_pl=summary,
            status=status,
            processing_state=processing_state,
        ),
        correlation={
            "case_id": case_id,
            "signal_id": signal_id,
            "message_id": str(trigger_message_id or "").strip(),
        },
    )


__all__ = [
    "publish_gmail_feed_push_event",
    "publish_gmail_reconcile_completed",
]
