"""
Build a Daszek V3 operational feed snapshot.

Modes:
  - Saved JSON files (cockpit/day/tasks from REST dumps) — no network.
  - Mailbox memory (Postgres / in-memory store) — read-only store queries only;
    does not call Gmail, Groq, Drive API, Calendar API, or get_context_pack / LLM.

POST envelope matches Daszek PHP validation: schema_name daszek_operational_feed_snapshot,
nested `feed` with desk/day/cases/tasks/case_details.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from pathlib import Path
from typing import Any, Protocol

from daszek_v3_operational_feed_contract import (
    FORBIDDEN_KEYS_ANYWHERE,
    OPERATIONAL_FEED_SCHEMA_NAME,
    desk_note_ref_warnings,
    strip_forbidden_nested,
    validate_operational_feed_snapshot,
)

from mailbox_v2_desk_note import resolve_v2_desk_note_id as _resolve_v2_desk_note_id

from case_family_boundary import is_operational_feed_case_row
from case_routing import desk_eligible
from calendar_models import active_calendar_events, infer_calendar_risk
from cieplo_orchestrator_hook import CIEPLO_DESK_INFO_BRIEF_PL

from case_context_contract import (
    feed_projection_summary_line,
    operator_feed_completeness_gaps,
    operator_feed_conflicting_facts,
    operator_feed_context_quality,
    operator_feed_evidence_cards,
    operator_feed_plain_summary,
    sort_conflicts_for_operator_projection,
    sort_gaps_for_operator_projection,
)

from case_os_platform import merge_decision_view_with_pipeline_proposals, resolve_feed_action_proposals
from decision_projection_blocks import _select_operator_essence, build_decision_view_blocks
from context_tray_set import operator_task_label_pl

SCHEMA_NAME = OPERATIONAL_FEED_SCHEMA_NAME
SCHEMA_VERSION = "1.3"

# Backward-compatible schema versions that this module can ingest
_LEGACY_SCHEMA_VERSIONS = frozenset({"1", "1.0", "1.1"})

_strip_forbidden = strip_forbidden_nested

_CLOSED_STATUSES = frozenset({"closed", "done", "archived", "resolved", "cancelled"})


def _emit_action_items(tasks_list: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Canonical feed.action_items (schema 1.3 — no tasks shim)."""
    return {"action_items": _strip_forbidden(tasks_list)}


