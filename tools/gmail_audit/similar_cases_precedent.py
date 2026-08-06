"""Deterministic similar-case precedent retrieval (v0 slice, no LLM)."""

from __future__ import annotations

from typing import Any
from _protocols import DatabaseConnection

from evidence_ref import normalize_evidence_ref
from mailbox_memory.active_facts import fetch_current_facts_for_case


def _active_fact_keys(facts: list[dict[str, Any]]) -> set[str]:
    """Return fact keys that still count as live for precedent overlap.

    Aligns with RP-29 / split_conflicting_facts on ``superseded``: a replaced
    value must not inflate similar-case overlap. ``rejected`` / ``stale`` stay
    excluded as before. Missing status remains live (default / older producers).
    """
    keys: set[str] = set()
    for row in facts:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "confirmed").strip().lower()
        if status in {"rejected", "stale", "superseded"}:
            continue
        key = str(row.get("fact_key") or row.get("predicate") or "").strip()
        if key:
            keys.add(key)
    return keys


def _resolution_outcome(case_row: dict[str, Any]) -> str:
    meta = case_row.get("metadata") if isinstance(case_row.get("metadata"), dict) else {}
    outcome = str(meta.get("resolution_outcome") or "").strip()
    if outcome:
        return outcome
    snap = case_row.get("snapshot_json") if isinstance(case_row.get("snapshot_json"), dict) else {}
    return str(snap.get("resolution_outcome") or snap.get("outcome") or "").strip()


