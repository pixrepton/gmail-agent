from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app
from offer_observability import (
    OFFER_CONFLICT_DETECTED_EVENT,
    OFFER_CONFLICT_RESOLVED_EVENT,
    OFFER_GENERATED_EVENT,
    OFFER_STATUS_UPDATED_EVENT,
    OfferObservationError,
    build_offer_field_provenance,
    build_offer_trust_reasons,
    derive_offer_trust_status,
    detect_offer_conflicts_for_case,
    record_operator_offer_resolution,
    reconcile_offer_truth_resolutions,
    project_latest_offer_for_case,
    record_offer_generated_from_os_event,
    record_offer_status_update_from_os_event,
)


def _source_field_provenance(
    *,
    workflow_id: str = "wf-1",
    model: str = "KIT-WC09K3E8",
    price: int = 36856,
    document: dict | None = None,
    delivery_status: str = "WARN",
) -> dict:
    doc = (
        {"document_id": "pdf-1", "url": "https://topinstal.example/pdf-1.pdf", "status": "ready"}
        if document is None
        else document
    )
    return {
        "selected_model": {
            "value": model,
            "producer": "cieplo-orchestrator",
            "source_repo": "cieplo-orchestrator",
            "source_workflow": workflow_id,
            "source_object": "kalk-top OfferDTO engineering selection",
            "source_path": "engineering.selection.pumpModel",
            "origin_kind": "calculated",
            "evidence_reference": "workflow.offer_json.engineering.selection",
            "observed_at": "2026-08-28T10:00:00+00:00",
            "revision": "964e784",
            "canonical_status": "VERIFIED",
            "provenance_quality": "PROVEN",
        },
        "final_price_pln": {
            "value": price,
            "producer": "cieplo-orchestrator",
            "source_repo": "cieplo-orchestrator",
            "source_workflow": workflow_id,
            "source_object": "kalk-top OfferDTO pricing",
            "source_path": "pricing.totals.gross",
            "source_value": price - 1000,
            "origin_kind": "derived",
            "operation": "gross_price_adjustment",
            "transformation": "CIEPLO_PRICE_ADJUSTMENT_GROSS_PLN",
            "adjustment": 1000,
            "result": price,
            "evidence_reference": "workflow.offer_json.pricing.totals.gross",
            "observed_at": "2026-08-28T10:00:00+00:00",
            "revision": "964e784",
            "canonical_status": "VERIFIED",
            "provenance_quality": "PROVEN",
        },
        "document": {
            "value": doc,
            "producer": "cieplo-orchestrator",
            "source_repo": "cieplo-orchestrator",
            "source_workflow": workflow_id,
            "source_object": "top-instal-generator response",
            "source_path": "generator_response.document | generator_response.readiness | workflow.pdf_download_url",
            "origin_kind": "generated",
            "evidence_reference": doc.get("document_id") or doc.get("url"),
            "observed_at": "2026-08-28T10:00:00+00:00",
            "revision": "964e784",
            "canonical_status": "VERIFIED",
            "provenance_quality": "PROVEN",
        },
        "delivery_status": {
            "value": delivery_status,
            "producer": "cieplo-orchestrator",
            "source_repo": "cieplo-orchestrator",
            "source_workflow": workflow_id,
            "source_object": "cieplo workflow execution",
            "source_path": "workflow.customer_delivery_decision",
            "origin_kind": "execution_fact",
            "evidence_reference": "workflow.customer_delivery_decision",
            "observed_at": "2026-08-28T10:00:00+00:00",
            "revision": "964e784",
            "canonical_status": "VERIFIED",
            "provenance_quality": "PROVEN",
        },
    }


def _raw_offer_event(**overrides):
    document = {
        "document_id": "pdf-2zahe",
        "url": "https://topinstal.example/offers/pdf-2zahe.pdf",
        "sha256": "abc123",
        "status": "ready",
    }
    payload = {
        "event_type": OFFER_GENERATED_EVENT,
        "source_repo": "cieplo-orchestrator",
        "engagement_id": "eng_offer_1",
        "occurred_at": "2026-08-28T10:00:00+00:00",
        "payload": {
            "offer_id": "offer-2zahe",
            "source": "cieplo",
            "selected_model": "PANASONIC KIT-WC09K3E8",
            "final_price_pln": 33346,
            "document": document,
            "delivery_status": "held_for_review",
            "status": "generated",
            "producer_revision": "cieplo-orchestrator:ccf4aad",
            "provenance": {"workflow_id": "wf-2zahe", "result_id": "2zahe"},
            "field_provenance": _source_field_provenance(
                workflow_id="wf-2zahe",
                model="PANASONIC KIT-WC09K3E8",
                price=33346,
                document=document,
                delivery_status="held_for_review",
            ),
        },
        "correlation": {"case_id": "case_offer_1", "workflow_id": "wf-2zahe"},
    }
    payload.update(overrides)
    return payload


def _projected_offer(**overrides):
    document = {"document_id": "pdf-1", "url": "https://topinstal.example/pdf-1.pdf", "status": "ready"}
    offer = {
        "case_id": "case_offer_1",
        "offer_id": "cieplo:wf-1",
        "source": "cieplo",
        "created_at": "2026-08-28T10:00:00+00:00",
        "updated_at": "2026-08-28T10:05:00+00:00",
        "selected_model": "KIT-WC09K3E8",
        "final_price_pln": 36856,
        "document": document,
        "delivery_status": "WARN",
        "status": "done",
        "provenance": {"source_repo": "cieplo-orchestrator", "workflow_id": "wf-1"},
        "producer_revision": "964e784",
        "latest_event_id": "osevt_latest",
    }
    offer.update(overrides)
    if "field_provenance" not in overrides:
        offer["field_provenance"] = _source_field_provenance(
            workflow_id=offer["provenance"].get("workflow_id", "wf-1") if isinstance(offer.get("provenance"), dict) else "wf-1",
            model=offer.get("selected_model", ""),
            price=offer.get("final_price_pln") or 0,
            document=offer.get("document") if isinstance(offer.get("document"), dict) else {},
            delivery_status=offer.get("delivery_status", ""),
        )
    return offer


