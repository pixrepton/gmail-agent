from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest
import requests


TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import load_settings
from correlation_registry.service import build_correlation_registry_service
from mailbox_memory_runtime import build_mailbox_memory_runtime


pytestmark = pytest.mark.skipif(
    os.getenv("OFFER_TRUTH_POSTGRES_PROOF") != "1",
    reason="set OFFER_TRUTH_POSTGRES_PROOF=1 for canonical local DB/API proof",
)


def _field_provenance(*, workflow_id: str, price: int, quality: str, producer: str) -> dict:
    common = {
        "producer": producer,
        "source_repo": producer,
        "source_workflow": workflow_id,
        "observed_at": "2026-08-29T12:00:00+00:00",
        "revision": f"{producer}:proof",
        "canonical_status": "VERIFIED",
        "provenance_quality": quality,
    }
    return {
        "selected_model": {
            **common,
            "value": "KIT-WC09K3E8",
            "source_object": "kalk-top OfferDTO engineering selection",
            "source_path": "engineering.selection.pumpModel",
            "origin_kind": "calculated",
            "evidence_reference": f"{workflow_id}:engineering.selection",
        },
        "final_price_pln": {
            **common,
            "value": price,
            "source_object": "kalk-top OfferDTO pricing",
            "source_path": "pricing.totals.gross",
            "origin_kind": "derived" if quality == "PROVEN" else "assumed",
            "transformation": "CIEPLO_PRICE_ADJUSTMENT_GROSS_PLN" if quality == "PROVEN" else "historical_reconstruction",
            "evidence_reference": f"{workflow_id}:pricing.totals.gross",
        },
        "document": {
            **common,
            "value": {"document_id": "pdf-truth", "url": "https://example.test/pdf-truth.pdf", "status": "ready"},
            "source_object": "top-instal-generator response",
            "source_path": "generator_response.document",
            "origin_kind": "generated",
            "evidence_reference": f"{workflow_id}:pdf-truth",
        },
        "delivery_status": {
            **common,
            "value": "not_sent_test_fixture",
            "source_object": "test workflow execution",
            "source_path": "workflow.customer_delivery_decision",
            "origin_kind": "execution_fact",
            "evidence_reference": f"{workflow_id}:delivery",
        },
    }


def _event(*, case_id: str, engagement_id: str, offer_id: str, workflow_id: str, price: int, quality: str, source_repo: str, occurred_at: str) -> dict:
    return {
        "event_type": "offer.generated",
        "source_repo": source_repo,
        "engagement_id": engagement_id,
        "case_id": case_id,
        "occurred_at": occurred_at,
        "payload": {
            "case_id": case_id,
            "offer_id": offer_id,
            "source": "cieplo",
            "selected_model": "KIT-WC09K3E8",
            "final_price_pln": price,
            "document": {"document_id": "pdf-truth", "url": "https://example.test/pdf-truth.pdf", "status": "ready"},
            "delivery_status": "not_sent_test_fixture",
            "status": "generated",
            "producer_revision": f"{source_repo}:proof",
            "provenance": {"workflow_id": workflow_id, "test_fixture": True},
            "field_provenance": _field_provenance(workflow_id=workflow_id, price=price, quality=quality, producer=source_repo),
        },
        "correlation": {"case_id": case_id, "offer_id": offer_id, "workflow_id": workflow_id},
    }


