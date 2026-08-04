"""Case row + operator essence mapping (thin feed PR-E)."""

from __future__ import annotations

from typing import Any

from daszek_engagement_feed.labels import (
    case_kind_ui_meta,
    operational_status_label,
    primary_next_action_pl,
)
from daszek_v3_operational_feed_contract import strip_forbidden_nested
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2


def _strip(obj: Any) -> Any:
    return strip_forbidden_nested(obj)


def _snapshot_title(snapshot: EngagementSnapshotV2, *, subject: str = "") -> str:
    subj = str(subject or "").strip()
    if subj:
        return subj[:120]
    profile = snapshot.hvac_profile
    parts: list[str] = []
    if profile.location.city:
        parts.append(str(profile.location.city))
    if profile.heated_area_m2:
        parts.append(f"{profile.heated_area_m2} m²")
    if parts:
        return " — ".join(parts)
    return f"Sprawa {snapshot.case_id}"


def operator_essence_pl_from_snapshot(snapshot: EngagementSnapshotV2) -> str:
    """A1: prefer the freshly-correlated Understanding essence (set once per
    turn by graph._ground_current_signal, cleared when unavailable — see
    CaseUnderstandingProjection) over the agent's own tool-call trace note.
    Falls back honestly to the prior trace-based essence when Understanding
    is absent for the current signal — never fabricates a business summary.
    """
    cu = snapshot.case_understanding
    if cu is not None and str(cu.essence_pl or "").strip():
        return str(cu.essence_pl)[:400]
    trace = snapshot.agent_memory.reasoning_trace
    if trace:
        return str(trace[-1].summary_pl or "")[:400]
    draft = next((a for a in snapshot.actions if a.id == "draft_reply" and a.enabled), None)
    if draft and draft.payload_pl:
        return str(draft.payload_pl)[:400]
    return ""


def recommended_next_step_pl_from_snapshot(snapshot: EngagementSnapshotV2) -> str:
    """A1: prefer Understanding's next-best-action recommendation (already
    validated projection-safe, labelled a recommendation, not an approved
    action) over the deterministic status-based fallback in labels.py."""
    cu = snapshot.case_understanding
    if cu is not None and str(cu.recommended_next_step_pl or "").strip():
        return str(cu.recommended_next_step_pl)[:400]
    return primary_next_action_pl(snapshot)


def why_on_desk_pl_from_snapshot(snapshot: EngagementSnapshotV2) -> str:
    """A1: 'why is this on the operator's desk' — only populated when honestly
    available from Understanding; empty (not fabricated) otherwise, matching
    Daszek's existing omit-if-empty pattern for this field."""
    cu = snapshot.case_understanding
    if cu is not None and str(cu.why_pl or "").strip():
        return str(cu.why_pl)[:400]
    return ""


def why_on_desk_reason_codes_from_snapshot(snapshot: EngagementSnapshotV2) -> list[str]:
    """Roadmap 2.4: the MEMBERSHIP half of "why am I seeing this".

    `why_on_desk_pl_from_snapshot` answers why the case matters (business prose from
    Understanding). This answers the different question the operator also asks — why is this card
    on the desk at all — with the visibility reason codes that actually decided it, including the
    dynamic executive override. Empty when there is nothing honest to say.

    Reading `effective_visibility_mode` here is read-only: this function projects the decision, it
    does not participate in making it.
    """
    from feed_visibility import effective_visibility_mode

    _mode, reasons = effective_visibility_mode(snapshot)
    return [str(reason)[:80] for reason in reasons][:8]


def feed_visibility_mode_from_snapshot(snapshot: EngagementSnapshotV2) -> str:
    from feed_visibility import effective_visibility_mode

    mode, _reasons = effective_visibility_mode(snapshot)
    return mode


def case_understanding_status_from_snapshot(snapshot: EngagementSnapshotV2) -> dict[str, Any]:
    """SLICE-2C: pass the status through for DISPLAY. Never used for card membership."""
    status = getattr(snapshot, "case_understanding_status", None)
    if status is None:
        return {}
    payload = status.model_dump(mode="python")
    payload["operator_label_pl"] = str(payload.get("reason") or "")
    return payload


def what_changed_pl_from_snapshot(snapshot: EngagementSnapshotV2) -> str:
    """A1: 'what changed since last time' — only populated when honestly
    available from Understanding's thread_delta; empty otherwise."""
    cu = snapshot.case_understanding
    if cu is not None and str(cu.what_changed_pl or "").strip():
        return str(cu.what_changed_pl)[:400]
    return ""


def draft_reply_pl_from_snapshot(snapshot: EngagementSnapshotV2) -> str:
    """Treść draftu do wysyłki (jeśli agent ją przygotował) — UI bramkuje WYŚLIJ na jej obecności."""
    draft = next(
        (a for a in snapshot.actions if a.id == "draft_reply" and a.enabled and a.payload_pl),
        None,
    )
    return str(draft.payload_pl) if draft else ""


