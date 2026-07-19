"""B2 bridge: v2 desk note ids in mailbox memory (Postgres) without WP desk_notes.json."""

from __future__ import annotations

import hashlib
from typing import Any


def stable_v2_desk_note_id_for_case(case_id: str) -> str:
    """Same algorithm as ``dash_projection_v2._stable_id('note', case_id)``."""

    cid = str(case_id or "").strip()
    if not cid.startswith("case_"):
        return ""
    seed = cid
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"note_{digest}"


def desk_note_id_from_v2_projection(v2_projection: dict[str, Any] | None) -> tuple[str, str]:
    """Return ``(case_id, note_id)`` from a v2 shadow projection."""

    if not isinstance(v2_projection, dict):
        return "", ""
    desk = v2_projection.get("desk_note_patch") if isinstance(v2_projection.get("desk_note_patch"), dict) else {}
    case_patch = v2_projection.get("case_patch") if isinstance(v2_projection.get("case_patch"), dict) else {}
    note_id = str(desk.get("desk_note_id") or desk.get("note_id") or "").strip()
    case_id = str(desk.get("case_id") or case_patch.get("case_id") or "").strip()
    if note_id.startswith("note_") and case_id.startswith("case_"):
        return case_id, note_id
    if case_id.startswith("case_"):
        return case_id, stable_v2_desk_note_id_for_case(case_id)
    return "", ""


def persist_open_desk_note_id_from_v2_projection(store: Any, v2_projection: dict[str, Any] | None) -> bool:
    """Write ``metadata.open_desk_note_id`` on mailbox_memory_cases after v2 push."""

    case_id, note_id = desk_note_id_from_v2_projection(v2_projection)
    if not case_id or not note_id.startswith("note_"):
        return False
    fetch = getattr(store, "fetch_case", None)
    upsert = getattr(store, "upsert_case", None)
    if not callable(fetch) or not callable(upsert):
        return False
    case = fetch(case_id)
    if not isinstance(case, dict):
        case = {"case_id": case_id}
    md = dict(case.get("metadata") or {}) if isinstance(case.get("metadata"), dict) else {}
    if md.get("open_desk_note_id") == note_id:
        return False
    md["open_desk_note_id"] = note_id
    md["desk_note_id"] = note_id
    case["case_id"] = case_id
    case["metadata"] = md
    upsert(case)
    return True


def _note_id_from_mapping(obj: dict[str, Any]) -> str:
    for key in ("open_desk_note_id", "desk_note_id", "v2_desk_note_id", "note_id", "desk_note_id"):
        nid = str(obj.get(key) or "").strip()
        if nid.startswith("note_"):
            return nid
    return ""


def _note_id_from_events(events: list[Any]) -> str:
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if str(ev.get("entity_type") or "").strip() == "desk_note":
            nid = str(ev.get("entity_id") or "").strip()
            if nid.startswith("note_"):
                return nid
        nid = _note_id_from_mapping(ev)
        if nid:
            return nid
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        nid = _note_id_from_mapping(payload)
        if nid:
            return nid
    return ""


def _case_has_intake_signal(case_row: dict[str, Any]) -> bool:
    if str(case_row.get("latest_signal_id") or "").strip():
        return True
    if str(case_row.get("last_projection_refresh_at") or "").strip():
        return True
    md = case_row.get("metadata") if isinstance(case_row.get("metadata"), dict) else {}
    return bool(str(md.get("source_message_id") or "").strip())


def resolve_v2_desk_note_id(case_row: dict[str, Any], pack_raw: dict[str, Any] | None = None) -> str:
    """B2 resolver: metadata, pack, events, intelligence, then stable id per case."""

    for src in (case_row, pack_raw or {}):
        if isinstance(src, dict):
            nid = _note_id_from_mapping(src)
            if nid:
                return nid
    md = case_row.get("metadata") if isinstance(case_row.get("metadata"), dict) else {}
    nid = _note_id_from_mapping(md)
    if nid:
        return nid

    pack = pack_raw if isinstance(pack_raw, dict) else {}
    ci = pack.get("_case_intelligence_from_hot")
    if isinstance(ci, dict):
        nid = _note_id_from_mapping(ci)
        if nid:
            return nid
        desk = ci.get("desk_composition") if isinstance(ci.get("desk_composition"), dict) else {}
        nid = _note_id_from_mapping(desk)
        if nid:
            return nid

    events = pack.get("recent_events")
    if isinstance(events, list):
        nid = _note_id_from_events(events)
        if nid:
            return nid

    cid = str(case_row.get("case_id") or pack.get("case_id") or "").strip()
    if cid.startswith("case_") and _case_has_intake_signal(case_row):
        return stable_v2_desk_note_id_for_case(cid)
    return ""


__all__ = [
    "desk_note_id_from_v2_projection",
    "persist_open_desk_note_id_from_v2_projection",
    "resolve_v2_desk_note_id",
    "stable_v2_desk_note_id_for_case",
]