def _dual_emit_action_items(tasks_list: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Legacy 1.2 shim — prefer _emit_action_items for schema 1.3."""
    stripped = _strip_forbidden(tasks_list)
    if SCHEMA_VERSION >= "1.3":
        return _emit_action_items(tasks_list)
    return {"action_items": stripped, "tasks": stripped}


def apply_cieplo_desk_brief_to_feed_row(case_row: dict[str, Any], row: dict[str, Any]) -> None:
    """Stamp Biurko copy when Cieplo orchestrator marked the case informational."""
    md = case_row.get("metadata") if isinstance(case_row.get("metadata"), dict) else {}
    source_kind = str(md.get("source_kind") or "").strip()
    orchestrator_status = str(md.get("orchestrator_status") or "").strip().lower()
    requires_action = md.get("requires_action")
    if (
        source_kind == "cieplo_orchestrated"
        and orchestrator_status == "ok"
        and requires_action is False
    ):
        row["operator_brief_pl"] = CIEPLO_DESK_INFO_BRIEF_PL
        row["summary"] = CIEPLO_DESK_INFO_BRIEF_PL


def _apply_contract_validation(payload: dict[str, Any]) -> dict[str, Any]:
    """Merge contract warnings; raise if invariant broken after strip."""

    rep = validate_operational_feed_snapshot(payload)
    if rep.warnings:
        payload.setdefault("warnings", []).extend(rep.warnings)
    if not rep.ok:
        raise ValueError("operational feed contract: " + "; ".join(rep.errors))
    # ingest warning if schema version is outdated
    sv = str(payload.get("schema_version") or "").strip()
    if sv in _LEGACY_SCHEMA_VERSIONS:
        w = f"schema_version={sv} is outdated; latest={SCHEMA_VERSION}. Consider migrating."
        if w not in payload.get("warnings", []):
            payload.setdefault("warnings", []).append(w)
    return payload


class MailboxMemoryStoreLike(Protocol):
    def fetch_cases(self, *, limit: int = 200) -> list[dict[str, Any]]: ...
    def fetch_case(self, case_id: str) -> dict[str, Any] | None: ...
    def fetch_snapshot(self, case_id: str) -> dict[str, Any] | None: ...
    def fetch_latest_case_snapshot_version(self, case_id: str) -> dict[str, Any] | None: ...
    def fetch_facts_for_case(self, case_id: str) -> list[dict[str, Any]]: ...
    def fetch_documents_for_case(self, case_id: str, *, limit: int = 10) -> list[dict[str, Any]]: ...
    def fetch_events_for_case(self, case_id: str, *, limit: int = 20) -> list[dict[str, Any]]: ...
    def fetch_next_action(self, case_id: str) -> dict[str, Any] | None: ...
    def fetch_action_proposals(self, *, case_id: str = "", status: str = "", limit: int = 100) -> list[dict[str, Any]]: ...
    def fetch_execution_results(self, *, case_id: str = "", proposal_id: str = "", limit: int = 100) -> list[dict[str, Any]]: ...
    def fetch_calendar_events_for_case(self, case_id: str, *, limit: int = 10) -> list[dict[str, Any]]: ...


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_safe_deep(value: Any) -> Any:
    """Recursively coerce values so :func:`json.dumps` succeeds (DB rows often use datetime)."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): json_safe_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_deep(v) for v in value]
    if isinstance(value, set):
        return [json_safe_deep(v) for v in value]
    return str(value)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _ensure_list(val: Any) -> list[Any]:
    return val if isinstance(val, list) else []


def _load_desk_note_ids_json(path: Path) -> frozenset[str]:
    """Keys from Daszek v2 ``desk_notes.json`` object map."""

    try:
        raw = _load_json(path)
    except OSError as exc:
        raise SystemExit(f"ERROR: nie można wczytać --desk-notes-json: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: nieprawidłowy JSON desk_notes: {exc}") from exc
    if not isinstance(raw, dict):
        return frozenset()
    return frozenset(str(k).strip() for k in raw.keys() if str(k).strip())


def _trim_meta(v: str, max_len: int = 240) -> str:
    t = str(v or "").strip()
    return t[:max_len] if len(t) > max_len else t


def apply_snapshot_meta_fields(
    payload: dict[str, Any],
    *,
    environment: str = "",
    source_run_id: str = "",
    build_git_sha: str = "",
) -> None:
    """Optional trust line on snapshot root (no PII); kept out of forbidden-key scan scope."""

    env = _trim_meta(environment)
    if env:
        payload["environment"] = env
    run = _trim_meta(source_run_id)
    if run:
        payload["source_run_id"] = run
    sha = _trim_meta(build_git_sha, max_len=64)
    if sha:
        payload["build_git_sha"] = sha


def apply_v2_desk_note_ids_from_desk_notes_store(
    payload: dict[str, Any],
    desk_notes: dict[str, Any],
) -> int:
    """Map case_id → latest note_* from WP desk_notes.json; set v2_desk_note_id + feedback_eligible."""

    if not isinstance(desk_notes, dict):
        return 0
    case_to_note: dict[str, str] = {}
    for nid, note in desk_notes.items():
        if not isinstance(note, dict):
            continue
        note_id = str(nid or note.get("note_id") or "").strip()
        if not note_id.startswith("note_"):
            continue
        cid = str(note.get("case_id") or "").strip()
        if cid:
            case_to_note[cid] = note_id
    if not case_to_note:
        return 0
    feed = payload.get("feed") if isinstance(payload.get("feed"), dict) else {}
    applied = 0
    for case in _ensure_list(feed.get("cases")):
        if not isinstance(case, dict):
            continue
        cid = str(case.get("case_id") or "").strip()
        nid = case_to_note.get(cid)
        if not nid:
            continue
        case["v2_desk_note_id"] = nid
        case["feedback_eligible"] = True
        applied += 1
    for desk in _ensure_list(feed.get("desk")):
        if not isinstance(desk, dict):
            continue
        cid = str(desk.get("case_id") or "").strip()
        nid = case_to_note.get(cid)
        if nid:
            desk["v2_desk_note_id"] = nid
            desk["feedback_eligible"] = True
    return applied


def merge_desk_note_preflight_warnings(payload: dict[str, Any], desk_note_ids: frozenset[str] | None) -> list[str]:
    """Console-side mirror of PHP desk note ref check; extends payload['warnings']."""

    if desk_note_ids is None:
        return []
    feed = payload.get("feed")
    extra = desk_note_ref_warnings(feed, desk_note_ids)
    if not extra:
        return []
    w = payload.setdefault("warnings", [])
    for line in extra:
        if line not in w:
            w.append(line)
    return extra


def _parse_iso_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _state_freshness_label(adj_at: str | None) -> str:
    """Classify state freshness based on last adjudication timestamp.

    Returns:
        "fresh" if < 5 minutes ago,
        "stale" if > 30 minutes ago,
        "aging" otherwise, or "unknown" if no timestamp.
    """
    if not adj_at:
        return "unknown"
    dt = _parse_iso_dt(adj_at)
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    minutes = delta.total_seconds() / 60.0
    if minutes < 5:
        return "fresh"
    if minutes > 30:
        return "stale"
    return "aging"


def _humanize_family(family: str) -> str:
    f = str(family or "").strip().replace("_", " ")
    if not f or f.lower() in {"unknown", "case family unknown"}:
        return ""
    return f[0].upper() + f[1:] if f else ""


_FEED_OMIT_IF_EMPTY_LISTS: tuple[str, ...] = (
    "service_signals",
    "marketing_signals",
    "graph_hints",
    "related_entities",
    "evidence_cards",
    "completeness_gaps",
    "conflicting_facts",
    "action_proposals",
)


def _omit_empty_list_fields(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in _FEED_OMIT_IF_EMPTY_LISTS:
        val = out.get(key)
        if isinstance(val, list) and not val:
            out.pop(key, None)
    return out


def _is_gateb_test_artifact(
    *,
    snapshot_id: str = "",
    case_id: str = "",
    note_id: str = "",
    title: str = "",
) -> bool:
    hay = " ".join(
        [
            str(snapshot_id or ""),
            str(case_id or ""),
            str(note_id or ""),
            str(title or ""),
        ]
    ).lower()
    if "gateb_badbad" in hay or "badbadbad" in hay:
        return True
    sid = str(snapshot_id or "").strip().lower()
    if sid.startswith("gateb-"):
        return True
    cid = str(case_id or "").strip().lower()
    if cid.startswith("gateb-") or "gateb_badbad" in cid:
        return True
    nid = str(note_id or "").strip().lower()
    return nid.startswith("gateb-") or "gateb_badbad" in nid


def _essence_pl_from_projection_routes(routes: dict[str, Any] | None) -> str:
    """First non-empty essence from Daszek projection router surfaces (desk_cards → router desk)."""

    if not isinstance(routes, dict):
        return ""
    surfaces = routes.get("surfaces")
    if isinstance(surfaces, dict):
        for card in _ensure_list(surfaces.get("desk")):
            if not isinstance(card, dict):
                continue
            summary = str(card.get("summary") or card.get("operator_essence_pl") or "").strip()
            if summary:
                return summary
    envelope = routes.get("projection_envelope")
    if isinstance(envelope, dict):
        for card in _ensure_list(envelope.get("desk_cards")):
            if not isinstance(card, dict):
                continue
            summary = str(card.get("summary") or card.get("operator_essence_pl") or "").strip()
            if summary:
                return summary
    return ""


def _essence_pl_from_projection_snapshot(projection_snapshot: dict[str, Any] | None) -> str:
    """Essence from operator_projection_snapshot (envelope desk_cards or top-level fields)."""

    if not isinstance(projection_snapshot, dict):
        return ""
    for key in ("operator_essence_pl", "summary_text", "summary"):
        text = str(projection_snapshot.get(key) or "").strip()
        if text:
            return text
    envelope = projection_snapshot.get("projection_envelope")
    if isinstance(envelope, dict):
        for card in _ensure_list(envelope.get("desk_cards")):
            if not isinstance(card, dict):
                continue
            summary = str(card.get("summary") or card.get("operator_essence_pl") or "").strip()
            if summary:
                return summary
    routes = projection_snapshot.get("daszek_routes")
    if isinstance(routes, dict):
        routed = _essence_pl_from_projection_routes(routes)
        if routed:
            return routed
    return ""


def _operator_essence_pl_for_feed(
    *,
    case_intelligence: dict[str, Any] | None,
    vnext: dict[str, Any],
    pack_raw: dict[str, Any] | None,
    projection_routes: dict[str, Any] | None = None,
    projection_snapshot: dict[str, Any] | None = None,
) -> str:
    cs = vnext.get("case_summary") if isinstance(vnext.get("case_summary"), dict) else {}
    snap = projection_snapshot if isinstance(projection_snapshot, dict) else {}
    chain_raw = (
        str(cs.get("summary_text") or "").strip()
        or str(snap.get("operator_essence_pl") or "").strip()
        or _essence_pl_from_projection_snapshot(snap)
        or _essence_pl_from_projection_routes(projection_routes)
        or str(snap.get("summary_text") or "").strip()
    )
    if chain_raw:
        plain = operator_feed_plain_summary(chain_raw, fallback="")
        if plain:
            return plain

    sel = _select_operator_essence(
        case_intelligence=case_intelligence if isinstance(case_intelligence, dict) else {},
        mailbox_context={"vnext": vnext, "pack": pack_raw if isinstance(pack_raw, dict) else {}},
    )
    raw = str(sel.get("essence_pl") or "").strip()
    if not raw:
        return ""
    return operator_feed_plain_summary(raw, fallback="")


def _apply_essence_pl_to_feed_case_row(row: dict[str, Any], essence: str) -> None:
    plain = operator_feed_plain_summary(str(essence or "").strip(), fallback="")
    if not plain:
        return
    clipped = plain[:2000]
    row["operator_essence_pl"] = clipped
    if not str(row.get("operator_brief_pl") or "").strip():
        row["operator_brief_pl"] = clipped
    if not str(row.get("summary") or "").strip():
        row["summary"] = clipped


def _enrich_feed_case_row(
    feed_case: dict[str, Any],
    *,
    case_row: dict[str, Any],
    vnext: dict[str, Any],
    pack_raw: dict[str, Any] | None = None,
    case_intelligence: dict[str, Any] | None = None,
    projection_routes: dict[str, Any] | None = None,
    projection_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = dict(feed_case)
    essence = _operator_essence_pl_for_feed(
        case_intelligence=case_intelligence,
        vnext=vnext,
        pack_raw=pack_raw,
        projection_routes=projection_routes,
        projection_snapshot=projection_snapshot,
    )
    if essence:
        _apply_essence_pl_to_feed_case_row(row, essence)
    v2_nid = _resolve_v2_desk_note_id(case_row, pack_raw)
    if v2_nid:
        row["v2_desk_note_id"] = v2_nid
    cid = str(row.get("case_id") or "").strip()
    row["feedback_eligible"] = bool(cid and v2_nid.startswith("note_"))
    if isinstance(case_intelligence, dict):
        from operator_visibility_policy import apply_desk_composition_visibility

        apply_desk_composition_visibility(row, case_intelligence.get("desk_composition"))
    return _omit_empty_list_fields(row)


def _signal_label_from_row(sig: dict[str, Any]) -> str:
    intake = sig.get("intake") if isinstance(sig.get("intake"), dict) else {}
    for v in (
        sig.get("primary_signal_name"),
        intake.get("primary_signal_name"),
        sig.get("signal_name"),
        sig.get("label"),
        sig.get("title"),
        sig.get("summary"),
    ):
        if v is not None:
            s = str(v).strip()
            if s:
                return s[:200]
    fam = str(sig.get("family") or "").strip()
    return fam[:200] if fam else ""


def _latest_signal_summary_pl(feed_case: dict[str, Any]) -> str:
    """Short projection-safe line for desk UI (no bodies / subjects)."""

    parts: list[str] = []
    for key in ("service_signals", "marketing_signals"):
        for sig in _ensure_list(feed_case.get(key))[:1]:
            if not isinstance(sig, dict):
                continue
            lab = _signal_label_from_row(sig)
            if lab:
                parts.append(lab)
                break
    return " · ".join(parts)[:280]


def _context_summary_items(items: Any, *, limit: int = 3) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _ensure_list(items):
        if not isinstance(item, dict):
            continue
        if item.get("exclude_from_operator_projection_top"):
            continue
        summary = _trim_meta(
            operator_feed_plain_summary(feed_projection_summary_line(item), fallback="Sygnał operatorski"),
            220,
        )
        if not summary:
            continue
        row: dict[str, Any] = {"summary": summary}
        for key in ("severity", "status", "type", "requires_operator", "evidence_status", "decision_usable", "evidence_ref_count"):
            if key in item and item.get(key) not in (None, ""):
                row[key] = item.get(key)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _decision_usable_conflicts(items: Any) -> list[dict[str, Any]]:
    return [item for item in _ensure_list(items) if isinstance(item, dict) and item.get("decision_usable") is True]


def _normalize_case_context_summary_fields(case_row: dict[str, Any]) -> dict[str, Any]:
    row = dict(case_row)
    if isinstance(row.get("conflicting_facts"), list):
        row["conflicting_facts"] = operator_feed_conflicting_facts(row["conflicting_facts"])
    if isinstance(row.get("completeness_gaps"), list):
        row["completeness_gaps"] = operator_feed_completeness_gaps(row["completeness_gaps"])
    if isinstance(row.get("evidence_cards"), list):
        row["evidence_cards"] = operator_feed_evidence_cards(row["evidence_cards"])
    if isinstance(row.get("conflicting_facts"), list):
        row["top_conflicts"] = _context_summary_items(_decision_usable_conflicts(row.get("conflicting_facts")), limit=3)
    else:
        row["top_conflicts"] = _context_summary_items(row.get("top_conflicts"), limit=3)
    row["top_gaps"] = _context_summary_items(row.get("completeness_gaps") or row.get("top_gaps"), limit=3)
    return row


def _snapshot_inner_from_row(snapshot_row: dict[str, Any]) -> dict[str, Any]:
    inner = snapshot_row.get("snapshot_json") if isinstance(snapshot_row.get("snapshot_json"), dict) else snapshot_row
    return inner if isinstance(inner, dict) else {}


def _sanitize_events_for_feed(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        row = {k: v for k, v in ev.items() if k not in FORBIDDEN_KEYS_ANYWHERE}
        payload = row.get("payload")
        if isinstance(payload, dict):
            row["payload"] = _strip_forbidden(dict(payload))
        out.append(row)
    return out


def _events_to_operational_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for ev in events:
        et = str(ev.get("event_type") or "").strip()
        timeline.append(
            {
                "occurred_at": str(ev.get("occurred_at") or ev.get("created_at") or ""),
                "event_type": et,
                "event_type_label": et,
                "summary_pl": str(ev.get("summary_pl") or et or "Zdarzenie"),
            }
        )
    return timeline


def _case_detail_payload(
    *,
    feed_case_row: dict[str, Any],
    vnext: dict[str, Any],
    proposals: list[dict[str, Any]],
    executions: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    cid = str(feed_case_row.get("case_id") or "").strip()
    case_ui = _strip_forbidden(dict(feed_case_row))
    case_ui["evidence_cards"] = operator_feed_evidence_cards(case_ui.get("evidence_cards") or vnext.get("evidence_cards"))
    case_ui["completeness_gaps"] = operator_feed_completeness_gaps(case_ui.get("completeness_gaps") or vnext.get("completeness_gaps"))
    case_ui["conflicting_facts"] = operator_feed_conflicting_facts(case_ui.get("conflicting_facts") or vnext.get("conflicting_facts"))
    case_ui.setdefault("operator_visible_conflicts", [])
    case_ui.setdefault("graph_hints", vnext.get("graph_hints") or [])
    case_ui.setdefault("related_entities", vnext.get("related_entities") or [])
    case_ui.setdefault("service_signals", vnext.get("service_signals") or [])
    case_ui.setdefault("marketing_signals", vnext.get("marketing_signals") or [])
    cal = vnext.get("calendar_context") or []
    if isinstance(cal, list) and cal:
        case_ui.setdefault("calendar", {"events": cal})
    return {
        "ok": True,
        "generated_at": _utc_now_iso(),
        "view": "case_detail_mailbox_projection",
        "case": case_ui,
        "desk_notes": [],
        "signals": [],
        "decision_traces": [],
        "last_change": {},
        "thread_memory": {},
        "operational_timeline": timeline,
        "action_proposals": _strip_forbidden(list(proposals)),
        "execution_results": _strip_forbidden(list(executions)),
        "case_id": cid,
        "feed_read_only_stub": True,
    }


def _case_detail_stub(case_row: dict[str, Any]) -> dict[str, Any]:
    """Minimal envelope compatible with Daszek case detail UI when full API detail is unavailable."""
    row = _strip_forbidden(dict(case_row))
    cid = str(row.get("case_id") or "").strip()
    if isinstance(row.get("conflicting_facts"), list):
        row["conflicting_facts"] = operator_feed_conflicting_facts(row["conflicting_facts"])
    if isinstance(row.get("completeness_gaps"), list):
        row["completeness_gaps"] = operator_feed_completeness_gaps(row["completeness_gaps"])
    if isinstance(row.get("evidence_cards"), list):
        row["evidence_cards"] = operator_feed_evidence_cards(row["evidence_cards"])
    return {
        "ok": True,
        "generated_at": _utc_now_iso(),
        "view": "case_detail_feed_stub",
        "case": row,
        "desk_notes": [],
        "signals": [],
        "decision_traces": [],
        "last_change": {},
        "thread_memory": {},
        "operational_timeline": [],
        "action_proposals": _ensure_list(row.get("action_proposals")),
        "execution_results": _ensure_list(row.get("execution_results")),
        "case_id": cid,
        "feed_read_only_stub": True,
    }


def _map_v1_tasks_to_feed_tasks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in rows:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "").strip()
        title = str(t.get("title") or "Zadanie").strip()
        out.append(
            {
                "task_id": tid,
                "title": title,
                "summary": str(t.get("note") or "").strip(),
                "linked_case_id": str(t.get("case_id") or t.get("case_key") or "").strip() or None,
                "source_type": "v1_task",
                "status": str(t.get("status") or "open").strip(),
                "priority": t.get("priority"),
                "due_at": json_safe_deep(t.get("due_at")),
                "requires_approval": False,
                "feed_read_only": False,
            }
        )
    return out


def assemble_mailbox_pack_dict(store: MailboxMemoryStoreLike, case_id: str) -> dict[str, Any]:
    """Build a CaseContextPack-shaped dict from store reads only (no get_context_pack)."""
    from feedback_event_contract import EVENT_TYPE_ADJUDICATION
    from mailbox_memory_runtime import collect_drive_case_enrichment, split_conflicting_facts

    case_row = store.fetch_case(case_id) or {}
    snapshot_row = store.fetch_snapshot(case_id) or {}
    snapshot_inner = _snapshot_inner_from_row(snapshot_row)
    hot_row = store.fetch_latest_case_snapshot_version(case_id) or {}
    hot_json: dict[str, Any] = {}
    if isinstance(hot_row.get("snapshot_json"), dict):
        hot_json = hot_row["snapshot_json"]

    facts = store.fetch_facts_for_case(case_id)
    active_facts, conflicting_mailbox = split_conflicting_facts(facts)
    from document_intelligence_runtime import superseded_facts_audit

    superseded_mailbox = superseded_facts_audit(facts)

    enrichment = collect_drive_case_enrichment(store=store, case_id=case_id, query_text="", graph_store=None)
    drive_facts = list(enrichment.get("drive_facts") or [])
    drive_active, conflicting_drive = split_conflicting_facts(drive_facts)

    from install_prep_projection import project_install_prep

    install_prep = project_install_prep(
        active_facts=list(active_facts) + list(drive_active),
        case_id=case_id,
    )

    documents = store.fetch_documents_for_case(case_id, limit=8)
    stripped_docs: list[dict[str, Any]] = []
    for d in documents:
        if not isinstance(d, dict):
            continue
        sd = {k: v for k, v in d.items() if k not in FORBIDDEN_KEYS_ANYWHERE}
        stripped_docs.append(sd)

    events = store.fetch_events_for_case(case_id, limit=14)
    sanitized_events = _sanitize_events_for_feed([e for e in events if isinstance(e, dict)])

    next_action = store.fetch_next_action(case_id) or {}
    proposals = store.fetch_action_proposals(case_id=case_id, limit=40)
    executions = store.fetch_execution_results(case_id=case_id, limit=40)
    raw_calendar_events = store.fetch_calendar_events_for_case(case_id, limit=12)
    calendar_events = active_calendar_events(raw_calendar_events)
    calendar_risk = infer_calendar_risk(events=raw_calendar_events, facts=active_facts + drive_active)

    runtime_state = {
        "latest_signal_id": str(case_row.get("latest_signal_id") or ""),
        "latest_signal_at": str(case_row.get("latest_signal_at") or ""),
        "last_rebuild_at": str(case_row.get("last_rebuild_at") or ""),
        "last_projection_refresh_at": str(case_row.get("last_projection_refresh_at") or ""),
        "last_source_kinds_seen": list(case_row.get("last_source_kinds_seen") or []),
    }
    sm = hot_json.get("snapshot_meta") if isinstance(hot_json.get("snapshot_meta"), dict) else {}
    if sm:
        runtime_state["hot_snapshot_version"] = sm.get("version")
        runtime_state["hot_snapshot_id"] = str(hot_row.get("snapshot_id") or "")

    merged_snapshot = dict(snapshot_inner)
    if hot_json and isinstance(hot_json.get("case_summary"), dict):
        merged_snapshot.setdefault("hot_case_summary", hot_json.get("case_summary"))

    calendar_payload = {
        "case_id": case_id,
        "events": calendar_events,
        "observed_events": raw_calendar_events,
        "next_event": calendar_events[0] if calendar_events else {},
        "has_calendar_event": bool(calendar_events),
        "calendar_risk": calendar_risk,
        "visit_lifecycle": "scheduled_visit" if calendar_events else ("proposed_visit" if calendar_risk == "customer_proposed_date" else "no_calendar_event"),
    }

    source_refs: list[dict[str, Any]] = []
    fetch_msgs = getattr(store, "fetch_messages_for_case", None)
    if callable(fetch_msgs):
        try:
            raw_msgs = fetch_msgs(case_id, limit=12)
        except Exception:
            raw_msgs = []
        for m in raw_msgs or []:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("message_id") or "").strip()
            if not mid:
                continue
            source_refs.append(
                {
                    "source_type": "gmail_message",
                    "source_id": mid,
                    "ref_kind": "message",
                    "thread_id": str(m.get("thread_id") or "").strip(),
                    "observed_at": str(m.get("received_at") or m.get("internal_date") or "")[:64],
                }
            )

    relevant_chunks: list[dict[str, Any]] = []
    vector_retrieval: dict[str, Any] = {
        "vector_path_status": "vector_path_disabled",
        "detail": "daszek_operational_feed_exporter",
        "semantic_candidate_count": 0,
        "embedding_error": "",
        "semantic_error": "",
    }
    fetch_chunks = getattr(store, "fetch_chunks_for_case", None)
    if callable(fetch_chunks):
        try:
            ch_rows = fetch_chunks(case_id, limit=16)
        except Exception:
            ch_rows = []
        safe_chunk_keys = ("chunk_id", "document_id", "case_id", "ordinal", "score", "source_kind", "drive_document_id")
        for ch in ch_rows or []:
            if not isinstance(ch, dict):
                continue
            slim = {k: ch.get(k) for k in safe_chunk_keys if k in ch and ch.get(k) not in (None, "")}
            if slim.get("chunk_id") or slim.get("document_id"):
                relevant_chunks.append(slim)
        if relevant_chunks:
            vector_retrieval = {
                "vector_path_status": "vector_path_probe",
                "detail": "daszek_operational_feed_exporter_chunks_meta_only",
                "semantic_candidate_count": len(relevant_chunks),
                "embedding_error": "",
                "semantic_error": "",
            }

    last_adj = ""
    last_adj_kind = ""
    for ev in sanitized_events:
        if str(ev.get("event_type") or "") == EVENT_TYPE_ADJUDICATION:
            last_adj = str(ev.get("occurred_at") or ev.get("created_at") or "")[:64]
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            last_adj_kind = str(payload.get("adjudication_kind") or payload.get("kind") or "")
            break
    if last_adj:
        runtime_state["last_adjudication_event_at"] = last_adj
    if last_adj_kind:
        runtime_state["last_adj_kind"] = last_adj_kind
    runtime_state["last_adj_reconcile_at"] = last_adj or ""
    runtime_state["state_freshness_label"] = _state_freshness_label(
        last_adj or runtime_state.get("last_projection_refresh_at")
    )

    out: dict[str, Any] = {
        "case_id": case_id,
        "snapshot": merged_snapshot,
        "recent_events": sanitized_events,
        "active_facts": active_facts + drive_active,
        "conflicting_facts": conflicting_mailbox + conflicting_drive,
        # 4.2: read-only superseded audit trail (projection only; mailbox store remains SoT).
        "superseded_facts": superseded_mailbox,
        # 4.4: install-prep readiness from active facts only (not a second SoT).
        "install_prep": install_prep,
        "latest_documents": stripped_docs,
        "drive_documents_summary": list(enrichment.get("drive_documents") or []),
        "completeness_gaps": list(enrichment.get("completeness_gaps") or []),
        "graph_hints": list(enrichment.get("graph_hints") or []),
        "reference_documents": list(enrichment.get("reference_documents") or []),
        "relevant_chunks": relevant_chunks,
        "source_refs": source_refs,
        "next_action": dict(next_action) if isinstance(next_action, dict) else {},
        "action_proposals": proposals,
        "execution_results": executions,
        "calendar": calendar_payload,
        "document_intelligence": {},
        "runtime_state": runtime_state,
        "vector_retrieval": vector_retrieval,
    }
    ci_hot = hot_json.get("case_intelligence") if isinstance(hot_json.get("case_intelligence"), dict) else {}
    if ci_hot:
        out["_case_intelligence_from_hot"] = ci_hot
    return out


def build_feed_and_api_case_dict(case_row: dict[str, Any], vnext: dict[str, Any]) -> dict[str, Any]:
    """Shared function building a feed case dict from vnext contract.

    Used by both the operational feed (mailbox memory path) and the FastAPI
    endpoint to ensure identical output shape for the same input.
    """
    cid = str(case_row.get("case_id") or vnext.get("case_id") or "").strip()
    cs = vnext.get("case_summary") or {}
    if not isinstance(cs, dict):
        cs = {}
    hot = vnext.get("hot_state") or {}
    snap_part = hot.get("snapshot") if isinstance(hot.get("snapshot"), dict) else {}
    if not isinstance(snap_part, dict):
        snap_part = {}
    summary_text = str(
        cs.get("summary_text") or snap_part.get("summary_text") or snap_part.get("summary") or case_row.get("subject") or ""
    ).strip()
    summary_text = operator_feed_plain_summary(summary_text, fallback="")
    title_candidates = [
        str(cs.get("title_pl") or "").strip(),
        str(vnext.get("title_pl") or "").strip(),
        str(case_row.get("subject") or "").strip(),
        summary_text,
    ]
    title_text = ""
    for cand in title_candidates:
        if not cand:
            continue
        low = cand.lower()
        if "case_family" in low and "unknown" in low:
            continue
        title_text = operator_feed_plain_summary(cand, fallback="Sprawa")[:500]
        break
    if not title_text:
        title_text = "Sprawa"
    status_raw = str(case_row.get("status") or cs.get("status") or snap_part.get("status") or "open").strip().lower()
    md = case_row.get("metadata") if isinstance(case_row.get("metadata"), dict) else {}
    priority = str(md.get("priority") or md.get("business_priority") or "normal").strip().lower()
    if priority not in {"low", "normal", "high", "urgent", "critical"}:
        priority = "normal"
    if priority == "critical":
        priority = "high"

    gap_n = len(_ensure_list(vnext.get("completeness_gaps")))
    conf_n = len(_ensure_list(vnext.get("conflicting_facts")))
    ev_n = len(_ensure_list(vnext.get("evidence_cards")))
    prop_n = len(_ensure_list(vnext.get("proposed_next_actions")))
    svc_n = len(_ensure_list(vnext.get("service_signals")))
    mkt_n = len(_ensure_list(vnext.get("marketing_signals")))

    sig_line = _latest_signal_summary_pl(
        {
            "service_signals": _ensure_list(vnext.get("service_signals")),
            "marketing_signals": _ensure_list(vnext.get("marketing_signals")),
        }
    )

    row: dict[str, Any] = {
        "case_id": cid,
        "case_key": str(case_row.get("case_key") or ""),
        "title": title_text,
        "summary": operator_feed_plain_summary(summary_text[:2000], fallback="")[:2000],
        "operator_brief_pl": operator_feed_plain_summary(summary_text[:2000], fallback="")[:2000],
        "status": status_raw,
        "family": str(case_row.get("case_family") or ""),
        "family_label": _humanize_family(str(case_row.get("case_family") or "")),
        "priority": priority,
        "primary_next_action_title_pl": str(cs.get("recommended_next_action") or "").strip(),
        "latest_signal_at": json_safe_deep(case_row.get("latest_signal_at")),
        "updated_at": json_safe_deep(case_row.get("updated_at")),
        "context_pack_version": str(vnext.get("version") or vnext.get("contract_version") or ""),
        "has_blocking_conflicts": bool(vnext.get("has_blocking_conflicts")),
        "has_blocking_gaps": bool(vnext.get("has_blocking_gaps")),
        "top_conflicts": _context_summary_items(
            _decision_usable_conflicts(sort_conflicts_for_operator_projection(list(vnext.get("conflicting_facts") or []))),
            limit=3,
        ),
        "top_gaps": _context_summary_items(
            sort_gaps_for_operator_projection(list(vnext.get("completeness_gaps") or [])),
            limit=3,
        ),
        "context_quality": operator_feed_context_quality(vnext.get("context_quality") if isinstance(vnext.get("context_quality"), dict) else {}),
        "evidence_cards": operator_feed_evidence_cards(vnext.get("evidence_cards")),
        "completeness_gaps": operator_feed_completeness_gaps(vnext.get("completeness_gaps")),
        "conflicting_facts": operator_feed_conflicting_facts(vnext.get("conflicting_facts")),
        "graph_hints": _ensure_list(vnext.get("graph_hints")),
        "related_entities": _ensure_list(vnext.get("related_entities")),
        "service_signals": _ensure_list(vnext.get("service_signals")),
        "marketing_signals": _ensure_list(vnext.get("marketing_signals")),
        "action_proposals": _ensure_list(vnext.get("proposed_next_actions")),
        "open_proposal_count": prop_n,
        "open_task_count": prop_n,
        "active_note_count": prop_n,
        "badges": {
            "gaps": gap_n,
            "conflicts": conf_n,
            "evidence": ev_n,
            "proposals": prop_n,
            "service_signals": svc_n,
            "marketing_signals": mkt_n,
            "blocking_conflict": bool(vnext.get("has_blocking_conflicts")),
            "blocking_gap": bool(vnext.get("has_blocking_gaps")),
            "needs_operator_review": bool((vnext.get("policy_context") or {}).get("human_review_suggested"))
            if isinstance(vnext.get("policy_context"), dict)
            else bool(vnext.get("has_blocking_conflicts") or vnext.get("has_blocking_gaps")),
        },
    }
    row["source_kind"] = str(md.get("source_kind") or "").strip()
    if md.get("orchestrator_status") is not None:
        row["orchestrator_status"] = str(md.get("orchestrator_status") or "").strip()
    if "requires_action" in md:
        row["requires_action"] = bool(md.get("requires_action"))
    apply_cieplo_desk_brief_to_feed_row(case_row, row)
    if sig_line:
        row["latest_signal_summary_pl"] = sig_line
    return row


def _vnext_to_feed_case_row(case_row: dict[str, Any], vnext: dict[str, Any]) -> dict[str, Any]:
    """Delegate to shared public function for feed/API parity."""
    return build_feed_and_api_case_dict(case_row, vnext)


def _desk_item_from_case(
    feed_case: dict[str, Any],
    *,
    desk_ix: int,
    kind: str,
    title: str,
    summary: str,
    reason: str,
    priority: str,
) -> dict[str, Any]:
    cid = str(feed_case.get("case_id") or "").strip()
    row: dict[str, Any] = {
        "note_id": f"desk-{cid}-{desk_ix}",
        "item_id": f"desk-{cid}-{desk_ix}",
        "case_id": cid,
        "title": title[:300],
        "summary": summary[:800],
        "why_on_desk": reason[:800],
        "presence_mode": "standard",
        "priority": priority,
        "recommended_next_step": str(feed_case.get("primary_next_action_title_pl") or "")[:400],
        "case_title": str(feed_case.get("title") or "")[:300],
        "record_type_label": kind,
    }
    sig_line = _latest_signal_summary_pl(feed_case)
    if sig_line:
        row["latest_signal_summary_pl"] = sig_line
    essence = str(feed_case.get("operator_essence_pl") or "").strip()
    if essence:
        row["operator_essence_pl"] = essence[:800]
    v2_nid = str(feed_case.get("v2_desk_note_id") or "").strip()
    if v2_nid:
        row["v2_desk_note_id"] = v2_nid
    cid = str(feed_case.get("case_id") or "").strip()
    row["feedback_eligible"] = bool(
        feed_case.get("feedback_eligible") is True
        or (cid and v2_nid.startswith("note_"))
    )
    return row


def _derive_desk_items(feed_case: dict[str, Any], desk_ix_start: int) -> tuple[list[dict[str, Any]], int]:
    """At most one desk card per case — operator_visibility_policy."""
    from operator_visibility_policy import desk_card_spec_for_case

    ix = desk_ix_start
    cid = str(feed_case.get("case_id") or "").strip()
    if not cid or _is_gateb_test_artifact(case_id=cid, title=str(feed_case.get("title") or "")):
        return [], ix

    spec = desk_card_spec_for_case(feed_case)
    if spec is None:
        return [], ix

    item = _desk_item_from_case(
        feed_case,
        desk_ix=ix,
        kind=spec.kind,
        title=spec.title,
        summary=spec.summary,
        reason=spec.reason,
        priority=spec.priority,
    )
    return [item], ix + 1


def _proposal_to_task(p: dict[str, Any], case_id: str, tix: int) -> dict[str, Any]:
    pid = str(p.get("proposal_id") or p.get("id") or f"prop-{case_id}-{tix}").strip()
    label = operator_task_label_pl(
        action_type=str(p.get("action_type") or p.get("recommended_operator_action") or ""),
        title=str(p.get("title") or ""),
        summary=str(p.get("summary") or p.get("reason") or ""),
        summary_pl=str(p.get("summary_pl") or ""),
        title_pl=str(p.get("title_pl") or ""),
    )
    return {
        "task_id": pid,
        "title": (label or str(p.get("title") or p.get("summary") or "Propozycja działania"))[:400],
        "summary": (label or str(p.get("reason") or ""))[:800],
        "linked_case_id": case_id,
        "case_id": case_id,
        "source_type": "proposed_next_action",
        "status": str(p.get("status") or "proposed"),
        "requires_approval": bool(p.get("requires_approval", True)),
        "risk_level": str(p.get("risk_level") or p.get("risk_class") or "unknown"),
        "evidence_refs": _ensure_list(p.get("evidence_refs")),
        "feed_read_only": True,
    }


def _next_action_to_task(na: dict[str, Any], case_id: str) -> dict[str, Any] | None:
    if not na:
        return None
    action_code = str(na.get("next_action") or na.get("title") or "").strip()
    if not action_code:
        return None
    label = operator_task_label_pl(
        action_type=action_code,
        title=action_code,
        summary=str(na.get("rationale") or ""),
        summary_pl=str(na.get("rationale_pl") or na.get("summary_pl") or ""),
        title_pl=str(na.get("title_pl") or ""),
    )
    return {
        "task_id": f"next-{case_id}",
        "title": label[:400],
        "summary": str(na.get("rationale_pl") or na.get("rationale") or "")[:800],
        "linked_case_id": case_id,
        "case_id": case_id,
        "source_type": "next_action",
        "status": "pending",
        "requires_approval": False,
        "risk_level": "unknown",
        "evidence_refs": [],
        "feed_read_only": True,
    }


def _build_day_sections_from_calendar(
    cases_calendar: list[tuple[str, list[dict[str, Any]]]],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for case_id, events in cases_calendar:
        for ev in events[:5]:
            if not isinstance(ev, dict):
                continue
            start = str(ev.get("start_at") or ev.get("start") or "")
            title = str(ev.get("title") or ev.get("summary") or "Wydarzenie")[:300]
            items.append(
                {
                    "note_id": f"day-{case_id}-{hashlib.sha256(start.encode()).hexdigest()[:10]}",
                    "case_id": case_id,
                    "title": title,
                    "summary": start,
                    "presence_mode": "standard",
                    "why_on_desk": "Zapis kalendarza w pamięci sprawy.",
                    "recommended_next_step": "Sprawdź termin w systemie kalendarza.",
                }
            )
    if not items:
        return {"sections": []}
    return {
        "sections": [
            {
                "key": "teraz",
                "title": "Teraz",
                "subtitle": "Z kalendarza zapisanej pamięci",
                "items": items[:25],
            }
        ]
    }


def _apply_schema_version_shim(cockpit: dict[str, Any]) -> dict[str, Any]:
    """Backward-compat shim: map legacy schema_version fields to v1.1.

    Currently supports v1->v1.1 with no field renames; serves as extension
    point for future field migrations.
    """
    if not isinstance(cockpit, dict):
        return cockpit
    sv = str(cockpit.get("schema_version") or "").strip()
    if sv not in _LEGACY_SCHEMA_VERSIONS:
        return cockpit
    out = dict(cockpit)
    # v1 field name aliases (example — extend as needed)
    # e.g. out["cases"] = out.pop("cases_v1", out.get("cases"))
    out.pop("schema_version", None)
    return out


def build_operational_feed_snapshot(
    *,
    cockpit: dict[str, Any] | None,
    day: dict[str, Any] | None,
    tasks: list[dict[str, Any]] | None,
    snapshot_id: str | None,
    counts: dict[str, int] | None = None,
    source: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    case_decision_views: dict[str, dict[str, Any]] | None = None,
    quality_readonly: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cockpit = _apply_schema_version_shim(cockpit or {})
    desk_obj = cockpit.get("desk") if isinstance(cockpit.get("desk"), dict) else {}
    desk_items = [
        item
        for item in _ensure_list(desk_obj.get("items"))
        if isinstance(item, dict)
        and not _is_gateb_test_artifact(
            case_id=str(item.get("case_id") or ""),
            note_id=str(item.get("note_id") or ""),
            title=str(item.get("title") or ""),
        )
    ]

    cases_obj = cockpit.get("cases") if isinstance(cockpit.get("cases"), dict) else {}
    case_items = [
        _normalize_case_context_summary_fields(item) if isinstance(item, dict) else item
        for item in _ensure_list(cases_obj.get("items"))
        if isinstance(item, dict)
        and not _is_gateb_test_artifact(
            case_id=str(item.get("case_id") or ""),
            title=str(item.get("title") or ""),
        )
    ]

    day_payload = day if isinstance(day, dict) else {}
    day_sections = day_payload.get("sections")
    feed_day: dict[str, Any] = {}
    if isinstance(day_sections, list):
        feed_day["sections"] = _strip_forbidden(day_sections)
    else:
        feed_day["sections"] = []

    case_details: dict[str, Any] = {}
    views = case_decision_views if isinstance(case_decision_views, dict) else {}
    for case in case_items:
        if not isinstance(case, dict):
            continue
        cid = str(case.get("case_id") or "").strip()
        if not cid:
            continue
        stub = _case_detail_stub(case)
        dv = views.get(cid)
        if isinstance(dv, dict) and dv:
            stub["decision_view"] = _strip_forbidden(dv)
        case_details[cid] = stub

    feed_tasks = _map_v1_tasks_to_feed_tasks(tasks or [])

    sid = (snapshot_id or "").strip()
    if not sid:
        raw = json.dumps(
            {"desk": desk_items, "cases": case_items, "generated": _utc_now_iso()},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        sid = hashlib.sha256(raw).hexdigest()[:24]

    payload: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": sid,
        "created_at": _utc_now_iso(),
        "generated_at": _utc_now_iso(),
        "title": "Operational feed",
        "subtitle": "Zunifikowany podgląd operacyjny (read-only).",
        "read_only": True,
        "creates_cases": False,
        "executes_actions": False,
        "gate_claim": False,
        "feed": {
            "feed_meta": {
                "exporter": "gmail-agent.tools.gmail_audit.daszek_v3_operational_feed",
                "contract_module": "daszek_v3_operational_feed_contract",
                "state_freshness": "unknown",
                "desk_filter": "P1_P2_operational",
                "full_cases_source": "node_b_mailbox_cases",
                "action_items_label": "Sugerowane działania",
                "ui_sources": {
                    "biurko": "feed.desk",
                    "sprawy": "node_b_GET_cases",
                },
            },
            "desk": _strip_forbidden(desk_items),
            "day": feed_day,
            "cases": _strip_forbidden(case_items),
            **_dual_emit_action_items(feed_tasks),
            "case_details": case_details,
        },
    }
    if counts is not None:
        payload["counts"] = dict(counts)
    if source is not None:
        payload["source"] = _strip_forbidden(dict(source))
    if warnings:
        payload["warnings"] = list(warnings)
    if errors:
        payload["errors"] = list(errors)

    if quality_readonly is not None:
        from quality_readonly_projection import prepare_quality_readonly_for_feed

        feed_obj = payload.get("feed") if isinstance(payload.get("feed"), dict) else {}
        feed_obj = dict(feed_obj)
        feed_obj["quality_readonly"] = prepare_quality_readonly_for_feed(quality_readonly)
        payload["feed"] = feed_obj

    for k in FORBIDDEN_KEYS_ANYWHERE:
        payload.pop(k, None)

    return _apply_contract_validation(payload)


def build_operational_feed_from_mailbox_store(
    store: MailboxMemoryStoreLike,
    *,
    case_limit: int = 20,
    task_limit: int = 50,
    since_days: int | None = None,
    include_closed: bool = False,
    snapshot_id: str | None = None,
    case_decision_views: dict[str, dict[str, Any]] | None = None,
    projection_routes_by_case: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Projection-only snapshot from mailbox memory store (read-only)."""
    from case_context_contract import build_case_context_pack_vnext

    warnings: list[str] = []
    errors: list[str] = []
    all_rows = store.fetch_cases(limit=max(500, case_limit * 4))
    cutoff: datetime | None = None
    if since_days is not None and since_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(since_days))

    candidates: list[dict[str, Any]] = []
    for row in all_rows:
        if not isinstance(row, dict):
            continue
        if not is_operational_feed_case_row(row):
            continue
        st = str(row.get("status") or "").strip().lower()
        if not include_closed and st in _CLOSED_STATUSES:
            continue
        if cutoff is not None:
            ut = _parse_iso_dt(row.get("updated_at")) or _parse_iso_dt(row.get("created_at"))
            if ut is None:
                continue
            if ut.tzinfo is None:
                ut = ut.replace(tzinfo=timezone.utc)
            co = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
            if ut < co:
                continue
        candidates.append(row)

    # Phase 3: Biurko = desk_eligible subset (P1/P2 + Cieplo info); full list → GET /cases (RFC).
    desk_candidates = [r for r in candidates if desk_eligible(r)]
    active_pref = [r for r in desk_candidates if str(r.get("status") or "").strip().lower() not in _CLOSED_STATUSES]
    pool = active_pref if active_pref else desk_candidates
    pool.sort(
        key=lambda r: str(r.get("updated_at") or r.get("created_at") or ""),
        reverse=True,
    )
    selected = pool[: max(1, case_limit)]

    feed_cases: list[dict[str, Any]] = []
    case_details: dict[str, Any] = {}
    desk_acc: list[dict[str, Any]] = []
    tasks_acc: list[dict[str, Any]] = []
    calendar_pairs: list[tuple[str, list[dict[str, Any]]]] = []
    desk_ix = 0
    views = case_decision_views if isinstance(case_decision_views, dict) else {}
    routes_by_case = projection_routes_by_case if isinstance(projection_routes_by_case, dict) else {}

    for case_row in selected:
        cid = str(case_row.get("case_id") or "").strip()
        if not cid:
            continue
        try:
            pack_raw = assemble_mailbox_pack_dict(store, cid)
            ci_hot: dict[str, Any] = {}
            if isinstance(pack_raw.get("_case_intelligence_from_hot"), dict):
                ci_hot = dict(pack_raw.pop("_case_intelligence_from_hot"))
            vnext = build_case_context_pack_vnext(pack_raw)
            vnext.pop("vector_retrieval", None)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"case {cid}: projection skipped ({type(exc).__name__})")
            continue

        case_routes = routes_by_case.get(cid) if isinstance(routes_by_case.get(cid), dict) else None
        feed_case = _enrich_feed_case_row(
            _vnext_to_feed_case_row(case_row, vnext),
            case_row=case_row,
            vnext=vnext,
            pack_raw=pack_raw,
            case_intelligence=ci_hot,
            projection_routes=case_routes,
        )
        dv_inline: dict[str, Any] = {}
        dv_ext = views.get(cid)
        if isinstance(dv_ext, dict) and dv_ext:
            dv_inline = dv_ext
        else:
            dv_inline = build_decision_view_blocks(
                case_intelligence=ci_hot if ci_hot else None,
                mailbox_context={"vnext": vnext, "pack": pack_raw},
            )
        pipeline_proposals = resolve_feed_action_proposals(
            vnext_proposals=_ensure_list(vnext.get("proposed_next_actions")),
            case_intelligence=ci_hot,
            decision_view=dv_inline if isinstance(dv_inline, dict) else None,
        )
        if pipeline_proposals:
            feed_case["action_proposals"] = pipeline_proposals
            feed_case["open_task_count"] = len(pipeline_proposals)
            feed_case["active_note_count"] = len(pipeline_proposals)
            if isinstance(feed_case.get("badges"), dict):
                feed_case["badges"]["proposals"] = len(pipeline_proposals)
        if _is_gateb_test_artifact(
            case_id=cid,
            title=str(feed_case.get("title") or case_row.get("subject") or ""),
            note_id=str(feed_case.get("v2_desk_note_id") or ""),
        ):
            continue
        feed_cases.append(_strip_forbidden(feed_case))

        proposals = pipeline_proposals if pipeline_proposals else _ensure_list(pack_raw.get("action_proposals"))
        executions = _ensure_list(pack_raw.get("execution_results"))
        timeline = _events_to_operational_timeline(_ensure_list(pack_raw.get("recent_events")))
        case_details[cid] = _case_detail_payload(
            feed_case_row=feed_case,
            vnext=vnext,
            proposals=proposals,
            executions=executions,
            timeline=timeline,
        )
        dv = dv_inline if isinstance(dv_inline, dict) and dv_inline else None
        if isinstance(dv, dict) and dv:
            merged = dict(case_details[cid])
            dv_merged, merged_props = merge_decision_view_with_pipeline_proposals(dv, pipeline_proposals)
            merged["decision_view"] = _strip_forbidden(dv_merged)
            if merged_props:
                merged["action_proposals"] = _strip_forbidden(merged_props)
            case_details[cid] = _strip_forbidden(merged)
        elif isinstance(dv_ext, dict) and dv_ext:
            merged = dict(case_details[cid])
            merged["decision_view"] = _strip_forbidden(dv_ext)
            case_details[cid] = _strip_forbidden(merged)

        if not bool(feed_case.get("desk_tasks_suppressed")):
            ditems, desk_ix = _derive_desk_items(feed_case, desk_ix)
            desk_acc.extend(ditems)

        if not bool(feed_case.get("desk_tasks_suppressed")):
            t_ix = 0
            na_task = _next_action_to_task(pack_raw.get("next_action") if isinstance(pack_raw.get("next_action"), dict) else {}, cid)
            if na_task and len(tasks_acc) < task_limit:
                tasks_acc.append(_strip_forbidden(na_task))
                t_ix += 1
            for p in proposals:
                if len(tasks_acc) >= task_limit:
                    break
                if not isinstance(p, dict):
                    continue
                tasks_acc.append(_strip_forbidden(_proposal_to_task(p, cid, t_ix)))
                t_ix += 1

        cev = store.fetch_calendar_events_for_case(cid, limit=8)
        if cev:
            calendar_pairs.append((cid, cev))

    feed_day = _build_day_sections_from_calendar(calendar_pairs)
    if not feed_day["sections"]:
        feed_day = {"sections": []}
        warnings.append("day: brak sekcji kalendarza w pamięci — UI zastąpi to fallbackiem (sprawy/zadania).")

    sid = (snapshot_id or "").strip()
    if not sid:
        raw = json.dumps(
            {"cases": [c.get("case_id") for c in feed_cases], "t": _utc_now_iso()},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        sid = f"operational-feed-{hashlib.sha256(raw).hexdigest()[:20]}"

    desk_final = [
        d
        for d in desk_acc[:120]
        if isinstance(d, dict)
        and not _is_gateb_test_artifact(
            case_id=str(d.get("case_id") or ""),
            note_id=str(d.get("note_id") or ""),
            title=str(d.get("title") or ""),
        )
    ]
    if _is_gateb_test_artifact(snapshot_id=sid):
        desk_final = []
        feed_cases = []
        warnings.append("gateb: pominięto artefakty testowe w eksporcie feedu")
    counts_map = {
        "desk_items": len(desk_final),
        "cases": len(feed_cases),
        "action_items": len(tasks_acc),
        "tasks": len(tasks_acc),
        "gaps": sum(len(_ensure_list(c.get("completeness_gaps"))) for c in feed_cases),
        "conflicts": sum(len(_ensure_list(c.get("conflicting_facts"))) for c in feed_cases),
        "proposals": sum(len(_ensure_list(c.get("action_proposals"))) for c in feed_cases),
        "service_signals": sum(len(_ensure_list(c.get("service_signals"))) for c in feed_cases),
        "marketing_signals": sum(len(_ensure_list(c.get("marketing_signals"))) for c in feed_cases),
    }

    payload: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": sid,
        "created_at": _utc_now_iso(),
        "generated_at": _utc_now_iso(),
        "title": "Operational feed (mailbox memory)",
        "subtitle": "Projekcja read-only z pamięci skrzynki Node B.",
        "read_only": True,
        "creates_cases": False,
        "executes_actions": False,
        "gate_claim": False,
        "counts": counts_map,
        "source": {
            "node": "node_b",
            "source_type": "mailbox_memory",
            "generated_by": "tools/gmail_audit/daszek_v3_operational_feed.py",
            "limits": {"case_limit": case_limit, "task_limit": task_limit},
        },
        "feed": {
            "feed_meta": {
                "exporter": "gmail-agent.tools.gmail_audit.daszek_v3_operational_feed",
                "contract_module": "daszek_v3_operational_feed_contract",
                "state_freshness": "unknown",
                "desk_filter": "P1_P2_operational",
                "full_cases_source": "node_b_mailbox_cases",
                "action_items_label": "Sugerowane działania",
                "cases_in_feed_count": len(feed_cases),
                "desk_case_count": len(desk_final),
                "operational_case_count": len(candidates),
                "desk_eligible_count": len(desk_candidates),
                "engagement_only_staging_count": 0,
                "ui_sources": {
                    "biurko": "feed.desk",
                    "sprawy": "node_b_GET_cases",
                },
            },
            "desk": _strip_forbidden(desk_final),
            "day": feed_day,
            "cases": feed_cases,
            **_dual_emit_action_items(tasks_acc),
            "case_details": case_details,
        },
    }
    if warnings:
        payload["warnings"] = warnings
    if errors:
        payload["errors"] = errors

    for k in FORBIDDEN_KEYS_ANYWHERE:
        payload.pop(k, None)

    return _apply_contract_validation(payload)


def _router_surfaces(routes: dict[str, Any]) -> dict[str, Any]:
    surfaces = routes.get("surfaces")
    return surfaces if isinstance(surfaces, dict) else {}


def _desk_item_from_router_card(feed_case: dict[str, Any], card: dict[str, Any], *, desk_ix: int) -> dict[str, Any]:
    kind = str(card.get("card_type") or card.get("kind") or "attention")
    title = str(card.get("title") or feed_case.get("title") or "Sprawa")[:300]
    summary = str(card.get("summary") or feed_case.get("operator_essence_pl") or feed_case.get("summary") or "")[:800]
    reason = str(card.get("reason") or card.get("why_on_desk") or "Projekcja z ProjectionEnvelope (router).")[:800]
    priority = str(card.get("priority") or feed_case.get("priority") or "normal")
    return _desk_item_from_case(
        feed_case,
        desk_ix=desk_ix,
        kind=kind,
        title=title,
        summary=summary,
        reason=reason,
        priority=priority,
    )


def _task_from_router_surface(task: dict[str, Any], case_id: str) -> dict[str, Any]:
    tid = str(task.get("task_id") or task.get("proposal_id") or task.get("id") or f"router-{case_id}").strip()
    label = operator_task_label_pl(
        action_type=str(task.get("action_type") or task.get("recommended_operator_action") or ""),
        title=str(task.get("title") or ""),
        summary=str(task.get("summary") or task.get("reason") or ""),
        summary_pl=str(task.get("summary_pl") or ""),
        title_pl=str(task.get("title_pl") or ""),
    )
    return _strip_forbidden(
        {
            "task_id": tid,
            "title": (label or str(task.get("title") or task.get("summary") or "Zadanie"))[:400],
            "summary": (label or str(task.get("summary") or task.get("reason") or ""))[:800],
            "linked_case_id": case_id,
            "case_id": case_id,
            "source_type": str(task.get("source_type") or "projection_router"),
            "status": str(task.get("status") or "proposed"),
            "requires_approval": bool(task.get("requires_approval", True)),
            "risk_level": str(task.get("risk_level") or task.get("risk_class") or "unknown"),
            "evidence_refs": _ensure_list(task.get("evidence_refs")),
            "feed_read_only": True,
        }
    )


def apply_projection_route_overlays(
    feed_snapshot: dict[str, Any],
    route_overlays_by_case: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Merge Daszek projection router surfaces into a mailbox-memory feed (CEL: envelope → router → feed)."""

    if not route_overlays_by_case:
        return feed_snapshot
    from operator_visibility_policy import desk_tasks_suppressed_on_routes

    payload = dict(feed_snapshot)
    inner = dict(payload.get("feed") or {})
    cases = [c for c in _ensure_list(inner.get("cases")) if isinstance(c, dict)]
    case_by_id = {str(c.get("case_id") or "").strip(): c for c in cases if str(c.get("case_id") or "").strip()}
    case_details = dict(inner.get("case_details") or {}) if isinstance(inner.get("case_details"), dict) else {}
    desk = [d for d in _ensure_list(inner.get("desk")) if isinstance(d, dict)]
    tasks = [
        t
        for t in _ensure_list(inner.get("action_items") or inner.get("tasks"))
        if isinstance(t, dict)
    ]
    task_ids = {str(t.get("task_id") or "").strip() for t in tasks if str(t.get("task_id") or "").strip()}

    for case_id, routes in route_overlays_by_case.items():
        cid = str(case_id or routes.get("case_id") or "").strip()
        if not cid or not isinstance(routes, dict):
            continue
        surfaces = _router_surfaces(routes)
        feed_case = case_by_id.get(cid)
        if feed_case is None and surfaces:
            feed_case = {"case_id": cid, "title": cid, "badges": {}}
            cases.append(feed_case)
            case_by_id[cid] = feed_case

        if feed_case and not str(feed_case.get("operator_essence_pl") or "").strip():
            router_essence = _essence_pl_from_projection_routes(routes)
            if router_essence:
                _apply_essence_pl_to_feed_case_row(feed_case, router_essence)

        suppress_desk_tasks = desk_tasks_suppressed_on_routes(routes) or bool(
            feed_case and feed_case.get("desk_tasks_suppressed")
        )
        if feed_case and suppress_desk_tasks:
            feed_case["desk_tasks_suppressed"] = True
            policy = routes.get("desk_surface_policy") if isinstance(routes.get("desk_surface_policy"), dict) else {}
            reason = str(policy.get("reason") or routes.get("desk_suppression_reason") or "non_business_noise").strip()
            if reason:
                feed_case["desk_suppression_reason"] = reason

        router_detail = _ensure_list(surfaces.get("case_detail"))
        if router_detail or isinstance(case_details.get(cid), dict):
            detail = dict(case_details[cid]) if isinstance(case_details.get(cid), dict) else {"case_id": cid}
            detail["projection_router"] = _strip_forbidden(
                {
                    "schema_version": routes.get("schema_version"),
                    "read_only": True,
                    "surfaces": {
                        "desk": len(surfaces.get("desk") or []),
                        "tasks": len(surfaces.get("tasks") or []),
                        "case_detail": len(router_detail),
                        "gaps": len(surfaces.get("gaps") or []),
                        "conflicts": len(surfaces.get("conflicts") or []),
                    },
                }
            )
            blocks = list(detail.get("projection_blocks") or [])
            for block in router_detail:
                if isinstance(block, dict):
                    blocks.append(_strip_forbidden({"source": "projection_router", **block}))
            if blocks:
                detail["projection_blocks"] = blocks[:32]
            case_details[cid] = detail

        desk = [d for d in desk if str(d.get("case_id") or "").strip() != cid]
        if not suppress_desk_tasks:
            router_desk = [c for c in _ensure_list(surfaces.get("desk")) if isinstance(c, dict)]
            if router_desk and feed_case:
                for ix, card in enumerate(router_desk):
                    desk.append(_desk_item_from_router_card(feed_case, card, desk_ix=len(desk) + ix))
            elif feed_case:
                ditems, _ = _derive_desk_items(feed_case, len(desk))
                desk.extend(ditems)

            for task in _ensure_list(surfaces.get("tasks")):
                if not isinstance(task, dict):
                    continue
                normalized = _task_from_router_surface(task, cid)
                tid = str(normalized.get("task_id") or "").strip()
                if tid and tid not in task_ids:
                    tasks.append(normalized)
                    task_ids.add(tid)

    inner["cases"] = _strip_forbidden(cases)
    inner["desk"] = _strip_forbidden(desk[:120])
    inner.update(_dual_emit_action_items(tasks))
    inner["case_details"] = _strip_forbidden(case_details)
    meta = dict(inner.get("feed_meta") or {})
    meta["projection_route_overlays_applied"] = len(route_overlays_by_case)
    meta["exporter"] = "gmail-agent.tools.gmail_audit.daszek_v3_operational_feed"
    meta["contract_module"] = "daszek_v3_operational_feed_contract"
    inner["feed_meta"] = meta
    payload["feed"] = inner
    return _apply_contract_validation(payload)


def build_operational_feed_for_cel(
    store: MailboxMemoryStoreLike,
    *,
    case_limit: int = 50,
    task_limit: int = 50,
    snapshot_id: str | None = None,
    route_overlays_by_case: dict[str, dict[str, Any]] | None = None,
    since_days: int | None = None,  # Krok 6: incremental feed — tylko case'y zmienione w ostatnich N dniach
) -> dict[str, Any]:
    """Mailbox-memory feed with optional projection-router overlays (CEL path) and incremental support."""

    overlays = route_overlays_by_case if isinstance(route_overlays_by_case, dict) else {}
    feed = build_operational_feed_from_mailbox_store(
        store,
        case_limit=case_limit,
        task_limit=task_limit,
        snapshot_id=snapshot_id,
        since_days=since_days,  # Krok 6: przekazujemy filtr czasowy
        projection_routes_by_case=overlays,
    )
    if overlays:
        feed = apply_projection_route_overlays(feed, overlays)
    return feed


def _empty_mailbox_snapshot(*, reason: str, allow_empty: bool, snapshot_id: str | None) -> dict[str, Any]:
    from uuid import uuid4

    sid = (snapshot_id or "").strip() or f"operational-feed-empty-{uuid4().hex[:12]}"
    warnings = [reason] if allow_empty else []
    errors = [] if allow_empty else [reason]
    payload = build_operational_feed_snapshot(
        cockpit={"desk": {"items": []}, "cases": {"items": []}},
        day=None,
        tasks=None,
        snapshot_id=sid,
        warnings=warnings if allow_empty else None,
        errors=errors if not allow_empty else None,
        source={
            "node": "node_b",
            "source_type": "mailbox_memory",
            "generated_by": "tools/gmail_audit/daszek_v3_operational_feed.py",
            "limits": {"case_limit": 0, "task_limit": 0},
        },
        counts={
            "desk_items": 0,
            "cases": 0,
            "action_items": 0,
            "tasks": 0,
            "gaps": 0,
            "conflicts": 0,
            "proposals": 0,
            "service_signals": 0,
            "marketing_signals": 0,
        },
    )
    return payload


def uuid4_hex() -> str:
    from uuid import uuid4

    return uuid4().hex[:12]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build Daszek V3 operational feed snapshot JSON (files or mailbox memory)."
    )
    parser.add_argument("--cockpit-json", type=Path, help="Saved GET /daszek/v3/cockpit JSON")
    parser.add_argument("--day-json", type=Path, help="Saved GET /daszek/v2/day JSON")
    parser.add_argument("--tasks-json", type=Path, help="Saved GET /daszek/v1/tasks JSON (array)")
    parser.add_argument("--from-mailbox-memory", action="store_true", help="Read projection from mailbox memory store")
    parser.add_argument("--case-limit", type=int, default=20, help="Max cases from mailbox (default 20)")
    parser.add_argument("--task-limit", type=int, default=50, help="Max tasks from mailbox (default 50)")
    parser.add_argument("--since-days", type=int, default=None, help="Only cases updated in the last N days")
    parser.add_argument("--include-closed", action="store_true", help="Include closed/archived cases")
    parser.add_argument("--allow-empty", action="store_true", help="Emit empty valid snapshot if mailbox missing / no cases")
    parser.add_argument("--dry-run", action="store_true", help="Print summary JSON to stdout; skip writing --out")
    parser.add_argument("--snapshot-id", type=str, default="", help="Stable snapshot id (default: content hash)")
    parser.add_argument(
        "--desk-notes-json",
        type=Path,
        default=None,
        help="Daszek v2 desk_notes.json — preflight warnings for feed.desk note_id refs (mirror PHP ingest)",
    )
    parser.add_argument("--environment", type=str, default="", help="Snapshot trust: environment label (or env DASZEK_SNAPSHOT_ENV)")
    parser.add_argument("--source-run-id", type=str, default="", help="Snapshot trust: pipeline run id (or env GMAIL_AGENT_RUN_ID)")
    parser.add_argument("--build-git-sha", type=str, default="", help="Snapshot trust: git sha (or env GIT_SHA / GITHUB_SHA)")
    parser.add_argument("--out", type=Path, help="Output JSON path (required unless --dry-run)")
    args = parser.parse_args()

    desk_preflight_ids: frozenset[str] | None = None
    if args.desk_notes_json is not None:
        desk_preflight_ids = _load_desk_note_ids_json(args.desk_notes_json)

    from mailbox_memory_runtime import build_mailbox_memory_runtime

    snap: dict[str, Any]

    if args.from_mailbox_memory:
        from config import load_settings

        settings = load_settings(require_groq=False, require_google=False)
        runtime = build_mailbox_memory_runtime(settings)
        if runtime is None:
            msg = "Mailbox memory nie jest skonfigurowana (MAILBOX_MEMORY_DATABASE_URL / stage)."
            if args.allow_empty:
                snap = _empty_mailbox_snapshot(reason=msg, allow_empty=True, snapshot_id=args.snapshot_id.strip() or None)
            else:
                raise SystemExit(f"ERROR: {msg} Użyj --allow-empty dla pustej migawki.")
        else:
            runtime.bootstrap()
            snap = build_operational_feed_from_mailbox_store(
                runtime.store,
                case_limit=max(1, int(args.case_limit)),
                task_limit=max(1, int(args.task_limit)),
                since_days=args.since_days,
                include_closed=bool(args.include_closed),
                snapshot_id=args.snapshot_id.strip() or None,
            )
            if not snap["feed"]["cases"] and not args.allow_empty:
                raise SystemExit(
                    "ERROR: Brak spraw do wyeksportowania. Użyj --allow-empty aby zapisać pustą migawkę z ostrzeżeniem."
                )
            if not snap["feed"]["cases"] and args.allow_empty:
                snap.setdefault("warnings", []).append("Brak spraw po filtrach — zapisano pusty feed.")
    else:
        cockpit_data: dict[str, Any] | None = None
        if args.cockpit_json:
            raw = _load_json(args.cockpit_json)
            cockpit_data = raw if isinstance(raw, dict) else {}

        day_data: dict[str, Any] | None = None
        if args.day_json:
            raw_d = _load_json(args.day_json)
            day_data = raw_d if isinstance(raw_d, dict) else {}

        tasks_list: list[dict[str, Any]] | None = None
        if args.tasks_json:
            raw_t = _load_json(args.tasks_json)
            if isinstance(raw_t, list):
                tasks_list = [x for x in raw_t if isinstance(x, dict)]
            else:
                tasks_list = []

        if cockpit_data is None:
            cockpit_data = {"desk": {"items": []}, "cases": {"items": []}}

        snap = build_operational_feed_snapshot(
            cockpit=cockpit_data,
            day=day_data,
            tasks=tasks_list,
            snapshot_id=args.snapshot_id.strip() or None,
        )

    meta_env = (args.environment or os.environ.get("DASZEK_SNAPSHOT_ENV") or "").strip()
    meta_run = (args.source_run_id or os.environ.get("GMAIL_AGENT_RUN_ID") or "").strip()
    meta_sha = (args.build_git_sha or os.environ.get("GIT_SHA") or os.environ.get("GITHUB_SHA") or "").strip()
    apply_snapshot_meta_fields(snap, environment=meta_env, source_run_id=meta_run, build_git_sha=meta_sha)
    desk_extra = merge_desk_note_preflight_warnings(snap, desk_preflight_ids)
    if args.desk_notes_json is not None:
        try:
            raw_dn = _load_json(args.desk_notes_json)
            if isinstance(raw_dn, dict):
                mapped = apply_v2_desk_note_ids_from_desk_notes_store(snap, raw_dn)
                if mapped:
                    line = f"desk_notes: v2_desk_note_id na {mapped} sprawach (feedback_eligible)"
                    w = snap.setdefault("warnings", [])
                    if line not in w:
                        w.append(line)
        except Exception as exc:  # noqa: BLE001
            snap.setdefault("warnings", []).append(f"desk_notes enrich: {type(exc).__name__}")

    if not snap.get("snapshot_id"):
        snap["snapshot_id"] = uuid4_hex()

    out_path = args.out
    if args.dry_run:
        print(
            json.dumps(
                json_safe_deep(
                    {
                        "snapshot_id": snap.get("snapshot_id"),
                        "counts": snap.get("counts"),
                        "warnings": snap.get("warnings"),
                        "desk_note_preflight": len(desk_extra) if desk_extra else 0,
                        "environment": snap.get("environment"),
                        "source_run_id": snap.get("source_run_id"),
                        "build_git_sha": snap.get("build_git_sha"),
                    }
                ),
                ensure_ascii=False,
            )
        )
        print(json.dumps(json_safe_deep(snap), ensure_ascii=False, indent=2))
        return

    if out_path is None:
        raise SystemExit("ERROR: Podaj --out lub użyj --dry-run.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(json_safe_deep(snap), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} snapshot_id={snap['snapshot_id']}")


if __name__ == "__main__":
    main()
