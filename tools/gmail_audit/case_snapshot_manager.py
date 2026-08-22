"""Deterministic Hot State / Case Snapshot manager for V2.1 (CaseSnapshotHotState)."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from case_snapshot_hot_state_contract import (
    CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION,
    validate_case_snapshot_hot_state,
)
from mailbox_memory.active_facts import fetch_current_facts_for_case
from signal_contract import CanonicalSignal
from signal_journal import SignalJournal
from _protocols import CaseSnapshotStore

MAX_SNAPSHOT_VERSIONS = int(os.getenv("MAX_SNAPSHOT_VERSIONS", "10000"))


@dataclass(slots=True)
class CaseSnapshot:
    """Legacy thin snapshot (deprecated). Use CaseSnapshotHotState dict from apply_signal."""

    case_id: str
    status: str
    summary: str
    open_loops: list[str]
    last_facts: list[dict[str, Any]]
    confidence: float
    version: int
    snapshot_id: str
    source_signal_id: str
    cold_evidence_pointers: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "summary": self.summary,
            "open_loops": self.open_loops,
            "last_facts": self.last_facts,
            "confidence": self.confidence,
            "version": self.version,
            "snapshot_id": self.snapshot_id,
            "source_signal_id": self.source_signal_id,
            "cold_evidence_pointers": self.cold_evidence_pointers,
            "created_at": self.created_at,
        }


class CaseSnapshotManager:
    """Deterministic append-only Hot State manager over canonical signals."""

    def __init__(self, *, store: CaseSnapshotStore) -> None:
        self.store = store

    def apply_signal(
        self,
        signal: CanonicalSignal,
        *,
        case_id_override: str = "",
        trace_id: str = "",
    ) -> dict[str, Any]:
        case_id = str(case_id_override or _resolve_case_id(signal) or "").strip()
        if not case_id:
            raise ValueError("CaseSnapshotManager requires a resolvable case_id.")

        fact_rows = _build_signal_fact_rows(signal)
        if fact_rows:
            self.store.append_fact_rows(fact_rows)

        prior_versions = list(self.store.fetch_case_snapshot_versions(case_id, limit=MAX_SNAPSHOT_VERSIONS) or [])
        version = (int(prior_versions[-1].get("version") or 0) + 1) if prior_versions else 1
        hot_state = _build_case_snapshot_hot_state(
            store=self.store,
            case_id=case_id,
            signal=signal,
            version=version,
            prior_versions=prior_versions,
            trace_id=trace_id,
        )
        row = {
            "snapshot_id": hot_state["snapshot_id"],
            "case_id": case_id,
            "version": hot_state["snapshot_meta"]["version"],
            "source_signal_id": hot_state["snapshot_meta"]["source_signal_id"],
            "confidence": float(hot_state["snapshot_meta"].get("confidence") or 0.0),
            "snapshot_json": hot_state,
            "created_at": hot_state["snapshot_meta"]["created_at"],
        }
        self.store.append_case_snapshot_version(row)
        errs = validate_case_snapshot_hot_state(hot_state)
        if errs:
            hot_state.setdefault("snapshot_meta", {})["validation_warnings"] = errs
        return hot_state

    def rebuild_from_signal_journal(
        self,
        *,
        journal: SignalJournal,
        case_id: str = "",
        case_key_hint: str = "",
    ) -> dict[str, Any]:
        signals = journal.fetch_signals_for_case(case_id=case_id, case_key_hint=case_key_hint, limit=10_000)
        if not signals:
            return {}

        rebuilt_store = _EphemeralSnapshotStore()
        manager = CaseSnapshotManager(store=rebuilt_store)
        resolved_case_id = case_id or _resolve_case_id(signals[0]) or "case_rebuild"
        for signal in signals:
            manager.apply_signal(signal, case_id_override=resolved_case_id, trace_id="rebuild_from_journal")
        latest = rebuilt_store.fetch_latest_case_snapshot_version(resolved_case_id)
        if not latest:
            return {}
        return dict(latest.get("snapshot_json") or {})


def _build_case_snapshot_hot_state(
    *,
    store: CaseSnapshotStore,
    case_id: str,
    signal: CanonicalSignal,
    version: int,
    prior_versions: list[dict[str, Any]],
    trace_id: str,
) -> dict[str, Any]:
    # 4.2b: prefer store-level active fetch (FACT-01 filter remains as defense in depth).
    facts = list(fetch_current_facts_for_case(store, case_id))
    case_record = dict(store.fetch_case(case_id) or {})
    last_facts_simple = _select_last_facts(facts)
    conflicts_map = _fact_conflicts(facts)
    fetch_ovl = getattr(store, "fetch_latest_adjudication_link_override", None)
    override = fetch_ovl(signal.signal_id) if callable(fetch_ovl) else None
    adjudication_conflict = (
        isinstance(override, dict)
        and str(override.get("override_kind") or "") == "reject_same_case"
        and str(override.get("rejected_case_id") or "").strip() == case_id
    )
    open_loop_strings = _build_open_loops(signal=signal, facts=facts, conflicts_map=conflicts_map)
    if adjudication_conflict:
        open_loop_strings = _dedupe_texts(
            [
                f"Adjudication conflict: operator rejected linking signal {signal.signal_id} to case {case_id}.",
                *open_loop_strings,
            ]
        )
    open_loops_struct = _open_loops_structured(open_loop_strings, conflicts_map)
    status = "awaiting_review" if open_loop_strings else "active"
    operational_status = "awaiting_review" if open_loop_strings else "active"
    if adjudication_conflict:
        operational_status = "CONFLICT"
        status = "awaiting_review"
    confidence = _derive_confidence(last_facts=last_facts_simple, open_loops=open_loop_strings)
    next_action = _infer_next_action(signal=signal, open_loops=open_loop_strings)
    document_intelligence = _fetch_document_intelligence(store, case_id)
    calendar_events = _fetch_calendar_events(store, case_id)
    document_conflicts = _document_conflicts_from_intelligence(document_intelligence)
    calendar_deadlines = _calendar_deadlines(calendar_events)
    if document_conflicts and calendar_deadlines:
        next_action = "review_document_and_calendar_context"
    elif document_conflicts and "review_document_conflicts" not in next_action:
        next_action = "review_document_conflicts"
    elif calendar_deadlines and next_action == "review":
        next_action = "review_calendar_context"
    cold_evidence = _build_cold_evidence_pointers(
        signal=signal,
        facts=facts,
        prior_versions=prior_versions,
    )
    snapshot_id = _stable_snapshot_id(case_id=case_id, version=version, source_signal_id=signal.signal_id)
    summary_bits = [
        f"Operational: {operational_status}.",
        f"Latest signal: {signal.signal_summary_pl or signal.signal_kind}.",
        f"Next: {next_action}.",
    ]
    if document_conflicts:
        summary_bits.append("Document intelligence has review conflicts.")
    if calendar_deadlines:
        summary_bits.append("Calendar context is linked.")
    summary_text = " ".join(summary_bits)
    key_facts = _key_facts_evidence_backed(facts, signal=signal)
    active_conflicts = [*_active_conflicts_struct(conflicts_map), *document_conflicts]
    blockers = [
        *_derive_blockers(signal=signal, open_loops=open_loop_strings),
        *_document_blockers(document_conflicts),
    ]
    participants = _participants_from_case(case_record, facts)
    documents_summary = _merge_document_summaries(
        _documents_summary_from_facts(facts, limit=8),
        document_intelligence,
        limit=8,
    )
    payload = dict(signal.payload or {})
    case_key = str(case_record.get("case_key") or payload.get("case_key") or case_id)
    case_family = str(case_record.get("case_family") or payload.get("case_family") or "unknown")
    lifecycle_status = str(case_record.get("status") or status)
    if adjudication_conflict:
        lifecycle_status = "awaiting_review"
    waiting_for = "operator" if operational_status in {"awaiting_review", "CONFLICT"} else "none"
    business_readiness = "needs_data" if open_loop_strings or adjudication_conflict else "ready_for_followup"
    operator_attention_class = (
        "act_now"
        if adjudication_conflict
        else ("act_soon" if operational_status == "awaiting_review" else "watch")
    )
    priority = "urgent" if adjudication_conflict else ("high" if operational_status == "awaiting_review" else "normal")

    hot_state: dict[str, Any] = {
        "schema_version": CASE_SNAPSHOT_HOT_STATE_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "trace_id": str(trace_id or ""),
        "case": {
            "case_id": case_id,
            "case_key": case_key,
            "case_family": case_family,
            "lifecycle_status": lifecycle_status,
            "operational_status": operational_status,
            "waiting_for": waiting_for,
            "business_readiness": business_readiness,
            "operator_attention_class": operator_attention_class,
            "priority": priority,
            "summary_text": summary_text,
        },
        "participants": participants,
        "entities": _entity_summaries(facts),
        "key_facts": key_facts,
        "open_loops": open_loops_struct,
        "deadlines": calendar_deadlines,
        "blockers": blockers,
        "active_conflicts": active_conflicts,
        "documents_summary": documents_summary,
        "latest_activity": {
            "signal_id": signal.signal_id,
            "signal_kind": signal.signal_kind,
            "observed_at": str(signal.observed_at or ""),
            "source_kind": signal.source_kind,
        },
        "recommended_next_step": next_action,
        "cold_evidence_pointers": cold_evidence,
        "snapshot_meta": {
            "version": version,
            "source_signal_id": signal.signal_id,
            "confidence": confidence,
            "created_at": datetime.now().astimezone().isoformat(),
            "legacy_digest": summary_text,
        },
        # Compat: older readers expected flat summary / open_loops strings / last_facts
        "summary_text": summary_text,
        "summary": summary_text,
        "status": status,
        "open_loops": open_loop_strings,
        "last_facts": last_facts_simple,
        "confidence": confidence,
        "version": version,
        "case_id": case_id,
        "source_signal_id": signal.signal_id,
    }
    return hot_state


def _entity_summaries(facts: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for fact in facts:
        scope = str(fact.get("entity_scope") or "").strip()
        key = str(fact.get("fact_key") or "").strip()
        if not scope or not key:
            continue
        t = (scope, key)
        if t in seen:
            continue
        seen.add(t)
        out.append(
            {
                "entity_scope": scope,
                "fact_key": key,
                "value_preview": str(fact.get("normalized_value") or "")[:200],
                "source_ref": str(fact.get("source_ref") or ""),
            }
        )
        if len(out) >= limit:
            break
    return out


def _participants_from_case(case_record: dict[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    name = str(case_record.get("customer_name") or "").strip()
    email = str(case_record.get("customer_email") or "").strip()
    if not email:
        for fact in facts:
            if str(fact.get("fact_key") or "") == "customer_email":
                email = str(fact.get("normalized_value") or "").strip()
                break
    if name or email:
        return [{"role": "customer", "name": name, "email": email}]
    return []


def _key_facts_evidence_backed(facts: list[dict[str, Any]], *, signal: CanonicalSignal) -> list[dict[str, Any]]:
    # FACT-01: defensive live-fact filter (same predicate as split_conflicting_facts).
    rows = [
        dict(item)
        for item in facts
        if str(item.get("status") or "active") != "superseded"
    ]
    rows.sort(
        key=lambda item: (
            -float(item.get("confidence") or 0.0),
            str(item.get("observed_at") or ""),
        ),
    )
    out: list[dict[str, Any]] = []
    for item in rows[:12]:
        fk = str(item.get("fact_key") or "").strip()
        if not fk:
            continue
        src = str(item.get("source_ref") or "").strip()
        prov_kind = str(item.get("source_type") or "fact_store")
        prov_ref = src or str((item.get("metadata") or {}).get("raw_observation_id") or "")
        if not prov_ref:
            prov_ref = str(signal.signal_id)
            prov_kind = "signal_anchor"
        out.append(
            {
                "fact_key": fk,
                "entity_scope": str(item.get("entity_scope") or "case"),
                "value": str(item.get("normalized_value") or ""),
                "confidence": float(item.get("confidence") or 0.0),
                "source_ref": src,
                "provenance": {"kind": prov_kind, "ref": prov_ref},
            }
        )
    return out


def _active_conflicts_struct(conflicts_map: dict[str, set[str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for fact_key, values in conflicts_map.items():
        result.append(
            {
                "fact_key": fact_key,
                "entity_scope": "mixed",
                "values": sorted(values),
            }
        )
    return result


def _open_loops_structured(open_loop_strings: list[str], conflicts_map: dict[str, set[str]]) -> list[dict[str, Any]]:
    loops: list[dict[str, Any]] = []
    for i, text in enumerate(open_loop_strings):
        kind = "conflict" if "conflict" in text.lower() or "Konflikt" in text else "review"
        if "review" in text.lower() or "Human review" in text:
            kind = "review"
        loops.append({"loop_id": f"ol_{i}", "description": text, "kind": kind})
    if conflicts_map and not any(l.get("kind") == "conflict" for l in loops):
        loops.insert(
            0,
            {
                "loop_id": "ol_conflict_register",
                "description": "Active fact conflicts require resolution before truth is stable.",
                "kind": "conflict",
            },
        )
    return loops


def _derive_blockers(*, signal: CanonicalSignal, open_loops: list[str]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if signal.signal_kind == "drive_conflict_detected":
        blockers.append({"blocker_id": "b_drive_conflict", "description": "Drive conflict detected", "severity": "high"})
    for i, text in enumerate(open_loops[:6]):
        blockers.append({"blocker_id": f"b_{i}", "description": text, "severity": "medium"})
    return blockers


def _documents_summary_from_facts(facts: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    by_doc: dict[str, dict[str, Any]] = {}
    for fact in facts:
        doc_id = str(fact.get("document_id") or "").strip()
        if not doc_id:
            continue
        entry = by_doc.setdefault(doc_id, {"document_id": doc_id, "fact_keys": []})
        fk = str(fact.get("fact_key") or "").strip()
        if fk and fk not in entry["fact_keys"]:
            entry["fact_keys"].append(fk)
    return [{"document_id": k, "fact_keys": v["fact_keys"][:6]} for k, v in list(by_doc.items())[:limit]]


def _fetch_document_intelligence(store: CaseSnapshotStore, case_id: str) -> list[dict[str, Any]]:
    fetch = getattr(store, "fetch_document_intelligence_for_case", None)
    if not callable(fetch):
        return []
    try:
        return [dict(item) for item in fetch(case_id, limit=20) or []]
    except (TypeError, ValueError, RuntimeError):
        return []


def _fetch_calendar_events(store: CaseSnapshotStore, case_id: str) -> list[dict[str, Any]]:
    fetch = getattr(store, "fetch_calendar_events_for_case", None)
    if not callable(fetch):
        return []
    try:
        return [dict(item) for item in fetch(case_id, limit=10) or []]
    except (TypeError, ValueError, RuntimeError):
        return []


def _document_conflicts_from_intelligence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for row in rows:
        for conflict in row.get("conflicts") or []:
            if not isinstance(conflict, dict):
                continue
            field_name = str(conflict.get("field_name") or "document_conflict")
            conflicts.append(
                {
                    "fact_key": field_name,
                    "source_kind": "document_intelligence",
                    "document_id": str(row.get("document_id") or ""),
                    "filename": str(row.get("filename") or ""),
                    "severity": str(conflict.get("severity") or "medium"),
                    "summary": f"Document conflict for {field_name}",
                    "values": list(conflict.get("values") or []),
                    "evidence_refs": list(conflict.get("evidence_refs") or []),
                }
            )
    return conflicts[:12]


def _document_blockers(conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "blocker_id": f"document_conflict_{i}",
            "source_kind": "document_intelligence",
            "description": str(item.get("summary") or "Document intelligence conflict"),
            "severity": str(item.get("severity") or "medium"),
        }
        for i, item in enumerate(conflicts[:6])
    ]


def _calendar_deadlines(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deadlines: list[dict[str, Any]] = []
    sorted_events = sorted(events, key=lambda item: str(item.get("start_at") or ""))
    for event in sorted_events[:3]:
        start_at = str(event.get("start_at") or "")
        if not start_at:
            continue
        deadlines.append(
            {
                "source_kind": "google_calendar",
                "calendar_event_id": str(event.get("calendar_event_id") or ""),
                "summary": str(event.get("summary") or ""),
                "due_at": start_at,
                "end_at": str(event.get("end_at") or ""),
                "confidence": float(event.get("link_confidence") or 0.0),
            }
        )
    return deadlines


def _merge_document_summaries(
    fact_summaries: list[dict[str, Any]],
    intelligence_rows: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    by_doc = {str(item.get("document_id") or ""): dict(item) for item in fact_summaries if str(item.get("document_id") or "")}
    for row in intelligence_rows:
        doc_id = str(row.get("document_id") or "")
        if not doc_id:
            continue
        entry = by_doc.setdefault(doc_id, {"document_id": doc_id})
        entry.update(
            {
                "filename": str(row.get("filename") or entry.get("filename") or ""),
                "document_type": str(row.get("document_type") or ""),
                "document_type_confidence": float(row.get("document_type_confidence") or 0.0),
                "requires_human_review": bool(row.get("requires_human_review")),
                "summary": str(row.get("summary") or ""),
            }
        )
    return list(by_doc.values())[:limit]


def _resolve_case_id(signal: CanonicalSignal) -> str:
    payload = dict(signal.payload or {})
    return str(payload.get("case_id") or payload.get("document_row", {}).get("case_id") or "").strip()


def _build_signal_fact_rows(signal: CanonicalSignal) -> list[dict[str, Any]]:
    payload = dict(signal.payload or {})
    raw_observation_id = str((signal.artifacts or {}).get("raw_observation_id") or "").strip()
    case_id = _resolve_case_id(signal)
    observed_at = str(signal.observed_at or datetime.now().astimezone().isoformat())
    fact_rows: list[dict[str, Any]] = []
    for row in payload.get("fact_rows") or []:
        item = dict(row)
        metadata = dict(item.get("metadata") or {})
        original_source_ref = str(item.get("source_ref") or "")
        if raw_observation_id:
            metadata["raw_observation_id"] = raw_observation_id
            if original_source_ref:
                metadata["original_source_ref"] = original_source_ref
            item["source_ref"] = raw_observation_id
        item["metadata"] = metadata
        fact_rows.append(item)

    if raw_observation_id:
        fact_rows.append(
            {
                "fact_id": _stable_fact_id(case_id=case_id, signal_id=signal.signal_id, fact_key="latest_signal_kind"),
                "case_id": case_id,
                "message_id": "",
                "document_id": "",
                "entity_scope": "case",
                "fact_key": "latest_signal_kind",
                "normalized_value": signal.signal_kind,
                "raw_value": signal.signal_kind,
                "confidence": 1.0,
                "observed_at": observed_at,
                "source_type": "signal",
                "source_ref": raw_observation_id,
                "status": "active",
                "metadata": {"signal_id": signal.signal_id},
            }
        )
    return fact_rows


def _select_last_facts(facts: list[dict[str, Any]], *, limit: int = 6) -> list[dict[str, Any]]:
    # FACT-01: defensive live-fact filter (same predicate as split_conflicting_facts).
    rows = [
        dict(item)
        for item in facts
        if str(item.get("status") or "active") != "superseded"
    ]
    rows.sort(
        key=lambda item: (
            str(item.get("observed_at") or ""),
            float(item.get("confidence") or 0.0),
            str(item.get("fact_key") or ""),
        ),
        reverse=True,
    )
    return [
        {
            "fact_key": str(item.get("fact_key") or ""),
            "value": str(item.get("normalized_value") or ""),
            "confidence": float(item.get("confidence") or 0.0),
            "source_ref": str(item.get("source_ref") or ""),
        }
        for item in rows[:limit]
        if str(item.get("fact_key") or "").strip()
    ]


def _build_open_loops(
    *,
    signal: CanonicalSignal,
    facts: list[dict[str, Any]],
    conflicts_map: dict[str, set[str]] | None = None,
) -> list[str]:
    conflicts_map = conflicts_map if conflicts_map is not None else _fact_conflicts(facts)
    loops: list[str] = []
    for fact_key, values in conflicts_map.items():
        loops.append(f"Resolve conflict for {fact_key}: {', '.join(sorted(values))}.")
    if signal.signal_kind == "drive_conflict_detected":
        loops.append("Human review required for detected drive conflict.")
    if bool((signal.payload or {}).get("intake_result_final", {}).get("review_required")):
        loops.append("Human review required by intake decision.")
    return _dedupe_texts(loops)


def _fact_conflicts(facts: list[dict[str, Any]]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for fact in facts:
        # FACT-01: superseded is settled history, not a live disagreement.
        if str(fact.get("status") or "active") == "superseded":
            continue
        entity_scope = str(fact.get("entity_scope") or "").strip()
        if entity_scope not in {"case", "customer", "location", "asset"}:
            continue
        key = str(fact.get("fact_key") or "").strip()
        value = str(fact.get("normalized_value") or "").strip()
        if not key or not value:
            continue
        grouped.setdefault(key, set()).add(value)
    return {key: values for key, values in grouped.items() if len(values) > 1}


def _derive_confidence(*, last_facts: list[dict[str, Any]], open_loops: list[str]) -> float:
    if not last_facts:
        return 0.0
    avg = sum(float(item.get("confidence") or 0.0) for item in last_facts) / max(1, len(last_facts))
    if open_loops:
        avg *= 0.75
    return round(max(0.0, min(1.0, avg)), 4)


def _infer_next_action(*, signal: CanonicalSignal, open_loops: list[str]) -> str:
    if open_loops:
        return "review"
    action = str((signal.payload or {}).get("action_plan_result", {}).get("primary_action") or "").strip()
    return action or "review"


def _build_cold_evidence_pointers(
    *,
    signal: CanonicalSignal,
    facts: list[dict[str, Any]],
    prior_versions: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_observation_ids = {
        str((signal.artifacts or {}).get("raw_observation_id") or "").strip(),
    }
    raw_observation_ids.update(str(item.get("source_ref") or "").strip() for item in facts if str(item.get("source_ref") or "").strip())
    signal_ids = [str(item.get("source_signal_id") or "") for item in prior_versions if str(item.get("source_signal_id") or "")]
    signal_ids.append(signal.signal_id)
    document_ids = sorted(
        {
            str(item.get("document_id") or "").strip()
            for item in facts
            if str(item.get("document_id") or "").strip()
        }
    )
    source_refs = sorted(
        {
            str((item.get("metadata") or {}).get("original_source_ref") or "").strip()
            for item in facts
            if str((item.get("metadata") or {}).get("original_source_ref") or "").strip()
        }
    )
    return {
        "signal_ids": [item for item in signal_ids if item],
        "raw_observation_ids": sorted(item for item in raw_observation_ids if item),
        "document_ids": document_ids,
        "source_refs": source_refs,
    }


def _stable_snapshot_id(*, case_id: str, version: int, source_signal_id: str) -> str:
    digest = hashlib.sha256(f"{case_id}|{version}|{source_signal_id}".encode("utf-8")).hexdigest()[:24]
    return f"casesnap_{digest}"


def _stable_fact_id(*, case_id: str, signal_id: str, fact_key: str) -> str:
    digest = hashlib.sha256(f"{case_id}|{signal_id}|{fact_key}".encode("utf-8")).hexdigest()[:24]
    return f"factsig_{digest}"


def _dedupe_texts(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


class _EphemeralSnapshotStore:
    def __init__(self) -> None:
        self.fact_rows: list[dict[str, Any]] = []
        self.snapshot_versions: dict[str, list[dict[str, Any]]] = {}

    def append_fact_rows(self, rows: list[dict[str, Any]]) -> None:
        # Parity with InMemory/Postgres: rebuild must supersede same
        # (case_id, entity_scope, fact_key), e.g. latest_signal_kind.
        self.append_facts_with_supersession(rows)

    def append_facts_with_supersession(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Mirror InMemoryMailboxMemoryStore.append_facts_with_supersession on a flat list."""
        from mailbox_memory.facts import merge_fact_evidence

        stats = {"inserted": 0, "superseded": 0, "unchanged": 0}
        if not rows:
            return stats
        for row in rows:
            payload = dict(row)
            case_id = str(payload.get("case_id") or "").strip()
            entity_scope = str(payload.get("entity_scope") or "case").strip() or "case"
            fact_key = str(payload.get("fact_key") or "").strip()
            new_value = str(payload.get("normalized_value") or "").strip()
            if not case_id or not fact_key:
                continue
            skip_insert = False
            updated_items: list[dict[str, Any]] = []
            for item in self.fact_rows:
                if (
                    str(item.get("case_id") or "") == case_id
                    and str(item.get("entity_scope") or "case") == entity_scope
                    and str(item.get("fact_key") or "") == fact_key
                    and str(item.get("status") or "active") == "active"
                ):
                    old_value = str(item.get("normalized_value") or "").strip()
                    if old_value == new_value:
                        stats["unchanged"] += 1
                        skip_insert = True
                        merged_meta = merge_fact_evidence(item.get("metadata"), payload)
                        if merged_meta != (item.get("metadata") if isinstance(item.get("metadata"), dict) else {}):
                            item = {**item, "metadata": merged_meta}
                        updated_items.append(item)
                        continue
                    meta = dict(item.get("metadata") or {})
                    meta["superseded_at"] = payload.get("observed_at")
                    meta["superseded_by_fact_id"] = str(payload.get("fact_id") or "")
                    updated_items.append({**item, "status": "superseded", "metadata": meta})
                    stats["superseded"] += 1
                else:
                    updated_items.append(item)
            self.fact_rows = updated_items
            if skip_insert:
                continue
            self.fact_rows.append(payload)
            stats["inserted"] += 1
        return stats

    def fetch_facts_for_case(self, case_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.fact_rows if str(item.get("case_id") or "") == case_id]

    def fetch_active_facts_for_case(self, case_id: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.fetch_facts_for_case(case_id)
            if str(item.get("status") or "active") != "superseded"
        ]

    def fetch_case(self, case_id: str) -> dict[str, Any] | None:
        return None

    def append_case_snapshot_version(self, row: dict[str, Any]) -> None:
        case_id = str(row.get("case_id") or "").strip()
        if not case_id:
            return
        rows = self.snapshot_versions.setdefault(case_id, [])
        rows.append(dict(row))
        rows.sort(key=lambda item: int(item.get("version") or 0))

    def fetch_case_snapshot_versions(self, case_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = [dict(item) for item in self.snapshot_versions.get(case_id, [])]
        rows.sort(key=lambda item: int(item.get("version") or 0))
        return rows[:limit]

    def fetch_latest_case_snapshot_version(self, case_id: str) -> dict[str, Any] | None:
        rows = self.fetch_case_snapshot_versions(case_id, limit=10_000)
        return rows[-1] if rows else None

    def fetch_latest_adjudication_link_override(self, signal_id: str) -> dict[str, Any] | None:
        return None


__all__ = [
    "CaseSnapshot",
    "CaseSnapshotManager",
]
