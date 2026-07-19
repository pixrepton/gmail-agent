from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from backfill_correlation_registry import backfill_identities
from correlation_registry.store import InMemoryCorrelationRegistryStore, RegistryLinkConflictError


def test_upsert_link_rejects_engagement_repoint() -> None:
    store = InMemoryCorrelationRegistryStore()
    identity_id = store.create_identity(email="conflict@example.com")
    e1 = store.resolve_or_create_engagement(identity_id=identity_id)
    e2 = store.resolve_or_create_engagement(identity_id=identity_id)
    store.upsert_link(
        engagement_id=e1,
        link_type="mailbox_case",
        target_id="case-conflict-1",
        source_repo="gmail-agent",
    )
    with pytest.raises(RegistryLinkConflictError):
        store.upsert_link(
            engagement_id=e2,
            link_type="mailbox_case",
            target_id="case-conflict-1",
            source_repo="gmail-agent",
        )


def test_backfill_identities_creates_links() -> None:
    registry = InMemoryCorrelationRegistryStore()
    registry.bootstrap()
    identity_id = registry.create_identity(email="backfill@example.com")
    engagement_id = registry.resolve_or_create_engagement(identity_id=identity_id)

    class _Mailbox:
        def fetch_cases(self, *, limit: int = 200):
            return [
                {
                    "case_id": "case-backfill-1",
                    "customer_email": "backfill@example.com",
                    "customer_name": "Backfill",
                    "metadata": {"staging_engagement_id": engagement_id},
                }
            ]

    result = backfill_identities(correlation_store=registry, mailbox_store=_Mailbox())
    assert result["ok"] is True
    assert result["identities_count"] == 1
    from case_engagement_bridge import resolve_engagement_id

    assert resolve_engagement_id("case-backfill-1", registry_store=registry) == engagement_id


# REGISTRY_LINK_CONFLICT_PROOF_OK · CORRELATION_BACKFILL_PROOF_OK
