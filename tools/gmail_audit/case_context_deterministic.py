"""Deterministic conflict and completeness gap helpers for CaseContextPack vNext.

Read-only heuristics; does not mutate mailbox memory. Keeps logic bounded and testable.
"""

from __future__ import annotations

from typing import Any


def normalize_value_for_conflict(value: Any) -> str:
    """Normalize a scalar fact value for equality checks (whitespace, case)."""

    return " ".join(str(value or "").strip().lower().split())


def _fact_predicate(fact: dict[str, Any]) -> str:
    return str(fact.get("fact_key") or fact.get("predicate") or "").strip()


def _fact_raw_value(fact: dict[str, Any]) -> str:
    raw = fact.get("normalized_value")
    if raw is None:
        raw = fact.get("value", fact.get("raw_value", ""))
    return normalize_value_for_conflict(raw)


def _primary_evidence_ref(fact: dict[str, Any]) -> dict[str, Any]:
    sr = str(fact.get("source_ref") or "").strip()
    if not sr:
        return {}
    return {
        "source_type": str(fact.get("source_type") or fact.get("type") or "unknown"),
        "source_id": sr,
        "timestamp": str(fact.get("observed_at") or fact.get("created_at") or ""),
        "field": _fact_predicate(fact),
        "document_id": str(fact.get("document_id") or ""),
        "message_id": str(fact.get("message_id") or ""),
        "confidence": fact.get("confidence"),
        "evidence_role": "contradicts",
    }


def _mixed_gmail_drive_sources(facts: list[dict[str, Any]]) -> bool:
    types = {str(f.get("source_type") or f.get("type") or "").lower() for f in facts}
    has_gmail = any("gmail" in t or t in {"message", "gmail_message"} for t in types)
    has_drive = any("drive" in t or "drive_" in t or t == "drive_document" for t in types)
    return has_gmail and has_drive


def collect_cross_source_predicate_conflicts(facts: list[dict[str, Any]], *, case_id: str) -> list[dict[str, Any]]:
    """Same predicate, different normalized values, at least two distinct evidence refs."""

    by_pred: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        pred = _fact_predicate(fact)
        if not pred:
            continue
        by_pred.setdefault(pred, []).append(fact)

    out: list[dict[str, Any]] = []
    for pred, items in by_pred.items():
        by_val: dict[str, list[dict[str, Any]]] = {}
        for fact in items:
            val = _fact_raw_value(fact)
            if not val:
                continue
            by_val.setdefault(val, []).append(fact)
        if len(by_val) <= 1:
            continue

        values_sorted = sorted(by_val.keys())
        evidence: list[dict[str, Any]] = []
        for group in by_val.values():
            for fact in group[:2]:
                ref = _primary_evidence_ref(fact)
                if ref:
                    evidence.append(ref)
        if len(evidence) < 2:
            continue

        ctype = "document_vs_email" if _mixed_gmail_drive_sources(items) else "fact_conflict"
        out.append(
            {
                "fact_key": pred,
                "predicate": pred,
                "values": values_sorted,
                "facts_in_conflict": values_sorted,
                "type": ctype,
                "summary": f"Rozne wartosci dla {pred}: {', '.join(values_sorted)}",
                "severity": "warning",
                "source_refs": evidence[:12],
                "status": "open",
                "suggested_resolution": "Zweryfikuj zrodla i ustal wartosc kanoniczna po decyzji operatora.",
                "requires_operator": True,
                "case_id": case_id,
            }
        )
    return out


def detect_status_snapshot_vs_next_action(
    snapshot: dict[str, Any],
    next_action: dict[str, Any],
    *,
    case_id: str,
) -> list[dict[str, Any]]:
    """Flag tension between case snapshot status and recommended next action."""

    st = str(snapshot.get("status") or "").strip().lower()
    na = str(next_action.get("next_action") or "").strip().lower()
    closed_like = st in {"closed", "done", "resolved", "cancelled"}
    if not closed_like or not na:
        return []
    idle_next = {"", "none", "noop", "closed", "done", "resolved", "archive"}
    if na in idle_next:
        return []
    return [
        {
            "fact_key": "case_status",
            "predicate": "case_status",
            "values": [st, na],
            "facts_in_conflict": [st, na],
            "type": "status_conflict",
            "summary": f"Status sprawy ({st}) vs nastepny krok ({na}) — wymaga weryfikacji.",
            "severity": "info",
            "source_refs": [],
            "status": "open",
            "suggested_resolution": "Uaktualnij status sprawy albo nastepny krok, aby byly spojne.",
            "requires_operator": True,
            "case_id": case_id,
        }
    ]


def _fact_keys(facts: list[dict[str, Any]]) -> set[str]:
    return {_fact_predicate(f) for f in facts if _fact_predicate(f)}


def _snapshot_customer(snapshot: dict[str, Any]) -> dict[str, Any]:
    cust = snapshot.get("customer")
    return cust if isinstance(cust, dict) else {}


