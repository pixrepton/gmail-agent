"""Canonical case-state rebuild paths for signal runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class CaseStateRebuildResult:
    case_id: str
    updated_case_state: dict[str, Any] = field(default_factory=dict)
    updated_snapshot: dict[str, Any] = field(default_factory=dict)
    updated_next_action: dict[str, Any] = field(default_factory=dict)
    updated_intelligence_package: dict[str, Any] = field(default_factory=dict)
    updated_guidance: dict[str, Any] = field(default_factory=dict)
    update_reasons: list[str] = field(default_factory=list)
    source_refs_used: list[dict[str, Any]] = field(default_factory=list)
    rebuild_mode: str = "incremental_refresh"
    projection_refresh_decision: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def incremental_refresh(
    *,
    case_id: str,
    runtime_context: Any,
    reconcile_result: Any | None = None,
) -> CaseStateRebuildResult:
    store = runtime_context.resolved_store
    case_row = store.fetch_case(case_id) if store is not None and case_id else {}
    snapshot_row = store.fetch_snapshot(case_id) if store is not None and case_id else {}
    next_action = store.fetch_next_action(case_id) if store is not None and case_id else {}
    snapshot_payload = snapshot_row.get("snapshot_json", snapshot_row) if isinstance(snapshot_row, dict) else {}
    stage_outputs = reconcile_result.stage_outputs if reconcile_result is not None else {}
    intelligence = dict(stage_outputs.get("case_intelligence_result") or {})
    return CaseStateRebuildResult(
        case_id=case_id,
        updated_case_state=case_row or {},
        updated_snapshot=snapshot_payload if isinstance(snapshot_payload, dict) else {},
        updated_next_action=next_action or {},
        updated_intelligence_package=intelligence,
        updated_guidance=dict((intelligence or {}).get("case_guidance") or {}),
        update_reasons=list((reconcile_result.rebuild_result or {}).get("update_reasons") or ["incremental_refresh"]),
        source_refs_used=list((reconcile_result.rebuild_result or {}).get("source_refs_used") or []),
        rebuild_mode="incremental_refresh",
        projection_refresh_decision=reconcile_result.projection_refresh_decision.to_dict() if reconcile_result and reconcile_result.projection_refresh_decision else {},
    )


def projection_only_refresh(
    *,
    case_id: str,
    runtime_context: Any,
) -> CaseStateRebuildResult:
    store = runtime_context.resolved_store
    snapshot_row = store.fetch_snapshot(case_id) if store is not None and case_id else {}
    snapshot_payload = snapshot_row.get("snapshot_json", snapshot_row) if isinstance(snapshot_row, dict) else {}
    return CaseStateRebuildResult(
        case_id=case_id,
        updated_case_state=store.fetch_case(case_id) if store is not None and case_id else {},
        updated_snapshot=snapshot_payload if isinstance(snapshot_payload, dict) else {},
        updated_next_action=store.fetch_next_action(case_id) if store is not None and case_id else {},
        update_reasons=["projection_only_refresh"],
        rebuild_mode="projection_only_refresh",
    )


def case_rebuild_from_journal(
    *,
    case_id: str,
    runtime_context: Any,
    case_key_hint: str = "",
) -> CaseStateRebuildResult:
    from signal_reconciler import reconcile_signal

    replay_context = runtime_context.for_replay()
    signals = replay_context.journal.fetch_signals_for_case(case_id=case_id, case_key_hint=case_key_hint, limit=500)
    latest_result = None
    for signal in signals:
        latest_result = reconcile_signal(signal, runtime_context=replay_context, dry_run=False)
    if latest_result is None:
        return CaseStateRebuildResult(case_id=case_id, rebuild_mode="case_rebuild_from_journal", update_reasons=["no_signals_found"])
    result = incremental_refresh(case_id=case_id, runtime_context=replay_context, reconcile_result=latest_result)
    result.rebuild_mode = "case_rebuild_from_journal"
    return result


__all__ = [
    "CaseStateRebuildResult",
    "case_rebuild_from_journal",
    "incremental_refresh",
    "projection_only_refresh",
]
