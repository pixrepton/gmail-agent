"""Apply operator adjudication to durable state and re-run signal reconcile (V2.1 truth loop)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from feedback_event_contract import (
    EVENT_TYPE_ADJUDICATION,
    adjudication_event_from_dict,
)
from mailbox_memory_runtime import stable_id
from operator_feedback_runtime import persist_routed_event, route_operator_payload
from signal_contract import CanonicalSignal
from signal_journal import SignalJournal
from signal_reconciler import SignalRuntimeContext, reconcile_signal

ADJUDICATION_LINK_OVERRIDE_EVENT = "adjudication_link_override"


def _with_bridge_case_hint_for_auxiliary_signal(
    signal: CanonicalSignal,
    *,
    adjudication_case_id: str,
) -> CanonicalSignal:
    """Auxiliary Gmail signals (thread/attachment) often omit payload case_id; bridge rows carry desk case_id."""

    if signal.signal_kind == "gmail_message_observed":
        return signal
    if str(signal.payload.get("case_id") or "").strip():
        return signal
    hint = str(adjudication_case_id or "").strip()
    if not hint:
        return signal
    merged = dict(signal.payload or {})
    merged["case_id"] = hint
    return replace(signal, payload=merged)


def append_reject_same_case_override(
    store: Any,
    *,
    signal_id: str,
    rejected_case_id: str,
    adjudication_event_id: str,
    trace_id: str = "",
) -> None:
    """Record that the operator rejected linking this signal to the given case (highest priority)."""
    sid = str(signal_id or "").strip()
    if not sid:
        raise ValueError("append_reject_same_case_override requires signal_id")
    eid = stable_id("adjov", sid, adjudication_event_id or "na")
    store.append_event(
        {
            "event_id": eid,
            "case_id": str(rejected_case_id or "").strip() or "_operator_desk",
            "message_id": "",
            "thread_id": "",
            "event_type": ADJUDICATION_LINK_OVERRIDE_EVENT,
            "occurred_at": None,
            "summary_text": f"Adjudication: reject link signal {sid} from case {rejected_case_id}",
            "payload": {
                "signal_id": sid,
                "rejected_case_id": str(rejected_case_id or "").strip(),
                "adjudication_event_id": str(adjudication_event_id or "").strip(),
                "override_kind": "reject_same_case",
                "trace_id": str(trace_id or "").strip(),
            },
            "source_refs": [{"kind": "signal_id", "ref": sid}],
        }
    )


def _strategy_confirm_same_case(
    store: Any,
    journal: SignalJournal,
    runtime_context: SignalRuntimeContext,
    ae: Any,
) -> dict[str, Any]:
    """Operator confirmed the signal-to-case link — no reconcile needed."""
    return {
        "adjudication_result": "no_reconcile_needed",
        "adjudication_kind": str(ae.adjudication_kind or "confirm_same_case"),
        "event_id": ae.event_id,
        "detail": "Operator potwierdził powiązanie — brak korekty rekonsyliacji w tej ścieżce.",
    }


def _strategy_reject_same_case(
    store: Any,
    journal: SignalJournal,
    runtime_context: SignalRuntimeContext,
    ae: Any,
) -> Any | None:
    """Operator rejected the signal-to-case link — persist override and re-run reconcile."""
    signal_id = str(ae.target_refs.get("signal_id") or ae.payload.get("signal_id") or "").strip()
    rejected_case_id = str(
        ae.target_refs.get("rejected_case_id") or ae.payload.get("rejected_case_id") or ""
    ).strip()
    if not signal_id:
        return None
    append_reject_same_case_override(
        store,
        signal_id=signal_id,
        rejected_case_id=rejected_case_id or str(ae.case_id or "").strip(),
        adjudication_event_id=ae.event_id,
        trace_id=ae.trace_id,
    )
    signal = journal.fetch_signal(signal_id)
    if signal is None:
        return None
    signal = _with_bridge_case_hint_for_auxiliary_signal(signal, adjudication_case_id=str(ae.case_id or "").strip())
    return reconcile_signal(signal, runtime_context=runtime_context, dry_run=False)


def _strategy_noop(
    store: Any,
    journal: SignalJournal,
    runtime_context: SignalRuntimeContext,
    ae: Any,
) -> dict[str, Any]:
    """Fallback for unknown/unhandled adjudication kinds."""
    return {
        "adjudication_result": "unsupported_kind",
        "adjudication_kind": str(ae.adjudication_kind or "unknown"),
        "event_id": ae.event_id,
        "detail": "Ten rodzaj adjudikacji nie uruchamia jeszcze automatycznej pętli reconcile.",
    }


# Dispatch dict: maps adjudication_kind → strategy function.
# Asymmetry by design: confirm_same_case affirms the existing link (no state change → no reconcile).
# reject_same_case persists an override and must re-run reconcile to assign the signal elsewhere.
_ADJUDICATION_STRATEGIES: dict[str, Any] = {
    "confirm_same_case": _strategy_confirm_same_case,
    "reject_same_case": _strategy_reject_same_case,
    "action_decision": _strategy_noop,
    "agent_hitl": _strategy_noop,
}


def execute_adjudication_reconcile(
    *,
    store: Any,
    journal: SignalJournal,
    runtime_context: SignalRuntimeContext,
    adjudication_dict: dict[str, Any],
) -> Any | None:
    """
    Persist adjudication override (when reject_same_case / wrong-case) and re-run SignalReconciler
    for the affected canonical signal — treats adjudication as a correction pass.

    Uses _ADJUDICATION_STRATEGIES dispatch dict so each kind maps to a dedicated strategy function.
    Unknown kinds fall through to _strategy_noop.
    """
    ae = adjudication_event_from_dict(adjudication_dict)
    kind = str(ae.adjudication_kind or "")
    strategy = _ADJUDICATION_STRATEGIES.get(kind, _strategy_noop)
    return strategy(store, journal, runtime_context, ae)


def bridge_operator_feedback(
    *,
    store: Any,
    journal: SignalJournal,
    runtime_context: SignalRuntimeContext,
    raw_operator_payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Daszek→Python bridge entrypoint: calibration persists only; adjudication persists + truth loop.

    Call from CLI, worker, or a thin HTTP shim on the operator side — not from Daszek PHP as semantic owner.
    """
    domain, _normalized = route_operator_payload(raw_operator_payload)
    if domain == "calibration":
        eid = persist_routed_event(store, "calibration", _normalized)
        return {
            "domain": "calibration",
            "event_id": eid,
            "truth_loop_executed": False,
            "schema_version": "operator_feedback_bridge.v1",
        }
    event_id, reconcile_out = persist_and_execute_adjudication_truth_loop(
        store=store,
        journal=journal,
        runtime_context=runtime_context,
        raw_operator_payload=raw_operator_payload,
    )
    summary: dict[str, Any] | None = None
    reconcile_signal_ran = False
    adjudication_kind = ""
    case_id_hint = ""
    if isinstance(_normalized, dict):
        adjudication_kind = str(_normalized.get("adjudication_kind") or "")
        case_id_hint = str(_normalized.get("case_id") or "")
    if reconcile_out is not None:
        if isinstance(reconcile_out, dict):
            summary = reconcile_out
            result = str(reconcile_out.get("adjudication_result") or "")
            reconcile_signal_ran = result not in {"no_reconcile_needed", "unsupported_kind"}
        else:
            reconcile_signal_ran = True
            to_dict = getattr(reconcile_out, "to_dict", None)
            if callable(to_dict):
                summary = to_dict()
            else:
                summary = {"repr": repr(reconcile_out)[:500]}
            case_id_hint = case_id_hint or str(getattr(reconcile_out, "case_id", "") or "")
    from projection_refresh_contract import build_adjudication_projection_refresh

    projection_refresh = build_adjudication_projection_refresh(
        adjudication_kind=adjudication_kind,
        case_id=case_id_hint,
        reconcile_result=reconcile_out if reconcile_signal_ran else None,
    )
    return {
        "domain": "adjudication",
        "event_id": event_id,
        "truth_loop_executed": True,
        "reconcile_signal_ran": reconcile_signal_ran,
        "reconcile_summary": summary,
        "projection_refresh": projection_refresh,
        "schema_version": "operator_feedback_bridge.v1",
    }


