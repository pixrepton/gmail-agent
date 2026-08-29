"""Offer -> Case OS visibility projection.

Node B owns the canonical case/engagement observation. It does not become the
OfferDTO or PDF renderer source of truth; those stay in kalk-top and
top-instal-generator. This module stores and reads only a durable offer
observation/reference in the existing unified OS event stream.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from event_spine.emitter import publish_os_event
from event_spine.query import event_to_api_item

OFFER_GENERATED_EVENT = "offer.generated"
OFFER_STATUS_UPDATED_EVENT = "offer.status_updated"
OFFER_EVENT_TYPES = frozenset({OFFER_GENERATED_EVENT, OFFER_STATUS_UPDATED_EVENT})
OFFER_CONFLICT_FIELDS = ("selected_model", "final_price_pln", "document_id", "document_url")
OFFER_FIELD_PROVENANCE_FIELDS = ("selected_model", "final_price_pln", "document", "delivery_status")
PROVENANCE_QUALITIES = frozenset({"PROVEN", "INFERRED", "MISSING", "CONFLICTED"})


class OfferObservationError(ValueError):
    """Invalid or unsafe offer observation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OfferObservation:
    case_id: str
    engagement_id: str
    offer_id: str
    source: str
    occurred_at: str
    selected_model: str
    final_price_pln: float | int | None
    document: dict[str, Any]
    delivery_status: str
    status: str
    provenance: dict[str, Any]
    producer_revision: str


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("payload")
    return dict(raw) if isinstance(raw, dict) else {}


