"""P2.0 customer identity metadata tests."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from correlation_registry.heuristics import register_link_bundle
from correlation_registry.identity_metadata import (
    IDENTITY_KIND_ORGANIZATION,
    IDENTITY_KIND_PERSON,
    build_property_anchor,
    infer_identity_kind,
    normalize_engagement_metadata,
    normalize_identity_metadata,
)
from correlation_registry.store import InMemoryCorrelationRegistryStore


def test_infer_identity_kind_person_vs_organization() -> None:
    assert infer_identity_kind(email="jan.kowalski@example.com") == IDENTITY_KIND_PERSON
    assert infer_identity_kind(email="biuro@firma.pl", display_name="Firma X") == IDENTITY_KIND_ORGANIZATION
    assert infer_identity_kind(email="a@b.pl", metadata={"nip": "5252445767"}) == IDENTITY_KIND_ORGANIZATION


def test_property_anchor_shape() -> None:
    anchor = build_property_anchor(
        address="ul. Testowa 12, Kraków",
        nip="525-244-57-67",
        investment_key="case_abc",
    )
    assert anchor["address_norm"] == "ul. testowa 12, kraków"
    assert anchor["nip"] == "5252445767"
    assert anchor["investment_key"] == "case_abc"


def test_register_link_bundle_writes_metadata() -> None:
    store = InMemoryCorrelationRegistryStore()
    result = register_link_bundle(
        store,
        identity_email="handel@sofiterm.pl",
        display_name="Sofiterm Sp. z o.o.",
        links=[
            {
                "link_type": "mailbox_case",
                "target_id": "case_inv_1",
                "source_repo": "gmail-agent",
                "metadata": {"address": "ul. Fabryczna 1", "nip": "1234567890"},
            }
        ],
    )
    identity = store.get_identity(result["identity_id"])
    engagement = store.get_engagement(result["engagement_id"])
    assert identity is not None and engagement is not None
    assert identity["metadata"]["identity_kind"] == IDENTITY_KIND_ORGANIZATION
    assert engagement["metadata"]["binding_level_applied"] == 1
    anchor = engagement["metadata"]["property_anchor"]
    assert anchor["investment_key"] == "case_inv_1"
    assert anchor["nip"] == "1234567890"
    assert "fabryczna" in anchor["address_norm"]


def test_normalize_identity_metadata_defaults_person() -> None:
    meta = normalize_identity_metadata({}, email="user@test.pl")
    assert meta["identity_kind"] == IDENTITY_KIND_PERSON


# Phase P2 proof token (gate): CUSTOMER_IDENTITY_METADATA_PROOF_OK
