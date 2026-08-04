"""Roadmap 2.4 (X1 exceptions-only) — order the operator desk by real exceptions, not by recency.

The desk currently shows every main-feed member in store order, so a case with a pending HITL
approval and a case with nothing to decide look equally urgent. X1 wants the exceptions first.

Two deliberate constraints:

* **Membership is untouched.** This module orders and, only when explicitly asked, filters a list
  that `feed_visibility` already decided. It never adds a card, and by default it never removes one
  — `exceptions_only=False` is a pure reordering, which keeps every existing consumer correct.
* **Readiness lives in one place.** The rank reads `CaseReadinessState`
  (`llm_contracts.case_readiness`); it does not re-invent "is there work here?" from raw fields.
  `feed_visibility` deliberately knows nothing about readiness, so this composition lives here
  instead of inside the membership contract.
"""
from __future__ import annotations

from typing import Any

from feed_visibility import VISIBILITY_ATTENTION_REQUIRED, effective_visibility_mode
from llm_contracts.case_readiness import (
    PENDING_READINESS_STATES,
    CaseReadinessState,
    build_case_readiness,
)

#: lower rank == closer to the top of the desk
_RANK_ATTENTION_REQUIRED = 0
_RANK_PENDING_READINESS = 1
_RANK_PLAIN_MAIN_FEED = 2
_RANK_NO_ACTION_REQUIRED = 3


def case_readiness_from_snapshot(snapshot: Any) -> dict[str, Any]:
    """Adapter: derive `CaseReadinessState` from an `EngagementSnapshotV2`'s executive fields.

    The v2 dash projection composes readiness from Guidance and intelligence output; the engagement
    feed has neither, but it does have the authoritative executive state (HITL gate, blocking gaps,
    operational status). Both paths end in the same `build_case_readiness` rules, so the desk and
    Daszek cannot disagree about what `ready_for_approval` means.
    """
    hitl = getattr(snapshot, "hitl_gate", None)
    hitl_required = bool(getattr(hitl, "required", False))
    gaps = list(getattr(snapshot, "gaps", None) or [])
    blocking_gaps = [g for g in gaps if str(getattr(g, "severity", "")) == "blocking"]
    status = getattr(snapshot, "operational_status", None)
    code = str(getattr(status, "code", "") or "").strip().lower()
    enabled_actions = [a for a in (getattr(snapshot, "actions", None) or []) if bool(getattr(a, "enabled", False))]

    if code == "ready_for_quote" and not blocking_gaps:
        context_readiness = "decision_ready"
    elif blocking_gaps:
        context_readiness = "not_ready"
    else:
        context_readiness = "review_only" if code in {"pending_operator", "node_a_error"} else ""

    facets = {
        "context_readiness": context_readiness,
        "ready_for_decision": context_readiness == "decision_ready",
        "ready_for_operator_review": context_readiness == "review_only",
        "blocked_by_data": bool(blocking_gaps),
        "policy_status": "",
        "gap_count": len(gaps),
        "conflict_count": 0,
    }
    return build_case_readiness(
        readiness_facets=facets,
        policy_status="approved" if (enabled_actions and not hitl_required and code == "ready_for_quote") else "",
        hitl_required=hitl_required,
    )


def readiness_state_of(item: Any) -> str:
    """Readiness state of a desk row (dict) or snapshot, or `""` when unknown."""
    if isinstance(item, dict):
        readiness = item.get("case_readiness")
        if isinstance(readiness, dict):
            return str(readiness.get("state") or "").strip()
        return ""
    return str(case_readiness_from_snapshot(item).get("state") or "")


def desk_priority_rank(*, visibility_mode: str, readiness_state: str) -> int:
    """Rank one card. Unknown readiness ranks as an ordinary main-feed card, never as noise."""
    if str(visibility_mode or "").strip().lower() == VISIBILITY_ATTENTION_REQUIRED:
        return _RANK_ATTENTION_REQUIRED
    state = str(readiness_state or "").strip()
    if state in PENDING_READINESS_STATES:
        return _RANK_PENDING_READINESS
    if state == CaseReadinessState.NO_ACTION_REQUIRED.value:
        return _RANK_NO_ACTION_REQUIRED
    return _RANK_PLAIN_MAIN_FEED


def order_desk_snapshots(
    snapshots: list[Any],
    *,
    exceptions_only: bool = False,
) -> list[Any]:
    """Sort main-feed snapshots so exceptions come first. Stable within equal rank.

    Args:
        snapshots: snapshots that `feed_visibility` already admitted to the main feed.
        exceptions_only: when True, ALSO drop plain `main_feed` cards whose readiness is
            `no_action_required`. Off by default — the soft preference is backward compatible and
            the hard filter is opt-in, because dropping a card the operator used to see is a
            visible behaviour change that must be requested, not assumed.
    """
    decorated: list[tuple[int, int, Any]] = []
    for index, snapshot in enumerate(snapshots or []):
        mode, _reasons = effective_visibility_mode(snapshot)
        rank = desk_priority_rank(
            visibility_mode=mode,
            readiness_state=readiness_state_of(snapshot),
        )
        if exceptions_only and rank == _RANK_NO_ACTION_REQUIRED:
            continue
        decorated.append((rank, index, snapshot))
    decorated.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in decorated]


__all__ = [
    "case_readiness_from_snapshot",
    "desk_priority_rank",
    "order_desk_snapshots",
    "readiness_state_of",
]