def _channel_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Sender / date / channel / attachments header fields (metadata only)."""
    m = dict(meta or {})
    attachments = [a for a in (m.get("attachments") or []) if isinstance(a, dict)]
    received_at = str(m.get("received_at") or "")
    sender_email = str(m.get("sender_email") or "")
    return {
        "channel": "email" if (sender_email or received_at or m.get("message_id")) else "",
        "sender_name": str(m.get("sender_name") or ""),
        "customer_email": sender_email,
        "received_at": received_at,
        "latest_message_at": received_at,
        "source_message_id": str(m.get("message_id") or ""),
        "thread_id": str(m.get("thread_id") or ""),
        "attachments": attachments,
        "attachment_count": len(attachments),
        "has_attachments": bool(attachments),
    }


def snapshot_to_feed_case(
    snapshot: EngagementSnapshotV2, *, subject: str = "", meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    profile = snapshot.hvac_profile
    gaps_pl = [str(g.ask_pl or g.field) for g in snapshot.gaps[:6]]
    essence = operator_essence_pl_from_snapshot(snapshot)
    family, family_label, business_area = case_kind_ui_meta(snapshot.case_kind)
    op_code = snapshot.operational_status.code
    next_step = recommended_next_step_pl_from_snapshot(snapshot)
    channel = _channel_meta(meta)
    cu = snapshot.case_understanding
    return _strip(
        {
            "case_id": snapshot.case_id,
            "engagement_id": snapshot.engagement_id,
            "title": _snapshot_title(snapshot, subject=subject),
            "summary_pl": essence,
            "operator_essence_pl": essence,
            "hitl_pending": snapshot.hitl_gate.required,
            "status": op_code,
            "operational_status": op_code,
            "status_label": operational_status_label(op_code),
            "current_state": op_code,
            "current_state_label": operational_status_label(op_code),
            "family": family,
            "family_label": family_label,
            "business_area": business_area,
            "business_priority": "high" if snapshot.hitl_gate.required else "medium",
            "operator_attention_class": (
                "hitl_required" if snapshot.hitl_gate.required else op_code
            ),
            "primary_next_action_title_pl": next_step,
            "recommended_next_step": next_step,
            "blocker_summary_pl": "; ".join(gaps_pl) if gaps_pl else "",
            "missing_info_summary_pl": "; ".join(gaps_pl[:3]),
            "heated_area_m2": profile.heated_area_m2,
            "city": profile.location.city,
            "cp2025_eligible": profile.cp2025_eligible,
            "completeness_gaps": [
                {"field": g.field, "severity": g.severity, "ask_pl": g.ask_pl} for g in snapshot.gaps
            ],
            "hitl_gate": snapshot.hitl_gate.model_dump(mode="python"),
            "agent_snapshot_version": snapshot.version,
            # F4: pola dla uczciwego panelu operatora w Daszku.
            "case_kind": snapshot.case_kind,
            "draft_reply_pl": draft_reply_pl_from_snapshot(snapshot),
            "operator_questions_pl": gaps_pl,
            "hitl_action_id": "draft_reply",
            "engagement_actions": [
                a.model_dump(mode="python") for a in (snapshot.actions or [])
            ],
            # A1: only populated when honestly available from a fresh, correlated
            # Understanding for this exact turn's signal — empty otherwise (never fabricated).
            "why_on_desk": why_on_desk_pl_from_snapshot(snapshot),
            # Roadmap 2.4: membership "why" next to the business "why" — never merged into the
            # prose field, so a consumer can render or ignore each independently.
            "why_on_desk_reason_codes": why_on_desk_reason_codes_from_snapshot(snapshot),
            "feed_visibility_mode": feed_visibility_mode_from_snapshot(snapshot),
            # SLICE-2C: display only.
            "case_understanding_status": case_understanding_status_from_snapshot(snapshot),
            "what_changed_pl": what_changed_pl_from_snapshot(snapshot),
            "risks": [r.model_dump(mode="python") for r in cu.risks] if cu is not None else [],
            "understanding_missing_critical_fields": list(cu.missing_critical_fields) if cu is not None else [],
            "understanding_current": cu is not None,
            # F5: nagłówek maila (nadawca / data / kanał / załączniki — tylko metadane).
            **channel,
        }
    )


def build_case_detail_from_engagement(
    snapshot: EngagementSnapshotV2,
    *,
    journal: Any = None,
    turns_builder: Any = None,
    subject: str = "",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    from daszek_engagement_feed.build import turns_from_snapshot_and_journal

    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    case_row = snapshot_to_feed_case(snapshot, subject=subject, meta=meta)
    turns = turns_from_snapshot_and_journal(snapshot, journal) if turns_builder is None else turns_builder(snapshot, journal)
    timeline = []
    for t in turns:
        tool_name = str(t.get("tool_name") or "")
        timeline.append(
            _strip(
                {
                    "occurred_at": str(t.get("created_at") or _utc_now_iso()),
                    "event_type": "agent_turn",
                    "event_type_label": f"Agent: {tool_name}" if tool_name else "Agent",
                    "summary_pl": str(t.get("turn_summary_pl") or t.get("tool_status") or ""),
                    "tool_name": tool_name,
                    "tool_status": t.get("tool_status"),
                    "tokens_used": int(t.get("tokens_used") or 0),
                }
            )
        )
    return _strip(
        {
            "ok": True,
            "generated_at": _utc_now_iso(),
            "view": "case_detail_agent_runtime",
            "case": case_row,
            "case_id": snapshot.case_id,
            "engagement_id": snapshot.engagement_id,
            "desk_notes": [],
            "signals": [],
            "decision_traces": [],
            "operational_timeline": timeline,
            "agent_turns": turns,
            "attachments": list(case_row.get("attachments") or []),
            "gaps": [g.model_dump(mode="python") for g in snapshot.gaps],
            "actions": [a.model_dump(mode="python") for a in snapshot.actions],
            "hitl_gate": snapshot.hitl_gate.model_dump(mode="python"),
            "hitl_pending": snapshot.hitl_gate.required,
            "hvac_profile": snapshot.hvac_profile.model_dump(mode="python"),
        }
    )
