"""Engagement resolution heuristics (OWNER_DECISIONS option A + precedence rules)."""

from __future__ import annotations

from typing import Any

from correlation_registry.store import CorrelationRegistryStore, DEFAULT_ENGAGEMENT_WINDOW_DAYS

# Technical identifiers override email time-window (thread revival after 6+ months).
_TECHNICAL_PRECEDENCE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gmail_message", ("gmail-agent", "topinstal-cieplo-orchestrator")),
    ("gmail_thread", ("gmail-agent",)),
    ("mailbox_case", ("gmail-agent",)),
    ("cieplo_workflow", ("topinstal-cieplo-orchestrator",)),
    ("calc_request_snapshot", ("topinstal-lead-widget",)),
)

_WIDGET_PLACEHOLDER_EMAIL_SUFFIX = "@widget.topinstal.local"


def _is_widget_placeholder_email(email: str) -> bool:
    normalized = str(email or "").strip().lower()
    return normalized.startswith("lead-widget+") and normalized.endswith(_WIDGET_PLACEHOLDER_EMAIL_SUFFIX)


def _enrich_identity_email_from_placeholder(
    store: CorrelationRegistryStore,
    *,
    identity_id: str,
    identity_email: str,
) -> None:
    """Replace widget placeholder primary_email when client submits a real address."""
    new_email = str(identity_email or "").strip()
    if not new_email or _is_widget_placeholder_email(new_email):
        return
    row = store.get_identity(identity_id)
    if not row:
        return
    current = str(row.get("primary_email") or "").strip()
    if not _is_widget_placeholder_email(current):
        return
    store.update_identity_primary_email(identity_id=identity_id, email=new_email)


