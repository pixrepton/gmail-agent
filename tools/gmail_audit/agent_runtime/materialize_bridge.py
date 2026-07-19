"""TUM materialize bridge — operator approve → upsert_case → linked reconcile (RFC E2)."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from agent_runtime.materialize import execute_materialize_proposal
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import AgentConcurrencyError, OperatorEngagementStore
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2, MaterializeProposalItem


class MaterializeConflictError(RuntimeError):
    """Raised when snapshot version_id doesn't match — operator B changed state while operator A was approving."""
    def __init__(self, *, engagement_id: str, expected_version: int, current_version: int) -> None:
        self.engagement_id = engagement_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Snapshot version conflict for {engagement_id}: expected version {expected_version}, current {current_version}"
        )


def _find_proposal(snapshot: EngagementSnapshotV2, proposal_id: str) -> MaterializeProposalItem | None:
    pid = str(proposal_id or "").strip()
    if not pid:
        return None
    for item in snapshot.agent_memory.materialize_proposals:
        if str(item.proposal_id or "") == pid:
            return item
    return None


def _patch_proposals_status(
    snapshot: EngagementSnapshotV2,
    proposal_id: str,
    *,
    status: str,
) -> EngagementSnapshotV2:
    updated: list[MaterializeProposalItem] = []
    for item in snapshot.agent_memory.materialize_proposals:
        if str(item.proposal_id or "") == proposal_id:
            updated.append(item.model_copy(update={"status": status}))  # type: ignore[arg-type]
        else:
            updated.append(item)
    memory = snapshot.agent_memory.model_copy(update={"materialize_proposals": updated})
    return snapshot.model_copy(update={"agent_memory": memory})