def test_offer_truth_resolution_real_postgres_api_projection() -> None:
    settings = load_settings(require_groq=False, require_google=False)
    database_url = str(getattr(settings, "mailbox_memory_database_url", "") or "").strip()
    token = str(os.getenv("NODE_B_REGISTRY_TOKEN") or "").strip()
    base_url = str(os.getenv("OFFER_TRUTH_NODE_B_BASE_URL") or "http://127.0.0.1:8766").rstrip("/")
    case_id = str(os.getenv("OFFER_TRUTH_PROOF_CASE_ID") or "case_offer_truth_20260829a")
    email = f"{case_id}@example.test"
    workflow_id = f"wf_{case_id}"
    auto_offer_id = f"cieplo:{workflow_id}:auto"
    operator_offer_id = f"cieplo:{workflow_id}:operator"
    assert database_url and token

    runtime = build_mailbox_memory_runtime(settings)
    assert runtime is not None
    runtime.bootstrap()
    runtime.store.upsert_case(
        {
            "case_id": case_id,
            "subject": "Offer Truth Resolution isolated proof",
            "title": "Offer Truth Resolution isolated proof",
            "status": "open",
            "customer_email": email,
            "metadata": {"fixture_kind": "offer_truth_resolution", "customer_side_effects": 0},
        }
    )
    registry = build_correlation_registry_service(database_url)
    assert registry is not None
    registry.bootstrap()
    mailbox_binding = registry.sync_mailbox_case(case_id=case_id, customer_email=email, message_id=f"msg_{case_id}")
    workflow_binding = registry.sync_cieplo_workflow(workflow_id=workflow_id, client_email=email, message_id=f"msg_{case_id}")
    assert mailbox_binding and workflow_binding
    engagement_id = str(mailbox_binding["engagement_id"])
    assert workflow_binding["engagement_id"] == engagement_id

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Auto-resolution: PROVEN beats INFERRED regardless of arrival order.
    for event in (
        _event(case_id=case_id, engagement_id=engagement_id, offer_id=auto_offer_id, workflow_id=workflow_id, price=35856, quality="INFERRED", source_repo="legacy-offer-import", occurred_at="2026-08-29T12:00:00+00:00"),
        _event(case_id=case_id, engagement_id=engagement_id, offer_id=auto_offer_id, workflow_id=workflow_id, price=36856, quality="PROVEN", source_repo="cieplo-orchestrator", occurred_at="2026-08-29T12:01:00+00:00"),
    ):
        response = requests.post(f"{base_url}/internal/os-events", headers=headers, json=event, timeout=20)
        assert response.status_code == 200, response.text

    # Operator path: equally strong PROVEN observations fail closed.
    for event in (
        _event(case_id=case_id, engagement_id=engagement_id, offer_id=operator_offer_id, workflow_id=workflow_id, price=36856, quality="PROVEN", source_repo="cieplo-orchestrator", occurred_at="2026-08-29T12:10:00+00:00"),
        _event(case_id=case_id, engagement_id=engagement_id, offer_id=operator_offer_id, workflow_id=workflow_id, price=37856, quality="PROVEN", source_repo="legacy-offer-import", occurred_at="2026-08-29T12:11:00+00:00"),
    ):
        response = requests.post(f"{base_url}/internal/os-events", headers=headers, json=event, timeout=20)
        assert response.status_code == 200, response.text

    latest = requests.get(f"{base_url}/cases/{case_id}/offers/latest", timeout=20)
    assert latest.status_code == 200, latest.text
    before = latest.json()
    auto_conflict = next(item for item in before["conflicts"] if item["offer_id"] == auto_offer_id)
    operator_conflict = next(item for item in before["conflicts"] if item["offer_id"] == operator_offer_id)
    assert auto_conflict["resolution_status"] == "AUTO_RESOLVED"
    assert auto_conflict["canonical_value"] == 36856
    assert operator_conflict["resolution_status"] == "OPERATOR_REQUIRED"
    chosen = next(item for item in operator_conflict["candidate_evidence"] if item["value"] == 36856)
    command = {
        "conflict_id": operator_conflict["conflict_id"],
        "expected_revision": operator_conflict["resolution_version"],
        "candidate_id": chosen["candidate_id"],
        "reason": "Canonical local Postgres proof",
    }
    denied = requests.post(
        f"{base_url}/cases/{case_id}/offers/{operator_offer_id}/conflicts/resolve",
        json=command,
        timeout=20,
    )
    assert denied.status_code == 401
    resolved = requests.post(
        f"{base_url}/cases/{case_id}/offers/{operator_offer_id}/conflicts/resolve",
        headers=headers,
        json=command,
        timeout=20,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolution_status"] == "OPERATOR_RESOLVED"
    duplicate = requests.post(
        f"{base_url}/cases/{case_id}/offers/{operator_offer_id}/conflicts/resolve",
        headers=headers,
        json=command,
        timeout=20,
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["idempotent"] is True

    after = requests.get(f"{base_url}/cases/{case_id}/offers/latest", timeout=20).json()
    assert after["offer"]["final_price_pln"] == 36856
    resolved_conflict = next(item for item in after["conflicts"] if item["offer_id"] == operator_offer_id)
    assert resolved_conflict["resolution_status"] == "OPERATOR_RESOLVED"
    assert resolved_conflict["history"]

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_type, count(*)
                FROM unified_os_events
                WHERE case_id = %s
                  AND event_type = ANY(%s)
                GROUP BY event_type
                ORDER BY event_type
                """,
                (case_id, ["offer.generated", "offer.conflict_detected", "offer.conflict_resolved"]),
            )
            counts = dict(cur.fetchall())
    assert counts["offer.generated"] == 4
    assert counts["offer.conflict_detected"] == 1
    assert counts["offer.conflict_resolved"] == 2

    # A later third PROVEN candidate reopens rather than overwriting the operator decision.
    stronger = _event(case_id=case_id, engagement_id=engagement_id, offer_id=operator_offer_id, workflow_id=workflow_id, price=38856, quality="PROVEN", source_repo="engineering-audit", occurred_at="2026-08-29T12:20:00+00:00")
    reopened_write = requests.post(f"{base_url}/internal/os-events", headers=headers, json=stronger, timeout=20)
    assert reopened_write.status_code == 200, reopened_write.text
    reopened = requests.get(f"{base_url}/cases/{case_id}/offers/latest", timeout=20).json()
    reopened_conflict = next(item for item in reopened["conflicts"] if item["offer_id"] == operator_offer_id)
    assert reopened_conflict["resolution_status"] == "OPERATOR_REQUIRED"
    assert reopened_conflict["resolution_basis"] == "NEW_STRONG_EVIDENCE_AFTER_OPERATOR_RESOLUTION"
    assert reopened["trust_status"] == "CONFLICTED"

