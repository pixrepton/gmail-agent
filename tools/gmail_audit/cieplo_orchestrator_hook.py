"""Apply Cieplo orchestrator terminal results to mailbox_memory case rows."""

from __future__ import annotations

from typing import Any

from case_routing import enrich_case_row_before_upsert

CIEPLO_EVENT_PREFIX = "cieplo.workflow."

CIEPLO_ORCHESTRATOR_SOURCE_REPOS = (
    "topinstal-cieplo-orchestrator",
    "cieplo-orchestrator",
    "gmail-agent",
)

CIEPLO_DESK_INFO_BRIEF_PL = (
    "Sprawa utworzona · Lead Cieplo.app · Proces automatyczny w toku · Brak pilnej akcji"
)


def is_cieplo_orchestrator_event(event_type: str) -> bool:
    return str(event_type or "").strip().startswith(CIEPLO_EVENT_PREFIX)


def orchestrator_status_for_event(event_type: str, event_payload: dict[str, Any] | None) -> str | None:
    et = str(event_type or "").strip()
    payload = event_payload if isinstance(event_payload, dict) else {}
    if et == "cieplo.workflow.done":
        return "ok"
    if et == "cieplo.workflow.failed":
        return "failed"
    if et == "cieplo.workflow.state_changed":
        to_state = str(payload.get("to_state") or payload.get("state") or "").strip().upper()
        if to_state == "DONE":
            return "ok"
        if to_state == "TIMEOUT":
            return "timeout"
        if to_state in {"FAILED_FINAL", "FAILED_RETRYABLE", "FAILED"}:
            return "failed"
    return None


def _case_id_from_engagement(registry_store: Any, engagement_id: str) -> str:
    eid = str(engagement_id or "").strip()
    if not eid or registry_store is None:
        return ""
    list_links = getattr(registry_store, "list_links_for_engagement", None)
    if not callable(list_links):
        return ""
    for link in list_links(eid):
        if not isinstance(link, dict):
            continue
        if str(link.get("link_type") or "").strip() == "mailbox_case":
            return str(link.get("target_id") or "").strip()
    return ""


def resolve_case_id_from_cieplo_event(
    *,
    correlation: dict[str, Any] | None,
    event_payload: dict[str, Any] | None,
    engagement_id: str = "",
    mailbox_store: Any = None,
    registry_store: Any = None,
) -> str:
    corr = correlation if isinstance(correlation, dict) else {}
    payload = event_payload if isinstance(event_payload, dict) else {}

    for key in ("case_id", "mailbox_case_id"):
        cid = str(payload.get(key) or corr.get(key) or "").strip()
        if cid:
            return cid

    message_id = str(corr.get("message_id") or payload.get("message_id") or "").strip()
    if message_id and mailbox_store is not None:
        fetch = getattr(mailbox_store, "fetch_case_by_message_id", None)
        if callable(fetch):
            row = fetch(message_id)
            if isinstance(row, dict):
                return str(row.get("case_id") or "").strip()

    workflow_id = str(corr.get("workflow_id") or payload.get("workflow_id") or "").strip()
    if workflow_id and registry_store is not None:
        find_link = getattr(registry_store, "find_engagement_by_link", None)
        if callable(find_link):
            for repo in CIEPLO_ORCHESTRATOR_SOURCE_REPOS:
                eid = find_link(link_type="cieplo_workflow", target_id=workflow_id, source_repo=repo)
                if eid:
                    cid = _case_id_from_engagement(registry_store, eid)
                    if cid:
                        return cid
            eid = find_link(link_type="cieplo_workflow", target_id=workflow_id, source_repo="")
            if eid:
                cid = _case_id_from_engagement(registry_store, eid)
                if cid:
                    return cid

    eid = str(engagement_id or corr.get("engagement_id") or payload.get("engagement_id") or "").strip()
    if eid:
        return _case_id_from_engagement(registry_store, eid)

    return ""


def apply_cieplo_orchestrator_result(
    case_id: str,
    status: str,
    *,
    mailbox_store: Any,
) -> dict[str, Any]:
    cid = str(case_id or "").strip()
    if not cid:
        return {"ok": False, "error": "case_id required"}
    st = str(status or "").strip().lower()
    if st not in {"ok", "failed", "timeout"}:
        return {"ok": False, "error": f"unsupported status: {status!r}"}
    if mailbox_store is None:
        return {"ok": False, "error": "mailbox_store unavailable"}

    fetch_case = getattr(mailbox_store, "fetch_case", None)
    if not callable(fetch_case):
        return {"ok": False, "error": "fetch_case unavailable"}
    row = fetch_case(cid)
    if not isinstance(row, dict) or not row:
        return {"ok": False, "error": "case not found", "case_id": cid}

    orchestrator_status = "failed" if st in {"failed", "timeout"} else "ok"
    enriched, routing = enrich_case_row_before_upsert(
        row,
        source_kind="cieplo_orchestrated",
        orchestrator_status=orchestrator_status,
    )
    if not routing.upsert_allowed:
        return {"ok": True, "skipped": True, "reason": "upsert_not_allowed", "case_id": cid}

    upsert_case = getattr(mailbox_store, "upsert_case", None)
    if callable(upsert_case):
        upsert_case(enriched)

    return {
        "ok": True,
        "case_id": cid,
        "orchestrator_status": orchestrator_status,
        "requires_action": routing.requires_action,
        "desk_eligible": routing.desk_eligible,
        "source_kind": routing.source_kind,
    }


def maybe_apply_cieplo_hook_from_os_event(
    *,
    event_type: str,
    correlation: dict[str, Any] | None = None,
    event_payload: dict[str, Any] | None = None,
    engagement_id: str = "",
    mailbox_store: Any = None,
    registry_store: Any = None,
) -> dict[str, Any]:
    et = str(event_type or "").strip()
    if not is_cieplo_orchestrator_event(et):
        return {"ok": True, "skipped": True, "reason": "not_cieplo_event"}

    status = orchestrator_status_for_event(et, event_payload)
    if status is None:
        return {"ok": True, "skipped": True, "reason": "non_terminal_event", "event_type": et}

    case_id = resolve_case_id_from_cieplo_event(
        correlation=correlation,
        event_payload=event_payload,
        engagement_id=engagement_id,
        mailbox_store=mailbox_store,
        registry_store=registry_store,
    )
    if not case_id:
        return {"ok": False, "error": "case_id_unresolved", "event_type": et}

    return apply_cieplo_orchestrator_result(case_id, status, mailbox_store=mailbox_store)


__all__ = [
    "CIEPLO_DESK_INFO_BRIEF_PL",
    "apply_cieplo_orchestrator_result",
    "is_cieplo_orchestrator_event",
    "maybe_apply_cieplo_hook_from_os_event",
    "orchestrator_status_for_event",
    "resolve_case_id_from_cieplo_event",
]
