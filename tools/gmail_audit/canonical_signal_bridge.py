"""
Bridge canonical signal journal entries to DownstreamSignal projection format.

Phase 2 (optional): when feature_flag=True, reads canonical signals from
journal/store and maps them to the DownstreamSignal shape for operator
projection surfaces. Not integrated with runtime — gated by feature flag.
"""

from __future__ import annotations

from typing import Any

from signal_contract import CanonicalSignal
from signal_journal import SignalJournal


def bridge_canonical_signals(
    store: Any,
    journal: SignalJournal,
    case_id: str,
    feature_flag: bool = False,
) -> list[dict[str, Any]]:
    """Read canonical signals from journal and return DownstreamSignal dicts.

    Args:
        store: Mailbox memory store (unused directly — journal wraps it).
        journal: SignalJournal for querying canonical signals.
        case_id: Limit results to this case.
        feature_flag: When False (default), returns empty list as a no-op
                      placeholder for the optional Phase 2 bridge.

    Returns:
        List of DownstreamSignal-shaped dicts (read-only projection).
    """
    _ = store  # reserved for future direct-store queries
    if not feature_flag:
        return []

    signals = journal.fetch_signals_for_case(case_id, limit=50)
    return [_canonical_to_downstream(sig) for sig in signals]


def _canonical_to_downstream(signal: CanonicalSignal) -> dict[str, Any]:
    """Map a single CanonicalSignal to the DownstreamSignal dict shape."""
    return {
        "signal_id": signal.signal_id,
        "case_id": str(signal.payload.get("case_id") or ""),
        "type": signal.signal_kind,
        "subtype": signal.source_kind,
        "summary": signal.signal_summary_pl or "",
        "observed_at": signal.observed_at,
        "source_kind": signal.source_kind,
        "source_ref": signal.source_ref or {},
        "payload": signal.payload or {},
        "policy_status": "allowed_for_projection",
        "status": "new",
        "bridge_source": "canonical_signal_bridge",
    }


__all__ = [
    "bridge_canonical_signals",
]
