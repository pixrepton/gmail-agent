"""Desk item mapping (thin feed PR-E)."""

from __future__ import annotations

from typing import Any

from daszek_engagement_feed.case import (
    _snapshot_title,
    draft_reply_pl_from_snapshot,
    operator_essence_pl_from_snapshot,
    recommended_next_step_pl_from_snapshot,
    why_on_desk_pl_from_snapshot,
)
from daszek_engagement_feed.labels import (
    case_kind_ui_meta,
    operational_status_label,
)
from daszek_v3_operational_feed_contract import strip_forbidden_nested
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

DESK_OPERATIONAL_CODES = frozenset(
    {
        "pending_operator",
        "ready_for_quote",
        "enriching",
        "raw_inquiry",
        "node_a_error",
    }
)


def _desk_channel_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    m = dict(meta or {})
    attachments = [a for a in (m.get("attachments") or []) if isinstance(a, dict)]
    received_at = str(m.get("received_at") or "")
    sender_email = str(m.get("sender_email") or "")
    return {
        "channel": "email" if (sender_email or received_at or m.get("message_id")) else "",
        "sender_name": str(m.get("sender_name") or ""),
        "customer_email": sender_email,
        "received_at": received_at,
        "latest_signal_at": received_at,
        "source_message_id": str(m.get("message_id") or ""),
        "thread_id": str(m.get("thread_id") or ""),
        "attachments": attachments,
        "attachment_count": len(attachments),
        "has_attachments": bool(attachments),
    }


def snapshot_to_desk_item(
    snapshot: EngagementSnapshotV2, *, subject: str = "", meta: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    code = snapshot.operational_status.code
    if code not in DESK_OPERATIONAL_CODES and not snapshot.hitl_gate.required:
        return None
    essence = operator_essence_pl_from_snapshot(snapshot)
    family, family_label, business_area = case_kind_ui_meta(snapshot.case_kind)
    next_step = recommended_next_step_pl_from_snapshot(snapshot)
    channel = _desk_channel_meta(meta)
    source_signal_id = str(snapshot.signal_id or snapshot.trace_id or "").strip()
    return strip_forbidden_nested(
        {
            "note_id": f"desk-{snapshot.engagement_id}",
            "case_id": snapshot.case_id,
            "engagement_id": snapshot.engagement_id,
            "title": _snapshot_title(snapshot, subject=subject),
            "summary_pl": essence,
            "operator_essence_pl": essence,
            "hitl_pending": snapshot.hitl_gate.required,
            "presence_mode": "advisory" if snapshot.hitl_gate.required else "standard",
            "lifecycle": "active",
            "status": code,
            "operational_status": code,
            "status_label": operational_status_label(code),
            "current_state_label": operational_status_label(code),
            "family": family,
            "family_label": family_label,
            "business_area": business_area,
            "primary_next_action_title_pl": next_step,
            "recommended_next_step": next_step,
            "hitl_required": snapshot.hitl_gate.required,
            "hitl_reason": snapshot.hitl_gate.reason,
            "source_signal_ids": [source_signal_id] if source_signal_id else [],
            # F4: spójnie z kartą sprawy — draft/pytania/typ dostępne też z biurka.
            "case_kind": snapshot.case_kind,
            "draft_reply_pl": draft_reply_pl_from_snapshot(snapshot),
            "operator_questions_pl": [str(g.ask_pl or g.field) for g in snapshot.gaps[:6]],
            "hitl_action_id": "draft_reply",
            # A1: only populated when honestly available from a fresh, correlated
            # Understanding for this exact turn's signal — empty otherwise.
            "why_on_desk": why_on_desk_pl_from_snapshot(snapshot),
            # F5: nagłówek maila (nadawca / data / kanał / załączniki — tylko metadane).
            **channel,
        }
    )
