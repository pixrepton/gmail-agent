"""TUM materialize bridge — operator approve → upsert_case → linked reconcile (RFC E2).

RP-26 / DQ-02 canonical sequence:
persist decision intent → execute retained effect → durable receipt → project.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from agent_runtime.materialize import execute_materialize_proposal
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import AgentConcurrencyError, OperatorEngagementStore
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2, MaterializeProposalItem

LIFECYCLE_KEY = "_dq02_lifecycle"
PHASE_INTENT = "intent_persisted"
PHASE_EFFECT = "effect_recorded"
PHASE_PROJECTED = "projected"


class MaterializeConflictError(RuntimeError):
    """Raised when snapshot version_id doesn't match — operator B changed state while operator A was approving."""

    def __init__(self, *, engagement_id: str, expected_version: int, current_version: int) -> None:
        self.engagement_id = engagement_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Snapshot version conflict for {engagement_id}: expected version {expected_version}, current {current_version}"
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_proposal(snapshot: EngagementSnapshotV2, proposal_id: str) -> MaterializeProposalItem | None:
    pid = str(proposal_id or "").strip()
    if not pid:
        return None
    for item in snapshot.agent_memory.materialize_proposals:
        if str(item.proposal_id or "") == pid:
            return item
    return None


def _lifecycle_of(proposal: MaterializeProposalItem) -> dict[str, Any]:
    raw = (proposal.payload_json or {}).get(LIFECYCLE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _patch_proposal(
    snapshot: EngagementSnapshotV2,
    proposal_id: str,
    *,
    status: str | None = None,
    lifecycle: dict[str, Any] | None = None,
) -> EngagementSnapshotV2:
    updated: list[MaterializeProposalItem] = []
    for item in snapshot.agent_memory.materialize_proposals:
        if str(item.proposal_id or "") != proposal_id:
            updated.append(item)
            continue
        payload = dict(item.payload_json or {})
        if lifecycle is not None:
            payload[LIFECYCLE_KEY] = dict(lifecycle)
        patch: dict[str, Any] = {"payload_json": payload}
        if status is not None:
            patch["status"] = status
        updated.append(item.model_copy(update=patch))  # type: ignore[arg-type]
    memory = snapshot.agent_memory.model_copy(update={"materialize_proposals": updated})
    return snapshot.model_copy(update={"agent_memory": memory})


def _save_cas(
    store: OperatorEngagementStore,
    *,
    engagement_id: str,
    snapshot: EngagementSnapshotV2,
    patched: EngagementSnapshotV2,
) -> tuple[EngagementSnapshotV2, int]:
    """Persist patched snapshot with short CAS retry. Returns (saved_snap_with_version, new_version)."""
    _CAS_MAX_RETRIES = 3
    _CAS_BACKOFF_BASE = 0.05
    current_snapshot = snapshot
    current_patched = patched
    for cas_attempt in range(_CAS_MAX_RETRIES + 1):
        fresh_snapshot = store.load_snapshot(engagement_id)
        if fresh_snapshot is not None and fresh_snapshot.version != current_snapshot.version:
            if cas_attempt < _CAS_MAX_RETRIES:
                delay = _CAS_BACKOFF_BASE * (2**cas_attempt)
                import time as _time

                _time.sleep(delay)
                prop = None
                for item in current_patched.agent_memory.materialize_proposals:
                    fresh_item = _find_proposal(fresh_snapshot, str(item.proposal_id or ""))
                    if fresh_item is None:
                        continue
                    if fresh_item.status != item.status or fresh_item.payload_json != item.payload_json:
                        prop = item
                        break
                if prop is None:
                    prop = next(iter(current_patched.agent_memory.materialize_proposals), None)
                if prop is None:
                    current_snapshot = fresh_snapshot
                    current_patched = fresh_snapshot
                    continue
                current_snapshot = fresh_snapshot
                current_patched = _patch_proposal(
                    fresh_snapshot,
                    str(prop.proposal_id),
                    status=str(prop.status),
                    lifecycle=_lifecycle_of(prop) or None,
                )
                continue
            raise MaterializeConflictError(
                engagement_id=engagement_id,
                expected_version=current_snapshot.version,
                current_version=fresh_snapshot.version,
            )
        try:
            new_version = store.save_snapshot(current_patched, expected_version=current_snapshot.version)
        except AgentConcurrencyError as exc:
            if cas_attempt < _CAS_MAX_RETRIES:
                continue
            raise MaterializeConflictError(
                engagement_id=engagement_id,
                expected_version=current_snapshot.version,
                current_version=-1,
            ) from exc
        saved = current_patched.model_copy(update={"version": new_version})
        return saved, new_version
    return current_patched, int(current_patched.version)


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


def _project_approved(
    store: OperatorEngagementStore,
    *,
    snapshot: EngagementSnapshotV2,
    engagement_id: str,
    proposal_id: str,
    proposal: MaterializeProposalItem,
    exec_result: dict[str, Any],
    operator_id: str,
    settings: Any,
) -> dict[str, Any]:
    case_id = str(exec_result.get("case_id") or "").strip()
    lifecycle = _lifecycle_of(proposal)
    lifecycle["phase"] = PHASE_PROJECTED
    lifecycle["projected_at"] = _utc_now()
    if exec_result and not lifecycle.get("effect_receipt"):
        lifecycle["effect_receipt"] = dict(exec_result)

    patched = _patch_proposal(
        snapshot,
        proposal_id,
        status="approved",
        lifecycle=lifecycle,
    )
    delta: dict[str, Any] = {
        "hitl_gate": {"required": False, "reason": ""},
        "operational_status": {"code": "ready_for_quote", "blocking": False},
        "agent_memory": patched.agent_memory.model_dump(mode="python"),
    }
    if case_id:
        delta["case_id"] = case_id
    final_snap = apply_snapshot_delta(patched, delta)
    try:
        saved, new_version = _save_cas(
            store,
            engagement_id=engagement_id,
            snapshot=snapshot,
            patched=final_snap,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"projection_failed_after_receipt: {exc}",
            "engagement_id": engagement_id,
            "proposal_id": proposal_id,
            "case_id": case_id,
            "materialize": exec_result,
            "lifecycle_phase": PHASE_EFFECT,
        }

    db_url = str(
        getattr(settings, "mailbox_memory_database_url", "")
        or os.environ.get("MAILBOX_MEMORY_DATABASE_URL")
        or ""
    ).strip()
    os_event_id = None
    if db_url:
        from event_spine.emitter import publish_os_event

        os_event_id = publish_os_event(
            database_url=db_url,
            event_type="case_os.materialize.approved",
            engagement_id=engagement_id,
            source_repo="gmail-agent",
            payload={
                "schema_version": "topinstal.os_event.v1",
                "summary_pl": f"Operator zatwierdzil materializacje ({proposal.proposal_type})",
                "proposal_id": proposal_id,
                "proposal_type": str(proposal.proposal_type or ""),
                "case_id": case_id,
                "operator_id": str(operator_id or ""),
            },
            correlation={
                "case_id": case_id,
                "adjudication_kind": "materialize_proposal_approved",
                "approve_key": f"{engagement_id}|{proposal_id}|{operator_id}",
            },
        )

    signal_id = str(saved.signal_id or saved.trace_id or "").strip()
    reconcile_result: dict[str, Any] = {}
    ptype = str(proposal.proposal_type or "")
    if case_id and signal_id and ptype in {"composite_plan"}:
        reconcile_result = reconcile_linked_after_materialize(
            settings,
            signal_id=signal_id,
            case_id=case_id,
            engagement_id=engagement_id,
        )

    return {
        "ok": True,
        "engagement_id": engagement_id,
        "proposal_id": proposal_id,
        "operator_id": str(operator_id or ""),
        "version": new_version,
        "case_id": case_id,
        "materialize": exec_result,
        "reconcile": reconcile_result,
        "os_event_id": os_event_id,
        "lifecycle_phase": PHASE_PROJECTED,
        "adjudication": {
            "event_domain": "adjudication",
            "adjudication_kind": "materialize_proposal_approved",
            "case_id": case_id,
            "engagement_id": engagement_id,
            "proposal_id": proposal_id,
            "operator_id": str(operator_id or ""),
        },
    }


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
        idempotency_key: Optional idempotency key (PR-5B / RP-26).
            When set, a durable receipt store (db_url) is required — fail-closed.
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

    if settings is None:
        from config import load_settings

        settings = load_settings(require_groq=False, require_google=False)

    db_url = str(
        getattr(settings, "mailbox_memory_database_url", "")
        or os.environ.get("MAILBOX_MEMORY_DATABASE_URL")
        or ""
    ).strip()
    key = str(idempotency_key or "").strip() or None
    if key and not db_url:
        return {
            "ok": False,
            "error": "idempotency_key requires mailbox_memory_database_url; refusing silent noop",
            "engagement_id": eid,
            "proposal_id": pid,
        }

    status = str(proposal.status or "")
    lifecycle = _lifecycle_of(proposal)
    phase = str(lifecycle.get("phase") or "")
    receipt = lifecycle.get("effect_receipt")
    if isinstance(receipt, dict) and receipt:
        if status == "approved" and phase == PHASE_PROJECTED:
            return {
                "ok": True,
                "engagement_id": eid,
                "proposal_id": pid,
                "operator_id": str(operator_id or ""),
                "version": snapshot.version,
                "case_id": str(receipt.get("case_id") or snapshot.case_id or ""),
                "materialize": dict(receipt),
                "reconcile": {},
                "os_event_id": None,
                "lifecycle_phase": PHASE_PROJECTED,
                "replayed_from_receipt": True,
            }
        return _project_approved(
            store,
            snapshot=snapshot,
            engagement_id=eid,
            proposal_id=pid,
            proposal=proposal,
            exec_result=dict(receipt),
            operator_id=operator_id,
            settings=settings,
        )

    if status == "approved":
        return {"ok": False, "error": f"proposal status is {status!r}, expected pending"}
    if status == "rejected":
        return {"ok": False, "error": f"proposal status is {status!r}, expected pending"}
    if status != "pending":
        return {"ok": False, "error": f"proposal status is {status!r}, expected pending"}

    if mailbox_store is None:
        from mailbox_memory_runtime import build_mailbox_memory_runtime

        runtime = build_mailbox_memory_runtime(settings, allow_in_memory=False)
        mailbox_store = runtime.store if runtime is not None else None

    # 1) Persist decision intent BEFORE any side effect.
    if phase != PHASE_INTENT:
        intent_lifecycle = {
            "phase": PHASE_INTENT,
            "operator_id": str(operator_id or ""),
            "intent_at": _utc_now(),
            "idempotency_key": key or "",
        }
        intent_snap = _patch_proposal(snapshot, pid, status="pending", lifecycle=intent_lifecycle)
        try:
            snapshot, _ = _save_cas(
                store,
                engagement_id=eid,
                snapshot=snapshot,
                patched=intent_snap,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"intent_persist_failed: {exc}", "engagement_id": eid, "proposal_id": pid}
        proposal = _find_proposal(snapshot, pid)
        if proposal is None:
            return {"ok": False, "error": f"proposal {pid!r} missing after intent persist"}

    correlation_store = None
    try:
        from agent_runtime.agent_reconcile import build_registry_for_reconcile

        # Skip registry when no mailbox DB is configured (unit/harness paths).
        if db_url:
            correlation_store = build_registry_for_reconcile(settings)
    except Exception as exc:
        import logging

        logging.getLogger("materialize_bridge").warning(
            "registry_not_available — materialize works without registry: %s", exc
        )

    # 2) Execute retained side effect.
    exec_result = execute_materialize_proposal(
        mailbox_store=mailbox_store,
        proposal=proposal,
        engagement_snapshot=snapshot,
        correlation_store=correlation_store,
        idempotency_key=key,
        db_url=db_url if db_url is not None else "",
    )
    if str(exec_result.get("action") or "") == "composite_failed" or str(exec_result.get("status") or "") == "error":
        fail_lifecycle = _lifecycle_of(proposal)
        fail_lifecycle["phase"] = PHASE_EFFECT
        fail_lifecycle["effect_receipt"] = dict(exec_result)
        fail_lifecycle["effect_at"] = _utc_now()
        fail_lifecycle["effect_ok"] = False
        fail_snap = _patch_proposal(snapshot, pid, status="pending", lifecycle=fail_lifecycle)
        try:
            store.save_snapshot(fail_snap, expected_version=snapshot.version)
        except Exception:
            pass
        return {
            "ok": False,
            "error": str(exec_result.get("error") or exec_result.get("summary") or "materialize_failed"),
            "materialize": exec_result,
            "engagement_id": eid,
            "proposal_id": pid,
            "lifecycle_phase": PHASE_EFFECT,
        }

    # 3) Persist durable effect receipt BEFORE projection / post-effect work.
    effect_lifecycle = _lifecycle_of(proposal)
    effect_lifecycle["phase"] = PHASE_EFFECT
    effect_lifecycle["effect_receipt"] = dict(exec_result)
    effect_lifecycle["effect_at"] = _utc_now()
    effect_lifecycle["effect_ok"] = True
    receipt_snap = _patch_proposal(snapshot, pid, status="pending", lifecycle=effect_lifecycle)
    try:
        snapshot, _ = _save_cas(
            store,
            engagement_id=eid,
            snapshot=snapshot,
            patched=receipt_snap,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"receipt_persist_failed_after_effect: {exc}",
            "engagement_id": eid,
            "proposal_id": pid,
            "materialize": exec_result,
            "lifecycle_phase": PHASE_INTENT,
            "warning": "side_effect_may_have_run_without_durable_receipt",
        }

    proposal = _find_proposal(snapshot, pid) or proposal

    # 4) Project approved state.
    return _project_approved(
        store,
        snapshot=snapshot,
        engagement_id=eid,
        proposal_id=pid,
        proposal=proposal,
        exec_result=exec_result,
        operator_id=operator_id,
        settings=settings,
    )


__all__ = [
    "approve_materialize_proposal",
    "reconcile_linked_after_materialize",
    "MaterializeConflictError",
    "LIFECYCLE_KEY",
]