def reconcile_linked_after_materialize(
    settings: Any,
    *,
    signal_id: str,
    case_id: str,
    engagement_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Replay original signal with case_id through linked agent reconcile path."""
    sid = str(signal_id or "").strip()
    cid = str(case_id or "").strip()
    if not sid or not cid:
        return {"ok": False, "skipped": True, "reason": "missing_signal_or_case_id"}
    try:
        from mailbox_memory_runtime import build_mailbox_memory_runtime
        from signal_contract import CanonicalSignal
        from signal_journal import SignalJournal
        from signal_reconciler import SignalRuntimeContext, reconcile_signal

        runtime = build_mailbox_memory_runtime(settings, allow_in_memory=False)
        if runtime is None:
            return {"ok": False, "skipped": True, "reason": "mailbox_runtime_unavailable"}
        row = runtime.store.fetch_signal(sid)
        if not row:
            return {"ok": False, "skipped": True, "reason": f"signal_not_found:{sid}"}
        signal = CanonicalSignal.from_dict(row)
        payload = dict(signal.payload or {})
        payload["case_id"] = cid
        payload["materialized_from_engagement"] = str(engagement_id or "")
        signal = replace(signal, payload=payload)
        journal = SignalJournal(runtime.store)
        ctx = SignalRuntimeContext(
            settings=settings,
            journal=journal,
            mailbox_memory_runtime=runtime,
            store=runtime.store,
            mode="active" if not dry_run else "shadow",
        )
        if dry_run:
            return {"ok": True, "dry_run": True, "case_id": cid, "signal_id": sid}
        result = reconcile_signal(signal, runtime_context=ctx, dry_run=False)
        return {
            "ok": True,
            "case_id": str(result.case_id or cid),
            "processing_state": str(result.processing_state or ""),
            "signal_id": sid,
            "warnings": list(result.warnings or [])[:20],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "signal_id": sid, "case_id": cid}


def approve_materialize_proposal(
    store: OperatorEngagementStore,
    *,
    engagement_id: str,
    proposal_id: str,
    operator_id: str = "",
    mailbox_store: Any | None = None,
    settings: Any | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Execute approved materialize proposal and trigger linked reconcile.

    Args:
        idempotency_key: Opcjonalny klucz idempotentności (PR-5B).
            Jeśli podany, operacja nie zostanie wykonana ponownie.
    """
    eid = str(engagement_id or "").strip()
    pid = str(proposal_id or "").strip()
    if not eid or not pid:
        return {"ok": False, "error": "engagement_id and proposal_id are required"}

    snapshot = store.load_snapshot(eid)
    if snapshot is None:
        return {"ok": False, "error": f"engagement not found: {eid}"}
    proposal = _find_proposal(snapshot, pid)
    if proposal is None:
        return {"ok": False, "error": f"proposal {pid!r} not found"}
    if str(proposal.status or "") != "pending":
        return {"ok": False, "error": f"proposal status is {proposal.status!r}, expected pending"}

    if settings is None:
        from config import load_settings

        settings = load_settings(require_groq=False, require_google=False)

    if mailbox_store is None:
        from mailbox_memory_runtime import build_mailbox_memory_runtime

        runtime = build_mailbox_memory_runtime(settings, allow_in_memory=False)
        mailbox_store = runtime.store if runtime is not None else None

    # Build correlation registry for Poziom 1 identity resolution
    correlation_store = None
    try:
        from agent_runtime.agent_reconcile import build_registry_for_reconcile
        correlation_store = build_registry_for_reconcile(settings)
    except Exception as exc:
        import logging; logging.getLogger("materialize_bridge").warning(
            "registry_not_available — materialize works without registry: %s", exc
        )

    exec_result = execute_materialize_proposal(
        mailbox_store=mailbox_store,
        proposal=proposal,
        engagement_snapshot=snapshot,
        correlation_store=correlation_store,
        idempotency_key=idempotency_key,
    )
    if str(exec_result.get("action") or "") == "composite_failed":
        return {
            "ok": False,
            "error": str(exec_result.get("error") or "composite_failed"),
            "materialize": exec_result,
            "engagement_id": eid,
            "proposal_id": pid,
        }

    case_id = str(exec_result.get("case_id") or "").strip()
    signal_id = str(snapshot.signal_id or snapshot.trace_id or "").strip()

    # PR-5A: CAS optimistic locking z retry i backoffem (Faza 5b)
    _CAS_MAX_RETRIES = 3
    _CAS_BACKOFF_BASE = 0.25
    current_snapshot = snapshot
    for cas_attempt in range(_CAS_MAX_RETRIES + 1):
        # Sprawdź wersję zanim zmienisz snapshot
        fresh_snapshot = store.load_snapshot(eid)
        if fresh_snapshot is not None and fresh_snapshot.version != current_snapshot.version:
            if cas_attempt < _CAS_MAX_RETRIES:
                # Ktoś nas wyprzedził — załaduj świeżą wersję i spróbuj ponownie
                delay = _CAS_BACKOFF_BASE * (2 ** cas_attempt)
                import time as _time
                _time.sleep(delay)
                current_snapshot = fresh_snapshot
                continue
            raise MaterializeConflictError(
                engagement_id=eid,
                expected_version=current_snapshot.version,
                current_version=fresh_snapshot.version,
            )
        break
    else:
        # Wszystkie retry wyczerpane
        return {"ok": False, "error": f"CAS conflict after {_CAS_MAX_RETRIES} retries — engagement {eid}"}

    patched = _patch_proposals_status(current_snapshot, pid, status="approved")
    delta: dict[str, Any] = {
        "hitl_gate": {"required": False, "reason": ""},
        "operational_status": {"code": "ready_for_quote", "blocking": False},
        "agent_memory": patched.agent_memory.model_dump(mode="python"),
    }
    if case_id:
        delta["case_id"] = case_id
    final_snap = apply_snapshot_delta(patched, delta)
    try:
        new_version = store.save_snapshot(final_snap, expected_version=current_snapshot.version)
    except AgentConcurrencyError as exc:
        return {"ok": False, "error": str(exc)}

    db_url = str(
        getattr(settings, "mailbox_memory_database_url", "")
        or os.environ.get("MAILBOX_MEMORY_DATABASE_URL")
        or ""
    ).strip()
    os_event_id = None

    # OS event — best-effort (event spine w shadow, brak automatycznych akcji)
    if db_url:
        from event_spine.emitter import publish_os_event

        os_event_id = publish_os_event(
            database_url=db_url,
            event_type="case_os.materialize.approved",
            engagement_id=eid,
            source_repo="gmail-agent",
            payload={
                "schema_version": "topinstal.os_event.v1",
                "summary_pl": f"Operator zatwierdził materializację ({proposal.proposal_type})",
                "proposal_id": pid,
                "proposal_type": str(proposal.proposal_type or ""),
                "case_id": case_id,
                "operator_id": str(operator_id or ""),
            },
            correlation={
                "case_id": case_id,
                "adjudication_kind": "materialize_proposal_approved",
                "approve_key": f"{eid}|{pid}|{operator_id}",
            },
        )

    reconcile_result: dict[str, Any] = {}
    ptype = str(proposal.proposal_type or "")
    if case_id and signal_id and ptype in {"create_case", "link_existing", "composite_plan"}:
        reconcile_result = reconcile_linked_after_materialize(
            settings,
            signal_id=signal_id,
            case_id=case_id,
            engagement_id=eid,
        )

    return {
        "ok": True,
        "engagement_id": eid,
        "proposal_id": pid,
        "operator_id": str(operator_id or ""),
        "version": new_version,
        "case_id": case_id,
        "materialize": exec_result,
        "reconcile": reconcile_result,
        "os_event_id": os_event_id,
        "adjudication": {
            "event_domain": "adjudication",
            "adjudication_kind": "materialize_proposal_approved",
            "case_id": case_id,
            "engagement_id": eid,
            "proposal_id": pid,
            "operator_id": str(operator_id or ""),
        },
    }


__all__ = [
    "approve_materialize_proposal",
    "reconcile_linked_after_materialize",
]
