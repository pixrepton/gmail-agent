"""Assemble feed envelope + turns (thin feed PR-E)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from agent_runtime.store import OperatorEngagementStore
from agent_runtime.turn_journal import AgentTurnJournal
from daszek_engagement_feed.case import (
    _snapshot_title,
    build_case_detail_from_engagement,
    operator_essence_pl_from_snapshot,
    snapshot_to_feed_case,
)
from daszek_engagement_feed.day import compose_day_sections
from daszek_engagement_feed.desk import snapshot_to_desk_item
from daszek_engagement_feed.tasks import snapshot_to_feed_tasks
from daszek_v3_operational_feed import build_operational_feed_snapshot
from daszek_v3_operational_feed_contract import strip_forbidden_nested, validate_operational_feed_snapshot
from feed_visibility import effective_visibility_mode
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2
from operator_desk_priority import order_desk_snapshots

ENGAGEMENT_FEED_SCHEMA_VERSION = "2"
_OPERATOR_DESK_PREFIX = "_operator_desk"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_excluded_case(case_id: str, title: str = "") -> bool:
    cid = str(case_id or "").strip().lower()
    if cid.startswith(_OPERATOR_DESK_PREFIX):
        return True
    if "gateb_badbad" in cid or "gateb-" in cid:
        return True
    if "gateb_badbad" in str(title or "").lower():
        return True
    return False


def turns_from_snapshot_and_journal(
    snapshot: EngagementSnapshotV2,
    journal: AgentTurnJournal | None,
) -> list[dict[str, Any]]:
    if journal is not None:
        rows = journal.list_turns(snapshot.engagement_id, limit=50)
        if rows:
            return [
                strip_forbidden_nested(
                    {
                        "turn_id": str(r.get("turn_id") or ""),
                        "tool_name": str(r.get("tool_name") or ""),
                        "tool_status": str(r.get("tool_status") or ""),
                        "turn_summary_pl": str(r.get("turn_summary_pl") or ""),
                        "tokens_used": int(r.get("tokens_used") or 0),
                        "snapshot_version": int(r.get("snapshot_version") or 0),
                        "created_at": str(r.get("created_at") or ""),
                    }
                )
                for r in rows
            ]
    turns: list[dict[str, Any]] = []
    for idx, call in enumerate(snapshot.agent_memory.tool_calls, start=1):
        trace = ""
        if idx <= len(snapshot.agent_memory.reasoning_trace):
            trace = str(snapshot.agent_memory.reasoning_trace[idx - 1].summary_pl or "")
        turns.append(
            strip_forbidden_nested(
                {
                    "turn_id": f"mem-{snapshot.engagement_id}-{idx}",
                    "tool_name": call.tool,
                    "tool_status": call.status,
                    "turn_summary_pl": trace,
                    "tokens_used": 0,
                    "snapshot_version": snapshot.version,
                }
            )
        )
    return turns


def _coerce_iso(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat().replace("+00:00", "Z")
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def _attachments_for_case(mailbox_store: Any, case_id: str) -> list[dict[str, Any]]:
    """Attachment metadata + extracted-text preview (NO bytes — privacy projection)."""
    fetch = getattr(mailbox_store, "fetch_documents_for_case", None)
    if not callable(fetch):
        return []
    out: list[dict[str, Any]] = []
    for row in fetch(case_id, limit=10) or []:
        if not isinstance(row, dict):
            continue
        file_name = str(row.get("file_name") or "").strip()
        if not file_name:
            continue
        summary = str(row.get("summary_text") or "").strip()
        out.append(
            strip_forbidden_nested(
                {
                    "attachment_id": str(row.get("attachment_id") or row.get("document_id") or ""),
                    "document_id": str(row.get("document_id") or ""),
                    "file_name": file_name,
                    "mime_type": str(row.get("mime_type") or ""),
                    "document_kind": str(row.get("document_kind") or "generic"),
                    "extraction_status": str(row.get("extraction_status") or ""),
                    "summary_pl": summary[:400],
                    "has_text": bool(summary),
                }
            )
        )
    return out


def _signal_meta(mailbox_store: Any | None, trace_id: str) -> dict[str, Any]:
    if mailbox_store is None:
        return {}
    fetch = getattr(mailbox_store, "fetch_signal", None)
    if not callable(fetch):
        return {}
    row = fetch(str(trace_id or "").strip())
    if not isinstance(row, dict):
        return {}
    source_ref = row.get("source_ref_json")
    if not isinstance(source_ref, dict):
        source_ref = row.get("source_ref") if isinstance(row.get("source_ref"), dict) else {}
    payload = row.get("payload_json")
    if not isinstance(payload, dict):
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    source_message = snapshot.get("source_message") if isinstance(snapshot.get("source_message"), dict) else {}
    return {
        "subject": str(source_message.get("subject") or payload.get("subject") or "").strip(),
        "sender_name": str(source_message.get("sender_name") or row.get("sender_name") or "").strip(),
        "sender_email": str(source_message.get("sender_email") or row.get("sender_email") or "").strip(),
        "received_at": _coerce_iso(source_message.get("received_at") or row.get("observed_at")),
        "message_id": str(source_message.get("message_id") or source_ref.get("message_id") or row.get("message_id") or "").strip(),
        "thread_id": str(source_message.get("thread_id") or source_ref.get("thread_id") or row.get("thread_id") or "").strip(),
        "attachments": [],
    }


def _snapshot_meta_key(snapshot: EngagementSnapshotV2) -> str:
    case_id = str(snapshot.case_id or "").strip()
    if case_id:
        return f"case:{case_id}"
    return f"engagement:{str(snapshot.engagement_id or '').strip()}"


def _message_meta_by_case(
    mailbox_store: Any | None,
    snapshots: list[EngagementSnapshotV2],
) -> dict[str, dict[str, Any]]:
    """Per-case message header (sender/date/subject) + attachment metadata.

    Subject stays under `title` downstream (FORBIDDEN as a key); attachment bytes are
    never included — only metadata + extracted-text preview.
    """
    if mailbox_store is None:
        return {}
    fetch = getattr(mailbox_store, "fetch_messages_for_case", None)
    meta: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        first: dict[str, Any] = {}
        if callable(fetch) and snapshot.case_id:
            rows = fetch(snapshot.case_id, limit=1) or []
            first = rows[0] if rows and isinstance(rows[0], dict) else {}
        signal_meta = _signal_meta(mailbox_store, str(snapshot.signal_id or snapshot.trace_id or "").strip())
        row_meta = {
            "subject": str(first.get("subject") or "").strip(),
            "sender_name": str(first.get("sender") or "").strip(),
            "sender_email": str(first.get("sender_email") or "").strip(),
            "received_at": _coerce_iso(first.get("received_at")),
            "message_id": str(first.get("message_id") or "").strip(),
            "thread_id": str(first.get("thread_id") or "").strip(),
            "attachments": _attachments_for_case(mailbox_store, snapshot.case_id),
        }
        if signal_meta:
            for key, value in signal_meta.items():
                if key == "attachments":
                    continue
                if not row_meta.get(key) and value:
                    row_meta[key] = value
        meta[_snapshot_meta_key(snapshot)] = row_meta
    return meta


def build_feed_from_engagement_snapshots(
    snapshots: list[EngagementSnapshotV2],
    *,
    journal: AgentTurnJournal | None = None,
    subjects_by_case: dict[str, str] | None = None,
    meta_by_case: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    desk: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    case_details: dict[str, Any] = {}
    meta_map = dict(meta_by_case or {})
    subject_map = dict(subjects_by_case or {})
    for snapshot in snapshots:
        meta = meta_map.get(_snapshot_meta_key(snapshot)) or meta_map.get(snapshot.case_id) or {}
        subject = str(meta.get("subject") or subject_map.get(snapshot.case_id, ""))
        if _is_excluded_case(
            snapshot.case_id,
            snapshot_to_feed_case(snapshot, subject=subject, meta=meta).get("title", ""),
        ):
            continue
        case_row = snapshot_to_feed_case(snapshot, subject=subject, meta=meta)
        cases.append(case_row)
        desk_item = snapshot_to_desk_item(snapshot, subject=subject, meta=meta)
        if desk_item is not None:
            desk.append(desk_item)
        tasks.extend(snapshot_to_feed_tasks(snapshot))
        case_details[snapshot.case_id] = build_case_detail_from_engagement(
            snapshot,
            journal=journal,
            subject=subject,
            meta=meta,
        )
    return {"desk": desk, "cases": cases, "tasks": tasks, "case_details": case_details}


def build_case_timeline_only_items(
    snapshots: list[EngagementSnapshotV2],
    *,
    meta_by_case: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Roadmap 2.4: give `case_timeline_only` an actual consumer.

    SLICE-2B classified reference-only signals as `case_timeline_only` — "belongs on the case
    timeline, is not a desk card". Nothing then read that classification, so in practice those
    signals were as invisible as `hidden` ones and the distinction was decorative.

    These rows are deliberately NOT desk items and NOT feed cases: they carry no `note_id`, no
    presence mode and no next action, and they are returned in their own bucket
    (`feed.case_timeline_only`) so no consumer can mistake one for operator work.
    """
    meta_map = dict(meta_by_case or {})
    items: list[dict[str, Any]] = []
    for snapshot in snapshots or []:
        meta = meta_map.get(_snapshot_meta_key(snapshot)) or meta_map.get(snapshot.case_id) or {}
        mode, reasons = effective_visibility_mode(snapshot)
        items.append(
            strip_forbidden_nested(
                {
                    "case_id": snapshot.case_id,
                    "engagement_id": snapshot.engagement_id,
                    "title": _snapshot_title(snapshot, subject=str(meta.get("subject") or "")),
                    "summary_pl": operator_essence_pl_from_snapshot(snapshot),
                    "feed_visibility_mode": mode,
                    "why_on_desk_reason_codes": [str(r)[:80] for r in reasons][:8],
                    "occurred_at": str(meta.get("received_at") or ""),
                    "main_feed_member": False,
                    "read_only": True,
                }
            )
        )
    return items