def fetch_similar_case_precedent_refs(
    store: Any,
    *,
    case_id: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return EvidenceRef rows (role=precedent) for resolved cases with overlapping fact_key."""
    cid = str(case_id or "").strip()
    if not cid:
        return []

    fetch_case = getattr(store, "fetch_case", None)
    if not callable(fetch_case):
        return []

    case_row = fetch_case(cid) or {}
    case_family = str(case_row.get("case_family") or "unknown").strip() or "unknown"
    # 4.2b: current-key overlap must not see superseded rows from the audit trail.
    active_keys = _active_fact_keys(fetch_current_facts_for_case(store, cid))
    if not active_keys or case_family == "unknown":
        return []

    fetch_resolved = getattr(store, "fetch_resolved_cases_by_family_and_fact_keys", None)
    candidates: list[dict[str, Any]] = []
    if callable(fetch_resolved):
        candidates = list(
            fetch_resolved(
                case_family=case_family,
                fact_keys=sorted(active_keys),
                exclude_case_id=cid,
                limit=max(1, int(limit)),
            )
            or []
        )
    else:
        candidates = _fetch_resolved_via_sql(store, case_family, active_keys, cid, limit)

    refs: list[dict[str, Any]] = []
    for row in candidates:
        if not isinstance(row, dict):
            continue
        prec_case_id = str(row.get("case_id") or "").strip()
        if not prec_case_id:
            continue
        overlap = int(row.get("overlap_count") or row.get("shared_fact_keys") or 0)
        outcome = _resolution_outcome(row)
        summary = str(row.get("subject") or row.get("summary_text") or prec_case_id).strip()
        label = f"Precedens: {summary[:120]}"
        if outcome:
            label = f"{label} (wynik: {outcome})"
        refs.append(
            normalize_evidence_ref(
                {
                    "evidence_role": "precedent",
                    "source_type": "mailbox_memory",
                    "source_id": prec_case_id,
                    "source_ref": f"case:{prec_case_id}",
                    "label_pl": label,
                    "trust_level": "medium",
                    "freshness": "current",
                    "metadata": {
                        "case_family": case_family,
                        "overlap_fact_keys": overlap,
                        "resolution_outcome": outcome,
                    },
                },
                default_role="precedent",
            )
        )
    return refs[: max(0, int(limit))]


def fetch_learning_rule_precedent_refs(
    conn: DatabaseConnection,
    *,
    case_family: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Precedents from Mechanism A — approved learning rules."""
    if conn is None:
        return []
    try:
        from divergence_loop import fetch_approved_rules_for_family

        rules = fetch_approved_rules_for_family(conn, case_family=case_family, limit=limit)
    except Exception:
        return []
    refs: list[dict[str, Any]] = []
    for row in rules:
        text = str(row.get("rule_text_pl") or "").strip()
        if not text:
            continue
        refs.append(
            normalize_evidence_ref(
                {
                    "evidence_role": "precedent",
                    "source_type": "learning_rule",
                    "source_id": str(row.get("candidate_id") or ""),
                    "source_ref": f"learning_rule:{row.get('candidate_id')}",
                    "label_pl": f"Zasada z obserwacji: {text[:160]}",
                    "trust_level": "medium",
                    "freshness": "current",
                    "metadata": {
                        "case_family": case_family,
                        "proposal_type": row.get("proposal_type"),
                        "signal_source": "mechanism_a",
                    },
                },
                default_role="precedent",
            )
        )
    return refs


def fetch_world_model_precedent_refs(
    conn: DatabaseConnection,
    *,
    case_family: str,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Precedents from Mechanism B — approved world model insights."""
    if conn is None:
        return []
    category_map = {
        "heat_pump_service": "situation",
        "heat_pump_install": "situation",
        "service_request": "situation",
    }
    category = category_map.get(str(case_family or "").strip(), "situation")
    try:
        from world_model import fetch_approved_insights_for_category

        insights = fetch_approved_insights_for_category(conn, category=category, limit=limit)
    except Exception:
        return []
    refs: list[dict[str, Any]] = []
    for row in insights:
        text = str(row.get("insight_text_pl") or "").strip()
        if not text:
            continue
        refs.append(
            normalize_evidence_ref(
                {
                    "evidence_role": "precedent",
                    "source_type": "world_model",
                    "source_id": str(row.get("insight_id") or ""),
                    "source_ref": f"world_model:{row.get('insight_id')}",
                    "label_pl": f"Model świata: {text[:160]}",
                    "trust_level": "low",
                    "freshness": "current",
                    "metadata": {
                        "category": category,
                        "case_family": case_family,
                        "signal_source": "mechanism_b",
                    },
                },
                default_role="precedent",
            )
        )
    return refs


def fetch_similar_case_precedent_refs_v1(
    store: Any,
    conn: DatabaseConnection,
    *,
    case_id: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """v1: v0 SQL overlap + Mechanism A rules + Mechanism B insights."""
    cap = max(0, int(limit))
    case_row = {}
    fetch_case = getattr(store, "fetch_case", None)
    if callable(fetch_case):
        case_row = fetch_case(case_id) or {}
    family = str(case_row.get("case_family") or "unknown").strip() or "unknown"
    extra_a = fetch_learning_rule_precedent_refs(conn, case_family=family, limit=2)
    extra_b = fetch_world_model_precedent_refs(conn, case_family=family, limit=2)
    # A+B first — v0 SQL can flood the cap and hide approved learning signals.
    learning_refs = list(extra_a) + list(extra_b)
    v0_cap = max(0, cap - len(learning_refs))
    v0 = fetch_similar_case_precedent_refs(store, case_id=case_id, limit=v0_cap)
    combined = learning_refs + list(v0)
    return combined[:cap]


def _fetch_resolved_via_sql(
    store: Any,
    case_family: str,
    active_keys: set[str],
    exclude_case_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    connect = getattr(store, "_connect", None)
    if not callable(connect):
        return []
    keys = sorted(active_keys)
    if not keys:
        return []
    sql = """
        SELECT c.case_id, c.case_family, c.subject, c.status, c.metadata,
               COUNT(DISTINCT f.fact_key) AS overlap_count
        FROM mailbox_memory_cases c
        JOIN mailbox_memory_facts f ON f.case_id = c.case_id
        WHERE c.case_family = %(case_family)s
          AND c.status = 'resolved'
          AND c.case_id <> %(exclude_case_id)s
          AND f.fact_key = ANY(%(fact_keys)s::text[])
        GROUP BY c.case_id, c.case_family, c.subject, c.status, c.metadata
        ORDER BY overlap_count DESC, c.updated_at DESC
        LIMIT %(limit)s
    """
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    {
                        "case_family": case_family,
                        "exclude_case_id": exclude_case_id,
                        "fact_keys": keys,
                        "limit": max(1, int(limit)),
                    },
                )
                cols = [d[0] for d in cur.description or []]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


__all__ = [
    "fetch_similar_case_precedent_refs",
    "fetch_similar_case_precedent_refs_v1",
    "fetch_learning_rule_precedent_refs",
    "fetch_world_model_precedent_refs",
]