def test_offer_generated_records_canonical_observation_reference() -> None:
    published = []

    def _publisher(**kwargs):
        published.append(kwargs)
        return "osevt_offer_1"

    result = record_offer_generated_from_os_event(
        database_url="postgresql://test",
        raw_event=_raw_offer_event(),
        source_repo="cieplo-orchestrator",
        engagement_id="eng_offer_1",
        existing_lookup=lambda *_args, **_kwargs: None,
        publisher=_publisher,
    )

    assert result["ok"] is True
    assert result["idempotent"] is False
    assert published[0]["event_type"] == OFFER_GENERATED_EVENT
    assert published[0]["case_id"] == "case_offer_1"
    assert published[0]["engagement_id"] == "eng_offer_1"
    assert published[0]["payload"]["selected_model"] == "PANASONIC KIT-WC09K3E8"
    assert published[0]["payload"]["final_price_pln"] == 33346
    assert published[0]["payload"]["document"]["document_id"] == "pdf-2zahe"
    assert published[0]["payload"]["provenance"]["workflow_id"] == "wf-2zahe"
    assert published[0]["payload"]["field_provenance"]["final_price_pln"]["origin_kind"] == "derived"
    assert published[0]["payload"]["field_provenance"]["final_price_pln"]["transformation"] == "CIEPLO_PRICE_ADJUSTMENT_GROSS_PLN"


def test_offer_generated_retry_is_idempotent_for_same_offer() -> None:
    existing = {
        "event_id": "osevt_existing",
        "event_type": OFFER_GENERATED_EVENT,
        "source_repo": "cieplo-orchestrator",
        "engagement_id": "eng_offer_1",
        "case_id": "case_offer_1",
        "occurred_at": "2026-08-28T10:00:00+00:00",
        "payload": {"case_id": "case_offer_1", "offer_id": "offer-2zahe", "status": "generated"},
        "correlation": {"case_id": "case_offer_1", "offer_id": "offer-2zahe"},
    }

    result = record_offer_generated_from_os_event(
        database_url="postgresql://test",
        raw_event=_raw_offer_event(),
        source_repo="cieplo-orchestrator",
        engagement_id="eng_offer_1",
        existing_lookup=lambda *_args, **_kwargs: existing,
        publisher=lambda **_kwargs: pytest.fail("duplicate generated event must not publish"),
    )

    assert result["ok"] is True
    assert result["event_id"] == "osevt_existing"
    assert result["idempotent"] is True


def test_offer_status_update_merges_without_conflicting_offer_fact() -> None:
    generated = {
        "event_id": "osevt_generated",
        "event_type": OFFER_GENERATED_EVENT,
        "source_repo": "cieplo-orchestrator",
        "engagement_id": "eng_offer_1",
        "case_id": "case_offer_1",
        "occurred_at": "2026-08-28T10:00:00+00:00",
        "payload": {
            "case_id": "case_offer_1",
            "offer_id": "offer-2zahe",
            "selected_model": "PANASONIC KIT-WC09K3E8",
            "final_price_pln": 33346,
            "document": {"document_id": "pdf-2zahe", "status": "ready"},
            "status": "generated",
        },
        "correlation": {"case_id": "case_offer_1", "offer_id": "offer-2zahe"},
    }
    published = []

    def _status_lookup(*_args, **kwargs):
        assert kwargs["event_type"] == OFFER_STATUS_UPDATED_EVENT
        assert kwargs["require_field_provenance"] is False
        return None

    def _publisher(**kwargs):
        published.append(kwargs)
        return "osevt_status_1"

    raw = _raw_offer_event(
        event_type=OFFER_STATUS_UPDATED_EVENT,
        occurred_at="2026-08-28T10:05:00+00:00",
        payload={"case_id": "case_offer_1", "offer_id": "offer-2zahe", "status": "sent", "delivery_status": "sent_to_customer"},
    )
    result = record_offer_status_update_from_os_event(
        database_url="postgresql://test",
        raw_event=raw,
        source_repo="cieplo-orchestrator",
        engagement_id="eng_offer_1",
        existing_generated_lookup=lambda *_args, **_kwargs: generated,
        existing_status_lookup=_status_lookup,
        publisher=_publisher,
    )

    assert result["ok"] is True
    assert published[0]["event_type"] == OFFER_STATUS_UPDATED_EVENT
    latest = project_latest_offer_for_case([generated, {
        "event_id": "osevt_status_1",
        "event_type": OFFER_STATUS_UPDATED_EVENT,
        "source_repo": "cieplo-orchestrator",
        "engagement_id": "eng_offer_1",
        "case_id": "case_offer_1",
        "occurred_at": "2026-08-28T10:05:00+00:00",
        "payload": published[0]["payload"],
        "correlation": published[0]["correlation"],
    }], case_id="case_offer_1")
    assert latest
    assert latest["offer_id"] == "offer-2zahe"
    assert latest["status"] == "sent"
    assert latest["delivery_status"] == "sent_to_customer"