def _correlation(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("correlation")
    return dict(raw) if isinstance(raw, dict) else {}


def _case_id(payload: dict[str, Any]) -> str:
    body = _payload(payload)
    corr = _correlation(payload)
    return _clean(payload.get("case_id") or corr.get("case_id") or body.get("case_id"))


def _offer_id(payload: dict[str, Any]) -> str:
    body = _payload(payload)
    corr = _correlation(payload)
    doc = body.get("document") if isinstance(body.get("document"), dict) else {}
    return _clean(
        payload.get("offer_id")
        or corr.get("offer_id")
        or body.get("offer_id")
        or doc.get("offer_id")
    )


def _document_identity(body: dict[str, Any], corr: dict[str, Any]) -> dict[str, Any]:
    doc = dict(body.get("document")) if isinstance(body.get("document"), dict) else {}
    document_id = _clean(
        doc.get("document_id")
        or doc.get("pdf_id")
        or body.get("document_id")
        or corr.get("document_id")
        or corr.get("pdf_id")
    )
    url = _clean(doc.get("url") or doc.get("pdf_url") or body.get("document_url") or body.get("pdf_url"))
    sha256 = _clean(doc.get("sha256") or doc.get("pdf_sha256") or body.get("document_sha256"))
    status = _clean(doc.get("status") or body.get("document_status") or body.get("pdf_status"))
    out = {k: v for k, v in {
        "document_id": document_id,
        "url": url,
        "sha256": sha256,
        "status": status,
    }.items() if v}
    return out


def _selected_model(body: dict[str, Any]) -> str:
    return _clean(
        body.get("selected_model")
        or body.get("model")
        or body.get("recommended_model")
        or body.get("heat_pump_model")
    )


def _final_price(body: dict[str, Any]) -> float | int | None:
    for key in ("final_price_pln", "price_pln", "total_price_pln", "cena_finalna_pln", "cena_min"):
        raw = body.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        return int(value) if value.is_integer() else value
    return None


def normalize_offer_observation(
    event_payload: dict[str, Any],
    *,
    source_repo: str,
    engagement_id: str,
) -> OfferObservation:
    body = _payload(event_payload)
    corr = _correlation(event_payload)
    case_id = _case_id(event_payload)
    offer_id = _offer_id(event_payload)
    eid = _clean(engagement_id or event_payload.get("engagement_id"))
    source = _clean(body.get("source") or event_payload.get("source") or source_repo)
    occurred_at = _clean(event_payload.get("occurred_at") or body.get("generated_at"))
    if not occurred_at:
        occurred_at = datetime.now(timezone.utc).isoformat()
    document = _document_identity(body, corr)

    if not case_id:
        raise OfferObservationError("case_binding_required", "case_id is required for offer observation")
    if not eid:
        raise OfferObservationError("engagement_binding_required", "engagement_id is required for offer observation")
    if not offer_id:
        raise OfferObservationError("offer_id_required", "offer_id is required for offer observation")

    provenance = dict(body.get("provenance")) if isinstance(body.get("provenance"), dict) else {}
    if not provenance:
        provenance = {"source_repo": source_repo, "source": source}
    provenance.setdefault("source_repo", source_repo)
    provenance.setdefault("case_id", case_id)
    provenance.setdefault("offer_id", offer_id)

    return OfferObservation(
        case_id=case_id,
        engagement_id=eid,
        offer_id=offer_id,
        source=source,
        occurred_at=occurred_at,
        selected_model=_selected_model(body),
        final_price_pln=_final_price(body),
        document=document,
        delivery_status=_clean(body.get("delivery_status") or body.get("delivery") or body.get("mail_status")),
        status=_clean(body.get("status") or body.get("offer_status") or "generated"),
        provenance=provenance,
        producer_revision=_clean(body.get("producer_revision") or body.get("runtime_revision")),
    )


def observation_to_event_payload(observation: OfferObservation, raw_payload: dict[str, Any]) -> dict[str, Any]:
    body = _payload(raw_payload)
    out = dict(body)
    out.update(
        {
            "schema_version": "topinstal.offer_observation.v1",
            "summary_pl": f"Oferta {observation.offer_id} dla sprawy {observation.case_id}",
            "case_id": observation.case_id,
            "offer_id": observation.offer_id,
            "source": observation.source,
            "selected_model": observation.selected_model,
            "final_price_pln": observation.final_price_pln,
            "document": observation.document,
            "delivery_status": observation.delivery_status,
            "status": observation.status,
            "provenance": observation.provenance,
            "producer_revision": observation.producer_revision,
        }
    )
    return {k: v for k, v in out.items() if v not in ("", None, {}, [])}


def find_existing_offer_event(
    database_url: str,
    *,
    event_type: str,
    case_id: str,
    offer_id: str,
    source_repo: str,
    status: str = "",
    require_field_provenance: bool = False,
) -> dict[str, Any] | None:
    if not _clean(database_url) or not _clean(case_id) or not _clean(offer_id):
        return None
    try:
        import psycopg
    except ImportError:
        return None

    from event_spine.query import _SELECT_EVENT_COLUMNS

    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_SELECT_EVENT_COLUMNS}
                    FROM unified_os_events
                    WHERE event_type = %s
                      AND source_repo = %s
                      AND (
                        case_id = %s
                        OR correlation->>'case_id' = %s
                        OR payload->>'case_id' = %s
                      )
                      AND (
                        correlation->>'offer_id' = %s
                        OR payload->>'offer_id' = %s
                      )
                      AND (%s = '' OR payload->>'status' = %s)
                      AND (%s = FALSE OR payload ? 'field_provenance')
                    ORDER BY occurred_at ASC
                    LIMIT 1
                    """,
                    (
                        event_type,
                        source_repo,
                        case_id,
                        case_id,
                        case_id,
                        offer_id,
                        offer_id,
                        status,
                        status,
                        require_field_provenance,
                    ),
                )
                row = cur.fetchone()
    except Exception:
        return None
    return event_to_api_item(row) if row else None


def find_existing_generated_offer_event(database_url: str, *, case_id: str, offer_id: str, source_repo: str) -> dict[str, Any] | None:
    return find_existing_offer_event(
        database_url,
        event_type=OFFER_GENERATED_EVENT,
        case_id=case_id,
        offer_id=offer_id,
        source_repo=source_repo,
    )


def fetch_offer_events_for_case(database_url: str, case_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    cid = _clean(case_id)
    if not _clean(database_url) or not cid:
        return []
    try:
        import psycopg
    except ImportError:
        return []

    from event_spine.query import _SELECT_EVENT_COLUMNS

    capped = max(1, min(int(limit or 100), 200))
    try:
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_SELECT_EVENT_COLUMNS}
                    FROM unified_os_events
                    WHERE event_type = ANY(%s)
                      AND (
                        case_id = %s
                        OR correlation->>'case_id' = %s
                        OR payload->>'case_id' = %s
                      )
                    ORDER BY occurred_at ASC
                    LIMIT %s
                    """,
                    (list(OFFER_EVENT_TYPES), cid, cid, cid, capped),
                )
                rows = cur.fetchall()
    except Exception:
        return []
    return [event_to_api_item(row) for row in rows]


def _event_case_id(event: dict[str, Any]) -> str:
    payload = dict(event.get("payload") or {})
    corr = dict(event.get("correlation") or {})
    return _clean(event.get("case_id") or corr.get("case_id") or payload.get("case_id"))


def _event_offer_id(event: dict[str, Any]) -> str:
    payload = dict(event.get("payload") or {})
    corr = dict(event.get("correlation") or {})
    return _clean(payload.get("offer_id") or corr.get("offer_id"))


