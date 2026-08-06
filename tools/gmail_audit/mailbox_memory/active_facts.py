"""Helpers for *current* mailbox-fact reads (active, not superseded).

``fetch_facts_for_case`` returns the full audit trail (active + superseded).
Consumers that need live / current facts must prefer
``fetch_active_facts_for_case`` (AI-OS 4.2) so superseded rows cannot decide
identity, invoice direction, snapshot ranking, or projection.
"""

from __future__ import annotations

from typing import Any


def is_live_fact(fact: dict[str, Any]) -> bool:
    """Same live-row predicate as ``split_conflicting_facts`` / store active fetch."""
    return str(fact.get("status") or "active") != "superseded"


def fetch_current_facts_for_case(store: Any, case_id: str) -> list[dict[str, Any]]:
    """Return non-superseded facts for current-semantics consumers.

    Prefers ``store.fetch_active_facts_for_case`` when present. Falls back to
    filtering ``fetch_facts_for_case`` for ephemeral / legacy test stores.

    Empty ``case_id`` is forwarded to the store (some fakes ignore it; real
    stores correctly return no rows).
    """
    cid = str(case_id or "").strip()
    fetch_active = getattr(store, "fetch_active_facts_for_case", None)
    if callable(fetch_active):
        return list(fetch_active(cid) or [])
    fetch_all = getattr(store, "fetch_facts_for_case", None)
    if not callable(fetch_all):
        return []
    return [fact for fact in list(fetch_all(cid) or []) if isinstance(fact, dict) and is_live_fact(fact)]
