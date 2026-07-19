from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from correlation_registry.heuristics import (
    find_engagement_by_technical_precedence,
    register_link_bundle,
)
from correlation_registry.service import CorrelationRegistryService
from correlation_registry.snapshot import fetch_workflow_context_packs_parallel
from correlation_registry.store import InMemoryCorrelationRegistryStore


def _service() -> CorrelationRegistryService:
    return CorrelationRegistryService(InMemoryCorrelationRegistryStore())


def test_same_email_one_identity_two_repo_links_via_message_id() -> None:
    svc = _service()
    first = register_link_bundle(
        svc.store,
        identity_email="Klient@Example.pl",
        message_id="msg-shared",
        links=[
            {"link_type": "mailbox_case", "target_id": "case-1", "source_repo": "gmail-agent"},
        ],
    )
    second = register_link_bundle(
        svc.store,
        identity_email="klient@example.pl",
        message_id="msg-shared",
        links=[
            {
                "link_type": "cieplo_workflow",
                "target_id": "wf-uuid-1",
                "source_repo": "topinstal-cieplo-orchestrator",
            },
        ],
    )
    assert first["identity_id"] == second["identity_id"]
    assert first["engagement_id"] == second["engagement_id"]
    links = svc.store.list_links_for_engagement(first["engagement_id"])
    types = {row["link_type"] for row in links}
    assert "mailbox_case" in types
    assert "cieplo_workflow" in types


def test_same_message_id_merges_engagement() -> None:
    svc = _service()
    a = svc.sync_mailbox_case(
        case_id="case-a",
        customer_email="a@test.pl",
        message_id="gmail-msg-99",
    )
    b = svc.sync_cieplo_workflow(
        workflow_id="wf-b",
        client_email="a@test.pl",
        message_id="gmail-msg-99",
    )
    assert a is not None and b is not None
    assert a["engagement_id"] == b["engagement_id"]


def test_engagement_id_never_equals_case_id() -> None:
    svc = _service()
    result = svc.sync_mailbox_case(
        case_id="case_xyz",
        customer_email="z@test.pl",
    )
    assert result is not None
    assert result["engagement_id"] != "case_xyz"


def test_upsert_link_idempotent() -> None:
    svc = _service()
    payload = {
        "identity_email": "idempotent@test.pl",
        "links": [{"link_type": "mailbox_case", "target_id": "case-idem", "source_repo": "gmail-agent"}],
    }
    one = svc.register_links_payload(payload)
    two = svc.register_links_payload(payload)
    assert one["engagement_id"] == two["engagement_id"]
    assert len(svc.store.list_links_for_engagement(one["engagement_id"])) >= 2


def test_same_email_different_cases_get_separate_engagements() -> None:
    """Property manager email — two investments must not collapse to one identity via UNIQUE email."""
    svc = _service()
    first = register_link_bundle(
        svc.store,
        identity_email="manager@firma.pl",
        links=[{"link_type": "mailbox_case", "target_id": "case-invest-A", "source_repo": "gmail-agent"}],
    )
    second = register_link_bundle(
        svc.store,
        identity_email="manager@firma.pl",
        links=[{"link_type": "mailbox_case", "target_id": "case-invest-B", "source_repo": "gmail-agent"}],
    )
    assert first["engagement_id"] != second["engagement_id"]
    assert first["identity_id"] != second["identity_id"]


def test_thread_id_revives_old_engagement_over_email_window() -> None:
    """Reply on old thread after 6 months must not open a new engagement due to email window."""
    store = InMemoryCorrelationRegistryStore()
    old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    identity_id = store.create_identity(email="rodzina@test.pl", display_name="Rodzina")
    old_engagement = store.resolve_or_create_engagement(identity_id=identity_id, anchor_at=old_ts)
    store.upsert_link(
        engagement_id=old_engagement,
        link_type="gmail_thread",
        target_id="thread-revival-42",
        source_repo="gmail-agent",
        confidence=1.0,
    )
    store.upsert_link(
        engagement_id=old_engagement,
        link_type="mailbox_case",
        target_id="case-old",
        source_repo="gmail-agent",
        confidence=1.0,
    )

    result = register_link_bundle(
        store,
        identity_email="rodzina@test.pl",
        message_id="msg-new-reply",
        links=[
            {"link_type": "gmail_thread", "target_id": "thread-revival-42", "source_repo": "gmail-agent"},
            {"link_type": "mailbox_case", "target_id": "case-old", "source_repo": "gmail-agent"},
        ],
        within_days=30,
    )
    assert result["engagement_id"] == old_engagement