def _event_document_identity(event: dict[str, Any]) -> dict[str, str]:
    payload = dict(event.get("payload") or {})
    doc = dict(payload.get("document") or {}) if isinstance(payload.get("document"), dict) else {}
    return {
        "document_id": _clean(doc.get("document_id") or doc.get("pdf_id")),
        "document_url": _clean(doc.get("url") or doc.get("pdf_url")),
    }


def _event_offer_field(event: dict[str, Any], field: str) -> Any:
    payload = dict(event.get("payload") or {})
    if field == "document_id" or field == "document_url":
        return _event_document_identity(event).get(field)
    return payload.get(field)


def _conflict_value_key(value: Any) -> str:
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def detect_offer_conflicts_for_case(events: list[dict[str, Any]], *, case_id: str) -> list[dict[str, Any]]:
    """Detect contradictory immutable offer observation fields.

    Case OS owns the observation/projection, not the OfferDTO. A status change is
    not a conflict. A conflict is only reported when the same business offer
    identifier is observed with different model, price or document identity.
    """
    cid = _clean(case_id)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        event_case = _event_case_id(event)
        if cid and event_case and event_case != cid:
            continue
        offer_id = _event_offer_id(event)
        if not offer_id:
            continue
        grouped.setdefault(offer_id, []).append(event)

    conflicts: list[dict[str, Any]] = []
    for offer_id, offer_events in grouped.items():
        for field in OFFER_CONFLICT_FIELDS:
            values: dict[str, dict[str, Any]] = {}
            for event in offer_events:
                value = _event_offer_field(event, field)
                if value in ("", None, {}, []):
                    continue
                key = _conflict_value_key(value)
                values.setdefault(
                    key,
                    {
                        "value": value,
                        "events": [],
                    },
                )
                values[key]["events"].append(
                    {
                        "event_id": _clean(event.get("event_id")),
                        "event_type": _clean(event.get("event_type")),
                        "occurred_at": _clean(event.get("occurred_at")),
                        "source_repo": _clean(event.get("source_repo")),
                    }
                )
            if len(values) <= 1:
                continue
            conflicts.append(
                {
                    "conflict_id": f"offer_conflict:{cid}:{offer_id}:{field}",
                    "case_id": cid,
                    "offer_id": offer_id,
                    "field": field,
                    "kind": "contradictory_offer_observation",
                    "values": list(values.values()),
                    "resolution_status": "unresolved",
                }
            )
    return conflicts


def fetch_offer_conflicts_for_case(database_url: str, case_id: str) -> list[dict[str, Any]]:
    return detect_offer_conflicts_for_case(fetch_offer_events_for_case(database_url, case_id), case_id=case_id)


def _conflicted_offer_fields(conflicts: list[dict[str, Any]], offer_id: str) -> set[str]:
    oid = _clean(offer_id)
    out: set[str] = set()
    for conflict in conflicts:
        if _clean(conflict.get("offer_id")) != oid:
            continue
        field = _clean(conflict.get("field"))
        if field in {"document_id", "document_url"}:
            out.add("document")
        elif field:
            out.add(field)
    return out


def _field_value(offer: dict[str, Any], field: str) -> Any:
    if field == "document":
        return offer.get("document") if isinstance(offer.get("document"), dict) else {}
    return offer.get(field)


def _field_observed_at(offer: dict[str, Any], field: str) -> str:
    if field == "delivery_status":
        return _clean(offer.get("updated_at") or offer.get("created_at"))
    return _clean(offer.get("created_at") or offer.get("updated_at"))


