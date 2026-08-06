"""CaseContextPack vNext projection-safe contract helpers.

This module does not own truth and does not mutate mailbox memory. It normalizes
the existing CaseContextPack into a stable read/reasoning contract for FastAPI
and Daszek projection surfaces.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from evidence_ref import normalize_evidence_refs
from context_quality_contract import normalize_context_quality, operator_feed_context_quality_view

from case_context_deterministic import merge_conflicts_deterministic, merge_gaps_deterministic, normalize_value_for_conflict


CONTRACT_NAME = "CaseContextPack"
SCHEMA_VERSION = "1"
CONTRACT_VERSION = "vNext-2026-04"
# Prior implementation slug; lineage only — primary version is CONTRACT_VERSION.
PACK_BUILD = "case_context_pack.vnext.3"

FORBIDDEN_RAW_KEYS = frozenset(
    {
        "body",
        "email_body",
        "snippet",
        "prompt",
        "prompt_text",
        "raw_llm",
        "raw_response",
        "raw_body",
        "message_body",
        "attachment_bytes",
    }
)
CONFLICT_STATUSES = frozenset({"open", "needs_review", "resolved", "weak_evidence"})
GAP_TYPES = frozenset(
    {
        "missing_contact",
        "missing_address",
        "missing_document",
        "missing_drive_link",
        "missing_scheduling_evidence",
        "missing_case_link",
        "missing_evidence",
        "unknown",
    }
)
GAP_REQUIRED_FOR = frozenset(
    {
        "operator_review",
        "case_understanding",
        "service_followup",
        "offer_handoff",
        "scheduling",
        "document_review",
    }
)
GAP_STATUSES = frozenset({"open", "needs_review", "resolved", "not_applicable", "weak_evidence"})

_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONEISH_TOKEN = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d|(?:\d\s*){9,})")

_CONTACT_EMAILISH_SUBSTRINGS = ("email", "e_mail", "adres_email")
_CONTACT_PHONEISH_SUBSTRINGS = ("phone", "telefon", "tel", "mobile", "komorka", "gsm", "msisdn")
_CITY_PREDICATES_EXACT = frozenset(
    {
        "city",
        "customer_city",
        "installation_city",
        "property_city",
        "postal_city",
        "location_city",
    }
)
_CITY_NOISE_GENERIC = frozenset(
    {
        "",
        "n/a",
        "na",
        "brak",
        "unknown",
        "nieznany",
        "test",
        "xxx",
        "tbd",
        "none",
        "null",
        "undefined",
        "vat",
        "netto",
        "brutto",
    }
)


def _fact_key_lower(fk: str) -> str:
    return str(fk or "").strip().lower()


def _predicate_is_contact_emailish(fk: str) -> bool:
    s = _fact_key_lower(fk)
    return any(tok in s for tok in _CONTACT_EMAILISH_SUBSTRINGS)


def _predicate_is_contact_phoneish(fk: str) -> bool:
    s = _fact_key_lower(fk)
    return any(tok in s for tok in _CONTACT_PHONEISH_SUBSTRINGS)


def _predicate_is_contact_sensitive(fk: str) -> bool:
    return _predicate_is_contact_emailish(fk) or _predicate_is_contact_phoneish(fk) or "contact" in _fact_key_lower(fk)


def _predicate_is_cityish(fk: str) -> bool:
    s = _fact_key_lower(fk)
    if s in _CITY_PREDICATES_EXACT:
        return True
    return s.endswith("_city") or s == "miasto" or (s.endswith("city") and len(s) <= 32)


def _text_has_emailish(value: str) -> bool:
    return bool(_EMAIL_IN_TEXT.search(value or ""))


def _text_has_phoneish(value: str) -> bool:
    return bool(_PHONEISH_TOKEN.search(value or ""))


def _city_token_is_noise(val: str) -> bool:
    raw = str(val or "").strip()
    if not raw:
        return True
    norm = normalize_value_for_conflict(raw)
    if norm in _CITY_NOISE_GENERIC:
        return True
    if len(norm) <= 1:
        return True
    if "@" in raw or "http://" in norm or "https://" in norm:
        return True
    if _text_has_emailish(raw):
        return True
    if "." in raw and " " not in raw and "/" not in raw:
        parts = raw.split(".")
        if len(parts) >= 2 and parts[-1].isalpha() and 2 <= len(parts[-1]) <= 12 and parts[0].isalnum():
            return True
    if _text_has_phoneish(raw):
        return True
    if re.fullmatch(r"[A-Za-z0-9+/=_-]{18,}", raw.replace(" ", "")):
        return True
    if re.search(r"(promo|newsletter|marketing|black\s*friday|click\s*here|unsubscribe)", norm, re.I):
        return True
    return False


def _contact_projection_summary_pl(fk: str) -> str:
    if _predicate_is_contact_emailish(fk):
        return "Sprzeczne dane kontaktowe: różne wartości e-mail — wymaga weryfikacji operatora."
    if _predicate_is_contact_phoneish(fk):
        return "Sprzeczne dane kontaktowe: różne numery telefonu — wymaga weryfikacji operatora."
    return "Sprzeczne dane kontaktowe — wymaga weryfikacji operatora."


def _city_projection_summary_pl(values: list[Any], *, evidence_ref_count: int) -> str:
    clean_vals = [str(v).strip() for v in values if not _city_token_is_noise(str(v))]
    if evidence_ref_count <= 0:
        return (
            "Konflikt pola miasto bez dowodów w sprawie — sygnał diagnostyczny; "
            "nie używaj automatycznie do decyzji ofertowej."
        )
    if not clean_vals:
        return (
            "Konflikt pola miasto z tokenami nietypowymi dla lokalizacji — wymaga weryfikacji operatora "
            "(niepewny jako adres docelowy)."
        )
    joined = ", ".join(clean_vals[:4])
    if len(clean_vals) > 4:
        joined += ", …"
    return f"Różne wartości miasta we faktach ({joined}) — wymaga weryfikacji operatora."


def _conflict_evidence_ref_count(row: dict[str, Any]) -> int:
    return len(_list_of_dicts(row.get("evidence_refs") or row.get("source_refs")))


def _conflict_evidence_status(*, ref_count: int, status: str) -> str:
    if ref_count <= 0:
        return "missing"
    if str(status or "").strip().lower() == "weak_evidence":
        return "weak"
    return "supported"


def _try_attach_evidence_refs_from_active_facts(row: dict[str, Any], *, active_facts: list[dict[str, Any]]) -> None:
    if _conflict_evidence_ref_count(row) > 0:
        return
    fk = str(row.get("fact_key") or row.get("predicate") or "").strip()
    if not fk:
        return
    vals = row.get("values") or row.get("facts_in_conflict") or []
    if not isinstance(vals, list):
        vals = [vals]
    norm_vals = {normalize_value_for_conflict(v) for v in vals if str(v).strip()}
    if not norm_vals:
        return
    collected: list[dict[str, Any]] = []
    for fact in active_facts:
        if not isinstance(fact, dict):
            continue
        pred = str(fact.get("fact_key") or fact.get("predicate") or "").strip()
        if pred != fk:
            continue
        raw = fact.get("normalized_value")
        if raw is None:
            raw = fact.get("value", fact.get("raw_value", ""))
        if normalize_value_for_conflict(raw) not in norm_vals:
            continue
        sr = str(fact.get("source_ref") or "").strip()
        if not sr:
            continue
        collected.append(
            {
                "source_type": str(fact.get("source_type") or fact.get("type") or "unknown"),
                "source_id": sr,
                "timestamp": str(fact.get("observed_at") or fact.get("created_at") or ""),
                "field": fk,
                "document_id": str(fact.get("document_id") or ""),
                "message_id": str(fact.get("message_id") or ""),
                "confidence": fact.get("confidence"),
                "evidence_role": "contradicts",
            }
        )
    if len(collected) < 2:
        return
    merged = normalize_evidence_refs(collected, default_role="contradicts")
    if not merged:
        return
    row["source_refs"] = list(merged)
    row["evidence_refs"] = list(merged)


def sort_conflicts_for_operator_projection(conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer decision-usable, evidence-backed rows; push projection-suppressed city noise last."""

    def _key(row: dict[str, Any]) -> tuple[int, int, int, str]:
        excl = 1 if row.get("exclude_from_operator_projection_top") else 0
        usable = 1 if row.get("decision_usable") else 0
        refs = int(row.get("evidence_ref_count") or _conflict_evidence_ref_count(row))
        sev_rank = {"blocking": 0, "warning": 1, "info": 2}.get(str(row.get("severity") or "warning"), 3)
        return (excl, -usable, -refs, sev_rank)

    return sorted(conflicts, key=_key)