def persist_and_execute_adjudication_truth_loop(
    *,
    store: Any,
    journal: SignalJournal,
    runtime_context: SignalRuntimeContext,
    raw_operator_payload: dict[str, Any],
) -> tuple[str, Any | None]:
    """
    Route + persist `v2_1_adjudication`, then immediately run the adjudication executioner
    (override + `reconcile_signal`) so operational truth updates in the next Hot State version.

    Use this (or `ingest_persisted_adjudication_event_row` on replay) when journal + runtime
    context are available — e.g. Python intake path after operator feedback ingest.
    """
    domain, normalized = route_operator_payload(raw_operator_payload)
    if domain != "adjudication":
        raise ValueError("persist_and_execute_adjudication_truth_loop requires an adjudication payload")
    event_id = persist_routed_event(store, "adjudication", normalized)
    event_row = {"event_type": EVENT_TYPE_ADJUDICATION, "payload": normalized}
    out = ingest_persisted_adjudication_event_row(
        store, journal, runtime_context, event_row=event_row
    )
    return event_id, out


def ingest_persisted_adjudication_event_row(
    store: Any,
    journal: SignalJournal,
    runtime_context: SignalRuntimeContext,
    *,
    event_row: dict[str, Any],
) -> Any | None:
    """If event_row is a v2_1_adjudication payload, execute reconcile."""
    if str(event_row.get("event_type") or "") != EVENT_TYPE_ADJUDICATION:
        return None
    payload = event_row.get("payload")
    if isinstance(payload, dict) and str(payload.get("schema_version") or "").startswith("adjudication_event"):
        return execute_adjudication_reconcile(store=store, journal=journal, runtime_context=runtime_context, adjudication_dict=payload)
    if isinstance(payload, dict) and str(payload.get("event_class") or "") == "AdjudicationEvent":
        return execute_adjudication_reconcile(store=store, journal=journal, runtime_context=runtime_context, adjudication_dict=payload)
    return None


__all__ = [
    "ADJUDICATION_LINK_OVERRIDE_EVENT",
    "append_reject_same_case_override",
    "bridge_operator_feedback",
    "execute_adjudication_reconcile",
    "ingest_persisted_adjudication_event_row",
    "persist_and_execute_adjudication_truth_loop",
]
