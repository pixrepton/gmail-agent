"""Merged engagement timeline — mailbox events + os_events + agent turns."""

from __future__ import annotations

from typing import Any


def _parse_ts(raw: str) -> str:
    return str(raw or "").strip()


def _case_event_rows(events: list[dict[str, Any]], *, engagement_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        et = str(ev.get("event_type") or "").strip()
        rows.append(
            {
                "timestamp": _parse_ts(ev.get("occurred_at") or ev.get("created_at") or ""),
                "source": "case_event",
                "summary_pl": str(ev.get("summary_pl") or ev.get("summary_text") or et or "Zdarzenie"),
                "engagement_id": engagement_id,
                "event_type": et,
                "event_type_label": et,
            }
        )
    return rows


def _os_event_rows(items: list[dict[str, Any]], *, engagement_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        et = str(item.get("event_type") or "").strip()
        rows.append(
            {
                "timestamp": _parse_ts(item.get("occurred_at") or ""),
                "source": "os_event",
                "summary_pl": str(item.get("summary_pl") or et or "Zdarzenie OS"),
                "engagement_id": engagement_id,
                "event_type": et,
                "event_type_label": et,
                "source_repo": str(item.get("source_repo") or ""),
                "status": str(item.get("status") or ""),
                "event_id": str(item.get("event_id") or ""),
            }
        )
    return rows


def _agent_turn_rows(turns: list[dict[str, Any]], *, engagement_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        tool_name = str(turn.get("tool_name") or "").strip()
        rows.append(
            {
                "timestamp": _parse_ts(turn.get("created_at") or ""),
                "source": "agent_turn",
                "summary_pl": str(turn.get("turn_summary_pl") or turn.get("tool_status") or tool_name or "Agent"),
                "engagement_id": engagement_id,
                "event_type": "agent_turn",
                "event_type_label": f"Agent: {tool_name}" if tool_name else "Agent",
                "tool_name": tool_name,
                "tool_status": turn.get("tool_status"),
            }
        )
    return rows


def fetch_merged_engagement_timeline(
    database_url: str,
    *,
    engagement_id: str,
    case_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """Merge three timeline sources in-process (no new table)."""
    from agent_runtime.turn_journal import PostgresAgentTurnJournal
    from event_spine.query import fetch_os_events_for_engagement
    from mailbox_memory_store import PostgresMailboxMemoryStore

    eid = str(engagement_id or "").strip()
    cid = str(case_id or "").strip()
    if not eid:
        raise ValueError("engagement_id is required")

    db_url = str(database_url or "").strip()
    if not db_url:
        raise ValueError("database_url is required")

    case_rows: list[dict[str, Any]] = []
    if cid:
        store = PostgresMailboxMemoryStore(db_url)
        store.bootstrap()
        events = store.fetch_events_for_case(cid, limit=limit)
        case_rows = _case_event_rows(events, engagement_id=eid)

    os_items = fetch_os_events_for_engagement(db_url, eid, limit=limit)
    os_rows = _os_event_rows(os_items, engagement_id=eid)

    agent_rows: list[dict[str, Any]] = []
    journal = PostgresAgentTurnJournal(db_url)
    turns = journal.list_turns(eid, limit=limit)
    agent_rows = _agent_turn_rows(turns, engagement_id=eid)

    merged = case_rows + os_rows + agent_rows
    merged.sort(key=lambda row: row.get("timestamp") or "", reverse=True)
    if limit > 0:
        merged = merged[:limit]

    lanes = {
        "case_event": len(case_rows),
        "os_event": len(os_rows),
        "agent_turn": len(agent_rows),
    }
    return {
        "ok": True,
        "schema_version": "topinstal.engagement_timeline.v1",
        "read_only": True,
        "engagement_id": eid,
        "case_id": cid,
        "items": merged,
        "count": len(merged),
        "lanes": lanes,
    }


__all__ = ["fetch_merged_engagement_timeline"]