def sort_gaps_for_operator_projection(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _key(row: dict[str, Any]) -> tuple[int, int, str]:
        excl = 1 if row.get("exclude_from_operator_projection_top") else 0
        usable = 1 if row.get("decision_usable") else 0
        return (excl, -usable, str(row.get("summary") or ""))

    return sorted(gaps, key=_key)


def feed_projection_summary_line(item: dict[str, Any]) -> str:
    """Single operator-facing line for feeds (PII-minimized when projection fields exist)."""

    for key in ("projection_summary", "safe_summary", "summary", "summary_pl"):
        raw = item.get(key)
        if raw is None:
            continue
        s = str(raw).strip()
        if s:
            return s[:500]
    t = str(item.get("type") or "").strip()
    return t[:500] if t else ""


def _operator_feed_text_has_contact_pii(text: str) -> bool:
    return _text_has_emailish(text) or _text_has_phoneish(text)


def _operator_feed_sanitize_free_text(value: Any, *, fallback: str) -> str:
    if not isinstance(value, str):
        return str(fallback)
    s = value.strip()
    if not s:
        return str(fallback)
    if _operator_feed_text_has_contact_pii(s):
        return str(fallback)
    return s


def operator_feed_plain_summary(text: str, *, fallback: str = "Sygnał operatorski (treść wrażliwa nie jest powielana).") -> str:
    """Redact e-mail/phone-like patterns from a single-line operator feed string."""

    return _operator_feed_sanitize_free_text(text, fallback=fallback)


def _operator_feed_sanitize_evidence_refs(refs: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ref in _list_of_dicts(refs):
        row: dict[str, Any] = {}
        for k, v in ref.items():
            if isinstance(v, str) and _operator_feed_text_has_contact_pii(v):
                row[k] = "[redacted]"
            else:
                row[k] = v
        out.append(row)
    return out


def operator_feed_conflicting_fact(row: dict[str, Any]) -> dict[str, Any]:
    """Slim, projection-safe conflict object for Daszek / operational feed (no raw contact values)."""

    if not isinstance(row, dict):
        return {}
    allow = (
        "conflict_id",
        "case_id",
        "fact_key",
        "predicate",
        "type",
        "severity",
        "status",
        "summary",
        "projection_summary",
        "safe_summary",
        "evidence_status",
        "evidence_ref_count",
        "decision_usable",
        "operator_verification_required",
        "exclude_from_operator_projection_top",
        "requires_operator",
        "suggested_resolution",
        "redaction_applied",
        "sensitive_value_redacted",
        "value_count",
    )
    out: dict[str, Any] = {}
    for k in allow:
        if k not in row:
            continue
        v = row[k]
        if k in {"summary", "projection_summary", "safe_summary", "suggested_resolution"} and isinstance(v, str):
            fb = feed_projection_summary_line(row) or "Konflikt — dane kontaktowe nie są powielane w kanale projekcji."
            v = _operator_feed_sanitize_free_text(v, fallback=fb)
        out[k] = v
    refs = row.get("evidence_refs") or row.get("source_refs")
    if _list_of_dicts(refs):
        sanitized = _operator_feed_sanitize_evidence_refs(refs)
        out["evidence_refs"] = sanitized
        out["source_refs"] = sanitized
    line = feed_projection_summary_line(row)
    out["summary"] = operator_feed_plain_summary(
        str(line or out.get("summary") or ""),
        fallback="Konflikt — dane kontaktowe nie są powielane w kanale projekcji.",
    )[:500]
    return out


def operator_feed_completeness_gap(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    allow = (
        "gap_id",
        "case_id",
        "type",
        "summary",
        "projection_summary",
        "safe_summary",
        "required_for",
        "severity",
        "status",
        "evidence_status",
        "evidence_ref_count",
        "decision_usable",
        "operator_verification_required",
        "exclude_from_operator_projection_top",
        "requires_operator",
        "suggested_next_action",
        "redaction_applied",
        "sensitive_value_redacted",
    )
    out: dict[str, Any] = {}
    for k in allow:
        if k not in row:
            continue
        v = row[k]
        if k in {"summary", "projection_summary", "safe_summary", "suggested_next_action"} and isinstance(v, str):
            fb = (
                str(row.get("projection_summary") or row.get("safe_summary") or row.get("type") or "gap")
                or "Luka — dane wrażliwe nie są powielane w kanale projekcji."
            )
            v = _operator_feed_sanitize_free_text(v, fallback=fb)
        out[k] = v
    refs = row.get("evidence_refs") or row.get("source_refs")
    if _list_of_dicts(refs):
        sanitized = _operator_feed_sanitize_evidence_refs(refs)
        out["evidence_refs"] = sanitized
        out["source_refs"] = sanitized
    line = feed_projection_summary_line(row)
    out["summary"] = operator_feed_plain_summary(
        str(line or out.get("summary") or ""),
        fallback="Luka — dane wrażliwe nie są powielane w kanale projekcji.",
    )[:500]
    return out


def operator_feed_evidence_card(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    allow = (
        "evidence_id",
        "case_id",
        "source_type",
        "source_id",
        "title",
        "summary",
        "url",
        "quote_or_chunk_id",
        "confidence",
        "timestamp",
        "parser_status",
        "provenance_note",
    )
    placeholder = "Metadane źródła — pola tekstowe z danymi kontaktowymi nie są powielane w projekcji operatora."
    out: dict[str, Any] = {}
    for k in allow:
        if k not in row:
            continue
        v = row[k]
        if k in {"summary", "title", "url", "provenance_note"} and isinstance(v, str):
            v = _operator_feed_sanitize_free_text(v, fallback=placeholder)
        out[k] = v
    out["summary"] = operator_feed_plain_summary(str(out.get("summary") or ""), fallback=placeholder)[:500]
    out["title"] = operator_feed_plain_summary(str(out.get("title") or ""), fallback="Źródło")[:300]
    return out


def operator_feed_context_quality(row: dict[str, Any]) -> dict[str, Any]:
    """Projection-safe readiness block for operator feed; no diagnostic internals."""

    return operator_feed_context_quality_view(row if isinstance(row, dict) else {})


def operator_feed_conflicting_facts(rows: Any) -> list[dict[str, Any]]:
    return [operator_feed_conflicting_fact(r) for r in _list_of_dicts(rows)]


def operator_feed_completeness_gaps(rows: Any) -> list[dict[str, Any]]:
    return [operator_feed_completeness_gap(r) for r in _list_of_dicts(rows)]


def operator_feed_evidence_cards(rows: Any) -> list[dict[str, Any]]:
    return [operator_feed_evidence_card(r) for r in _list_of_dicts(rows)]


def build_case_context_pack_vnext(
    pack: Any,
    *,
    evidence_limit: int = 32,
    chunk_limit: int = 8,
    conflict_limit: int = 48,
    gap_limit: int = 48,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return a stable vNext contract from an existing CaseContextPack/dict."""

    source = _as_dict(pack)
    case_id = str(source.get("case_id") or "").strip()
    snapshot = _dict(source.get("snapshot"))
    runtime_state = _dict(source.get("runtime_state"))
    next_action = _dict(source.get("next_action"))
    active_facts = _list_of_dicts(source.get("active_facts"))
    precedent_evidence_refs = _list_of_dicts(source.get("precedent_evidence_refs"))
    drive_documents = _list_of_dicts(source.get("drive_documents_summary"))
    graph_hints = _list_of_dicts(source.get("graph_hints"))
    source_refs = _list_of_dicts(source.get("source_refs"))
    calendar_block = _dict(source.get("calendar"))
    calendar_events = calendar_block.get("events") or []
    if not isinstance(calendar_events, list):
        calendar_events = []

    conflicts = normalize_conflicting_facts(source.get("conflicting_facts"), case_id=case_id)
    conflicts = merge_conflicts_deterministic(
        conflicts,
        case_id=case_id,
        active_facts=active_facts,
        snapshot=snapshot,
        next_action=next_action,
    )
    for row in conflicts:
        _try_attach_evidence_refs_from_active_facts(row, active_facts=active_facts)
        if _conflict_evidence_ref_count(row) > 0:
            refs2 = normalize_evidence_refs(row.get("source_refs") or row.get("evidence_refs"), default_role="contradicts")
            row["evidence_refs"] = list(refs2)
            row["source_refs"] = list(refs2)
            row["status"] = _conflict_status(row.get("status"), refs=refs2)
    conflicts = _dedupe_conflicts_prefer_evidence(conflicts)
    conflicts = [_finalize_conflict_row(row) for row in conflicts]

    gaps = normalize_completeness_gaps(source.get("completeness_gaps"), case_id=case_id)
    gaps = merge_gaps_deterministic(
        gaps,
        case_id=case_id,
        snapshot=snapshot,
        active_facts=active_facts,
        drive_documents_summary=drive_documents,
        source_refs=source_refs,
    )
    gaps = [_finalize_gap_row(row) for row in gaps]

    evidence_cards = build_evidence_cards(
        case_id=case_id,
        source_refs=source_refs,
        facts=active_facts,
        drive_documents=drive_documents,
        chunks=_list_of_dicts(source.get("relevant_chunks")),
        chunk_limit=max(0, int(chunk_limit)),
        evidence_limit=max(0, int(evidence_limit)),
    )
    facts_out = normalize_facts(active_facts, case_id=case_id)
    related_entities = build_related_entities(graph_hints)
    service_signals, marketing_signals = build_downstream_signals(
        case_id=case_id,
        drive_documents=drive_documents,
        gaps=gaps,
        graph_hints=graph_hints,
        evidence_cards=evidence_cards,
    )

    warnings, limitations = _collect_warnings_and_limitations(
        facts=facts_out,
        conflicts=conflicts,
        gaps=gaps,
        calendar_events=calendar_events,
        drive_documents=drive_documents,
    )

    ts = generated_at or datetime.now(timezone.utc).isoformat()
    conflicts = conflicts[: max(0, int(conflict_limit))]
    gaps = gaps[: max(0, int(gap_limit))]

    blocking_c = any(str(c.get("severity")) == "blocking" for c in conflicts)
    blocking_g = any(str(g.get("severity")) == "blocking" for g in gaps)
    context_quality = _build_context_quality(
        conflicts=conflicts,
        gaps=gaps,
        warnings=warnings,
        facts=facts_out,
        evidence_cards=evidence_cards,
        source_refs=source_refs,
    )

    return {
        "contract_name": CONTRACT_NAME,
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "version": CONTRACT_VERSION,
        "pack_build": PACK_BUILD,
        "case_id": case_id,
        "generated_at": ts,
        "source_scope": "mailbox_memory+drive_projection+vnext_deterministic",
        "warnings": warnings,
        "limitations": limitations,
        "hot_state": {
            "snapshot": snapshot,
            "runtime_state": runtime_state,
            "next_action": next_action,
        },
        "case_summary": {
            "status": str(snapshot.get("status") or ""),
            "summary_text": str(snapshot.get("summary_text") or snapshot.get("summary") or ""),
            "recommended_next_action": str(next_action.get("next_action") or snapshot.get("recommended_next_action") or ""),
        },
        "messages_summary": _messages_summary(source_refs, _list_of_dicts(source.get("recent_events"))),
        "drive_documents_summary": drive_documents,
        "calendar_context": calendar_events,
        "evidence_cards": evidence_cards,
        "facts": facts_out,
        "conflicting_facts": conflicts,
        "completeness_gaps": gaps,
        "graph_hints": graph_hints,
        "related_entities": related_entities,
        "service_signals": service_signals,
        "marketing_signals": marketing_signals,
        "policy_context": {
            "autonomy_mode": "operator_safe_projection",
            "outbound_actions": "requires_operator_approval",
            "truth_mutation": "requires_adjudication_or_rule",
            "human_review_suggested": bool(blocking_c or blocking_g or warnings),
        },
        "operator_history": _list_of_dicts(source.get("execution_results")),
        "proposed_next_actions": _list_of_dicts(source.get("action_proposals")),
        "source_refs": source_refs,
        "precedent_evidence_refs": precedent_evidence_refs,
        "runtime_state": runtime_state,
        "vector_retrieval": _dict(source.get("vector_retrieval")),
        "has_blocking_conflicts": blocking_c,
        "has_blocking_gaps": blocking_g,
        "context_quality": context_quality,
    }


def normalize_facts(facts: Any, *, case_id: str) -> list[dict[str, Any]]:
    # Include superseded explicitly: unknown statuses fall through to inferred, which
    # would make a settled prior fact look current in the vNext contract (FACT-04).
    allowed_status = {"confirmed", "inferred", "disputed", "stale", "rejected", "unproven", "superseded"}
    out: list[dict[str, Any]] = []
    for item in _list_of_dicts(facts):
        predicate = str(item.get("predicate") or item.get("fact_key") or item.get("key") or "").strip()
        value = item.get("value", item.get("normalized_value", item.get("raw_value", "")))
        fact_id = str(item.get("fact_id") or "").strip() or _stable_id("fact", case_id, predicate, str(value))
        status = str(item.get("status") or "inferred").strip()
        if status == "active":
            status = "inferred"
        if status not in allowed_status:
            status = "unproven" if status in {"", "unknown", "pending"} else "inferred"
        observed_at = str(item.get("observed_at") or item.get("created_at") or "")
        notes = str(item.get("notes") or item.get("note") or "").strip()
        refs = _source_refs_for_item(item)
        out.append(
            {
                "fact_id": fact_id,
                "case_id": str(item.get("case_id") or case_id),
                "subject": str(item.get("subject") or item.get("entity_scope") or "case"),
                "predicate": predicate,
                "value": value,
                "confidence": _bounded_float(item.get("confidence")),
                "status": status,
                "source_refs": refs,
                "valid_from": str(item.get("valid_from") or observed_at or ""),
                "valid_to": item.get("valid_to"),
                "observed_at": observed_at,
                "created_by": str(item.get("created_by") or "extractor"),
                "last_reviewed_by": str(item.get("last_reviewed_by") or "system"),
                "notes": notes,
            }
        )
    return out


def normalize_conflicting_facts(conflicts: Any, *, case_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _list_of_dicts(conflicts):
        fact_key = str(item.get("fact_key") or item.get("predicate") or item.get("type") or "").strip()
        values = list(item.get("values") or item.get("facts_in_conflict") or [])
        summary = str(item.get("summary") or item.get("summary_pl") or "").strip()
        if not summary:
            suffix = f": {', '.join(str(v) for v in values)}" if values else ""
            summary = f"Conflicting fact {fact_key}{suffix}".strip()
        refs = normalize_evidence_refs(item.get("source_refs") or item.get("evidence_refs"), default_role="contradicts")
        out.append(
            {
                "conflict_id": str(item.get("conflict_id") or "").strip()
                or _stable_id(
                    "conflict",
                    case_id,
                    fact_key,
                    tuple(sorted(normalize_value_for_conflict(v) for v in values)),
                    str(item.get("type") or "fact_conflict"),
                ),
                "case_id": str(item.get("case_id") or case_id),
                "fact_key": fact_key,
                "predicate": fact_key,
                "type": str(item.get("type") or "fact_conflict"),
                "summary": summary,
                "severity": _severity(item.get("severity"), default="warning"),
                "facts_in_conflict": values,
                "values": values,
                "source_refs": refs,
                "evidence_refs": refs,
                "status": _conflict_status(item.get("status"), refs=refs),
                "operator_decision": item.get("operator_decision"),
                "suggested_resolution": str(item.get("suggested_resolution") or ""),
                "requires_operator": bool(item.get("requires_operator", True)),
            }
        )
    return out


def normalize_completeness_gaps(gaps: Any, *, case_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in gaps or []:
        req_for = ""
        requires_op = True
        if isinstance(raw, dict):
            summary = str(raw.get("summary") or raw.get("summary_pl") or raw.get("text") or "").strip()
            gap_type = _gap_type(raw.get("type") or _infer_gap_type(summary))
            evidence_refs = normalize_evidence_refs(raw.get("evidence_refs") or raw.get("source_refs"), default_role="explains_gap")
            severity = _severity(raw.get("severity"), default="warning")
            status = _gap_status(raw.get("status"), refs=evidence_refs)
            suggested = str(raw.get("suggested_next_action") or "")
            req_for = str(raw.get("required_for") or "").strip()
            if not req_for:
                req_for = _infer_gap_required_for(gap_type, summary)
            req_for = _gap_required_for(req_for)
            requires_op = bool(raw.get("requires_operator", True))
        else:
            summary = str(raw or "").strip()
            gap_type = _gap_type(_infer_gap_type(summary))
            evidence_refs = []
            severity = "warning"
            status = "weak_evidence"
            suggested = ""
            req_for = _gap_required_for(_infer_gap_required_for(gap_type, summary))
            requires_op = True
        if not summary:
            continue
        out.append(
            {
                "gap_id": _stable_id("gap", case_id, gap_type, req_for, normalize_value_for_conflict(summary)),
                "case_id": case_id,
                "type": gap_type,
                "summary": summary,
                "required_for": req_for,
                "severity": severity,
                "evidence_refs": evidence_refs,
                "suggested_next_action": suggested,
                "status": status,
                "requires_operator": requires_op,
            }
        )
    return out


def build_evidence_cards(
    *,
    case_id: str,
    source_refs: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    drive_documents: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    chunk_limit: int = 8,
    evidence_limit: int = 32,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    chunk_limit = max(0, chunk_limit)
    evidence_limit = max(0, evidence_limit)

    for item in source_refs:
        card = _evidence_card_from_source_ref(case_id, item)
        _append_unique(cards, seen, card)
    for item in facts:
        card = _evidence_card_from_fact(case_id, item)
        _append_unique(cards, seen, card)
    for item in drive_documents:
        card = _evidence_card_from_drive_document(case_id, item)
        _append_unique(cards, seen, card)
    for item in chunks[:chunk_limit]:
        card = _evidence_card_from_chunk(case_id, item)
        _append_unique(cards, seen, card)

    return cards[:evidence_limit]


def build_related_entities(graph_hints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in graph_hints[:20]:
        title = str(item.get("related_title") or item.get("target_title") or "").strip()
        entity_type = str(item.get("related_node_type") or item.get("target_node_type") or item.get("node_type") or "related_entity").strip()
        relation = str(item.get("relation_type") or item.get("predicate") or "").strip()
        entity_id = str(item.get("entity_id") or item.get("related_node_id") or "").strip() or _stable_id("entity", entity_type, title, relation)
        out.append(
            {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "label": title,
                "relations": [
                    {
                        "predicate": relation,
                        "target_entity_id": entity_id,
                        "confidence": _bounded_float(item.get("confidence")),
                        "source_refs": _source_refs_for_item(item),
                        "valid_from": str(item.get("valid_from") or item.get("observed_at") or ""),
                        "valid_to": item.get("valid_to"),
                        "status": str(item.get("status") or "inferred"),
                    }
                ],
            }
        )
    return out


def _rule_service_from_documents_or_gaps(
    *,
    case_id: str,
    drive_documents: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any] | None:
    kinds = {str(item.get("document_kind") or "").strip() for item in drive_documents}
    gap_text = " ".join(str(item.get("summary") or "") for item in gaps).lower()
    if kinds.intersection({"service_protocol", "warranty_card"}) or "service" in gap_text or "serwis" in gap_text:
        return _downstream_signal(
            case_id=case_id,
            subtype="warranty_service_state",
            signal_type="service",
            summary="Wykryto kontekst serwisu lub gwarancji — utrzymaj widoczność dla operatora.",
            action="Zweryfikuj stan serwisu i poproś o brakujące dowody, jeśli są wymagane.",
            evidence_cards=evidence_cards,
        )
    return None


def _rule_marketing_media(
    *,
    case_id: str,
    drive_documents: list[dict[str, Any]],
    graph_hints: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any] | None:
    media_present = any(str(item.get("document_kind") or "") in {"media_asset", "media_bundle"} for item in drive_documents) or any(
        "media" in str(item.get("relation_type") or "").lower() for item in graph_hints
    )
    if media_present:
        return _downstream_signal(
            case_id=case_id,
            subtype="media_evidence_presence",
            signal_type="marketing",
            summary="Dostępne są materiały medialne do przeglądu operatora.",
            action="Przed prośbą o opinię, referencję lub case study sprawdź zgodę i politykę zgód.",
            evidence_cards=evidence_cards,
        )
    return None


# Signal rules registry — ordered by priority (lower = higher priority)
SIGNAL_RULES: list[dict[str, Any]] = [
    {"rule_id": "svc_warranty", "family": "service", "priority": 10, "trigger": _rule_service_from_documents_or_gaps},
    {"rule_id": "mkt_media", "family": "marketing", "priority": 20, "trigger": _rule_marketing_media},
]


def build_downstream_signals(
    *,
    case_id: str,
    drive_documents: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    graph_hints: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic downstream recommendations derived from projection fields.

    Iterates over SIGNAL_RULES sorted by priority (ascending). Each rule produces
    a raw signal dict tagged with rule_id; finalize_downstream_signal normalizes
    the shape and preserves rule_id.
    """
    service_signals: list[dict[str, Any]] = []
    marketing_signals: list[dict[str, Any]] = []

    sorted_rules = sorted(SIGNAL_RULES, key=lambda r: int(r.get("priority", 99)))
    for rule in sorted_rules:
        trigger = rule["trigger"]
        rule_id = rule["rule_id"]
        family = rule["family"]
        try:
            result = trigger(
                case_id=case_id,
                drive_documents=drive_documents,
                gaps=gaps,
                graph_hints=graph_hints,
                evidence_cards=evidence_cards,
            )
        except Exception:
            # Skip broken rules gracefully
            continue
        if result is None:
            continue
        result["rule_id"] = rule_id
        result["family"] = family
        if family == "service":
            service_signals.append(result)
        elif family == "marketing":
            marketing_signals.append(result)

    return (
        [finalize_downstream_signal(item) for item in service_signals],
        [finalize_downstream_signal(item) for item in marketing_signals],
    )


def finalize_downstream_signal(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize any downstream recommendation to the projection-safe DownstreamSignal shape."""

    case_id = str(row.get("case_id") or "").strip()
    signal_type = str(row.get("type") or row.get("signal_type") or "").strip()
    subtype = str(row.get("subtype") or "").strip()
    summary = str(row.get("summary") or "").strip()
    action = str(row.get("recommended_operator_action") or row.get("recommended_action") or "").strip()
    risk = str(row.get("risk_level") or "low").strip().lower()
    if risk not in {"low", "medium", "high"}:
        risk = "low"
    requires = row.get("requires_approval")
    if requires is None:
        requires = True
    evidence_refs: list[dict[str, Any]] = []
    for ref in _list_of_dicts(row.get("evidence_refs"))[:12]:
        evidence_refs.append(
            {
                "evidence_id": ref.get("evidence_id"),
                "source_type": str(ref.get("source_type") or ""),
                "source_id": str(ref.get("source_id") or ""),
            }
        )
    policy_status = str(row.get("policy_status") or "allowed_for_projection")
    status = str(row.get("status") or "new")
    signal_id = str(row.get("signal_id") or "").strip() or _stable_id("downstream", case_id, signal_type, subtype, summary)
    return {
        "signal_id": signal_id,
        "case_id": case_id,
        "type": signal_type,
        "subtype": subtype,
        "rule_id": str(row.get("rule_id") or "").strip(),
        "summary": summary,
        "recommended_operator_action": action,
        "risk_level": risk,
        "requires_approval": bool(requires),
        "evidence_refs": evidence_refs,
        "policy_status": policy_status,
        "status": status,
    }


def _downstream_signal(
    *,
    case_id: str,
    subtype: str,
    signal_type: str,
    summary: str,
    action: str,
    evidence_cards: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "signal_id": _stable_id("downstream", case_id, signal_type, subtype, summary),
        "case_id": case_id,
        "type": signal_type,
        "subtype": subtype,
        "summary": summary,
        "recommended_operator_action": action,
        "risk_level": "low",
        "requires_approval": True,
        "evidence_refs": [
            {
                "evidence_id": item.get("evidence_id"),
                "source_type": item.get("source_type"),
                "source_id": item.get("source_id"),
            }
            for item in evidence_cards[:5]
        ]
        if evidence_cards
        else [],
        "policy_status": "allowed_for_projection",
        "status": "new",
    }


def _safe_drive_url(item: dict[str, Any]) -> str:
    for key in ("web_view_link", "webViewLink", "web_link", "alternate_link"):
        u = str(item.get(key) or "").strip()
        if u.startswith("https://drive.google.com") or u.startswith("https://docs.google.com"):
            return u
    return ""


def _evidence_card_from_source_ref(case_id: str, item: dict[str, Any]) -> dict[str, Any]:
    raw_type = str(item.get("source_type") or item.get("type") or item.get("source_kind") or "source_ref").strip()
    if raw_type in {"message", "gmail"}:
        st = "gmail_message"
    elif raw_type in {"calendar", "calendar_event"}:
        st = "calendar_event"
    else:
        st = raw_type or "unknown"
    source_id = str(item.get("source_id") or item.get("message_id") or item.get("document_id") or item.get("source_ref") or "")
    title = str(item.get("title") or item.get("subject") or "").strip()
    summary = str(item.get("summary") or item.get("source_ref") or source_id)
    return _evidence_card(
        case_id=case_id,
        source_type=st,
        source_id=source_id,
        title=title or summary[:120],
        summary=summary,
        quote_or_chunk_id=str(item.get("quote_or_chunk_id") or item.get("chunk_id") or ""),
        confidence=item.get("confidence"),
        timestamp=str(item.get("timestamp") or item.get("observed_at") or item.get("created_at") or ""),
        parser_status="",
        provenance_note="Mailbox source_ref",
        url="",
    )


def _evidence_card_from_fact(case_id: str, item: dict[str, Any]) -> dict[str, Any]:
    predicate = str(item.get("predicate") or item.get("fact_key") or "")
    value = item.get("value", item.get("normalized_value", item.get("raw_value", "")))
    summary = f"{predicate}: {value}".strip(": ")
    return _evidence_card(
        case_id=case_id,
        source_type=str(item.get("source_type") or "fact"),
        source_id=str(item.get("source_ref") or item.get("fact_id") or ""),
        title=predicate or "Fact",
        summary=summary,
        quote_or_chunk_id=str(item.get("quote_or_chunk_id") or ""),
        confidence=item.get("confidence"),
        timestamp=str(item.get("observed_at") or item.get("created_at") or ""),
        parser_status="",
        provenance_note="Derived from mailbox fact row",
        url="",
    )


def _evidence_card_from_drive_document(case_id: str, item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("file_name") or item.get("title") or "").strip()
    summary = str(item.get("summary_text") or title or "")
    ps = str(item.get("parser_status") or item.get("embedding_status") or item.get("linkage_status") or "").strip()
    return _evidence_card(
        case_id=case_id,
        source_type="drive_document",
        source_id=str(item.get("document_id") or item.get("drive_item_id") or item.get("source_ref") or ""),
        title=title or "Drive document",
        summary=summary,
        quote_or_chunk_id="",
        confidence=item.get("confidence") or item.get("link_confidence") or item.get("classification_confidence"),
        timestamp=str(item.get("updated_at") or item.get("created_at") or ""),
        parser_status=ps,
        provenance_note="Drive projection summary",
        url=_safe_drive_url(item),
    )


def _evidence_card_from_chunk(case_id: str, item: dict[str, Any]) -> dict[str, Any]:
    ps = str(item.get("embedding_status") or item.get("parser_status") or "").strip()
    return _evidence_card(
        case_id=case_id,
        source_type="document_chunk",
        source_id=str(item.get("document_id") or item.get("source_ref") or ""),
        title=str(item.get("metadata", {}).get("file_name") or "Chunk") if isinstance(item.get("metadata"), dict) else "Chunk",
        summary=str(item.get("chunk_text") or "")[:240],
        quote_or_chunk_id=str(item.get("chunk_id") or ""),
        confidence=item.get("retrieval_score") or item.get("confidence"),
        timestamp=str(item.get("created_at") or ""),
        parser_status=ps,
        provenance_note="Mailbox document chunk",
        url="",
    )


def _evidence_card(
    *,
    case_id: str,
    source_type: str,
    source_id: str,
    title: str,
    summary: str,
    quote_or_chunk_id: str,
    confidence: Any,
    timestamp: str,
    parser_status: str,
    provenance_note: str,
    url: str,
) -> dict[str, Any]:
    return {
        "evidence_id": _stable_id("ev", case_id, source_type, source_id, quote_or_chunk_id, summary[:80]),
        "case_id": case_id,
        "source_type": source_type,
        "source_id": source_id,
        "title": title,
        "quote_or_chunk_id": quote_or_chunk_id,
        "summary": summary,
        "confidence": _bounded_float(confidence),
        "timestamp": timestamp,
        "parser_status": parser_status,
        "provenance_note": provenance_note,
        "url": url,
    }


def _append_unique(cards: list[dict[str, Any]], seen: set[str], card: dict[str, Any]) -> None:
    evidence_id = str(card.get("evidence_id") or "")
    if not evidence_id or evidence_id in seen:
        return
    seen.add(evidence_id)
    cards.append(card)


def _messages_summary(source_refs: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    message_ids = []
    for item in source_refs:
        mid = str(item.get("message_id") or item.get("source_id") or "").strip()
        st = str(item.get("source_type") or item.get("type") or "")
        if mid and ("gmail" in st or st in {"message", "gmail_message"}):
            message_ids.append(mid)
    return {
        "message_ids": list(dict.fromkeys(message_ids))[:20],
        "recent_events": events[:12],
    }


def _source_refs_for_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    refs = item.get("source_refs") or item.get("evidence_refs")
    if isinstance(refs, list):
        return normalize_evidence_refs(refs, default_role=str(item.get("evidence_role") or "supports"))
    source_ref = str(item.get("source_ref") or "").strip()
    if not source_ref:
        return []
    return normalize_evidence_refs(
        [
            {
                "source_type": str(item.get("source_type") or item.get("type") or "source_ref"),
                "source_id": source_ref,
                "timestamp": str(item.get("observed_at") or item.get("created_at") or ""),
                "field": str(item.get("fact_key") or item.get("predicate") or ""),
                "chunk_id": str(item.get("chunk_id") or item.get("quote_or_chunk_id") or ""),
                "document_id": str(item.get("document_id") or ""),
                "message_id": str(item.get("message_id") or ""),
                "evidence_role": str(item.get("evidence_role") or "supports"),
                "confidence": item.get("confidence"),
            }
        ],
        default_role="supports",
    )


def _infer_gap_required_for(gap_type: str, summary: str) -> str:
    gt = (gap_type or "").strip().lower()
    mapping = {
        "missing_invoice": "document_review",
        "missing_protocol": "document_review",
        "missing_photo": "service_followup",
        "missing_device_model": "service_followup",
        "missing_customer_answer": "scheduling",
        "missing_scheduling_evidence": "scheduling",
        "missing_contact": "operator_review",
        "missing_customer_data": "operator_review",
        "missing_address": "offer_handoff",
        "missing_drive_link": "document_review",
        "missing_document": "document_review",
        "missing_case_link": "case_understanding",
        "missing_evidence": "case_understanding",
    }
    if gt in mapping:
        return mapping[gt]
    lowered = summary.lower()
    if "serwis" in lowered or "service" in lowered:
        return "service_followup"
    if "ofert" in lowered or "offer" in lowered:
        return "offer_handoff"
    return "case_understanding"


def _dedupe_conflicts_prefer_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per predicate/fact_key; prefer the row with richer evidence_refs."""

    def _pred_key(row: dict[str, Any]) -> str:
        return str(row.get("fact_key") or row.get("predicate") or "").strip()

    def _val_key(row: dict[str, Any]) -> tuple[str, ...]:
        vals = row.get("values") or row.get("facts_in_conflict") or []
        if not isinstance(vals, list):
            vals = [vals]
        return tuple(sorted(normalize_value_for_conflict(v) for v in vals if str(v).strip()))

    buckets: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
    loose: list[dict[str, Any]] = []
    for row in rows:
        pk = _pred_key(row)
        vk = _val_key(row)
        if not pk and not vk:
            loose.append(row)
            continue
        key = (pk, vk) if pk else ("", vk)
        buckets.setdefault(key, []).append(row)

    out: list[dict[str, Any]] = []
    for group in buckets.values():
        ranked = sorted(
            group,
            key=lambda r: len(_list_of_dicts(r.get("source_refs") or r.get("evidence_refs"))),
            reverse=True,
        )
        out.append(ranked[0])
    out.extend(loose)
    return out


def _finalize_conflict_row(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    refs = normalize_evidence_refs(row.get("source_refs") or row.get("evidence_refs"), default_role="contradicts")
    row["evidence_refs"] = list(refs)
    row["source_refs"] = list(refs)
    values = row.get("values") or row.get("facts_in_conflict") or []
    if not isinstance(values, list):
        values = [values]
    row["values"] = values
    row["facts_in_conflict"] = values
    fact_key = str(row.get("fact_key") or row.get("predicate") or "").strip()
    row["fact_key"] = fact_key
    row["predicate"] = str(row.get("predicate") or fact_key)
    row["severity"] = _severity(row.get("severity"), default="warning")
    row["status"] = _conflict_status(row.get("status"), refs=refs)
    row["conflict_id"] = str(row.get("conflict_id") or "").strip() or _stable_id(
        "conflict",
        str(row.get("case_id") or ""),
        fact_key,
        tuple(sorted(normalize_value_for_conflict(v) for v in values)),
        str(row.get("type") or "fact_conflict"),
    )
    if not row.get("suggested_resolution"):
        row["suggested_resolution"] = "Zweryfikuj zrodla i ustal wartosc po decyzji operatora."
    row["requires_operator"] = bool(row.get("requires_operator", True))
    if str(row.get("severity")) == "blocking" and not refs:
        row["severity"] = "warning"

    ref_count = len(refs)
    row["evidence_ref_count"] = ref_count
    ev_status = _conflict_evidence_status(ref_count=ref_count, status=str(row.get("status") or ""))
    row["evidence_status"] = ev_status

    exclude_top = False
    if _predicate_is_cityish(fact_key) and ref_count == 0:
        exclude_top = True
        row["severity"] = "info"
    row["exclude_from_operator_projection_top"] = exclude_top

    value_count = len([v for v in values if str(v).strip()])
    row["value_count"] = value_count
    raw_summary = str(row.get("summary") or "").strip()
    sensitive_redacted = False
    summary_changed = False

    if _predicate_is_contact_sensitive(fact_key):
        proj = _contact_projection_summary_pl(fact_key)
        sensitive_redacted = True
        summary_changed = True
        row["summary"] = proj
    elif _predicate_is_cityish(fact_key):
        proj = _city_projection_summary_pl(values, evidence_ref_count=ref_count)
        summary_changed = proj != raw_summary
        row["summary"] = proj
    else:
        proj = raw_summary
        joined = " ".join(str(v) for v in values)
        scan = f"{raw_summary} {joined}"
        if _text_has_emailish(scan) or _text_has_phoneish(scan):
            proj = _contact_projection_summary_pl("contact")
            sensitive_redacted = True
            summary_changed = True
            row["summary"] = proj

    row["projection_summary"] = proj
    row["safe_summary"] = proj
    row["redaction_applied"] = bool(summary_changed or sensitive_redacted)
    row["sensitive_value_redacted"] = bool(sensitive_redacted)

    row["operator_verification_required"] = bool(row.get("requires_operator", True)) or ev_status in {"weak", "missing"}
    row["decision_usable"] = (
        ev_status == "supported"
        and str(row.get("severity")) in {"warning", "blocking"}
        and not exclude_top
    )
    return row


def _finalize_gap_row(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    refs = normalize_evidence_refs(row.get("evidence_refs") or row.get("source_refs"), default_role="explains_gap")
    row["evidence_refs"] = refs
    row["source_refs"] = refs
    row["type"] = _gap_type(row.get("type") or _infer_gap_type(str(row.get("summary") or "")))
    if not str(row.get("required_for") or "").strip() or row.get("required_for") == "case_progress":
        row["required_for"] = _infer_gap_required_for(str(row.get("type") or ""), str(row.get("summary") or ""))
    row["required_for"] = _gap_required_for(row.get("required_for"))
    row["severity"] = _severity(row.get("severity"), default="warning")
    row["status"] = _gap_status(row.get("status"), refs=refs)
    row["gap_id"] = str(row.get("gap_id") or "").strip() or _stable_id(
        "gap",
        str(row.get("case_id") or ""),
        str(row.get("type") or ""),
        str(row.get("required_for") or ""),
        normalize_value_for_conflict(str(row.get("summary") or "")),
    )
    row["requires_operator"] = bool(row.get("requires_operator", True))

    ref_count = len(refs)
    row["evidence_ref_count"] = ref_count
    if ref_count <= 0:
        gap_ev = "missing"
    elif str(row.get("status") or "").strip().lower() == "weak_evidence":
        gap_ev = "weak"
    else:
        gap_ev = "supported"
    row["evidence_status"] = gap_ev
    row["operator_verification_required"] = bool(row.get("requires_operator", True)) or gap_ev in {"weak", "missing"}
    row["decision_usable"] = gap_ev == "supported"
    row["exclude_from_operator_projection_top"] = False

    raw_s = str(row.get("summary") or "").strip()
    proj = raw_s
    gap_redacted = False
    if _text_has_emailish(raw_s) or _text_has_phoneish(raw_s):
        proj = (
            "Luka z opisem kontaktowym — szczegóły wymagają weryfikacji operatora "
            "(wartości e-mail/telefon nie są powielane w podsumowaniu projekcji)."
        )
        gap_redacted = True
        row["summary"] = proj
    row["projection_summary"] = proj
    row["safe_summary"] = proj
    row["redaction_applied"] = gap_redacted
    row["sensitive_value_redacted"] = gap_redacted
    row["value_count"] = 0
    return row


def _collect_warnings_and_limitations(
    *,
    facts: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    calendar_events: list[Any],
    drive_documents: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    limitations: list[str] = []
    for fact in facts:
        if not _list_of_dicts(fact.get("source_refs")) and str(fact.get("status")) in {"inferred", "unproven"}:
            pred = str(fact.get("predicate") or "")
            warnings.append(f"Fakt bez pelnego provenance: {pred or fact.get('fact_id')}")
            if len(warnings) >= 12:
                break
    for conf in conflicts:
        if str(conf.get("severity")) == "warning" and not _list_of_dicts(conf.get("source_refs") or conf.get("evidence_refs")):
            csum = str(conf.get("projection_summary") or conf.get("summary") or "")[:120]
            warnings.append(f"Konflikt bez pelnych dowodow: {csum}")
            if len(warnings) >= 12:
                break
    for gap in gaps:
        if str(gap.get("status")) == "weak_evidence" or not _list_of_dicts(gap.get("evidence_refs")):
            warnings.append(f"Gap without evidence: {gap.get('summary', '')[:120]}")
            if len(warnings) >= 12:
                break
    if not calendar_events:
        limitations.append("calendar_context_empty")
    if not drive_documents:
        limitations.append("drive_documents_summary_empty")
    limitations.append("llm_output_not_operational_truth")
    limitations.append("projection_safe_read_only")
    return warnings, limitations


def _build_context_quality(
    *,
    conflicts: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    warnings: list[str],
    facts: list[dict[str, Any]],
    evidence_cards: list[dict[str, Any]],
    source_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    blocking_c = any(str(c.get("severity")) == "blocking" for c in conflicts)
    blocking_g = any(str(g.get("severity")) == "blocking" for g in gaps)
    weak_evidence_count = 0
    for item in [*conflicts, *gaps, *facts]:
        refs = _list_of_dicts(item.get("evidence_refs") or item.get("source_refs"))
        if str(item.get("status")) == "weak_evidence" or not refs:
            weak_evidence_count += 1
    evidence_warning_count = len(
        [w for w in warnings if "dowod" in w.lower() or "evidence" in w.lower() or "provenance" in w.lower()]
    )
    source_types = {
        str(item.get("source_type") or item.get("type") or "").strip()
        for item in [
            *evidence_cards,
            *source_refs,
            *(r for f in facts for r in _list_of_dicts(f.get("source_refs"))),
            *(r for c in conflicts for r in _list_of_dicts(c.get("evidence_refs"))),
            *(r for g in gaps for r in _list_of_dicts(g.get("evidence_refs"))),
        ]
        if isinstance(item, dict) and str(item.get("source_type") or item.get("type") or "").strip()
    }
    not_ready: list[str] = []
    if blocking_c:
        not_ready.append("blocking_conflicts")
    if blocking_g:
        not_ready.append("blocking_gaps")
    if evidence_warning_count:
        not_ready.append("evidence_warnings")
    if weak_evidence_count:
        not_ready.append("weak_or_missing_evidence")
    ready_for_decision = (
        not blocking_c and not blocking_g and evidence_warning_count == 0 and weak_evidence_count == 0
    )
    operator_review_possible = not blocking_c and not blocking_g
    if blocking_c or blocking_g:
        action_readiness = "not_ready"
    elif ready_for_decision:
        action_readiness = "decision_ready"
    else:
        action_readiness = "review_only"
    return normalize_context_quality({
        "has_blocking_conflicts": blocking_c,
        "has_blocking_gaps": blocking_g,
        "conflict_count": len(conflicts),
        "gap_count": len(gaps),
        "evidence_warning_count": evidence_warning_count,
        "ready_for_operator_review": not blocking_c and not blocking_g,
        "ready_for_decision": ready_for_decision,
        "operator_review_possible": operator_review_possible,
        "action_readiness": action_readiness,
        "not_ready_reasons": not_ready,
        "source_diversity_count": len(source_types),
        "weak_evidence_count": weak_evidence_count,
    })


def format_vnext_human_summary(contract: dict[str, Any]) -> str:
    """Short operator-facing text; no raw Gmail body."""

    lines: list[str] = []
    cid = str(contract.get("case_id") or "")
    lines.append(f"Case: {cid}")
    cs = contract.get("case_summary") or {}
    if isinstance(cs, dict):
        lines.append(f"Status: {cs.get('status', '')}")
        st = str(cs.get("summary_text") or "").strip()
        if st:
            lines.append(f"Summary: {st[:400]}")
        na = str(cs.get("recommended_next_action") or "").strip()
        if na:
            lines.append(f"Next action: {na[:200]}")
    for w in (contract.get("warnings") or [])[:5]:
        lines.append(f"Warning: {w}")
    for item in (contract.get("conflicting_facts") or [])[:5]:
        if isinstance(item, dict):
            lines.append(f"Conflict [{item.get('severity')}] {item.get('summary', '')[:200]}")
    for item in (contract.get("completeness_gaps") or [])[:5]:
        if isinstance(item, dict):
            lines.append(f"Gap [{item.get('severity')}] {item.get('summary', '')[:200]}")
    for lim in (contract.get("limitations") or [])[:6]:
        lines.append(f"Limitation: {lim}")
    return "\n".join(lines) + "\n"


def _infer_gap_type(summary: str) -> str:
    lowered = summary.lower()
    if "case" in lowered and ("link" in lowered or "reference" in lowered or "powiaz" in lowered):
        return "missing_case_link"
    if "drive" in lowered:
        return "missing_drive_link"
    if "termin" in lowered or "schedule" in lowered or "scheduling" in lowered or "service date" in lowered or "answer" in lowered or "odpow" in lowered:
        return "missing_scheduling_evidence"
    if "email" in lowered or "e-mail" in lowered or "phone" in lowered or "telefon" in lowered or "contact" in lowered or "kontakt" in lowered:
        return "missing_contact"
    if "address" in lowered or "adres" in lowered or "lokaliz" in lowered:
        return "missing_address"
    if "invoice" in lowered or "fakt" in lowered or "protocol" in lowered or "protok" in lowered or "document" in lowered or "dokument" in lowered:
        return "missing_document"
    return "missing_evidence"


def _gap_type(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    aliases = {
        "missing_customer_data": "missing_contact",
        "missing_customer_answer": "missing_scheduling_evidence",
        "missing_device_model": "missing_evidence",
        "missing_invoice": "missing_document",
        "missing_protocol": "missing_document",
        "missing_photo": "missing_evidence",
    }
    candidate = aliases.get(candidate, candidate)
    return candidate if candidate in GAP_TYPES else "unknown"


def _gap_required_for(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    aliases = {
        "closure": "operator_review",
        "case_progress": "case_understanding",
        "offer": "offer_handoff",
        "service": "service_followup",
        "warranty": "document_review",
        "unknown": "case_understanding",
    }
    candidate = aliases.get(candidate, candidate)
    return candidate if candidate in GAP_REQUIRED_FOR else "case_understanding"


def _conflict_status(value: Any, *, refs: list[dict[str, Any]]) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in CONFLICT_STATUSES and not (candidate == "open" and not refs):
        return candidate
    return "open" if refs else "weak_evidence"


def _gap_status(value: Any, *, refs: list[dict[str, Any]]) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in GAP_STATUSES and not (candidate == "open" and not refs):
        return candidate
    return "open" if refs else "weak_evidence"


def _severity(value: Any, *, default: str) -> str:
    candidate = str(value or default).strip().lower()
    return candidate if candidate in {"info", "warning", "blocking"} else default


def _bounded_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    return {}


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


__all__ = [
    "CONTRACT_NAME",
    "CONTRACT_VERSION",
    "PACK_BUILD",
    "SCHEMA_VERSION",
    "build_case_context_pack_vnext",
    "build_evidence_cards",
    "build_related_entities",
    "finalize_downstream_signal",
    "format_vnext_human_summary",
    "normalize_completeness_gaps",
    "normalize_conflicting_facts",
    "normalize_evidence_refs",
    "normalize_context_quality",
    "normalize_facts",
    "feed_projection_summary_line",
    "operator_feed_plain_summary",
    "operator_feed_conflicting_fact",
    "operator_feed_completeness_gap",
    "operator_feed_evidence_card",
    "operator_feed_context_quality",
    "operator_feed_conflicting_facts",
    "operator_feed_completeness_gaps",
    "operator_feed_evidence_cards",
    "sort_conflicts_for_operator_projection",
    "sort_gaps_for_operator_projection",
]
