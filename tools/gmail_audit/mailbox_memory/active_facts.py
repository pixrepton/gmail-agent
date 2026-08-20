"""Helpers for *current* mailbox-fact reads (active, not superseded).

``fetch_facts_for_case`` returns the full audit trail (active + superseded).
Consumers that need live / current facts must prefer
``fetch_active_facts_for_case`` (AI-OS 4.2) so superseded rows cannot decide
identity, invoice direction, snapshot ranking, or projection.
"""

from __future__ import annotations

from typing import Any


DECISION_CRITICAL_FACT_KEYS = frozenset(
    {
        "customer_email",
        "address",
        "heated_area_m2",
        "device",
        "device_model",
        "model",
        "service_date",
        "price",
        "amount",
        "amount_total",
        "warranty",
        "service_obligation",
        "warranty_service_obligation",
    }
)

_FACT_KEY_ALIASES = {
    "device/model": "device_model",
    "device.model": "device_model",
    "model_urzadzenia": "device_model",
    "device/model_name": "device_model",
    "price/amount": "amount",
    "total_amount": "amount_total",
    "warranty/service_obligation": "warranty_service_obligation",
}

_ACTION_REQUIRED_FACT_KEYS = {
    "calculate_quote": frozenset({"heated_area_m2"}),
    "call_kalk_top_quote": frozenset({"heated_area_m2"}),
    "prepare_offer": frozenset({"heated_area_m2"}),
    "send_offer": frozenset({"heated_area_m2", "customer_email"}),
    "schedule_service": frozenset({"service_date", "address"}),
    "dispatch_service": frozenset({"service_date", "address", "device_model"}),
    "send_invoice": frozenset({"customer_email", "amount_total"}),
    "acknowledge_documents": frozenset(),
    "ask_for_missing_data": frozenset(),
    "prepare_reply": frozenset(),
}


def normalize_decision_fact_key(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    normalized = raw.replace(" ", "_")
    return _FACT_KEY_ALIASES.get(normalized, normalized)


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


def conflict_fact_keys(conflicting_facts: Any) -> set[str]:
    keys: set[str] = set()
    rows = conflicting_facts if isinstance(conflicting_facts, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = normalize_decision_fact_key(row.get("fact_key") or row.get("predicate") or row.get("type"))
        if key:
            keys.add(key)
    return keys


def annotate_decision_fact_use(
    facts: Any,
    conflicting_facts: Any,
) -> list[dict[str, Any]]:
    """Add read-only decision-usability metadata to current facts.

    This does not resolve truth. It only says whether a fact may be used as a
    decision premise when the same critical key has an unresolved conflict.
    """
    blocked_keys = conflict_fact_keys(conflicting_facts) & DECISION_CRITICAL_FACT_KEYS
    out: list[dict[str, Any]] = []
    rows = facts if isinstance(facts, list) else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        key = normalize_decision_fact_key(row.get("fact_key") or row.get("predicate") or row.get("key"))
        if key in blocked_keys:
            row["trust_state"] = "conflicted"
            row["decision_usable"] = False
            row["decision_block_reason"] = "fact_conflict"
        else:
            row.setdefault("trust_state", "confirmed" if key else "provisional")
            row.setdefault("decision_usable", True)
            row.setdefault("decision_block_reason", None)
        out.append(row)
    return out


def action_required_fact_keys(action_type: Any) -> set[str]:
    action = str(action_type or "").strip().lower()
    return set(_ACTION_REQUIRED_FACT_KEYS.get(action, frozenset()))


def action_conflict_block(
    *,
    action_type: Any,
    facts: Any,
) -> dict[str, Any]:
    required = action_required_fact_keys(action_type)
    blocked: set[str] = set()
    rows = facts if isinstance(facts, list) else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        key = normalize_decision_fact_key(item.get("fact_key") or item.get("predicate") or item.get("key"))
        if key in required and item.get("decision_usable") is False and item.get("decision_block_reason") == "fact_conflict":
            blocked.add(key)
    return {
        "blocked": bool(blocked),
        "blocked_fact_keys": sorted(blocked),
        "decision_block_reason": "fact_conflict" if blocked else None,
    }


__all__ = [
    "DECISION_CRITICAL_FACT_KEYS",
    "action_conflict_block",
    "action_required_fact_keys",
    "annotate_decision_fact_use",
    "conflict_fact_keys",
    "fetch_current_facts_for_case",
    "is_live_fact",
    "normalize_decision_fact_key",
]