def test_widget_placeholder_primary_email_upgraded_on_real_client_email() -> None:
    svc = _service()
    placeholder = "lead-widget+abc@widget.topinstal.local"
    first = svc.register_links_payload(
        {
            "identity_email": placeholder,
            "links": [
                {
                    "link_type": "calc_request_snapshot",
                    "target_id": "snap-widget-1",
                    "source_repo": "topinstal-lead-widget",
                }
            ],
        }
    )
    identity_id = first["identity_id"]
    assert svc.store.get_identity(identity_id)["primary_email"] == placeholder

    second = svc.register_links_payload(
        {
            "identity_email": "Klient@Example.pl",
            "links": [
                {
                    "link_type": "calc_request_snapshot",
                    "target_id": "snap-widget-1",
                    "source_repo": "topinstal-lead-widget",
                }
            ],
        }
    )
    assert second["identity_id"] == identity_id
    assert second["engagement_id"] == first["engagement_id"]
    assert svc.store.get_identity(identity_id)["primary_email"] == "klient@example.pl"


def test_register_links_payload_validates_links_and_normalizes_link_type() -> None:
    svc = _service()
    with pytest.raises(ValueError, match="links must be a list"):
        svc.register_links_payload({"identity_email": "a@test.pl", "links": "bad"})
    with pytest.raises(ValueError, match="link_type is required"):
        svc.register_links_payload(
            {
                "identity_email": "a@test.pl",
                "links": [{"target_id": "x", "source_repo": "gmail-agent"}],
            }
        )
    result = svc.register_links_payload(
        {
            "identity_email": "widget@test.pl",
            "links": [
                {
                    "link_type": "CALC_REQUEST_SNAPSHOT",
                    "target_id": "calc-1",
                    "source_repo": "topinstal-lead-widget",
                }
            ],
            "within_days": 9999,
        }
    )
    links = svc.store.list_links_for_engagement(result["engagement_id"])
    assert any(row["link_type"] == "calc_request_snapshot" for row in links)


def test_calc_request_snapshot_technical_precedence_reuses_engagement() -> None:
    store = InMemoryCorrelationRegistryStore()
    first = register_link_bundle(
        store,
        identity_email="lead-widget+foo@widget.topinstal.local",
        links=[
            {
                "link_type": "calc_request_snapshot",
                "target_id": "calc-99",
                "source_repo": "topinstal-lead-widget",
            }
        ],
    )
    second = register_link_bundle(
        store,
        identity_email="klient@real.pl",
        links=[
            {
                "link_type": "calc_request_snapshot",
                "target_id": "calc-99",
                "source_repo": "topinstal-lead-widget",
            }
        ],
    )
    assert first["engagement_id"] == second["engagement_id"]


def test_technical_precedence_beats_recent_email_only_engagement() -> None:
    store = InMemoryCorrelationRegistryStore()
    identity_a = store.create_identity(email="shared@test.pl")
    recent_engagement = store.resolve_or_create_engagement(identity_id=identity_a)
    store.upsert_link(
        engagement_id=recent_engagement,
        link_type="identity_email",
        target_id="shared@test.pl",
        source_repo="gmail-agent",
        confidence=0.7,
    )

    identity_b = store.create_identity(email="shared@test.pl")
    old_engagement = store.resolve_or_create_engagement(
        identity_id=identity_b,
        anchor_at=(datetime.now(timezone.utc) - timedelta(days=120)).isoformat(),
    )
    store.upsert_link(
        engagement_id=old_engagement,
        link_type="mailbox_case",
        target_id="case-technical",
        source_repo="gmail-agent",
        confidence=1.0,
    )

    matched = find_engagement_by_technical_precedence(
        store,
        links=[{"link_type": "mailbox_case", "target_id": "case-technical", "source_repo": "gmail-agent"}],
        message_id="",
    )
    assert matched == old_engagement
    assert matched != recent_engagement


@pytest.mark.asyncio
async def test_parallel_workflow_fetch_partial_on_timeout() -> None:
    async def _fake_fetch(client: Any, workflow_id: str) -> tuple[str, dict | None, str | None]:
        if workflow_id == "wf-ok":
            return workflow_id, {"cieplo_workflow_id": workflow_id, "ok": True}, None
        return workflow_id, None, "timeout"

    with patch(
        "correlation_registry.snapshot.fetch_workflow_context_pack_async",
        side_effect=_fake_fetch,
    ):
        packs, missing = await fetch_workflow_context_packs_parallel(["wf-ok", "wf-slow"])

    assert len(packs) == 1
    assert packs[0]["cieplo_workflow_id"] == "wf-ok"
    assert any(m.get("reason") == "timeout" for m in missing)