def _producer_field_provenance(offer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = offer.get("field_provenance")
    if not isinstance(raw, dict):
        return {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}


def _field_provenance_is_complete(item: dict[str, Any]) -> bool:
    required = (
        "value",
        "producer",
        "source_workflow",
        "source_object",
        "source_path",
        "origin_kind",
        "evidence_reference",
        "observed_at",
        "revision",
        "provenance_quality",
    )
    return all(item.get(key) not in ("", None, {}, []) for key in required)


def _provenance_quality(item: dict[str, Any]) -> str:
    quality = _clean(item.get("provenance_quality")).upper()
    return quality if quality in PROVENANCE_QUALITIES else "MISSING"


def _field_trust_reason(field: str, status: str) -> str:
    if status == "DISPUTED":
        return f"{field} has contradictory observations for the same offer_id"
    if status == "INCOMPLETE":
        return f"{field} is missing value or source provenance"
    return f"{field} has producer-supplied source provenance"


def build_offer_field_provenance(
    offer: dict[str, Any],
    *,
    conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build machine-readable provenance for operator-visible offer fields."""
    active_conflicts = list(conflicts or [])
    offer_id = _clean(offer.get("offer_id"))
    producer_provenance = _producer_field_provenance(offer)
    conflicted = _conflicted_offer_fields(active_conflicts, offer_id)
    latest_event_id = _clean(offer.get("latest_event_id"))

    out: dict[str, dict[str, Any]] = {}
    for field in OFFER_FIELD_PROVENANCE_FIELDS:
        value = _field_value(offer, field)
        item = dict(producer_provenance.get(field) or {})
        if not item:
            item = {
                "value": value,
                "evidence_reference": latest_event_id or offer_id,
                "observed_at": _field_observed_at(offer, field),
                "canonical_status": "INCOMPLETE",
                "provenance_quality": "MISSING",
                "incomplete_reason": "producer_source_provenance_missing",
            }
            out[field] = {k: v for k, v in item.items() if v not in ("", None)}
            continue
        item.setdefault("value", value)
        item.setdefault("evidence_reference", latest_event_id or offer_id)
        if item.get("value") in ("", None, {}, []):
            item["value"] = value
        if item.get("observed_at") in ("", None):
            item["observed_at"] = _field_observed_at(offer, field)
        canonical_status = _clean(item.get("canonical_status") or "VERIFIED")
        if field in conflicted:
            canonical_status = "DISPUTED"
            item["provenance_quality"] = "CONFLICTED"
        elif not _field_provenance_is_complete(item):
            canonical_status = "INCOMPLETE"
            if item.get("value") in ("", None, {}, []):
                item["provenance_quality"] = "MISSING"
            else:
                item["provenance_quality"] = _provenance_quality(item)
            item.setdefault("incomplete_reason", "producer_source_provenance_incomplete")
        else:
            item["provenance_quality"] = _provenance_quality(item)
        item["canonical_status"] = canonical_status
        out[field] = {k: v for k, v in item.items() if v not in ("", None)}
    return out


def derive_offer_trust_status(
    offer: dict[str, Any],
    *,
    conflicts: list[dict[str, Any]] | None = None,
    field_provenance: dict[str, dict[str, Any]] | None = None,
) -> str:
    active_conflicts = list(conflicts or [])
    if _conflicted_offer_fields(active_conflicts, _clean(offer.get("offer_id"))):
        return "CONFLICTED"
    fp = field_provenance or build_offer_field_provenance(offer, conflicts=active_conflicts)
    for field in OFFER_FIELD_PROVENANCE_FIELDS:
        item = fp.get(field) if isinstance(fp.get(field), dict) else {}
        if field in _conflicted_offer_fields(active_conflicts, _clean(offer.get("offer_id"))):
            return "CONFLICTED"
        value = _field_value(offer, field)
        if value in ("", None, {}, []):
            return "INCOMPLETE"
        if item.get("canonical_status") != "VERIFIED":
            return "INCOMPLETE"
        if _provenance_quality(item) != "PROVEN":
            return "INCOMPLETE"
    return "VERIFIED"


def build_offer_trust_reasons(field_provenance: dict[str, dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for field in OFFER_FIELD_PROVENANCE_FIELDS:
        item = field_provenance.get(field) if isinstance(field_provenance.get(field), dict) else {}
        reasons.append(_field_trust_reason(field, _clean(item.get("canonical_status") or "INCOMPLETE")))
    return reasons


def project_latest_offer_for_case(events: list[dict[str, Any]], *, case_id: str) -> dict[str, Any] | None:
    cid = _clean(case_id)
    by_offer: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        payload = dict(event.get("payload") or {})
        corr = dict(event.get("correlation") or {})
        event_case = _clean(event.get("case_id") or corr.get("case_id") or payload.get("case_id"))
        if cid and event_case and event_case != cid:
            continue
        offer_id = _clean(payload.get("offer_id") or corr.get("offer_id"))
        if not offer_id:
            continue
        if offer_id not in by_offer:
            by_offer[offer_id] = {
                "case_id": cid or event_case,
                "offer_id": offer_id,
                "source": _clean(payload.get("source") or event.get("source_repo")),
                "created_at": _clean(event.get("occurred_at")),
                "updated_at": _clean(event.get("occurred_at")),
                "selected_model": "",
                "final_price_pln": None,
                "document": {},
                "delivery_status": "",
                "status": "generated",
                "provenance": {},
                "producer_revision": "",
                "latest_event_id": "",
            }
            order.append(offer_id)
        current = by_offer[offer_id]
        current["updated_at"] = _clean(event.get("occurred_at")) or current["updated_at"]
        current["latest_event_id"] = _clean(event.get("event_id")) or current["latest_event_id"]
        for key in ("source", "selected_model", "delivery_status", "status", "producer_revision"):
            value = _clean(payload.get(key))
            if value:
                current[key] = value
        if payload.get("final_price_pln") is not None:
            current["final_price_pln"] = payload.get("final_price_pln")
        if isinstance(payload.get("document"), dict) and payload["document"]:
            current["document"] = dict(payload["document"])
        if isinstance(payload.get("provenance"), dict) and payload["provenance"]:
            current["provenance"] = dict(payload["provenance"])
        if isinstance(payload.get("field_provenance"), dict) and payload["field_provenance"]:
            current["field_provenance"] = dict(payload["field_provenance"])
    if not order:
        return None
    latest_id = max(order, key=lambda oid: str(by_offer[oid].get("updated_at") or ""))
    return by_offer[latest_id]


def fetch_latest_offer_for_case(database_url: str, case_id: str) -> dict[str, Any] | None:
    return project_latest_offer_for_case(fetch_offer_events_for_case(database_url, case_id), case_id=case_id)


def record_offer_generated_from_os_event(
    *,
    database_url: str,
    raw_event: dict[str, Any],
    source_repo: str,
    engagement_id: str,
    existing_lookup: Callable[..., dict[str, Any] | None] = find_existing_generated_offer_event,
    publisher: Callable[..., str | None] = publish_os_event,
) -> dict[str, Any]:
    observation = normalize_offer_observation(raw_event, source_repo=source_repo, engagement_id=engagement_id)
    existing = existing_lookup(
        database_url,
        case_id=observation.case_id,
        offer_id=observation.offer_id,
        source_repo=source_repo,
    )
    if existing:
        return {"ok": True, "event_id": existing.get("event_id"), "idempotent": True, "offer": project_latest_offer_for_case([existing], case_id=observation.case_id)}

    correlation = _correlation(raw_event)
    correlation.update({"case_id": observation.case_id, "offer_id": observation.offer_id})
    if observation.document.get("document_id"):
        correlation.setdefault("document_id", observation.document["document_id"])
    event_id = publisher(
        database_url=database_url,
        event_type=OFFER_GENERATED_EVENT,
        engagement_id=observation.engagement_id,
        source_repo=source_repo,
        payload=observation_to_event_payload(observation, raw_event),
        correlation=correlation,
        occurred_at=observation.occurred_at,
        case_id=observation.case_id,
        success=True,
    )
    return {"ok": bool(event_id), "event_id": event_id, "idempotent": False, "offer": asdict(observation)}


def record_offer_status_update_from_os_event(
    *,
    database_url: str,
    raw_event: dict[str, Any],
    source_repo: str,
    engagement_id: str,
    existing_generated_lookup: Callable[..., dict[str, Any] | None] = find_existing_generated_offer_event,
    existing_status_lookup: Callable[..., dict[str, Any] | None] = find_existing_offer_event,
    publisher: Callable[..., str | None] = publish_os_event,
) -> dict[str, Any]:
    observation = normalize_offer_observation(raw_event, source_repo=source_repo, engagement_id=engagement_id)
    existing_generated = existing_generated_lookup(
        database_url,
        case_id=observation.case_id,
        offer_id=observation.offer_id,
        source_repo=source_repo,
    )
    if not existing_generated:
        raise OfferObservationError("offer_observation_required", "offer status update requires an existing generated offer observation")
    incoming_has_source_provenance = isinstance(_payload(raw_event).get("field_provenance"), dict)
    existing_status = existing_status_lookup(
        database_url,
        event_type=OFFER_STATUS_UPDATED_EVENT,
        case_id=observation.case_id,
        offer_id=observation.offer_id,
        source_repo=source_repo,
        status=observation.status,
        require_field_provenance=incoming_has_source_provenance,
    )
    if existing_status:
        latest = project_latest_offer_for_case([existing_generated, existing_status], case_id=observation.case_id)
        return {"ok": True, "event_id": existing_status.get("event_id"), "idempotent": True, "offer": latest}

    correlation = _correlation(raw_event)
    correlation.update({"case_id": observation.case_id, "offer_id": observation.offer_id})
    event_id = publisher(
        database_url=database_url,
        event_type=OFFER_STATUS_UPDATED_EVENT,
        engagement_id=observation.engagement_id,
        source_repo=source_repo,
        payload=observation_to_event_payload(observation, raw_event),
        correlation=correlation,
        occurred_at=observation.occurred_at,
        case_id=observation.case_id,
        success=True,
    )
    return {"ok": bool(event_id), "event_id": event_id, "idempotent": False, "offer": asdict(observation)}
