"""Bounded Gmail send executor for operator HITL draft_reply actions."""

from __future__ import annotations

import hashlib
import os
import re
from email.message import EmailMessage
from typing import Any, Callable

from llm_contracts.engagement_snapshot_v2 import ActionItem, EngagementSnapshotV2
from config import Settings

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
GOOGLE_WRITE_DISABLED_REASON = "google_write_disabled_manual_operator_only"


class SendTargetResolutionError(RuntimeError):
    """Fail-closed target resolution error raised before any Gmail side effect."""


def _has_gmail_send_scope(settings: Settings) -> bool:
    scopes = [str(s).strip() for s in (getattr(settings, "google_oauth_scopes", None) or []) if str(s).strip()]
    return GMAIL_SEND_SCOPE in scopes or GMAIL_COMPOSE_SCOPE in scopes


def _action_draft(snapshot: EngagementSnapshotV2, action_id: str) -> ActionItem | None:
    for action in snapshot.actions:
        if str(action.id) == action_id and action.enabled:
            return action
    return None


def _extract_email(value: str) -> str:
    match = _EMAIL_RE.search(str(value or ""))
    return match.group(0).strip() if match else ""


def _resolve_send_target(
    *,
    settings: Settings,
    snapshot: EngagementSnapshotV2,
    case_id: str,
) -> dict[str, Any]:
    if not str(getattr(settings, "mailbox_memory_database_url", "") or "").strip():
        raise SendTargetResolutionError("mailbox_memory_database_url_required")

    override_to = str(os.environ.get("AGENT_HITL_SEND_TO") or "").strip()
    if override_to:
        return {"to": override_to, "thread_id": "", "source": "env_override"}

    thread_id = ""
    to_addr = ""
    try:
        from mailbox_memory_runtime import build_mailbox_memory_runtime

        runtime = build_mailbox_memory_runtime(settings, allow_in_memory=False)
        if runtime is None:
            raise SendTargetResolutionError("durable_mailbox_runtime_unavailable")
        runtime.bootstrap()
        pack = runtime.get_context_pack(case_id=case_id or str(snapshot.case_id or ""), query_text="")
        if isinstance(pack, dict):
            intake = pack.get("intake_output") if isinstance(pack.get("intake_output"), dict) else {}
            message = intake.get("message") if isinstance(intake.get("message"), dict) else {}
            thread_id = str(message.get("thread_id") or "").strip()
            sender = str(message.get("from") or message.get("sender") or "").strip()
            to_addr = _extract_email(sender)
            if not to_addr:
                for fact in pack.get("facts") or []:
                    if not isinstance(fact, dict):
                        continue
                    fk = str(fact.get("field_key") or fact.get("key") or "").lower()
                    if "email" in fk or "contact" in fk:
                        to_addr = _extract_email(str(fact.get("value") or ""))
                        if to_addr:
                            break
    except SendTargetResolutionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SendTargetResolutionError("mailbox_memory_lookup_failed") from exc

    return {"to": to_addr, "thread_id": thread_id, "source": "mailbox_memory"}


def _build_mime_message(*, to_addr: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["Subject"] = subject or "Re: Twoje zapytanie — TOP-INSTAL"
    msg.set_content(body)
    return msg


def execute_hitl_gmail_send(
    *,
    settings: Settings,
    snapshot: EngagementSnapshotV2,
    action_id: str,
    case_id: str = "",
    operator_id: str = "",
    on_effect_start: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Fail-closed tombstone for legacy Gmail send entrypoints.

    Node B is read-only for Gmail and Google Calendar. The approved draft stays
    durable in Node B/Daszek for manual operator delivery, but this function
    must never start a real side effect and must never report dry-run success.
    """

    action = _action_draft(snapshot, action_id)
    if action is None:
        return {
            "executed": False,
            "reason": f"action_not_enabled:{action_id}",
            "effect_started": False,
            "decision_status": "failed_before_execution",
        }

    body = str(action.payload_pl or "").strip()
    if not body:
        return {"executed": False, "reason": "draft_body_empty", "effect_started": False, "decision_status": "failed_before_execution"}

    resolved_case = str(case_id or snapshot.case_id or "").strip()
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return {
        "executed": False,
        "reason": GOOGLE_WRITE_DISABLED_REASON,
        "effect_started": False,
        "decision_status": "failed_before_execution",
        "delivery_mode": "manual_operator",
        "draft_sha256": digest,
        "operator_id": operator_id,
        "case_id": resolved_case,
    }


__all__ = ["execute_hitl_gmail_send", "GMAIL_SEND_SCOPE"]