def build_engagement_feed_envelope(
    feed_core: dict[str, Any],
    *,
    snapshot_id: str,
    source: dict[str, Any] | None = None,
    day: dict[str, Any] | None = None,
    case_timeline_only: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build validated Daszek envelope (mapping in thin package; contract via v3 builder)."""
    sid = str(snapshot_id or "").strip()
    envelope = build_operational_feed_snapshot(
        cockpit={"desk": {"items": feed_core["desk"]}, "cases": {"items": feed_core["cases"]}},
        day=day if isinstance(day, dict) else {"sections": []},
        tasks=feed_core["tasks"],
        snapshot_id=sid,
        counts={
            "desk": len(feed_core["desk"]),
            "cases": len(feed_core["cases"]),
            "tasks": len(feed_core["tasks"]),
        },
        source={
            "feed_source": "engagement_snapshot_v2",
            "exporter": "gmail-agent.tools.gmail_audit.daszek_engagement_feed",
            **(source or {}),
        },
        warnings=[],
    )
    feed_obj = dict(envelope.get("feed") or {})
    feed_obj["case_details"] = feed_core["case_details"]
    # Separate bucket, never merged into desk/cases: these are case-history rows, not desk work.
    feed_obj["case_timeline_only"] = list(case_timeline_only or [])
    meta = dict(feed_obj.get("feed_meta") or {})
    meta["feed_schema_version"] = ENGAGEMENT_FEED_SCHEMA_VERSION
    meta["exporter"] = "gmail-agent.tools.gmail_audit.daszek_engagement_feed"
    meta["agent_runtime"] = True
    meta["case_timeline_only_count"] = len(feed_obj["case_timeline_only"])
    feed_obj["feed_meta"] = meta
    envelope["feed"] = feed_obj
    envelope["subtitle"] = "Podgląd operacyjny z EngagementSnapshot.v2 (agent runtime)."
    return envelope


#: hard ceiling on how far back the overfetch walks, so a mailbox full of noise can never turn one
#: feed build into an unbounded scan
_MAIN_FEED_MAX_SCAN = 1000


def _list_main_feed_snapshots(list_fn: Any, *, limit: int) -> list[EngagementSnapshotV2]:
    """SLICE-2B: return `limit` MAIN-FEED-QUALIFYING snapshots, newest first.

    Membership must be applied BEFORE the effective limit. Fetching `LIMIT n` and filtering
    afterwards would let n recent noise snapshots push older, genuinely qualifying cases out of the
    operator's feed entirely -- the exact failure the routing proof exposed.

    Bounded overfetch is used rather than a SQL predicate deliberately: the rule (stored routing
    classification + CURRENT executive-state override) lives in one pure Python function, and
    duplicating it in SQL would create two sources of truth for the same contract. The scan is
    capped by `_MAIN_FEED_MAX_SCAN` and stops as soon as the store is exhausted.
    """
    from feed_visibility import is_main_feed_member

    main_feed, _timeline_only = _list_feed_snapshots_by_bucket(list_fn, limit=limit)
    return main_feed


def _list_feed_snapshots_by_bucket(
    list_fn: Any,
    *,
    limit: int,
) -> tuple[list[EngagementSnapshotV2], list[EngagementSnapshotV2]]:
    """Same bounded overfetch, returning BOTH buckets: main feed and `case_timeline_only`.

    Roadmap 2.4: the timeline-only bucket is collected from the rows the scan already walked, so
    giving `case_timeline_only` a consumer costs no extra query and cannot change which snapshots
    reach the main feed.
    """
    from feed_visibility import is_case_timeline_only, is_main_feed_member

    fetch = max(1, int(limit))
    seen_rows = 0
    while True:
        rows = list_fn(limit=fetch) or []
        qualifying = [snap for snap in rows if is_main_feed_member(snap)]
        timeline_only = [snap for snap in rows if is_case_timeline_only(snap)]
        if len(qualifying) >= limit:
            return qualifying[:limit], timeline_only[:limit]
        if len(rows) <= seen_rows or len(rows) < fetch or fetch >= _MAIN_FEED_MAX_SCAN:
            # store exhausted, or the scan ceiling reached: return everything that qualifies
            return qualifying[:limit], timeline_only[:limit]
        seen_rows = len(rows)
        fetch = min(fetch * 4, _MAIN_FEED_MAX_SCAN)


def build_operational_feed_from_engagement_store(
    operator_store: OperatorEngagementStore,
    *,
    case_ids: list[str] | None = None,
    journal: AgentTurnJournal | None = None,
    mailbox_store: Any | None = None,
    case_limit: int = 50,
    snapshot_id: str | None = None,
    source: dict[str, Any] | None = None,
    exceptions_only: bool = False,
) -> dict[str, Any]:
    """Build the Daszek operational feed from the engagement store.

    Args:
        exceptions_only: Roadmap 2.4 opt-in hard filter — drop plain `main_feed` cards whose
            readiness is `no_action_required`. Default `False` keeps the feed backward compatible;
            the soft exception-first ORDERING is always applied and removes nothing.
    """
    limit = max(1, int(case_limit))
    snapshots: list[EngagementSnapshotV2] = []
    timeline_only: list[EngagementSnapshotV2] = []
    if case_ids:
        # An explicit case query is a direct lookup, not the main feed: membership does not apply.
        snapshots = operator_store.load_snapshots_for_case_ids(case_ids[:limit])
    else:
        list_fn = getattr(operator_store, "list_recent_snapshots", None)
        if callable(list_fn):
            snapshots, timeline_only = _list_feed_snapshots_by_bucket(list_fn, limit=limit)
            snapshots = order_desk_snapshots(snapshots, exceptions_only=exceptions_only)
    meta = _message_meta_by_case(mailbox_store, snapshots + timeline_only)
    feed_core = build_feed_from_engagement_snapshots(
        snapshots,
        journal=journal,
        meta_by_case=meta,
    )
    sid = str(snapshot_id or "").strip()
    if not sid:
        raw = json.dumps(
            {"engagement": [s.engagement_id for s in snapshots], "at": _utc_now_iso()},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        sid = "eng-feed-" + hashlib.sha256(raw).hexdigest()[:20]
    day = compose_day_sections(mailbox_store, snapshots)
    envelope = build_engagement_feed_envelope(
        feed_core,
        snapshot_id=sid,
        source=source,
        day=day,
        case_timeline_only=build_case_timeline_only_items(timeline_only, meta_by_case=meta),
    )
    rep = validate_operational_feed_snapshot(envelope)
    if not rep.ok:
        raise ValueError("engagement feed invalid: " + "; ".join(rep.errors))
    return envelope
