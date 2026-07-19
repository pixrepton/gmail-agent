from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_engagement_bridge import resolve_case_id, resolve_engagement_id
from correlation_registry.store import InMemoryCorrelationRegistryStore


def test_resolve_engagement_id_via_mailbox_case_link() -> None:
    registry = InMemoryCorrelationRegistryStore()
    registry.bootstrap()
    identity_id = registry.create_identity(email="lead@example.com")
    engagement_id = registry.resolve_or_create_engagement(identity_id=identity_id)
    registry.upsert_link(
        engagement_id=engagement_id,
        link_type="mailbox_case",
        target_id="case-bridge-1",
        source_repo="gmail-agent",
    )
    assert resolve_engagement_id("case-bridge-1", registry_store=registry) == engagement_id


def test_resolve_case_id_round_trip() -> None:
    registry = InMemoryCorrelationRegistryStore()
    registry.bootstrap()
    identity_id = registry.create_identity(email="round@example.com")
    engagement_id = registry.resolve_or_create_engagement(identity_id=identity_id)
    registry.upsert_link(
        engagement_id=engagement_id,
        link_type="mailbox_case",
        target_id="case-round-1",
        source_repo="gmail-agent",
    )
    assert resolve_case_id(engagement_id, registry_store=registry) == "case-round-1"
    assert resolve_engagement_id("case-round-1", registry_store=registry) == engagement_id


def test_write_executors_reexports_bridge_helper() -> None:
    from agent_runtime.tools.write_executors import _engagement_id_for_case

    registry = InMemoryCorrelationRegistryStore()
    registry.bootstrap()
    identity_id = registry.create_identity(email="exec@example.com")
    engagement_id = registry.resolve_or_create_engagement(identity_id=identity_id)
    registry.upsert_link(
        engagement_id=engagement_id,
        link_type="mailbox_case",
        target_id="case-exec-1",
        source_repo="gmail-agent",
    )
    assert _engagement_id_for_case(registry, "case-exec-1") == engagement_id


# Phase 7 proof token (gate): CASE_ENGAGEMENT_BRIDGE_PROOF_OK
