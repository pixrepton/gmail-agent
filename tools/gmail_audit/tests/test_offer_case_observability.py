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
    OFFER_GENERATED_EVENT,
    OFFER_STATUS_UPDATED_EVENT,
    OfferObservationError,
    build_offer_field_provenance,
    build_offer_trust_reasons,
    derive_offer_trust_status,
    detect_offer_conflicts_for_case,
    project_latest_offer_for_case,
    record_offer_generated_from_os_event,
    record_offer_status_update_from_os_event,
)


def _raw_offer_event(**overrides):
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
            "document": {
                "document_id": "pdf-2zahe",
                "url": "https://topinstal.example/offers/pdf-2zahe.pdf",
                "sha256": "abc123",
                "status": "ready",
            },
            "delivery_status": "held_for_review",
            "status": "generated",
            "producer_revision": "cieplo-orchestrator:ccf4aad",
            "provenance": {"workflow_id": "wf-2zahe", "result_id": "2zahe"},
        },
        "correlation": {"case_id": "case_offer_1", "workflow_id": "wf-2zahe"},
    }
    payload.update(overrides)
    return payload


def _projected_offer(**overrides):
    offer = {
        "case_id": "case_offer_1",
        "offer_id": "cieplo:wf-1",
        "source": "cieplo",
        "created_at": "2026-08-28T10:00:00+00:00",
        "updated_at": "2026-08-28T10:05:00+00:00",
        "selected_model": "KIT-WC09K3E8",
        "final_price_pln": 36856,
        "document": {"document_id": "pdf-1", "url": "https://topinstal.example/pdf-1.pdf", "status": "ready"},
        "delivery_status": "WARN",
        "status": "done",
        "provenance": {"source_repo": "cieplo-orchestrator", "workflow_id": "wf-1"},
        "producer_revision": "964e784",
        "latest_event_id": "osevt_latest",
    }
    offer.update(overrides)
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
    assert conflicts[0]["resolution_status"] == "unresolved"
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
    assert field_provenance["selected_model"]["source_path"] == "offer_json.engineering.selection.pumpModel"
    assert field_provenance["final_price_pln"]["origin_kind"] == "calculated"
    assert field_provenance["document"]["origin_kind"] == "generated"
    assert field_provenance["delivery_status"]["origin_kind"] == "execution_fact"
    assert all(item["canonical_status"] == "VERIFIED" for item in field_provenance.values())
    assert "final_price_pln comes from persisted OfferDTO pricing totals" in build_offer_trust_reasons(field_provenance)


def test_offer_field_provenance_missing_critical_field_is_incomplete() -> None:
    offer = _projected_offer(document={})

    field_provenance = build_offer_field_provenance(offer, conflicts=[])

    assert field_provenance["document"]["canonical_status"] == "INCOMPLETE"
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
    offer = _projected_offer(provenance={})

    field_provenance = build_offer_field_provenance(offer, conflicts=[])

    assert "source_repo" not in field_provenance["selected_model"]
    assert "source_workflow" not in field_provenance["selected_model"]
    assert field_provenance["selected_model"]["canonical_status"] == "INCOMPLETE"
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
        "status": "generated",
        "provenance": {"workflow_id": "wf-2zahe"},
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
    assert body["field_provenance"]["selected_model"]["canonical_status"] == "INCOMPLETE"
    assert body["trust_status"] == "INCOMPLETE"
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