def test_offer_status_update_can_enrich_existing_status_with_source_provenance_once() -> None:
    generated = {
        "event_id": "osevt_generated",
        "event_type": OFFER_GENERATED_EVENT,
        "source_repo": "cieplo-orchestrator",
        "engagement_id": "eng_offer_1",
        "case_id": "case_offer_1",
        "occurred_at": "2026-08-28T10:00:00+00:00",
        "payload": {
            "case_id": "case_offer_1",
            "offer_id": "offer-2zahe",
            "selected_model": "PANASONIC KIT-WC09K3E8",
            "final_price_pln": 33346,
            "document": {"document_id": "pdf-2zahe", "status": "ready"},
            "status": "generated",
        },
        "correlation": {"case_id": "case_offer_1", "offer_id": "offer-2zahe"},
    }
    existing_enriched = {
        "event_id": "osevt_status_enriched",
        "event_type": OFFER_STATUS_UPDATED_EVENT,
        "source_repo": "cieplo-orchestrator",
        "engagement_id": "eng_offer_1",
        "case_id": "case_offer_1",
        "occurred_at": "2026-08-28T10:06:00+00:00",
        "payload": {
            "case_id": "case_offer_1",
            "offer_id": "offer-2zahe",
            "status": "done",
            "field_provenance": _source_field_provenance(
                workflow_id="wf-2zahe",
                model="PANASONIC KIT-WC09K3E8",
                price=33346,
                document={"document_id": "pdf-2zahe", "status": "ready"},
            ),
        },
        "correlation": {"case_id": "case_offer_1", "offer_id": "offer-2zahe"},
    }
    calls = []

    def _status_lookup(*_args, **kwargs):
        calls.append(kwargs)
        assert kwargs["require_field_provenance"] is True
        return None if len(calls) == 1 else existing_enriched

    raw = _raw_offer_event(
        event_type=OFFER_STATUS_UPDATED_EVENT,
        occurred_at="2026-08-28T10:06:00+00:00",
        payload={
            "case_id": "case_offer_1",
            "offer_id": "offer-2zahe",
            "status": "done",
            "delivery_status": "WARN",
            "field_provenance": existing_enriched["payload"]["field_provenance"],
        },
    )
    published = []
    first = record_offer_status_update_from_os_event(
        database_url="postgresql://test",
        raw_event=raw,
        source_repo="cieplo-orchestrator",
        engagement_id="eng_offer_1",
        existing_generated_lookup=lambda *_args, **_kwargs: generated,
        existing_status_lookup=_status_lookup,
        publisher=lambda **kwargs: published.append(kwargs) or "osevt_status_enriched",
    )
    second = record_offer_status_update_from_os_event(
        database_url="postgresql://test",
        raw_event=raw,
        source_repo="cieplo-orchestrator",
        engagement_id="eng_offer_1",
        existing_generated_lookup=lambda *_args, **_kwargs: generated,
        existing_status_lookup=_status_lookup,
        publisher=lambda **_kwargs: pytest.fail("second enriched status must be idempotent"),
    )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert len(published) == 1
    assert published[0]["payload"]["field_provenance"]["final_price_pln"]["origin_kind"] == "derived"


def test_offer_observation_fails_closed_without_case_or_engagement_binding() -> None:
    with pytest.raises(OfferObservationError) as missing_case:
        record_offer_generated_from_os_event(
            database_url="postgresql://test",
            raw_event=_raw_offer_event(correlation={}),
            source_repo="cieplo-orchestrator",
            engagement_id="eng_offer_1",
            existing_lookup=lambda *_args, **_kwargs: None,
        )
    assert missing_case.value.code == "case_binding_required"

    with pytest.raises(OfferObservationError) as missing_engagement:
        record_offer_generated_from_os_event(
            database_url="postgresql://test",
            raw_event=_raw_offer_event(engagement_id=""),
            source_repo="cieplo-orchestrator",
            engagement_id="",
            existing_lookup=lambda *_args, **_kwargs: None,
        )
    assert missing_engagement.value.code == "engagement_binding_required"


def test_latest_offer_projection_answers_operator_question() -> None:
    latest = project_latest_offer_for_case(
        [
            {
                "event_id": "osevt_generated",
                "event_type": OFFER_GENERATED_EVENT,
                "source_repo": "cieplo-orchestrator",
                "engagement_id": "eng_offer_1",
                "case_id": "case_offer_1",
                "occurred_at": "2026-08-28T10:00:00+00:00",
                "payload": {
                    "case_id": "case_offer_1",
                    "offer_id": "offer-2zahe",
                    "source": "cieplo",
                    "selected_model": "PANASONIC KIT-WC09K3E8",
                    "final_price_pln": 33346,
                    "document": {"document_id": "pdf-2zahe", "status": "ready"},
                    "status": "generated",
                    "provenance": {"workflow_id": "wf-2zahe"},
                },
                "correlation": {"case_id": "case_offer_1", "offer_id": "offer-2zahe"},
            },
            {
                "event_id": "osevt_status",
                "event_type": OFFER_STATUS_UPDATED_EVENT,
                "source_repo": "cieplo-orchestrator",
                "engagement_id": "eng_offer_1",
                "case_id": "case_offer_1",
                "occurred_at": "2026-08-28T10:05:00+00:00",
                "payload": {"case_id": "case_offer_1", "offer_id": "offer-2zahe", "status": "review_required"},
                "correlation": {"case_id": "case_offer_1", "offer_id": "offer-2zahe"},
            },
        ],
        case_id="case_offer_1",
    )

    assert latest == {
        "case_id": "case_offer_1",
        "offer_id": "offer-2zahe",
        "source": "cieplo",
        "created_at": "2026-08-28T10:00:00+00:00",
        "updated_at": "2026-08-28T10:05:00+00:00",
        "selected_model": "PANASONIC KIT-WC09K3E8",
        "final_price_pln": 33346,
        "document": {"document_id": "pdf-2zahe", "status": "ready"},
        "delivery_status": "",
        "status": "review_required",
        "provenance": {"workflow_id": "wf-2zahe"},
        "producer_revision": "",
        "latest_event_id": "osevt_status",
    }