def _link_targets(links: list[dict[str, Any]], link_type: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in links:
        if not isinstance(item, dict):
            continue
        if str(item.get("link_type") or "").strip() != link_type:
            continue
        target_id = str(item.get("target_id") or "").strip()
        if not target_id:
            continue
        repo = str(item.get("source_repo") or "gmail-agent").strip() or "gmail-agent"
        out.append((target_id, repo))
    return out


# New mailbox_case / cieplo_workflow must not collapse via email time window (multi-investment).
_ENGAGEMENT_SPLIT_LINK_TYPES = ("mailbox_case", "cieplo_workflow")


def _has_unlinked_engagement_split_targets(
    store: CorrelationRegistryStore,
    *,
    links: list[dict[str, Any]],
) -> bool:
    """True when bundle introduces a new case/workflow not yet in registry."""
    for link_type in _ENGAGEMENT_SPLIT_LINK_TYPES:
        for target_id, repo in _link_targets(links, link_type):
            default_repos = (
                ("gmail-agent",)
                if link_type == "mailbox_case"
                else ("topinstal-cieplo-orchestrator",)
            )
            repos = (repo,) if repo else default_repos
            found = False
            for candidate_repo in repos:
                if store.find_engagement_by_link(
                    link_type=link_type,
                    target_id=target_id,
                    source_repo=candidate_repo,
                ):
                    found = True
                    break
            if not found:
                return True
    return False


def find_engagement_by_technical_precedence(
    store: CorrelationRegistryStore,
    *,
    links: list[dict[str, Any]],
    message_id: str = "",
) -> str | None:
    """Technical IMAP/case/workflow keys always beat email time window."""
    mid = str(message_id or "").strip()
    if mid:
        for repo in ("gmail-agent", "topinstal-cieplo-orchestrator"):
            existing = store.find_engagement_by_link(
                link_type="gmail_message",
                target_id=mid,
                source_repo=repo,
            )
            if existing:
                return existing

    for link_type, default_repos in _TECHNICAL_PRECEDENCE:
        for target_id, repo in _link_targets(links, link_type):
            repos = (repo,) if repo else default_repos
            for candidate_repo in repos:
                existing = store.find_engagement_by_link(
                    link_type=link_type,
                    target_id=target_id,
                    source_repo=candidate_repo,
                )
                if existing:
                    return existing
    return None


def resolve_engagement_for_links(
    store: CorrelationRegistryStore,
    *,
    identity_id: str,
    links: list[dict[str, Any]],
    message_id: str = "",
    within_days: int = DEFAULT_ENGAGEMENT_WINDOW_DAYS,
) -> str:
    """Pick or create engagement; technical match overrules email window."""
    technical = find_engagement_by_technical_precedence(
        store,
        links=links,
        message_id=message_id,
    )
    if technical:
        return technical

    recent = store.find_recent_engagement_for_email(
        email="",
        within_days=within_days,
        identity_id=identity_id,
    )
    if recent:
        return recent

    return store.resolve_or_create_engagement(identity_id=identity_id)


def resolve_identity_and_engagement(
    store: CorrelationRegistryStore,
    *,
    identity_email: str,
    display_name: str = "",
    message_id: str = "",
    links: list[dict[str, Any]],
    within_days: int = DEFAULT_ENGAGEMENT_WINDOW_DAYS,
) -> tuple[str, str]:
    """Resolve engagement first (technical), then identity (no global email UNIQUE)."""
    technical_engagement = find_engagement_by_technical_precedence(
        store,
        links=links,
        message_id=message_id,
    )
    if technical_engagement:
        row = store.get_engagement(technical_engagement)
        if row:
            identity_id = str(row.get("identity_id") or "")
            if identity_id:
                if display_name:
                    store.update_identity_display_name(identity_id, display_name)
                _enrich_identity_email_from_placeholder(
                    store,
                    identity_id=identity_id,
                    identity_email=identity_email,
                )
                return identity_id, technical_engagement

    email = str(identity_email or "").strip().lower()
    recent_engagement = None
    if email and not _has_unlinked_engagement_split_targets(store, links=links):
        recent_engagement = store.find_recent_engagement_for_email(
            email=email,
            within_days=within_days,
        )
    if recent_engagement:
        row = store.get_engagement(recent_engagement)
        if row:
            identity_id = str(row.get("identity_id") or "")
            if identity_id:
                if display_name:
                    store.update_identity_display_name(identity_id, display_name)
                return identity_id, recent_engagement

    identity_id = store.create_identity(
        email=identity_email,
        display_name=display_name,
    )
    engagement_id = store.resolve_or_create_engagement(identity_id=identity_id)
    return identity_id, engagement_id


def _apply_customer_identity_metadata(
    store: CorrelationRegistryStore,
    *,
    identity_id: str,
    engagement_id: str,
    identity_email: str,
    display_name: str,
    identity_metadata: dict[str, Any] | None = None,
    engagement_metadata: dict[str, Any] | None = None,
    links: list[dict[str, Any]] | None = None,
) -> None:
    from correlation_registry.identity_metadata import extract_identity_hints_from_payload

    id_meta, eng_meta = extract_identity_hints_from_payload(
        email=identity_email,
        display_name=display_name,
        identity_metadata=identity_metadata,
        engagement_metadata=engagement_metadata,
        links=links,
    )
    merge_identity = getattr(store, "merge_identity_metadata", None)
    merge_engagement = getattr(store, "merge_engagement_metadata", None)
    if callable(merge_identity):
        merge_identity(identity_id, id_meta)
    if callable(merge_engagement):
        merge_engagement(engagement_id, eng_meta)


def register_link_bundle(
    store: CorrelationRegistryStore,
    *,
    identity_email: str,
    display_name: str = "",
    message_id: str = "",
    links: list[dict[str, Any]],
    within_days: int = DEFAULT_ENGAGEMENT_WINDOW_DAYS,
    identity_metadata: dict[str, Any] | None = None,
    engagement_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register identity + engagement + idempotent links."""
    email = str(identity_email or "").strip()
    if not email and not any(
        str(item.get("target_id") or "").strip()
        for item in links
        if isinstance(item, dict)
    ):
        raise ValueError("identity_email or technical link target required")

    identity_id, engagement_id = resolve_identity_and_engagement(
        store,
        identity_email=email,
        display_name=display_name,
        message_id=message_id,
        links=links,
        within_days=within_days,
    )

    if email:
        store.upsert_link(
            engagement_id=engagement_id,
            link_type="identity_email",
            target_id=email.lower(),
            source_repo="gmail-agent",
            confidence=0.7,
        )

    mid = str(message_id or "").strip()
    if mid:
        store.upsert_link(
            engagement_id=engagement_id,
            link_type="gmail_message",
            target_id=mid,
            source_repo="gmail-agent",
            confidence=1.0,
        )

    written: list[dict[str, Any]] = []
    for item in links:
        if not isinstance(item, dict):
            continue
        link_type = str(item.get("link_type") or "").strip()
        target_id = str(item.get("target_id") or "").strip()
        if not link_type or not target_id:
            continue
        row = store.upsert_link(
            engagement_id=engagement_id,
            link_type=link_type,
            target_id=target_id,
            source_repo=str(item.get("source_repo") or "gmail-agent").strip() or "gmail-agent",
            confidence=float(item.get("confidence") or 1.0),
            metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
        )
        written.append(row)

    _apply_customer_identity_metadata(
        store,
        identity_id=identity_id,
        engagement_id=engagement_id,
        identity_email=email,
        display_name=display_name,
        identity_metadata=identity_metadata,
        engagement_metadata=engagement_metadata,
        links=links,
    )

    return {
        "identity_id": identity_id,
        "engagement_id": engagement_id,
        "links": written,
    }
