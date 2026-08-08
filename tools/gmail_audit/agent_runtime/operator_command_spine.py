"""OperatorCommand → SignalJournal → reconcile → receipt (AI-OS 6.3)."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from agent_runtime.command_receipt import build_command_receipt
from agent_runtime.operator_command import OperatorCommand
from signal_contract import build_canonical_signal

logger = logging.getLogger(__name__)


def _snapshot_from_reconcile_result(reconcile_result: Any) -> Any | None:
    stage_outputs = getattr(reconcile_result, "stage_outputs", None)
    if not isinstance(stage_outputs, dict):
        return None
    raw_snapshot = stage_outputs.get("agent_engagement_snapshot")
    if raw_snapshot is None:
        return None
    try:
        from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

        if isinstance(raw_snapshot, EngagementSnapshotV2):
            return raw_snapshot
        if isinstance(raw_snapshot, dict):
            return EngagementSnapshotV2.model_validate(raw_snapshot)
    except Exception as exc:  # noqa: BLE001 - receipt mapping must not hide reconcile success
        logger.warning("operator_command_spine: snapshot hydration failed: %s", exc)
    return None


def _proposal_payloads(snapshot_eng: Any | None) -> list[dict[str, Any]]:
    if not snapshot_eng or not getattr(snapshot_eng, "agent_memory", None):
        return []
    return [
        {
            "proposal_id": p.proposal_id,
            "proposal_type": p.proposal_type,
            "status": p.status,
            "payload": p.payload_json,
        }
        for p in snapshot_eng.agent_memory.materialize_proposals
    ]


def _engagement_id_from_reconcile_result(reconcile_result: Any, snapshot_eng: Any | None) -> str:
    if snapshot_eng is not None:
        engagement_id = str(getattr(snapshot_eng, "engagement_id", "") or "").strip()
        if engagement_id:
            return engagement_id
    for attr in ("mailbox_memory_result", "rebuild_result", "preview"):
        payload = getattr(reconcile_result, attr, None)
        if isinstance(payload, dict):
            engagement_id = str(payload.get("engagement_id") or "").strip()
            if engagement_id:
                return engagement_id
    return ""


def run_operator_command_spine(
    *,
    user_input: str,
    session_id: str,
    case_id: str,
    opmem_context: dict[str, Any],
    settings: Any,
    operator_scope: Any = None,
    command_id: str = "",
) -> dict[str, Any]:
    """Shared spine for sync and async agent chat."""
    from datetime import datetime, timezone

    command = OperatorCommand(
        user_input=user_input,
        session_id=session_id,
        case_id=case_id,
        operator_id=str(getattr(operator_scope, "operator_id", "") or "default"),
        command_id=command_id or "",
    )
    now = datetime.now(timezone.utc).isoformat()
    signal = build_canonical_signal(
        signal_kind="operator_command",
        source_kind="operator_command",
        source_ref={"session_id": session_id, "case_id": case_id, "command_id": command.command_id}
        if case_id
        else {"session_id": session_id, "command_id": command.command_id},
        observed_at=now,
        signal_summary_pl=user_input[:120],
        revision_marker=command.idempotency_key,
        payload=command.to_signal_payload(
            operator_memory_context=str(opmem_context.get("prompt") or ""),
        ),
    )

    journal_inserted = False
    journal_duplicate = False
    runtime = None
    journal = None
    try:
        from mailbox_memory_runtime import build_mailbox_memory_runtime
        from signal_journal import SignalJournal

        runtime = build_mailbox_memory_runtime(settings)
        if runtime is not None:
            runtime.bootstrap()
            journal = SignalJournal(runtime.store)
            append_result = journal.append(signal)
            journal_inserted = bool(append_result.inserted)
            journal_duplicate = not append_result.inserted
            if journal_duplicate:
                signal = append_result.signal
    except Exception as exc:
        logger.warning("operator_command_spine: journal append skipped: %s", exc)

    try:
        from signal_reconciler import ReconcileResult, SignalRuntimeContext, reconcile_signal

        if journal is None:
            raise RuntimeError("signal journal unavailable for operator_command")

        runtime_ctx = SignalRuntimeContext(
            settings=settings,
            journal=journal,
            mailbox_memory_runtime=runtime,
            store=runtime.store if runtime is not None else None,
            graph_store=getattr(runtime, "graph_store", None) if runtime is not None else None,
            run_state={},
            mode="prep",
            verbose=False,
            persist_entity_links=False,
        )
        if operator_scope is not None:
            runtime_ctx.operator_scope = operator_scope

        if journal_duplicate:
            reconcile_result = ReconcileResult(
                signal_id=signal.signal_id,
                source_kind=signal.source_kind,
                signal_kind=signal.signal_kind,
                processing_state="skipped_duplicate",
                warnings=["signal_journal_duplicate_skipped_reconcile"],
            )
        else:
            reconcile_result = reconcile_signal(signal, runtime_context=runtime_ctx, dry_run=False)
    except Exception as exc:
        logger.error("operator_command_spine: reconcile failed: %s", exc)
        receipt = build_command_receipt(
            command_id=command.command_id,
            signal_id=getattr(signal, "signal_id", ""),
            status="failed",
            error=str(exc),
            journal_inserted=journal_inserted,
        )
        return {
            "command_id": command.command_id,
            "signal_id": getattr(signal, "signal_id", ""),
            "engagement_id": "",
            "warnings": [str(exc)],
            "turns": [{"role": "assistant", "content": f"Blad: {exc}"}],
            "proposals": [],
            "snapshot_eng": None,
            "receipt": receipt,
            "journal_inserted": journal_inserted,
            "journal_duplicate": journal_duplicate,
        }

    snapshot_eng = _snapshot_from_reconcile_result(reconcile_result)
    proposals = _proposal_payloads(snapshot_eng)
    engagement_id = _engagement_id_from_reconcile_result(reconcile_result, snapshot_eng)
    warnings = list(getattr(reconcile_result, "warnings", []) or [])

    hitl_required = bool(snapshot_eng and snapshot_eng.hitl_gate and snapshot_eng.hitl_gate.required)
    proposal_ids = [str(p.get("proposal_id") or "") for p in proposals if p.get("proposal_id")]
    receipt_status = "hitl_required" if hitl_required else "completed"
    receipt = build_command_receipt(
        command_id=command.command_id,
        signal_id=signal.signal_id,
        status=receipt_status,
        engagement_id=engagement_id,
        proposal_ids=proposal_ids,
        hitl_required=hitl_required,
        journal_inserted=journal_inserted,
        warnings=warnings,
    )

    turns = [
        {"role": "assistant", "content": f"{p.get('proposal_type', 'action')}: {p.get('status', 'done')}"}
        for p in proposals
    ] or [{"role": "assistant", "content": "Przyjalem polecenie."}]

    if snapshot_eng and getattr(snapshot_eng, "user_instruction", None):
        instr_hash = hashlib.sha256(str(snapshot_eng.user_instruction).encode("utf-8")).hexdigest()[:16]
        logger.info(
            "OPERATOR_INSTRUCTION_ENVELOPE",
            extra={"x": {"command_id": command.command_id, "instruction_hash": instr_hash, "present": True}},
        )

    return {
        "command_id": command.command_id,
        "signal_id": signal.signal_id,
        "engagement_id": engagement_id,
        "warnings": warnings,
        "turns": turns,
        "proposals": proposals,
        "snapshot_eng": snapshot_eng,
        "receipt": receipt,
        "journal_inserted": journal_inserted,
        "journal_duplicate": journal_duplicate,
    }


__all__ = ["run_operator_command_spine"]
