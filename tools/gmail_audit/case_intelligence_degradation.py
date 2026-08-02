"""DQ-18: durable degradation state for a case's Case Intelligence stage.

Core reconciliation (intake, case linking, durable Case OS persistence, operator
projection) and Case Intelligence enrichment are separate state dimensions. When
Case Intelligence raises, `intake_shared_downstream.fallback_case_intelligence_result`
already produces a structurally-degraded result — this module is what makes that
degradation durable, visible to the operator, and retryable without rerunning the
whole signal or repeating any effect.

No new store, queue or scheduler: state lives in the existing `mailbox_memory_cases`
row's `metadata` JSONB column, mutated through the existing CAS-safe `mutate_case`
(advisory lock + SELECT ... FOR UPDATE + single transaction). Retry reuses the
existing `signal_reconciler.replay_signal` / journal replay path.
"""

from __future__ import annotations

from typing import Any

DEGRADATION_MAX_ATTEMPTS = 3
_METADATA_KEY = "case_intelligence_degradation"


def _empty_state() -> dict[str, Any]:
    return {
        "degraded": False,
        "attempts": 0,
        "terminally_degraded": False,
        "last_failure_reason": "",
        "last_degraded_signal_id": "",
    }


def read_case_intelligence_degradation(store: Any, case_id: str) -> dict[str, Any]:
    """Read-only fetch. Never raises; missing case or store returns the empty state."""
    case_id = str(case_id or "").strip()
    if store is None or not case_id:
        return _empty_state()
    fetch = getattr(store, "fetch_case", None)
    if not callable(fetch):
        return _empty_state()
    row = fetch(case_id)
    if not isinstance(row, dict):
        return _empty_state()
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    state = metadata.get(_METADATA_KEY)
    if not isinstance(state, dict):
        return _empty_state()
    out = _empty_state()
    out.update({k: state.get(k, out[k]) for k in out})
    return out


def is_case_intelligence_terminally_degraded(store: Any, case_id: str) -> bool:
    return bool(read_case_intelligence_degradation(store, case_id).get("terminally_degraded"))


def record_case_intelligence_degradation(
    store: Any,
    case_id: str,
    *,
    signal_id: str,
    failure_reason: str,
    max_attempts: int = DEGRADATION_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Atomically record a Case Intelligence fallback for this case.

    Increments the durable attempt counter and marks the case terminally degraded
    once `max_attempts` is reached, so retry stops instead of looping forever on an
    unfixable failure. Returns the new state. No-op (returns the empty state) when
    the store cannot support case mutation.
    """
    case_id = str(case_id or "").strip()
    if store is None or not case_id:
        return _empty_state()
    mutate = getattr(store, "mutate_case", None)
    if not callable(mutate):
        return _empty_state()

    result: dict[str, Any] = {}

    def _mutator(row: dict[str, Any]) -> dict[str, Any]:
        case_row = dict(row)
        metadata = dict(case_row.get("metadata") or {})
        prior = metadata.get(_METADATA_KEY)
        state = _empty_state()
        if isinstance(prior, dict):
            state.update({k: prior.get(k, state[k]) for k in state})
        attempts = int(state.get("attempts") or 0) + 1
        state["degraded"] = True
        state["attempts"] = attempts
        state["terminally_degraded"] = bool(state.get("terminally_degraded")) or attempts >= max(1, int(max_attempts))
        state["last_failure_reason"] = str(failure_reason or "")[:500]
        state["last_degraded_signal_id"] = str(signal_id or "")
        metadata[_METADATA_KEY] = state
        case_row["metadata"] = metadata
        result.update(state)
        return case_row

    mutate(case_id, _mutator, create_if_missing=True)
    return result or _empty_state()


def clear_case_intelligence_degradation(store: Any, case_id: str) -> None:
    """Atomically clear degradation state after a successful (retry) attempt."""
    case_id = str(case_id or "").strip()
    if store is None or not case_id:
        return
    mutate = getattr(store, "mutate_case", None)
    if not callable(mutate):
        return

    def _mutator(row: dict[str, Any]) -> dict[str, Any]:
        case_row = dict(row)
        metadata = dict(case_row.get("metadata") or {})
        if _METADATA_KEY in metadata:
            metadata = dict(metadata)
            metadata.pop(_METADATA_KEY, None)
            case_row["metadata"] = metadata
        return case_row

    try:
        mutate(case_id, _mutator, create_if_missing=False)
    except LookupError:
        # Nothing to clear if the case row is gone; clearing must never raise.
        return


def maybe_record_case_intelligence_degradation(
    store: Any,
    case_id: str,
    case_intelligence_result: dict[str, Any] | None,
    *,
    signal_id: str,
) -> list[str]:
    """Shared call-site helper used by both live reconcile paths.

    No-ops (returns `[]`) when `case_intelligence_result` is not a fallback. When it
    is, records the degradation and returns warning strings to append to the
    caller's own `warnings` list — kept as plain strings, not a new result field, so
    both `ReconcileResult.warnings` (signal_reconciler.py) and the agent runtime's
    `warnings` list can absorb it identically.
    """
    if not case_id or not case_intelligence_result_is_fallback(case_intelligence_result):
        return []
    degradation = record_case_intelligence_degradation(
        store,
        case_id,
        signal_id=signal_id,
        failure_reason=str(
            (case_intelligence_result.get("execution_metadata") or {}).get("error_type") or ""
        ),
    )
    if not degradation.get("degraded"):
        return []
    state_word = "terminally_degraded" if degradation.get("terminally_degraded") else "degraded"
    return [f"case_intelligence_{state_word}:attempts={degradation.get('attempts')}"]


def case_intelligence_result_is_fallback(case_intelligence_result: dict[str, Any] | None) -> bool:
    if not isinstance(case_intelligence_result, dict):
        return False
    meta = case_intelligence_result.get("execution_metadata")
    if not isinstance(meta, dict):
        return False
    return str(meta.get("source_mode") or "") == "fallback"


def latest_signal_id_for_case(store: Any, case_id: str) -> str:
    case_id = str(case_id or "").strip()
    if store is None or not case_id:
        return ""
    fetch = getattr(store, "fetch_case", None)
    if not callable(fetch):
        return ""
    row = fetch(case_id)
    if not isinstance(row, dict):
        return ""
    return str(row.get("latest_signal_id") or "")


__all__ = [
    "DEGRADATION_MAX_ATTEMPTS",
    "read_case_intelligence_degradation",
    "is_case_intelligence_terminally_degraded",
    "record_case_intelligence_degradation",
    "clear_case_intelligence_degradation",
    "case_intelligence_result_is_fallback",
    "latest_signal_id_for_case",
]
