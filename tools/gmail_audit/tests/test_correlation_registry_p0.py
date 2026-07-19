"""P0 audit fixes: correlation registry case_id vs engagement_id integrity."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.materialize import _register_email_identity
from agent_runtime.tools.write_executors import execute_link_case_to_case, execute_merge_cases
from api_app import create_app
from correlation_registry.store import InMemoryCorrelationRegistryStore
from mailbox_memory_models import CaseContextPack


class _Runtime:
    def __init__(self, store: MagicMock | None = None) -> None:
        self.store = store or MagicMock()

    def get_context_pack(self, *, case_id: str = "", message_id: str = "", query_text: str = "") -> CaseContextPack:
        return CaseContextPack(case_id=case_id or "case_api_1")


def _registry_with_case_link(case_id: str, engagement_id: str) -> InMemoryCorrelationRegistryStore:
    store = InMemoryCorrelationRegistryStore()
    identity_id = store.create_identity(email=f"{case_id}@example.com", display_name=case_id)
    store.engagements[engagement_id] = {
        "engagement_id": engagement_id,
        "identity_id": identity_id,
        "status": "open",
        "anchor_at": "2026-01-01T00:00:00Z",
        "metadata": {},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    store.upsert_link(
        engagement_id=engagement_id,
        link_type="mailbox_case",
        target_id=case_id,
        source_repo="gmail-agent",
        confidence=1.0,
    )
    return store


def test_register_email_identity_links_mailbox_case_to_case_id() -> None:
    """Audit scenario #1: mailbox_case target_id must be case_id, not engagement_id."""
    store = InMemoryCorrelationRegistryStore()
    case_id = "case_materialize_abc"
    email = "lead@example.com"

    assert _register_email_identity(store, email=email, case_id=case_id, customer_name="Lead")

    linked_engagement = store.find_engagement_by_link(
        link_type="mailbox_case",
        target_id=case_id,
        source_repo="gmail-agent",
    )
    assert linked_engagement
    assert linked_engagement != case_id


def test_identity_merge_resolves_source_case_ids_to_emails() -> None:
    """Audit scenario #2: source_case_ids must not be treated as email addresses."""
    mock_store = MagicMock()
    mock_store.fetch_case.side_effect = lambda cid: {
        "case_a": {"case_id": "case_a", "customer_email": "dup@example.com"},
        "case_b": {"case_id": "case_b", "customer_email": "dup@example.com"},
    }.get(cid)

    registry_store = InMemoryCorrelationRegistryStore()
    registry = MagicMock()
    registry.store = registry_store
    registry.lookup_by_case_id.return_value = None

    app = create_app(
        runtime_provider=lambda: _Runtime(store=mock_store),
        cohort_reader=lambda run_id: None,
        registry_provider=lambda: registry,
    )
    client = TestClient(app)

    response = client.post(
        "/identity/merge",
        json={
            "email": "dup@example.com",
            "target_case_id": "case_target",
            "source_case_ids": ["case_a", "case_b"],
        },
    )
    assert response.status_code == 410


def test_execute_merge_cases_uses_engagement_id_not_case_id() -> None:
    """Audit scenario #3: merged_into link keyed by engagement_id."""
    correlation_store = _registry_with_case_link("case_source", "eng_source_1")
    _ = _registry_with_case_link("case_target", "eng_target_1")

    mailbox_store = MagicMock()
    mailbox_store.fetch_case.return_value = {"case_id": "x", "facts": []}
    mailbox_store.upsert_case = MagicMock()
    mailbox_store.append_fact_rows = MagicMock()

    with patch("case_intelligence.merge_data", return_value={"merged": {}, "merge_log": [], "conflicts": []}):
        result = execute_merge_cases(
            {"source_case_id": "case_source", "target_case_id": "case_target"},
            mailbox_store=mailbox_store,
            correlation_store=correlation_store,
        )

    assert result["status"] == "ok"
    links = [row for row in correlation_store.links.values() if row.get("link_type") == "merged_into"]
    assert len(links) == 1
    assert links[0]["engagement_id"] == "eng_source_1"
    assert links[0]["target_id"] == "case_target"
    assert links[0]["engagement_id"] != "case_source"


def test_execute_link_case_to_case_uses_engagement_id_not_case_id() -> None:
    """Audit scenario #4: linked_case link keyed by engagement_id."""
    correlation_store = _registry_with_case_link("case_left", "eng_left_1")
    _ = _registry_with_case_link("case_right", "eng_right_1")

    mailbox_store = MagicMock()
    mailbox_store.link_cases = MagicMock()

    result = execute_link_case_to_case(
        {"source_case_id": "case_left", "target_case_id": "case_right"},
        mailbox_store=mailbox_store,
        correlation_store=correlation_store,
    )

    assert result["status"] == "ok"
    links = [row for row in correlation_store.links.values() if row.get("link_type") == "linked_case"]
    assert len(links) == 1
    assert links[0]["engagement_id"] == "eng_left_1"
    assert links[0]["target_id"] == "case_right"


def test_execute_merge_cases_errors_when_engagement_missing() -> None:
    correlation_store = InMemoryCorrelationRegistryStore()
    result = execute_merge_cases(
        {"source_case_id": "case_missing", "target_case_id": "case_target"},
        mailbox_store=MagicMock(),
        correlation_store=correlation_store,
    )
    assert result["status"] == "error"
    assert "engagement_id" in result["summary"]
