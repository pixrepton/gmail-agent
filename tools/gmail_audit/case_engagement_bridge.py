"""Canonical case_id ↔ engagement_id resolution via correlation registry."""

from __future__ import annotations

from typing import Any


def resolve_engagement_id(case_id: str, *, registry_store: Any = None) -> str | None:
    """Resolve engagement_id for a mailbox case_id."""
    cid = str(case_id or "").strip()
    if not cid or registry_store is None:
        return None

    find_by_case = getattr(registry_store, "find_engagement_by_case_id", None)
    if callable(find_by_case):
        bundle = find_by_case(cid)
        if isinstance(bundle, dict):
            eid = str(bundle.get("engagement_id") or "").strip()
            if eid:
                return eid

    find_link = getattr(registry_store, "find_engagement_by_link", None)
    if callable(find_link):
        eid = find_link(link_type="mailbox_case", target_id=cid, source_repo="gmail-agent")
        if eid:
            return str(eid).strip()
    return None


def resolve_case_id(engagement_id: str, *, registry_store: Any = None) -> str | None:
    """Resolve mailbox case_id from engagement_id via mailbox_case link."""
    eid = str(engagement_id or "").strip()
    if not eid or registry_store is None:
        return None

    list_links = getattr(registry_store, "list_links_for_engagement", None)
    if not callable(list_links):
        return None
    for link in list_links(eid):
        if not isinstance(link, dict):
            continue
        if str(link.get("link_type") or "").strip() == "mailbox_case":
            cid = str(link.get("target_id") or "").strip()
            if cid:
                return cid
    return None


__all__ = ["resolve_case_id", "resolve_engagement_id"]