def test_latest_offer_projection_picks_newest_distinct_real_offer() -> None:
    events = [
        {
            "event_id": "osevt_old",
            "event_type": OFFER_GENERATED_EVENT,
            "source_repo": "cieplo-orchestrator",
            "engagement_id": "eng_offer_1",
            "case_id": "case_offer_1",
            "occurred_at": "2026-08-28T09:00:00+00:00",
            "payload": {
                "case_id": "case_offer_1",
                "offer_id": "cieplo:wf-old",
                "source": "cieplo",
                "selected_model": "PANASONIC OLD",
                "final_price_pln": 30000,
                "document": {"document_id": "pdf-old", "status": "ready"},
                "status": "generated",
                "provenance": {"workflow_id": "wf-old"},
            },
            "correlation": {"case_id": "case_offer_1", "offer_id": "cieplo:wf-old"},
        },
        {
            "event_id": "osevt_new",
            "event_type": OFFER_GENERATED_EVENT,
            "source_repo": "cieplo-orchestrator",
            "engagement_id": "eng_offer_1",
            "case_id": "case_offer_1",
            "occurred_at": "2026-08-28T12:00:00+00:00",
            "payload": {
                "case_id": "case_offer_1",
                "offer_id": "cieplo:wf-new",
                "source": "cieplo",
                "selected_model": "PANASONIC NEW",
                "final_price_pln": 41000,
                "document": {"document_id": "pdf-new", "status": "ready"},
                "status": "generated",
                "provenance": {"workflow_id": "wf-new"},
            },
            "correlation": {"case_id": "case_offer_1", "offer_id": "cieplo:wf-new"},
        },
    ]

    latest = project_latest_offer_for_case(events, case_id="case_offer_1")

    assert latest
    assert latest["offer_id"] == "cieplo:wf-new"
    assert latest["selected_model"] == "PANASONIC NEW"
    assert latest["final_price_pln"] == 41000
    assert latest["document"]["document_id"] == "pdf-new"


def test_offer_conflict_detection_flags_same_offer_price_divergence() -> None:
    events = [
        {
            "event_id": "osevt_a",
            "event_type": OFFER_GENERATED_EVENT,
            "source_repo": "cieplo-orchestrator",
            "engagement_id": "eng_offer_1",
            "case_id": "case_offer_1",
            "occurred_at": "2026-08-28T10:00:00+00:00",
            "payload": {
                "case_id": "case_offer_1",
                "offer_id": "cieplo:wf-1",
                "source": "cieplo",
                "selected_model": "KIT-WC09K3E8",
                "final_price_pln": 36856,
                "document": {"document_id": "pdf-1", "url": "https://topinstal.example/pdf-1.pdf"},
                "status": "generated",
                "provenance": {"workflow_id": "wf-1"},
            },
            "correlation": {"case_id": "case_offer_1", "offer_id": "cieplo:wf-1"},
        },
        {
            "event_id": "osevt_b",
            "event_type": OFFER_GENERATED_EVENT,
            "source_repo": "cieplo-orchestrator",
            "engagement_id": "eng_offer_1",
            "case_id": "case_offer_1",
            "occurred_at": "2026-08-28T10:01:00+00:00",
            "payload": {
                "case_id": "case_offer_1",
                "offer_id": "cieplo:wf-1",
                "source": "cieplo",
                "selected_model": "KIT-WC09K3E8",
                "final_price_pln": 37856,
                "document": {"document_id": "pdf-1", "url": "https://topinstal.example/pdf-1.pdf"},
                "status": "generated",
                "provenance": {"workflow_id": "wf-1"},
            },
            "correlation": {"case_id": "case_offer_1", "offer_id": "cieplo:wf-1"},
        },
    ]

    conflicts = detect_offer_conflicts_for_case(events, case_id="case_offer_1")

    assert len(conflicts) == 1
    assert conflicts[0]["offer_id"] == "cieplo:wf-1"
    assert conflicts[0]["field"] == "final_price_pln"
    assert conflicts[0]["resolution_status"] == "OPERATOR_REQUIRED"
    assert {item["value"] for item in conflicts[0]["values"]} == {36856, 37856}


def test_offer_conflict_detection_ignores_status_only_update() -> None:
    generated = {
        "event_id": "osevt_generated",
        "event_type": OFFER_GENERATED_EVENT,
        "source_repo": "cieplo-orchestrator",
        "engagement_id": "eng_offer_1",
        "case_id": "case_offer_1",
        "occurred_at": "2026-08-28T10:00:00+00:00",
        "payload": {
            "case_id": "case_offer_1",
            "offer_id": "cieplo:wf-1",
            "source": "cieplo",
            "selected_model": "KIT-WC09K3E8",
            "final_price_pln": 36856,
            "document": {"document_id": "pdf-1", "url": "https://topinstal.example/pdf-1.pdf"},
            "status": "generated",
        },
        "correlation": {"case_id": "case_offer_1", "offer_id": "cieplo:wf-1"},
    }
    status = {
        "event_id": "osevt_done",
        "event_type": OFFER_STATUS_UPDATED_EVENT,
        "source_repo": "cieplo-orchestrator",
        "engagement_id": "eng_offer_1",
        "case_id": "case_offer_1",
        "occurred_at": "2026-08-28T10:02:00+00:00",
        "payload": {
            "case_id": "case_offer_1",
            "offer_id": "cieplo:wf-1",
            "status": "done",
            "delivery_status": "WARN",
        },
        "correlation": {"case_id": "case_offer_1", "offer_id": "cieplo:wf-1"},
    }

    assert detect_offer_conflicts_for_case([generated, status], case_id="case_offer_1") == []


def test_offer_field_provenance_complete_real_offer_is_verified() -> None:
    offer = _projected_offer()

    field_provenance = build_offer_field_provenance(offer, conflicts=[])

    assert derive_offer_trust_status(offer, conflicts=[], field_provenance=field_provenance) == "VERIFIED"
    assert field_provenance["selected_model"]["value"] == "KIT-WC09K3E8"
    assert field_provenance["selected_model"]["source_repo"] == "cieplo-orchestrator"
    assert field_provenance["selected_model"]["source_workflow"] == "wf-1"
    assert field_provenance["selected_model"]["source_path"] == "engineering.selection.pumpModel"
    assert field_provenance["final_price_pln"]["origin_kind"] == "derived"
    assert field_provenance["final_price_pln"]["source_value"] == 35856
    assert field_provenance["final_price_pln"]["adjustment"] == 1000
    assert field_provenance["final_price_pln"]["provenance_quality"] == "PROVEN"
    assert field_provenance["document"]["origin_kind"] == "generated"
    assert field_provenance["delivery_status"]["origin_kind"] == "execution_fact"
    assert all(item["canonical_status"] == "VERIFIED" for item in field_provenance.values())
    assert "final_price_pln has producer-supplied source provenance" in build_offer_trust_reasons(field_provenance)


