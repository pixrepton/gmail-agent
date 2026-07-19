"""Read-only planning for backfill dry-run (no DB writes)."""

from __future__ import annotations

from typing import Any

from correlation_registry.heuristics import (
    _has_unlinked_engagement_split_targets,
    find_engagement_by_technical_precedence,
)
from correlation_registry.store import CorrelationRegistryStore, DEFAULT_ENGAGEMENT_WINDOW_DAYS


def _mailbox_case_links(
    *,
    case_id: str,
    thread_id: str,
    message_id: str,
) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = [
        {
            "link_type": "mailbox_case",
            "target_id": case_id,
            "source_repo": "gmail-agent",
        },
    ]
    if thread_id:
        links.append(
            {
                "link_type": "gmail_thread",
                "target_id": thread_id,
                "source_repo": "gmail-agent",
            }
        )
    if message_id:
        links.append(
            {
                "link_type": "gmail_message",
                "target_id": message_id,
                "source_repo": "gmail-agent",
            }
        )
    return links


def _count_missing_links(
    store: CorrelationRegistryStore,
    *,
    engagement_id: str,
    links: list[dict[str, Any]],
    identity_email: str,
    message_id: str,
) -> int:
    missing = 0
    email = str(identity_email or "").strip().lower()
    if email:
        if not store.find_engagement_by_link(
            link_type="identity_email",
            target_id=email,
            source_repo="gmail-agent",
        ):
            missing += 1
    mid = str(message_id or "").strip()
    if mid:
        if not store.find_engagement_by_link(
            link_type="gmail_message",
            target_id=mid,
            source_repo="gmail-agent",
        ):
            missing += 1
    for item in links:
        link_type = str(item.get("link_type") or "").strip()
        target_id = str(item.get("target_id") or "").strip()
        repo = str(item.get("source_repo") or "gmail-agent").strip() or "gmail-agent"
        if not link_type or not target_id:
            continue
        if not store.find_engagement_by_link(
            link_type=link_type,
            target_id=target_id,
            source_repo=repo,
        ):
            missing += 1
    return missing


def plan_mailbox_case_sync(
    store: CorrelationRegistryStore,
    *,
    case_id: str,
    customer_email: str,
    thread_id: str = "",
    message_id: str = "",
    within_days: int = DEFAULT_ENGAGEMENT_WINDOW_DAYS,
) -> dict[str, Any] | None:
    """Plan sync without writes. Returns None when case has no email."""
    case_id = str(case_id or "").strip()
    email = str(customer_email or "").strip()
    if not case_id or not email:
        return None

    thread_id = str(thread_id or "").strip()
    message_id = str(message_id or "").strip()
    links = _mailbox_case_links(
        case_id=case_id,
        thread_id=thread_id,
        message_id=message_id,
    )

    technical = find_engagement_by_technical_precedence(
        store,
        links=links,
        message_id=message_id,
    )
    new_identity = False
    new_engagement = False
    merge_engagement = False
    engagement_id = technical or ""

    if technical:
        merge_engagement = True
    else:
        recent = None
        if email and not _has_unlinked_engagement_split_targets(store, links=links):
            recent = store.find_recent_engagement_for_email(
                email=email,
                within_days=within_days,
            )
        if recent:
            engagement_id = recent
            merge_engagement = True
        else:
            new_identity = True
            new_engagement = True

    links_would_create = 0
    if merge_engagement and engagement_id:
        links_would_create = _count_missing_links(
            store,
            engagement_id=engagement_id,
            links=links,
            identity_email=email,
            message_id=message_id,
        )
    elif new_engagement:
        links_would_create = _count_missing_links(
            store,
            engagement_id="",
            links=links,
            identity_email=email,
            message_id=message_id,
        )

    return {
        "case_id": case_id,
        "new_identity": new_identity,
        "new_engagement": new_engagement,
        "merge_engagement": merge_engagement,
        "links_would_create": links_would_create,
    }


def plan_workflow_sync(
    store: CorrelationRegistryStore,
    *,
    workflow_id: str,
    client_email: str,
    message_id: str = "",
    within_days: int = DEFAULT_ENGAGEMENT_WINDOW_DAYS,
) -> dict[str, Any] | None:
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        return None
    email = str(client_email or "").strip()
    message_id = str(message_id or "").strip()
    links: list[dict[str, Any]] = [
        {
            "link_type": "cieplo_workflow",
            "target_id": workflow_id,
            "source_repo": "topinstal-cieplo-orchestrator",
        },
    ]
    if message_id:
        links.append(
            {
                "link_type": "gmail_message",
                "target_id": message_id,
                "source_repo": "topinstal-cieplo-orchestrator",
            }
        )

    technical = find_engagement_by_technical_precedence(
        store,
        links=links,
        message_id=message_id,
    )
    new_identity = False
    new_engagement = False
    merge_engagement = False
    engagement_id = technical or ""

    if technical:
        merge_engagement = True
    elif email and not _has_unlinked_engagement_split_targets(store, links=links):
        recent = store.find_recent_engagement_for_email(email=email, within_days=within_days)
        if recent:
            engagement_id = recent
            merge_engagement = True
        else:
            new_identity = True
            new_engagement = True
    else:
        new_identity = bool(email)
        new_engagement = True

    links_would_create = _count_missing_links(
        store,
        engagement_id=engagement_id if merge_engagement else "",
        links=links,
        identity_email=email,
        message_id=message_id,
    )

    return {
        "workflow_id": workflow_id,
        "new_identity": new_identity,
        "new_engagement": new_engagement,
        "merge_engagement": merge_engagement,
        "links_would_create": links_would_create,
    }


def empty_dry_run_stats() -> dict[str, int]:
    return {
        "cases_seen": 0,
        "workflows_seen": 0,
        "identities_would_create": 0,
        "engagements_would_create": 0,
        "engagements_would_merge": 0,
        "links_would_create": 0,
    }


def accumulate_plan(stats: dict[str, int], plan: dict[str, Any] | None) -> None:
    if not plan:
        return
    if plan.get("new_identity"):
        stats["identities_would_create"] += 1
    if plan.get("new_engagement"):
        stats["engagements_would_create"] += 1
    if plan.get("merge_engagement"):
        stats["engagements_would_merge"] += 1
    stats["links_would_create"] += int(plan.get("links_would_create") or 0)


def print_dry_run_summary(stats: dict[str, int], *, mode: str, delta_hours: int | None) -> None:
    delta_note = f" (ostatnie {delta_hours} h)" if delta_hours else ""
    print("")
    print("=== P0 Correlation Registry — dry-run ===")
    print(f"Tryb: {mode}{delta_note}")
    print(f"Tożsamości do utworzenia (nowe identity_id):     {stats['identities_would_create']}")
    print(f"Zaangażowania do połączenia (istniejące):        {stats['engagements_would_merge']}")
    print(f"Zaangażowania do utworzenia (nowe):              {stats['engagements_would_create']}")
    print(f"Linki korelacji do utworzenia (brakujące):        {stats['links_would_create']}")
    print(f"Sprawy mailbox (wiersze):                        {stats['cases_seen']}")
    print(f"Workflow Cieplo (wiersze):                       {stats['workflows_seen']}")
    print("Brak zapisu do bazy (dry-run).")
    print("")