def infer_generic_completeness_gaps(
    *,
    case_id: str,
    snapshot: dict[str, Any],
    active_facts: list[dict[str, Any]],
    drive_documents_summary: list[dict[str, Any]],
    existing_gap_summaries: set[str],
    source_refs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Projection-safe generic gaps from snapshot + fact keys (no HVAC / kalk-top)."""

    gaps: list[dict[str, Any]] = []
    fk = _fact_keys(active_facts)
    cust = _snapshot_customer(snapshot)
    has_customer_block = isinstance(snapshot.get("customer"), dict)
    email = str(cust.get("email") or "").strip()
    name = str(cust.get("name") or "").strip()
    seen_kinds = snapshot.get("last_source_kinds_seen") or []
    if isinstance(seen_kinds, list):
        lane = " ".join(str(x) for x in seen_kinds).lower()
    else:
        lane = str(seen_kinds).lower()
    refs = source_refs or []

    def _push(
        *,
        gap_type: str,
        summary: str,
        required_for: str,
        severity: str = "warning",
        suggested: str = "",
    ) -> None:
        if summary in existing_gap_summaries:
            return
        gaps.append(
            {
                "type": gap_type,
                "summary": summary,
                "required_for": required_for,
                "severity": severity,
                "suggested_next_action": suggested or "Uzupelnij dane w CRM lub popros klienta o brakujace informacje.",
                "status": "open",
                "case_id": case_id,
                "evidence_refs": [],
            }
        )

    if has_customer_block and not email and "customer_email" not in fk:
        _push(
            gap_type="missing_contact",
            summary="Brak adresu e-mail klienta w snapshotcie i faktach.",
            required_for="operator_review",
            suggested="Potwierdz email kontaktowy w kanale sprawy.",
        )
    if has_customer_block and not name and "customer_name" not in fk:
        _push(
            gap_type="missing_contact",
            summary="Brak nazwy klienta w snapshotcie i faktach.",
            required_for="operator_review",
            suggested="Ustal nazwe klienta lub nazwe firmy przed oferta.",
        )

    address_keys = fk.intersection(
        {
            "property_address",
            "address",
            "installation_address",
            "postal_code",
            "city",
            "location",
        }
    )
    # Require multiple distinct fact keys before flagging address — avoids noise when only one technical fact exists.
    if len(fk) >= 2 and not address_keys:
        _push(
            gap_type="missing_address",
            summary="Brak jawnego adresu instalacji / lokalizacji we faktach.",
            required_for="offer_handoff",
            suggested="Dopisz adres lub potwierdz lokalizacje instalacji.",
        )

    if "device_model" not in fk and ("service" in lane or snapshot.get("open_questions")):
        oq = snapshot.get("open_questions") or []
        if isinstance(oq, list) and any("serwis" in str(x).lower() or "service" in str(x).lower() for x in oq):
            _push(
                gap_type="missing_evidence",
                summary="Kontekst serwisowy bez modelu urzadzenia we faktach.",
                required_for="service_followup",
                suggested="Uzupelnij model urzadzenia z dokumentacji lub wizyty.",
            )

    drive_hint = any(
        "drive" in str(r.get("source_ref") or r.get("source_id") or "").lower()
        or str(r.get("source_type") or r.get("type") or "").lower() in {"drive_document", "drive_file"}
        for r in refs
        if isinstance(r, dict)
    )
    if drive_hint and not drive_documents_summary:
        _push(
            gap_type="missing_drive_link",
            summary="Sygnaly odniesien do Drive bez powiazanych dokumentow w podsumowaniu.",
            required_for="document_review",
            severity="info",
            suggested="Sprawdz linkowanie dokumentow Drive do tej sprawy.",
        )

    return gaps


def merge_conflicts_deterministic(
    existing: list[dict[str, Any]],
    *,
    case_id: str,
    active_facts: list[dict[str, Any]],
    snapshot: dict[str, Any],
    next_action: dict[str, Any],
) -> list[dict[str, Any]]:
    """Append generated conflicts without duplicating same predicate/value set."""

    generated: list[dict[str, Any]] = []
    generated.extend(collect_cross_source_predicate_conflicts(active_facts, case_id=case_id))
    generated.extend(detect_status_snapshot_vs_next_action(snapshot, next_action, case_id=case_id))

    def _key(row: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
        fk = str(row.get("fact_key") or row.get("predicate") or "")
        vals = row.get("values") or row.get("facts_in_conflict") or []
        if isinstance(vals, list):
            norm = tuple(sorted(str(v) for v in vals))
        else:
            norm = (str(vals),)
        return (fk, norm)

    merged = list(existing)
    for row in generated:
        k = _key(row)
        if not k[0]:
            merged.append(row)
            continue
        dup_idx = next((idx for idx, cur in enumerate(merged) if _key(cur) == k), None)
        if dup_idx is not None:
            existing_refs = merged[dup_idx].get("source_refs") or merged[dup_idx].get("evidence_refs") or []
            new_refs = row.get("source_refs") or row.get("evidence_refs") or []
            if isinstance(new_refs, list) and isinstance(existing_refs, list) and len(new_refs) > len(existing_refs):
                merged[dup_idx] = row
            continue
        merged.append(row)
    return merged


def merge_gaps_deterministic(
    existing: list[dict[str, Any]],
    *,
    case_id: str,
    snapshot: dict[str, Any],
    active_facts: list[dict[str, Any]],
    drive_documents_summary: list[dict[str, Any]],
    source_refs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    summaries = {str(g.get("summary") or "").strip() for g in existing if isinstance(g, dict)}
    summaries.update(str(g) for g in existing if isinstance(g, str))
    new_raw = infer_generic_completeness_gaps(
        case_id=case_id,
        snapshot=snapshot,
        active_facts=active_facts,
        drive_documents_summary=drive_documents_summary,
        existing_gap_summaries=summaries,
        source_refs=source_refs,
    )
    merged = list(existing)
    for row in new_raw:
        s = str(row.get("summary") or "").strip()
        if s and s not in summaries:
            summaries.add(s)
            merged.append(row)
    return merged


__all__ = [
    "merge_conflicts_deterministic",
    "merge_gaps_deterministic",
    "normalize_value_for_conflict",
    "collect_cross_source_predicate_conflicts",
    "detect_status_snapshot_vs_next_action",
    "infer_generic_completeness_gaps",
]