def test_offer_field_provenance_missing_critical_field_is_incomplete() -> None:
    offer = _projected_offer(document={})

    field_provenance = build_offer_field_provenance(offer, conflicts=[])

    assert field_provenance["document"]["canonical_status"] == "INCOMPLETE"
    assert field_provenance["document"]["provenance_quality"] == "MISSING"
    assert derive_offer_trust_status(offer, conflicts=[], field_provenance=field_provenance) == "INCOMPLETE"


def test_offer_field_provenance_inferred_price_makes_whole_offer_incomplete() -> None:
    offer = _projected_offer()
    offer["field_provenance"]["final_price_pln"]["provenance_quality"] = "INFERRED"
    offer["field_provenance"]["final_price_pln"]["incomplete_reason"] = "historical_price_source_reconstructed_from_current_adjustment"

    field_provenance = build_offer_field_provenance(offer, conflicts=[])

    assert field_provenance["final_price_pln"]["canonical_status"] == "VERIFIED"
    assert field_provenance["final_price_pln"]["provenance_quality"] == "INFERRED"
    assert derive_offer_trust_status(offer, conflicts=[], field_provenance=field_provenance) == "INCOMPLETE"


def test_offer_field_provenance_conflicting_price_marks_disputed() -> None:
    offer = _projected_offer()
    conflicts = [
        {
            "case_id": "case_offer_1",
            "offer_id": "cieplo:wf-1",
            "field": "final_price_pln",
            "kind": "contradictory_offer_observation",
            "values": [{"value": 36856}, {"value": 37856}],
        }
    ]

    field_provenance = build_offer_field_provenance(offer, conflicts=conflicts)

    assert field_provenance["final_price_pln"]["canonical_status"] == "DISPUTED"
    assert field_provenance["final_price_pln"]["provenance_quality"] == "CONFLICTED"
    assert derive_offer_trust_status(offer, conflicts=conflicts, field_provenance=field_provenance) == "CONFLICTED"


def test_offer_field_provenance_conflicting_model_marks_disputed() -> None:
    offer = _projected_offer()
    conflicts = [
        {
            "case_id": "case_offer_1",
            "offer_id": "cieplo:wf-1",
            "field": "selected_model",
            "kind": "contradictory_offer_observation",
            "values": [{"value": "KIT-WC09K3E8"}, {"value": "KIT-WC12K3E8"}],
        }
    ]

    field_provenance = build_offer_field_provenance(offer, conflicts=conflicts)

    assert field_provenance["selected_model"]["canonical_status"] == "DISPUTED"
    assert field_provenance["selected_model"]["provenance_quality"] == "CONFLICTED"
    assert derive_offer_trust_status(offer, conflicts=conflicts, field_provenance=field_provenance) == "CONFLICTED"


def test_offer_field_provenance_status_update_only_stays_verified() -> None:
    generated = {
        "event_id": "osevt_generated",
        "event_type": OFFER_GENERATED_EVENT,
        "source_repo": "cieplo-orchestrator",
        "engagement_id": "eng_offer_1",
        "case_id": "case_offer_1",
        "occurred_at": "2026-08-28T10:00:00+00:00",
            "payload": {
                "case_id": "case_offer_1",
                "offer_id": "cieplo:wf-1",
                "source": "cieplo",
                "selected_model": "KIT-WC09K3E8",
                "final_price_pln": 36856,
                "document": {"document_id": "pdf-1", "url": "https://topinstal.example/pdf-1.pdf"},
                "status": "generated",
                "provenance": {"source_repo": "cieplo-orchestrator", "workflow_id": "wf-1"},
                "field_provenance": _source_field_provenance(),
            },
            "correlation": {"case_id": "case_offer_1", "offer_id": "cieplo:wf-1"},
        }
    status = {
        "event_id": "osevt_done",
        "event_type": OFFER_STATUS_UPDATED_EVENT,
        "source_repo": "cieplo-orchestrator",
        "engagement_id": "eng_offer_1",
        "case_id": "case_offer_1",
        "occurred_at": "2026-08-28T10:02:00+00:00",
        "payload": {
            "case_id": "case_offer_1",
            "offer_id": "cieplo:wf-1",
            "status": "done",
            "delivery_status": "WARN",
            "provenance": {"source_repo": "cieplo-orchestrator", "workflow_id": "wf-1"},
        },
        "correlation": {"case_id": "case_offer_1", "offer_id": "cieplo:wf-1"},
    }
    conflicts = detect_offer_conflicts_for_case([generated, status], case_id="case_offer_1")
    latest = project_latest_offer_for_case([generated, status], case_id="case_offer_1")

    assert conflicts == []
    field_provenance = build_offer_field_provenance(latest, conflicts=conflicts)
    assert derive_offer_trust_status(latest, conflicts=conflicts, field_provenance=field_provenance) == "VERIFIED"


def test_offer_field_provenance_two_distinct_offers_do_not_false_conflict() -> None:
    latest = _projected_offer(offer_id="cieplo:wf-new")
    older_conflict = [
        {
            "case_id": "case_offer_1",
            "offer_id": "cieplo:wf-old",
            "field": "final_price_pln",
            "kind": "contradictory_offer_observation",
            "values": [{"value": 30000}, {"value": 31000}],
        }
    ]

    field_provenance = build_offer_field_provenance(latest, conflicts=older_conflict)

    assert all(item["canonical_status"] == "VERIFIED" for item in field_provenance.values())
    assert derive_offer_trust_status(latest, conflicts=older_conflict, field_provenance=field_provenance) == "VERIFIED"


