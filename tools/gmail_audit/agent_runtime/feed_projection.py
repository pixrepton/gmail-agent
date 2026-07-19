"""Thin v2 projection bridge from EngagementSnapshot.v2 (PR-D → PR-E)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from dash_projection_v2 import validate_v2_shadow_projection
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2
from signal_contract import CanonicalSignal


def _preclassification_lane(signal: CanonicalSignal) -> str:
    pre = (signal.payload or {}).get("preclassification_result")
    if isinstance(pre, dict):
        return str(pre.get("lane") or "intake_llm")
    return "intake_llm"


def _presence_for_status(code: str, *, hitl: bool) -> str:
    if hitl or code in {"pending_operator", "node_a_error"}:
        return "advisory"
    if code == "ready_for_quote":
        return "standard"
    return "subtle"


def _desk_command(status_code: str) -> str:
    if status_code in {"pending_operator", "ready_for_quote"}:
        return "update"
    return "create"


def _summary_pl(engagement: EngagementSnapshotV2) -> str:
    profile = engagement.hvac_profile
    parts: list[str] = []
    if profile.heated_area_m2:
        parts.append(f"{profile.heated_area_m2} m²")
    if profile.location.city:
        parts.append(str(profile.location.city))
    if engagement.gaps:
        parts.append(f"braki: {len(engagement.gaps)}")
    draft = next((a for a in engagement.actions if a.id == "draft_reply" and a.enabled), None)
    if draft and draft.payload_pl:
        return str(draft.payload_pl)[:400]
    if parts:
        return "Agent: " + ", ".join(parts)
    return f"Agent runtime — status {engagement.operational_status.code}"


def build_v2_projection_from_engagement(
    engagement: EngagementSnapshotV2,
    *,
    signal: CanonicalSignal,
    intake_output: dict[str, Any] | None = None,
    case_key: str = "",
) -> dict[str, Any]:
    """Minimal Daszek-compatible v2 projection (validated)."""
    intake = dict(intake_output or {})
    message = dict(intake.get("message") or {})
    source = dict(intake.get("source") or {})
    if signal.source_kind == "drive":
        message_id = str(signal.source_ref.get("file_id") or signal.signal_id)
        thread_id = str(case_key or signal.case_key_hint or message_id)
    else:
        message_id = str(message.get("message_id") or signal.source_ref.get("message_id") or "")
        thread_id = str(message.get("thread_id") or signal.source_ref.get("thread_id") or signal.thread_key_hint or "")

    presence = _presence_for_status(
        engagement.operational_status.code,
        hitl=engagement.hitl_gate.required,
    )
    desk_command = _desk_command(engagement.operational_status.code)
    summary = _summary_pl(engagement)

    signal_projection: dict[str, Any] = {
        "signal_id": signal.signal_id,
        "observed_at": str(signal.observed_at or ""),
        "source_kind": str(signal.source_kind or "gmail"),
        "source_ref": {
            "mailbox": str(source.get("mailbox") or signal.source_ref.get("mailbox") or ""),
            "message_id": message_id,
            "thread_id": thread_id,
            "received_at": str(message.get("date") or signal.observed_at or ""),
        },
        "intake": {
            "decision_action": str((intake.get("decision") or {}).get("action") or "review"),
            "business_area": str(intake.get("business_area") or "operations"),
            "case_family": "hvac_sales",
            "preclassification_lane": _preclassification_lane(signal),
            "review_required": engagement.hitl_gate.required,
        },
        "agent_runtime": {
            "engagement_id": engagement.engagement_id,
            "version": engagement.version,
            "operational_status": engagement.operational_status.model_dump(mode="python"),
            "hvac_profile": engagement.hvac_profile.model_dump(mode="python"),
            "gaps_count": len(engagement.gaps),
            "tool_calls_count": len(engagement.agent_memory.tool_calls),
        },
    }

    case_patch: dict[str, Any] = {
        "command": "update_state" if engagement.case_id else "noop",
        "case_id": engagement.case_id,
        "case_key": case_key or signal.case_key_hint or "",
        "summary_pl": summary,
        "operational_status": engagement.operational_status.code,
        "latest_signal_id": signal.signal_id,
        "agent_engagement_id": engagement.engagement_id,
    }

    desk_note_patch: dict[str, Any] = {
        "command": desk_command,
        "presence_mode": presence,
        "lifecycle": "active",
        "source_signal_ids": [signal.signal_id],
        "case_id": engagement.case_id,
        "summary_pl": summary,
        "title_pl": str(signal.signal_summary_pl or summary)[:200],
        "hitl_required": engagement.hitl_gate.required,
        "hitl_reason": engagement.hitl_gate.reason,
    }

    decision_trace: dict[str, Any] = {
        "trigger_signal_id": signal.signal_id,
        "presence_mode": presence,
        "decision_type": "agent_runtime",
        "summary_pl": summary,
        "agent_turns": len(engagement.agent_memory.tool_calls),
    }

    projection = {
        "signal_projection": signal_projection,
        "case_patch": case_patch,
        "desk_note_patch": desk_note_patch,
        "decision_trace": decision_trace,
    }
    validate_v2_shadow_projection(projection)
    return projection


def build_operator_snapshot_from_engagement(
    engagement: EngagementSnapshotV2,
    *,
    signal: CanonicalSignal,
    intake_output: dict[str, Any] | None = None,
    case_key: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    v2 = build_v2_projection_from_engagement(
        engagement,
        signal=signal,
        intake_output=intake_output,
        case_key=case_key,
    )
    return {
        "schema_version": "operator_projection_snapshot.v1",
        "run_id": run_id or "agent-runtime",
        "v2_projection": v2,
        "canonical_signal_id": signal.signal_id,
        "agent_runtime": {
            "engagement_id": engagement.engagement_id,
            "snapshot_version": engagement.version,
        },
    }


# ── FAZA 3: konwergencja na kanoniczny kompozytor projekcji (za flagą) ─────────


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def projection_canonical_enabled() -> bool:
    """Flaga: tor agenta przez kanoniczny build_operator_projection_snapshot (LLM composer)."""
    return str(os.getenv("AGENT_PROJECTION_CANONICAL") or "").strip().lower() in {"1", "true", "yes", "on"}


def _draft_pl(engagement: EngagementSnapshotV2) -> str:
    draft = next(
        (a for a in engagement.actions if a.id == "draft_reply" and a.enabled and a.payload_pl),
        None,
    )
    return str(draft.payload_pl) if draft else ""


def _agent_ci_stub(engagement: EngagementSnapshotV2) -> dict[str, Any]:
    """Minimalny case_intelligence_result ze snapshotu agenta — żeby kompozytor miał esencję."""
    trace = engagement.agent_memory.reasoning_trace
    essence = str(trace[-1].summary_pl or "")[:400] if trace else ""
    return {
        "case_kind": engagement.case_kind,
        "essence_pl": _draft_pl(engagement)[:400] or essence,
        "gaps": [{"field": g.field, "severity": g.severity, "ask_pl": g.ask_pl} for g in engagement.gaps],
    }


def enrich_envelope_from_engagement(
    operator_snapshot: dict[str, Any],
    engagement: EngagementSnapshotV2,
) -> dict[str, Any]:
    """Nałóż na kanoniczny snapshot operatorskie pola ze stanu agenta (draft / pytania / case_kind)."""
    if not isinstance(operator_snapshot, dict):
        return operator_snapshot
    draft_pl = _draft_pl(engagement)
    questions = [str(g.ask_pl or g.field) for g in engagement.gaps[:6]]
    trace = engagement.agent_memory.reasoning_trace
    essence = draft_pl[:400] if draft_pl else (str(trace[-1].summary_pl or "")[:400] if trace else "")

    out = dict(operator_snapshot)
    env = out.get("projection_envelope")
    if isinstance(env, dict):
        env = dict(env)
        env["case_kind"] = engagement.case_kind
        env["draft_reply_pl"] = draft_pl
        env["operator_questions_pl"] = questions
        env["hitl_action_id"] = "draft_reply"
        cards = env.get("desk_cards")
        if isinstance(cards, list) and cards:
            first = dict(cards[0])
            if essence:
                first.setdefault("operator_essence_pl", essence)
                if not str(first.get("summary") or "").strip():
                    first["summary"] = essence
            env["desk_cards"] = [first, *cards[1:]]
        out["projection_envelope"] = env
    # Pola na poziomie snapshotu — dla nakładek feedu i prostych konsumentów.
    out["case_kind"] = engagement.case_kind
    out["draft_reply_pl"] = draft_pl
    out["operator_questions_pl"] = questions
    return out


def build_canonical_operator_snapshot(
    *,
    engagement: EngagementSnapshotV2,
    signal: CanonicalSignal,
    intake_output: dict[str, Any] | None,
    run_id: str,
    store: Any | None,
    settings: Any | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Kanoniczny operator_projection_snapshot: CaseContextPack → ContextTraySet → LLM composer → envelope."""
    from mailbox_memory_runtime import build_case_context_pack
    from projection_snapshot_transport import build_operator_projection_snapshot

    pack: dict[str, Any] = {}
    if store is not None:
        try:
            pack_obj = build_case_context_pack(store=store, case_id=engagement.case_id)
            pack = pack_obj.to_dict() if hasattr(pack_obj, "to_dict") else (pack_obj if isinstance(pack_obj, dict) else {})
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"agent_pack_build_failed:{type(exc).__name__}")
    stage_outputs = {
        "canonical_signal_id": signal.signal_id,
        "generated_at": _utc_now_iso(),
        "preclassification_result": {"lane": "intake_llm", "case_kind": engagement.case_kind},
        "mailbox_memory_result": {"case_id": engagement.case_id, "context_pack": pack},
        "case_intelligence_result": _agent_ci_stub(engagement),
        "agent_engagement_snapshot": engagement.model_dump(mode="python"),
        "reconcile_path": "agent_runtime",
    }
    # build_v2_shadow_projection oczekuje pełnego kształtu intake (thread/message/source/decision).
    # Tor agenta bywa cienki — uzupełnij wymagane klucze, by transport nie wywalał się na KeyError.
    intake = dict(intake_output) if isinstance(intake_output, dict) else {}
    intake.setdefault("thread", {})
    intake.setdefault("message", {})
    intake.setdefault("source", {})
    intake.setdefault("decision", {})
    operator_snapshot = build_operator_projection_snapshot(
        intake,
        stage_outputs=stage_outputs,
        run_id=run_id,
        settings=settings,
    )
    return enrich_envelope_from_engagement(operator_snapshot, engagement)
