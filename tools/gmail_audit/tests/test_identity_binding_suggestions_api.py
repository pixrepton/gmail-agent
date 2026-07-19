"""P2.1 identity binding suggestions API read tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app
from correlation_registry.identity_binding import (
    SUGGESTION_PENDING,
    upsert_binding_suggestions,
)
from correlation_registry.service import CorrelationRegistryService
from correlation_registry.store import InMemoryCorrelationRegistryStore

# D1: /identity/binding-suggestions/scan now requires a verified mutation
# principal (default-deny). Read-only routes are unaffected.
_MUTATION_TOKEN = "d1-test-token"


def _client_with_store() -> tuple[TestClient, CorrelationRegistryService]:
    registry = CorrelationRegistryService(InMemoryCorrelationRegistryStore())
    registry.bootstrap()
    app = create_app(registry_provider=lambda: registry)
    return TestClient(app), registry


def test_binding_suggestions_list_enriched_read() -> None:
    client, registry = _client_with_store()
    store = registry.store
    src = store.create_identity(
        email="alpha@example.com",
        display_name="Alpha Sp. z o.o.",
        metadata={"nip": "5252445767", "identity_kind": "organization"},
    )
    tgt = store.create_identity(
        email="beta@example.com",
        display_name="Beta Sp. z o.o.",
        metadata={"nip": "5252445767", "identity_kind": "organization"},
    )
    upsert_binding_suggestions(
        store,
        [
            {
                "source_identity_id": src,
                "target_identity_id": tgt,
                "signal_type": "nip_match",
                "confidence": 0.8,
                "evidence_json": {"nip": "5252445767"},
            }
        ],
    )

    response = client.get("/identity/binding-suggestions", params={"status": SUGGESTION_PENDING})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["schema_version"] == "identity_binding_suggestion_list.v1"
    assert body["count"] == 1
    item = body["items"][0]
    assert item["schema_version"] == "identity_binding_suggestion.v1"
    assert item["signal_label_pl"] == "Ten sam NIP"
    assert item["source_identity"]["primary_email"] == "alpha@example.com"
    assert item["target_identity"]["primary_email"] == "beta@example.com"
    assert item["source_identity"]["identity_kind"] == "organization"

    suggestion_id = item["suggestion_id"]
    detail = client.get(f"/identity/binding-suggestions/{suggestion_id}")
    assert detail.status_code == 200
    assert detail.json()["item"]["suggestion_id"] == suggestion_id


def test_binding_suggestions_scan_and_read() -> None:
    client, registry = _client_with_store()
    store = registry.store
    store.create_identity(email="one@firma.pl", metadata={"nip": "1234567890"})
    store.create_identity(email="two@firma.pl", metadata={"nip": "1234567890"})

    os.environ["DASZEK_NODE_B_API_TOKEN"] = _MUTATION_TOKEN
    try:
        scan = client.post(
            "/identity/binding-suggestions/scan",
            params={"limit": 10},
            headers={"Authorization": f"Bearer {_MUTATION_TOKEN}"},
        )
    finally:
        os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
    assert scan.status_code == 200
    assert scan.json()["detected"] >= 1

    listed = client.get("/identity/binding-suggestions")
    assert listed.status_code == 200
    assert listed.json()["count"] >= 1


# Phase P2 proof token (gate): IDENTITY_BINDING_SUGGEST_PROOF_OK