def test_offer_field_provenance_missing_source_is_not_fabricated() -> None:
    offer = _projected_offer(field_provenance={})

    field_provenance = build_offer_field_provenance(offer, conflicts=[])

    assert "source_repo" not in field_provenance["selected_model"]
    assert "source_workflow" not in field_provenance["selected_model"]
    assert field_provenance["selected_model"]["canonical_status"] == "INCOMPLETE"
    assert field_provenance["selected_model"]["provenance_quality"] == "MISSING"
    assert derive_offer_trust_status(offer, conflicts=[], field_provenance=field_provenance) == "INCOMPLETE"


def test_case_latest_offer_route_is_read_only_and_owner_explicit() -> None:
    runtime = SimpleNamespace(store=SimpleNamespace(fetch_case=lambda case_id: {"case_id": case_id}))
    sample = {
        "case_id": "case_offer_1",
        "offer_id": "offer-2zahe",
        "source": "cieplo",
        "selected_model": "PANASONIC KIT-WC09K3E8",
        "final_price_pln": 33346,
        "document": {"document_id": "pdf-2zahe", "status": "ready"},
        "delivery_status": "WARN",
        "status": "generated",
        "provenance": {"workflow_id": "wf-2zahe"},
        "field_provenance": _source_field_provenance(
            workflow_id="wf-2zahe",
            model="PANASONIC KIT-WC09K3E8",
            price=33346,
            document={"document_id": "pdf-2zahe", "status": "ready"},
        ),
    }
    with patch("api_app.load_settings", return_value=SimpleNamespace(mailbox_memory_database_url="postgresql://test")):
        with patch("api_app.fetch_latest_offer_for_case", return_value=sample):
            with patch("api_app.fetch_offer_conflicts_for_case", return_value=[]):
                client = TestClient(create_app(runtime_provider=lambda: runtime, registry_provider=lambda: None))
                response = client.get("/cases/case_offer_1/offers/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["read_only"] is True
    assert body["offer"]["selected_model"] == "PANASONIC KIT-WC09K3E8"
    assert body["offer"]["final_price_pln"] == 33346
    assert body["field_provenance"]["selected_model"]["canonical_status"] == "VERIFIED"
    assert body["field_provenance"]["final_price_pln"]["transformation"] == "CIEPLO_PRICE_ADJUSTMENT_GROSS_PLN"
    assert body["trust_status"] == "VERIFIED"
    assert body["trust_reasons"]
    assert body["conflicts"] == []
    assert body["owner"]["offer_dto"] == "kalk-top"
    assert body["owner"]["projection"] == "gmail-agent/unified_os_events"


def test_case_latest_offer_route_fails_closed_for_unknown_case() -> None:
    runtime = SimpleNamespace(store=SimpleNamespace(fetch_case=lambda _case_id: None))
    with patch("api_app.load_settings", return_value=SimpleNamespace(mailbox_memory_database_url="postgresql://test")):
        client = TestClient(create_app(runtime_provider=lambda: runtime, registry_provider=lambda: None))
        response = client.get("/cases/case_missing/offers/latest")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "case_not_found"


def test_internal_os_events_offer_route_resolves_case_engagement_binding() -> None:
    class Registry:
        def lookup_by_case_id(self, case_id: str):
            return {"case_id": case_id, "engagement_id": "eng_from_registry"}

    captured = {}

    def _record(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "event_id": "osevt_offer_1", "idempotent": False}

    with patch("api_app.verify_registry_bearer", return_value=True):
        with patch("api_app.load_settings", return_value=SimpleNamespace(mailbox_memory_database_url="postgresql://test")):
            with patch("api_app.record_offer_generated_from_os_event", side_effect=_record):
                client = TestClient(create_app(runtime_provider=lambda: None, registry_provider=lambda: Registry()))
                response = client.post(
                    "/internal/os-events",
                    headers={"Authorization": "Bearer test"},
                    json=_raw_offer_event(engagement_id=""),
                )

    assert response.status_code == 200
    assert captured["engagement_id"] == "eng_from_registry"
    assert captured["source_repo"] == "cieplo-orchestrator"


def test_internal_os_events_offer_route_resolves_case_from_engagement_binding() -> None:
    class Store:
        def list_links_for_engagement(self, engagement_id: str):
            assert engagement_id == "eng_case_bound"
            return [
                {
                    "link_type": "mailbox_case",
                    "target_id": "case_offer_1",
                    "source_repo": "gmail-agent",
                }
            ]

    class Registry:
        store = Store()

    captured = {}

    def _record(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "event_id": "osevt_offer_1", "idempotent": False}

    event = _raw_offer_event(
        engagement_id="eng_case_bound",
        correlation={"workflow_id": "wf-2zahe", "offer_id": "offer-2zahe"},
    )

    with patch("api_app.verify_registry_bearer", return_value=True):
        with patch("api_app.load_settings", return_value=SimpleNamespace(mailbox_memory_database_url="postgresql://test")):
            with patch("api_app.record_offer_generated_from_os_event", side_effect=_record):
                client = TestClient(create_app(runtime_provider=lambda: None, registry_provider=lambda: Registry()))
                response = client.post(
                    "/internal/os-events",
                    headers={"Authorization": "Bearer test"},
                    json=event,
                )

    assert response.status_code == 200
    assert captured["engagement_id"] == "eng_case_bound"
    assert captured["raw_event"]["case_id"] == "case_offer_1"
    assert captured["raw_event"]["payload"]["case_id"] == "case_offer_1"
    assert captured["raw_event"]["correlation"]["case_id"] == "case_offer_1"


def _truth_event(
    *,
    event_id: str,
    value: int | str,
    quality: str,
    field: str = "final_price_pln",
    occurred_at: str = "2026-08-28T10:00:00+00:00",
    offer_id: str = "cieplo:wf-truth",
) -> dict:
    model = "KIT-WC09K3E8"
    price = 36856
    if field == "final_price_pln":
        price = int(value)
    elif field == "selected_model":
        model = str(value)
    fp = _source_field_provenance(workflow_id="wf-truth", model=model, price=price)
    fp[field]["provenance_quality"] = quality
    if quality != "PROVEN":
        fp[field]["incomplete_reason"] = "historical_reconstruction"
    return {
        "event_id": event_id,
        "event_type": OFFER_GENERATED_EVENT,
        "source_repo": "cieplo-orchestrator",
        "engagement_id": "eng_truth",
        "case_id": "case_truth",
        "occurred_at": occurred_at,
        "payload": {
            "case_id": "case_truth",
            "offer_id": offer_id,
            "source": "cieplo",
            "selected_model": model,
            "final_price_pln": price,
            "document": {"document_id": "pdf-truth", "url": "https://topinstal.example/pdf-truth.pdf"},
            "delivery_status": "WARN",
            "status": "generated",
            "field_provenance": fp,
        },
        "correlation": {"case_id": "case_truth", "offer_id": offer_id},
    }


def test_truth_resolver_auto_selects_proven_over_inferred_order_independently() -> None:
    inferred = _truth_event(event_id="osevt_inferred", value=35856, quality="INFERRED")
    proven = _truth_event(event_id="osevt_proven", value=36856, quality="PROVEN", occurred_at="2026-08-28T09:00:00+00:00")

    forward = detect_offer_conflicts_for_case([inferred, proven], case_id="case_truth")
    reverse = detect_offer_conflicts_for_case([proven, inferred], case_id="case_truth")

    assert forward == reverse
    assert len(forward) == 1
    conflict = forward[0]
    assert conflict["resolution_status"] == "AUTO_RESOLVED"
    assert conflict["resolution_basis"] == "STRONGER_PROVENANCE"
    assert conflict["canonical_value"] == 36856
    assert conflict["requires_operator"] is False
    assert "dowód zapisany przez producenta" in conflict["explanation"]["human_summary"]
    projected = project_latest_offer_for_case([inferred, proven], case_id="case_truth")
    assert projected["final_price_pln"] == 36856
    assert projected["truth_resolution"]["final_price_pln"]["current_value"] == 35856


@pytest.mark.parametrize("quality", ["PROVEN", "INFERRED"])
def test_truth_resolver_equal_strength_conflict_requires_operator(quality: str) -> None:
    first = _truth_event(event_id="osevt_a", value=36856, quality=quality)
    second = _truth_event(event_id="osevt_b", value=37856, quality=quality, occurred_at="2026-08-28T10:01:00+00:00")

    conflict = detect_offer_conflicts_for_case([first, second], case_id="case_truth")[0]

    assert conflict["resolution_status"] == "OPERATOR_REQUIRED"
    assert conflict["canonical_value"] is None
    assert conflict["requires_operator"] is True


def test_truth_resolver_same_value_status_only_and_distinct_offers_do_not_conflict() -> None:
    first = _truth_event(event_id="osevt_a", value=36856, quality="PROVEN")
    duplicate = _truth_event(event_id="osevt_b", value=36856, quality="PROVEN")
    other_offer = _truth_event(event_id="osevt_c", value=37856, quality="PROVEN", offer_id="cieplo:wf-other")
    status = {
        **first,
        "event_id": "osevt_status",
        "event_type": OFFER_STATUS_UPDATED_EVENT,
        "payload": {"case_id": "case_truth", "offer_id": "cieplo:wf-truth", "status": "done", "delivery_status": "sent"},
    }

    assert detect_offer_conflicts_for_case([first, duplicate, other_offer, status], case_id="case_truth") == []


def test_truth_resolution_reconcile_persists_once_and_retry_is_idempotent() -> None:
    events = [
        _truth_event(event_id="osevt_inferred", value=35856, quality="INFERRED"),
        _truth_event(event_id="osevt_proven", value=36856, quality="PROVEN"),
    ]
    published: list[dict] = []

    def _publisher(**kwargs):
        published.append(kwargs)
        return "osevt_resolution_1"

    with patch("offer_observability.fetch_offer_events_for_case", return_value=events):
        first = reconcile_offer_truth_resolutions(
            "postgresql://test",
            case_id="case_truth",
            offer_id="cieplo:wf-truth",
            engagement_id="eng_truth",
            publisher=_publisher,
        )
    resolution_event = {
        "event_id": "osevt_resolution_1",
        "event_type": OFFER_CONFLICT_RESOLVED_EVENT,
        "source_repo": "gmail-agent",
        "engagement_id": "eng_truth",
        "case_id": "case_truth",
        "occurred_at": "2026-08-28T10:02:00+00:00",
        "payload": published[0]["payload"],
        "correlation": published[0]["correlation"],
    }
    with patch("offer_observability.fetch_offer_events_for_case", return_value=[*events, resolution_event]):
        second = reconcile_offer_truth_resolutions(
            "postgresql://test",
            case_id="case_truth",
            offer_id="cieplo:wf-truth",
            engagement_id="eng_truth",
            publisher=_publisher,
        )

    assert first["published"] == ["osevt_resolution_1"]
    assert second["published"] == []
    assert len(published) == 1
    persisted = detect_offer_conflicts_for_case([*events, resolution_event], case_id="case_truth")[0]
    assert persisted["resolution_event_id"] == "osevt_resolution_1"
    assert persisted["resolved_at"] == "2026-08-28T10:02:00+00:00"


def test_operator_cannot_override_an_auto_resolved_conflict() -> None:
    events = [
        _truth_event(event_id="osevt_inferred", value=35856, quality="INFERRED"),
        _truth_event(event_id="osevt_proven", value=36856, quality="PROVEN"),
    ]
    conflict = detect_offer_conflicts_for_case(events, case_id="case_truth")[0]

    with pytest.raises(OfferObservationError, match="operator"):
        record_operator_offer_resolution(
            database_url="postgresql://test",
            case_id="case_truth",
            offer_id="cieplo:wf-truth",
            conflict_id=conflict["conflict_id"],
            expected_revision=conflict["resolution_version"],
            candidate_id=conflict["canonical_candidate_id"],
            principal_id="operator",
            events=events,
        )


def test_operator_resolution_is_durable_idempotent_and_reopens_on_new_proven_evidence() -> None:
    first = _truth_event(event_id="osevt_a", value=36856, quality="PROVEN")
    second = _truth_event(event_id="osevt_b", value=37856, quality="PROVEN")
    events = [first, second]
    conflict = detect_offer_conflicts_for_case(events, case_id="case_truth")[0]
    chosen = next(item for item in conflict["candidate_evidence"] if item["value"] == 36856)
    published: list[dict] = []

    def _publisher(**kwargs):
        published.append(kwargs)
        return "osevt_operator_resolution"

    first_result = record_operator_offer_resolution(
        database_url="postgresql://test",
        case_id="case_truth",
        offer_id="cieplo:wf-truth",
        conflict_id=conflict["conflict_id"],
        expected_revision=conflict["resolution_version"],
        candidate_id=chosen["candidate_id"],
        principal_id="operator",
        reason="Potwierdzone z dokumentem źródłowym",
        events=events,
        publisher=_publisher,
    )
    resolution_event = {
        "event_id": "osevt_operator_resolution",
        "event_type": OFFER_CONFLICT_RESOLVED_EVENT,
        "source_repo": "gmail-agent",
        "engagement_id": "eng_truth",
        "case_id": "case_truth",
        "occurred_at": "2026-08-28T10:02:00+00:00",
        "payload": published[0]["payload"],
        "correlation": published[0]["correlation"],
    }
    resolved_events = [*events, resolution_event]
    duplicate = record_operator_offer_resolution(
        database_url="postgresql://test",
        case_id="case_truth",
        offer_id="cieplo:wf-truth",
        conflict_id=conflict["conflict_id"],
        expected_revision=conflict["resolution_version"],
        candidate_id=chosen["candidate_id"],
        principal_id="operator",
        events=resolved_events,
        publisher=_publisher,
    )
    resolved = detect_offer_conflicts_for_case(resolved_events, case_id="case_truth")[0]
    # Event identity, not wall-clock ordering, proves this evidence was not part
    # of the prior operator decision (producer clocks may be skewed).
    new_proven = _truth_event(event_id="osevt_c", value=38856, quality="PROVEN", occurred_at="2026-08-28T09:00:00+00:00")
    reopened = detect_offer_conflicts_for_case([*resolved_events, new_proven], case_id="case_truth")[0]

    assert first_result["resolution_status"] == "OPERATOR_RESOLVED"
    assert duplicate["idempotent"] is True
    assert len(published) == 1
    assert resolved["canonical_value"] == 36856
    assert project_latest_offer_for_case(resolved_events, case_id="case_truth")["final_price_pln"] == 36856
    assert reopened["resolution_status"] == "OPERATOR_REQUIRED"
    assert reopened["resolution_basis"] == "NEW_STRONG_EVIDENCE_AFTER_OPERATOR_RESOLUTION"


def test_operator_resolution_fails_closed_for_stale_revision_or_foreign_candidate() -> None:
    events = [
        _truth_event(event_id="osevt_a", value=36856, quality="PROVEN"),
        _truth_event(event_id="osevt_b", value=37856, quality="PROVEN"),
    ]
    conflict = detect_offer_conflicts_for_case(events, case_id="case_truth")[0]

    with pytest.raises(OfferObservationError, match="stale"):
        record_operator_offer_resolution(
            database_url="postgresql://test",
            case_id="case_truth",
            offer_id="cieplo:wf-truth",
            conflict_id=conflict["conflict_id"],
            expected_revision="stale",
            candidate_id=conflict["candidate_evidence"][0]["candidate_id"],
            principal_id="operator",
            events=events,
        )
    with pytest.raises(OfferObservationError, match="candidate"):
        record_operator_offer_resolution(
            database_url="postgresql://test",
            case_id="case_truth",
            offer_id="cieplo:wf-truth",
            conflict_id=conflict["conflict_id"],
            expected_revision=conflict["resolution_version"],
            candidate_id="foreign",
            principal_id="operator",
            events=events,
        )


def test_operator_resolution_api_requires_principal_and_uses_verified_identity() -> None:
    runtime = SimpleNamespace(store=SimpleNamespace(fetch_case=lambda case_id: {"case_id": case_id}))
    conflict = {
        "conflict_id": "offer_conflict:case_truth:cieplo:wf-truth:final_price_pln",
        "resolution_version": "rev-1",
        "candidate_evidence": [{"candidate_id": "candidate-a", "value": 36856}],
    }
    with patch("api_app.load_settings", return_value=SimpleNamespace(mailbox_memory_database_url="postgresql://test")):
        client = TestClient(create_app(runtime_provider=lambda: runtime, registry_provider=lambda: None))
        denied = client.post(
            "/cases/case_truth/offers/cieplo:wf-truth/conflicts/resolve",
            json={"conflict_id": conflict["conflict_id"], "expected_revision": "rev-1", "candidate_id": "candidate-a"},
        )
    assert denied.status_code == 401

    with patch("api_app.load_settings", return_value=SimpleNamespace(mailbox_memory_database_url="postgresql://test")):
        with patch("api_app._require_mutation_principal", new=lambda: SimpleNamespace(operator_id="verified-operator", scope="operator")):
            with patch("api_app.record_operator_offer_resolution", return_value={"ok": True, "resolution_status": "OPERATOR_RESOLVED"}) as record:
                client = TestClient(create_app(runtime_provider=lambda: runtime, registry_provider=lambda: None))
                accepted = client.post(
                    "/cases/case_truth/offers/cieplo:wf-truth/conflicts/resolve",
                    headers={"Authorization": "Bearer test"},
                    json={
                        "conflict_id": conflict["conflict_id"],
                        "expected_revision": "rev-1",
                        "candidate_id": "candidate-a",
                        "operator_id": "spoofed",
                    },
                )
    assert accepted.status_code == 200
    assert record.call_args.kwargs["principal_id"] == "verified-operator"
