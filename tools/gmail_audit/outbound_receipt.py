"""Manual-send receipt: outbound direction + communication_sent close-loop.

Approve HITL is not send. When Gmail history later observes a Sent/outbound
message for a case whose engagement is `ready_for_manual_send`, mark
`communication_sent` on the EngagementSnapshot and emit an os_event so
follow-up / waiting clocks can key off real delivery.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)


def _first_email(value: str) -> str:
    match = _EMAIL_RE.search(str(value or ""))
    return str(match.group(0) if match else "").strip().lower()


def infer_live_direction(message: dict[str, Any], *, mailbox: str) -> str:
    """Direction for live intake: SENT label or From==mailbox => outbound."""
    labels = {str(item).strip().upper() for item in (message.get("labels") or []) if str(item).strip()}
    if "SENT" in labels:
        return "outbound"
    mailbox_email = _first_email(mailbox)
    sender = _first_email(str(message.get("sender") or message.get("from") or ""))
    recipients = {
        _first_email(str(item))
        for item in [
            *(message.get("to") or [] if isinstance(message.get("to"), list) else [message.get("to")]),
            *(message.get("cc") or [] if isinstance(message.get("cc"), list) else [message.get("cc")]),
            *(message.get("bcc") or [] if isinstance(message.get("bcc"), list) else [message.get("bcc")]),
        ]
        if item
    }
    recipients.discard("")
    if mailbox_email and sender and sender == mailbox_email:
        return "outbound"
    if mailbox_email and mailbox_email in recipients:
        return "inbound"
    return "unknown"


def counterparty_email_for_message(message: dict[str, Any], *, direction: str, mailbox: str) -> str:
    """Customer/counterparty email — never the operator mailbox on outbound."""
    if direction == "outbound":
        for item in message.get("to") or []:
            email = _first_email(str(item))
            if email and email != _first_email(mailbox):
                return email
        return ""
    return _first_email(str(message.get("sender") or message.get("from") or ""))


def source_kind_for_direction(direction: str) -> str:
    if direction == "outbound":
        return "gmail_outbound"
    return "gmail_inbound"


def build_ready_for_manual_send_receipt(*, draft_id: str = "", body_hash: str = "", draft_origin: str = "") -> dict[str, Any]:
    origin = str(draft_origin or "legacy_unknown").strip() or "legacy_unknown"
    return {
        "state": "ready_for_manual_send",
        "sent_at": "",
        "gmail_message_id": "",
        "thread_id": "",
        "draft_id": str(draft_id or ""),
        "body_hash": str(body_hash or ""),
        "draft_origin": origin,
    }


def build_communication_sent_receipt(
    *,
    gmail_message_id: str,
    thread_id: str = "",
    sent_at: str = "",
    draft_id: str = "",
    body_hash: str = "",
) -> dict[str, Any]:
    return {
        "state": "communication_sent",
        "sent_at": str(sent_at or datetime.now(timezone.utc).isoformat()),
        "gmail_message_id": str(gmail_message_id or "").strip(),
        "thread_id": str(thread_id or "").strip(),
        "draft_id": str(draft_id or ""),
        "body_hash": str(body_hash or ""),
    }


def should_apply_communication_sent(snapshot: Any) -> bool:
    """True when approve already marked manual delivery pending."""
    if snapshot is None:
        return False
    receipt = getattr(snapshot, "communication_receipt", None)
    state = str(getattr(receipt, "state", "") or "")
    if state == "ready_for_manual_send":
        return True
    if state == "communication_sent":
        return False
    hitl = getattr(snapshot, "hitl_gate", None)
    ops = getattr(snapshot, "operational_status", None)
    hitl_required = bool(getattr(hitl, "required", False)) if hitl is not None else False
    ops_code = str(getattr(ops, "code", "") or "")
    return (not hitl_required) and ops_code == "ready_for_quote"


def try_apply_communication_sent_receipt(
    *,
    case_id: str,
    thread_id: str,
    message_id: str,
    occurred_at: str,
    correlation_registry: Any | None,
    database_url: str = "",
    operator_store: Any | None = None,
) -> dict[str, Any]:
    """Best-effort close-loop: outbound observation → communication_sent on engagement."""
    cid = str(case_id or "").strip()
    mid = str(message_id or "").strip()
    if not cid or not mid:
        return {"ok": False, "reason": "missing_case_or_message"}

    engagement_id = ""
    if correlation_registry is not None:
        try:
            row = correlation_registry.lookup_by_case_id(cid)
            if isinstance(row, dict):
                engagement_id = str(row.get("engagement_id") or "").strip()
        except Exception:  # noqa: BLE001
            engagement_id = ""
    if not engagement_id:
        return {"ok": False, "reason": "engagement_not_found"}

    store = operator_store
    if store is None:
        try:
            from agent_runtime.agent_reconcile import build_operator_engagement_store
            from config import load_settings

            settings = load_settings(require_groq=False, require_google=False)
            store = build_operator_engagement_store(settings)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"operator_store_unavailable:{exc}"}

    try:
        snapshot = store.load_snapshot(engagement_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"load_failed:{exc}"}
    if snapshot is None:
        return {"ok": False, "reason": "snapshot_missing"}
    if not should_apply_communication_sent(snapshot):
        return {"ok": False, "reason": "not_awaiting_manual_send"}

    prev = getattr(snapshot, "communication_receipt", None)
    receipt = build_communication_sent_receipt(
        gmail_message_id=mid,
        thread_id=thread_id,
        sent_at=occurred_at,
        draft_id=str(getattr(prev, "draft_id", "") or ""),
        body_hash=str(getattr(prev, "body_hash", "") or ""),
    )
    try:
        from feed_visibility import clear_execution_attention
        from llm_contracts.engagement_snapshot_v2 import CommunicationReceipt, FeedVisibility

        updates: dict[str, Any] = {
            "communication_receipt": CommunicationReceipt(**receipt),
        }
        current_fv = snapshot.feed_visibility
        if current_fv is not None and bool(getattr(current_fv, "execution_attention", False)):
            try:
                updates["feed_visibility"] = FeedVisibility(**clear_execution_attention(current_fv))
            except Exception:  # noqa: BLE001
                cleared = current_fv.model_dump(mode="python")
                cleared["execution_attention"] = False
                cleared["execution_attention_reason"] = ""
                updates["feed_visibility"] = FeedVisibility(**cleared)
        patched = snapshot.model_copy(update=updates)
        store.save_snapshot(patched, expected_version=snapshot.version)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"save_failed:{exc}"}

    _emit_communication_sent_os_event(
        database_url=database_url or str(getattr(getattr(store, "settings", None), "mailbox_memory_database_url", "") or ""),
        engagement_id=engagement_id,
        case_id=cid,
        message_id=mid,
        thread_id=thread_id,
        occurred_at=occurred_at,
    )
    return {
        "ok": True,
        "engagement_id": engagement_id,
        "state": "communication_sent",
        "gmail_message_id": mid,
    }


def _emit_communication_sent_os_event(
    *,
    database_url: str,
    engagement_id: str,
    case_id: str,
    message_id: str,
    thread_id: str,
    occurred_at: str,
) -> None:
    db_url = str(database_url or "").strip()
    if not db_url:
        return
    try:
        from event_spine.emitter import publish_os_event

        publish_os_event(
            database_url=db_url,
            event_type="gmail.communication_sent",
            engagement_id=engagement_id,
            case_id=case_id,
            source_repo="gmail-agent",
            payload={
                "gmail_message_id": message_id,
                "thread_id": thread_id,
                "occurred_at": occurred_at,
                "delivery_mode": "manual_operator",
            },
            correlation={
                "case_id": case_id,
                "message_id": message_id,
                "thread_id": thread_id,
            },
        )
    except Exception:  # noqa: BLE001
        return


__all__ = [
    "infer_live_direction",
    "counterparty_email_for_message",
    "source_kind_for_direction",
    "build_ready_for_manual_send_receipt",
    "build_communication_sent_receipt",
    "should_apply_communication_sent",
    "try_apply_communication_sent_receipt",
]
