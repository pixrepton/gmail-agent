"""Offer -> Case OS visibility projection.

Node B owns the canonical case/engagement observation. It does not become the
OfferDTO or PDF renderer source of truth; those stay in kalk-top and
top-instal-generator. This module stores and reads only a durable offer
observation/reference in the existing unified OS event stream.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from event_spine.emitter import publish_os_event
from event_spine.query import event_to_api_item

OFFER_GENERATED_EVENT = "offer.generated"
OFFER_STATUS_UPDATED_EVENT = "offer.status_updated"
OFFER_CONFLICT_DETECTED_EVENT = "offer.conflict_detected"
OFFER_CONFLICT_RESOLVED_EVENT = "offer.conflict_resolved"
OFFER_OBSERVATION_EVENT_TYPES = frozenset({OFFER_GENERATED_EVENT, OFFER_STATUS_UPDATED_EVENT})
OFFER_RESOLUTION_EVENT_TYPES = frozenset({OFFER_CONFLICT_DETECTED_EVENT, OFFER_CONFLICT_RESOLVED_EVENT})
OFFER_EVENT_TYPES = OFFER_OBSERVATION_EVENT_TYPES | OFFER_RESOLUTION_EVENT_TYPES
OFFER_CONFLICT_FIELDS = ("selected_model", "final_price_pln", "document_id", "document_url")
OFFER_FIELD_PROVENANCE_FIELDS = ("selected_model", "final_price_pln", "document", "delivery_status")
PROVENANCE_QUALITIES = frozenset({"PROVEN", "INFERRED", "MISSING", "CONFLICTED"})
PROVENANCE_QUALITY_RANK = {"MISSING": 0, "INFERRED": 1, "PROVEN": 2}
OFFER_FIELD_OWNERS = {
    "selected_model": "kalk-top",
    "final_price_pln": "kalk-top",
    "document_id": "top-instal-generator",
    "document_url": "top-instal-generator",
}


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
                      AND (
                        %s = FALSE
                        OR (
                          payload ? 'field_provenance'
                          AND payload->'field_provenance'->'final_price_pln'->>'provenance_quality' IS NOT NULL
                        )
                      )
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
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _resolution_field(field: str) -> str:
    return "document" if field in {"document_id", "document_url"} else field


def _event_field_provenance(event: dict[str, Any], field: str) -> dict[str, Any]:
    payload = dict(event.get("payload") or {})
    raw = payload.get("field_provenance")
    if not isinstance(raw, dict):
        return {}
    item = raw.get(_resolution_field(field))
    return dict(item) if isinstance(item, dict) else {}


def _candidate_id(field: str, value: Any) -> str:
    raw = f"{field}:{_conflict_value_key(value)}".encode("utf-8")
    return f"offer_candidate:{hashlib.sha256(raw).hexdigest()[:16]}"


def _conflict_revision(conflict_id: str, candidates: list[dict[str, Any]]) -> str:
    material = {
        "conflict_id": conflict_id,
        "candidates": [
            {
                "candidate_id": item.get("candidate_id"),
                "value": item.get("value"),
                "event_ids": sorted(str(event_id) for event_id in item.get("event_ids") or []),
                "provenance_quality": item.get("provenance_quality"),
            }
            for item in sorted(candidates, key=lambda row: str(row.get("candidate_id") or ""))
        ],
    }
    digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"offer_resolution:{digest[:20]}"


def _quality(item: dict[str, Any]) -> str:
    quality = _clean(item.get("provenance_quality")).upper()
    return quality if quality in PROVENANCE_QUALITY_RANK else "MISSING"


def _candidate_owner_supported(field: str, provenance: dict[str, Any]) -> bool:
    expected = OFFER_FIELD_OWNERS.get(field, "")
    declared = _clean(provenance.get("canonical_owner") or provenance.get("source_owner"))
    return bool(expected and declared and declared == expected)


def _build_conflict_candidates(offer_events: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for event in sorted(offer_events, key=lambda row: (_clean(row.get("occurred_at")), _clean(row.get("event_id")))):
        value = _event_offer_field(event, field)
        if value in ("", None, {}, []):
            continue
        key = _conflict_value_key(value)
        provenance = _event_field_provenance(event, field)
        evidence = {
            "event_id": _clean(event.get("event_id")),
            "event_type": _clean(event.get("event_type")),
            "occurred_at": _clean(event.get("occurred_at")),
            "source_repo": _clean(event.get("source_repo")),
            "producer": _clean(provenance.get("producer")),
            "source_workflow": _clean(provenance.get("source_workflow")),
            "source_path": _clean(provenance.get("source_path")),
            "source_object": _clean(provenance.get("source_object")),
            "origin_kind": _clean(provenance.get("origin_kind")),
            "evidence_reference": provenance.get("evidence_reference"),
            "transformation": provenance.get("transformation"),
            "revision": _clean(provenance.get("revision")),
            "provenance_quality": _quality(provenance),
            "canonical_owner": _clean(provenance.get("canonical_owner") or provenance.get("source_owner")),
        }
        evidence = {k: v for k, v in evidence.items() if v not in ("", None, {}, [])}
        candidate = values.setdefault(
            key,
            {
                "candidate_id": _candidate_id(field, value),
                "value": value,
                "events": [],
                "event_ids": [],
                "evidence": [],
                "provenance_quality": "MISSING",
                "owner_supported": False,
            },
        )
        candidate["events"].append(
            {
                "event_id": _clean(event.get("event_id")),
                "event_type": _clean(event.get("event_type")),
                "occurred_at": _clean(event.get("occurred_at")),
                "source_repo": _clean(event.get("source_repo")),
            }
        )
        if evidence.get("event_id"):
            candidate["event_ids"].append(evidence["event_id"])
        candidate["evidence"].append(evidence)
        if PROVENANCE_QUALITY_RANK[_quality(provenance)] > PROVENANCE_QUALITY_RANK[_quality(candidate)]:
            candidate["provenance_quality"] = _quality(provenance)
        candidate["owner_supported"] = bool(candidate["owner_supported"] or _candidate_owner_supported(field, provenance))

    candidates = list(values.values())
    for candidate in candidates:
        candidate["events"] = sorted(candidate["events"], key=lambda row: (row.get("occurred_at", ""), row.get("event_id", "")))
        candidate["event_ids"] = sorted(set(candidate["event_ids"]))
        candidate["evidence"] = sorted(candidate["evidence"], key=lambda row: (row.get("occurred_at", ""), row.get("event_id", "")))
    return sorted(candidates, key=lambda row: str(row.get("candidate_id") or ""))


def _latest_raw_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked: list[tuple[str, str, dict[str, Any]]] = []
    for candidate in candidates:
        for event in candidate.get("events") or []:
            ranked.append((_clean(event.get("occurred_at")), _clean(event.get("event_id")), candidate))
    return max(ranked, key=lambda row: (row[0], row[1]))[2] if ranked else None


def _resolution_events_for_conflict(events: list[dict[str, Any]], conflict_id: str) -> list[dict[str, Any]]:
    out = []
    for event in events:
        if _clean(event.get("event_type")) not in OFFER_RESOLUTION_EVENT_TYPES:
            continue
        payload = dict(event.get("payload") or {})
        if _clean(payload.get("conflict_id")) == conflict_id:
            out.append(event)
    return sorted(out, key=lambda row: (_clean(row.get("occurred_at")), _clean(row.get("event_id"))))


def _deterministic_resolution(
    *,
    field: str,
    candidates: list[dict[str, Any]],
    resolution_events: list[dict[str, Any]],
    resolution_version: str,
) -> dict[str, Any]:
    operator_events = [
        event
        for event in resolution_events
        if _clean((event.get("payload") or {}).get("resolution_status")) == "OPERATOR_RESOLVED"
    ]
    if operator_events:
        latest_operator = operator_events[-1]
        payload = dict(latest_operator.get("payload") or {})
        selected_id = _clean(payload.get("canonical_candidate_id"))
        selected = next((item for item in candidates if _clean(item.get("candidate_id")) == selected_id), None)
        if selected is not None:
            if _clean(payload.get("resolution_version")) == resolution_version:
                return {
                    "resolution_status": "OPERATOR_RESOLVED",
                    "resolution_basis": "OPERATOR_SELECTION",
                    "winner": selected,
                    "resolved_by": _clean(payload.get("resolved_by")),
                    "resolved_at": _clean(latest_operator.get("occurred_at") or payload.get("resolved_at")),
                    "resolution_event_id": _clean(latest_operator.get("event_id")),
                }
            resolved_at = _clean(latest_operator.get("occurred_at") or payload.get("resolved_at"))
            previously_considered_event_ids = {
                _clean(event_id)
                for prior_candidate in payload.get("candidate_evidence") or []
                if isinstance(prior_candidate, dict)
                for event_id in prior_candidate.get("event_ids") or []
                if _clean(event_id)
            }
            new_strong = any(
                item.get("candidate_id") != selected_id
                and _quality(item) == "PROVEN"
                and (
                    any(
                        _clean(event_id) not in previously_considered_event_ids
                        for event_id in item.get("event_ids") or []
                    )
                    if previously_considered_event_ids
                    else any(_clean(ev.get("occurred_at")) > resolved_at for ev in item.get("events") or [])
                )
                for item in candidates
            )
            if not new_strong:
                return {
                    "resolution_status": "OPERATOR_RESOLVED",
                    "resolution_basis": "OPERATOR_SELECTION",
                    "winner": selected,
                    "resolved_by": _clean(payload.get("resolved_by")),
                    "resolved_at": resolved_at,
                    "resolution_event_id": _clean(latest_operator.get("event_id")),
                }
            return {
                "resolution_status": "OPERATOR_REQUIRED",
                "resolution_basis": "NEW_STRONG_EVIDENCE_AFTER_OPERATOR_RESOLUTION",
                "winner": None,
                "previous_resolution": {
                    "canonical_value": selected.get("value"),
                    "resolved_by": _clean(payload.get("resolved_by")),
                    "resolved_at": resolved_at,
                    "resolution_event_id": _clean(latest_operator.get("event_id")),
                },
            }

    ranked = sorted(
        candidates,
        key=lambda item: (
            PROVENANCE_QUALITY_RANK[_quality(item)],
            1 if item.get("owner_supported") else 0,
            str(item.get("candidate_id") or ""),
        ),
        reverse=True,
    )
    if ranked:
        winner = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        winner_rank = PROVENANCE_QUALITY_RANK[_quality(winner)]
        second_rank = PROVENANCE_QUALITY_RANK[_quality(second or {})]
        if _quality(winner) == "PROVEN" and winner_rank > second_rank:
            return {"resolution_status": "AUTO_RESOLVED", "resolution_basis": "STRONGER_PROVENANCE", "winner": winner}
        if (
            second is not None
            and _quality(winner) == "PROVEN"
            and _quality(second) == "PROVEN"
            and winner.get("owner_supported")
            and not second.get("owner_supported")
        ):
            return {"resolution_status": "AUTO_RESOLVED", "resolution_basis": "CANONICAL_OWNER_EVIDENCE", "winner": winner}
    return {"resolution_status": "OPERATOR_REQUIRED", "resolution_basis": "AMBIGUOUS_EVIDENCE", "winner": None}


def _human_resolution_summary(field: str, result: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    winner = result.get("winner") if isinstance(result.get("winner"), dict) else None
    status = _clean(result.get("resolution_status"))
    basis = _clean(result.get("resolution_basis"))
    if status == "AUTO_RESOLVED" and winner is not None:
        losers = [item for item in candidates if item.get("candidate_id") != winner.get("candidate_id")]
        if basis == "STRONGER_PROVENANCE":
            losing_quality = ", ".join(sorted({_quality(item) for item in losers})) or "słabszy"
            detail = "dowód zapisany przez producenta" if _quality(winner) == "PROVEN" else _quality(winner)
            return f"Wybrano {winner.get('value')}, ponieważ ma {detail}; konkurencyjny dowód ma jakość {losing_quality}."
        return f"Wybrano {winner.get('value')} na podstawie jawnego dowodu właściciela kanonicznego."
    if status == "OPERATOR_RESOLVED" and winner is not None:
        return f"Wartość {winner.get('value')} jest kanoniczna, ponieważ wybrał ją uwierzytelniony operator."
    if basis == "NEW_STRONG_EVIDENCE_AFTER_OPERATOR_RESOLUTION":
        return "Po decyzji operatora pojawiła się nowa sprzeczna obserwacja PROVEN; wymagane jest ponowne rozstrzygnięcie."
    qualities = sorted({_quality(item) for item in candidates})
    return f"Wymagana jest decyzja operatora: pole {field} ma równie silne lub niejednoznaczne dowody ({', '.join(qualities)})."


def detect_offer_conflicts_for_case(events: list[dict[str, Any]], *, case_id: str) -> list[dict[str, Any]]:
    """Detect contradictory immutable offer observation fields.

    Case OS owns the observation/projection, not the OfferDTO. A status change is
    not a conflict. A conflict is only reported when the same business offer
    identifier is observed with different model, price or document identity.
    """
    cid = _clean(case_id)
    grouped: dict[str, list[dict[str, Any]]] = {}
    observation_events = [event for event in events if _clean(event.get("event_type")) in OFFER_OBSERVATION_EVENT_TYPES]
    for event in observation_events:
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
            candidates = _build_conflict_candidates(offer_events, field)
            if len(candidates) <= 1:
                continue
            conflict_id = f"offer_conflict:{cid}:{offer_id}:{field}"
            resolution_version = _conflict_revision(conflict_id, candidates)
            resolution_events = _resolution_events_for_conflict(events, conflict_id)
            result = _deterministic_resolution(
                field=field,
                candidates=candidates,
                resolution_events=resolution_events,
                resolution_version=resolution_version,
            )
            if result["resolution_status"] == "AUTO_RESOLVED":
                persisted_auto = next(
                    (
                        event
                        for event in reversed(resolution_events)
                        if _clean((event.get("payload") or {}).get("resolution_status")) == "AUTO_RESOLVED"
                        and _clean((event.get("payload") or {}).get("resolution_version")) == resolution_version
                        and _clean((event.get("payload") or {}).get("canonical_candidate_id"))
                        == _clean((result.get("winner") or {}).get("candidate_id"))
                    ),
                    None,
                )
                if persisted_auto is not None:
                    result["resolution_event_id"] = _clean(persisted_auto.get("event_id"))
                    result["resolved_at"] = _clean(
                        persisted_auto.get("occurred_at") or (persisted_auto.get("payload") or {}).get("resolved_at")
                    )
            winner = result.get("winner") if isinstance(result.get("winner"), dict) else None
            raw_candidate = _latest_raw_candidate(candidates)
            explanation = {
                "basis": result["resolution_basis"],
                "winning_evidence": list(winner.get("evidence") or []) if winner else [],
                "losing_evidence": [
                    evidence
                    for item in candidates
                    if winner is None or item.get("candidate_id") != winner.get("candidate_id")
                    for evidence in item.get("evidence") or []
                ],
                "policy_rule": result["resolution_basis"],
                "human_summary": _human_resolution_summary(field, result, candidates),
            }
            conflicts.append(
                {
                    "conflict_id": conflict_id,
                    "case_id": cid,
                    "offer_id": offer_id,
                    "field": field,
                    "kind": "contradictory_offer_observation",
                    "values": candidates,
                    "candidate_values": [item.get("value") for item in candidates],
                    "candidate_evidence": candidates,
                    "current_value": raw_candidate.get("value") if raw_candidate else None,
                    "canonical_value": winner.get("value") if winner else None,
                    "canonical_candidate_id": winner.get("candidate_id") if winner else None,
                    "canonical_event_id": (winner.get("event_ids") or [None])[0] if winner else None,
                    "resolution_status": result["resolution_status"],
                    "resolution_basis": result["resolution_basis"],
                    "resolved_by": result.get("resolved_by") or ("gmail-agent/offer_truth_resolver" if result["resolution_status"] == "AUTO_RESOLVED" else None),
                    "resolved_at": result.get("resolved_at"),
                    "requires_operator": result["resolution_status"] == "OPERATOR_REQUIRED",
                    "resolution_version": resolution_version,
                    "resolution_event_id": result.get("resolution_event_id"),
                    "previous_resolution": result.get("previous_resolution"),
                    "explanation": explanation,
                    "history": [
                        {
                            "event_id": _clean(event.get("event_id")),
                            "event_type": _clean(event.get("event_type")),
                            "occurred_at": _clean(event.get("occurred_at")),
                            "resolution_status": _clean((event.get("payload") or {}).get("resolution_status")),
                            "resolved_by": _clean((event.get("payload") or {}).get("resolved_by")),
                        }
                        for event in resolution_events
                    ],
                }
            )
    return sorted(conflicts, key=lambda item: (item["offer_id"], item["field"]))


def fetch_offer_conflicts_for_case(database_url: str, case_id: str) -> list[dict[str, Any]]:
    return detect_offer_conflicts_for_case(fetch_offer_events_for_case(database_url, case_id), case_id=case_id)


def _conflicted_offer_fields(conflicts: list[dict[str, Any]], offer_id: str) -> set[str]:
    oid = _clean(offer_id)
    out: set[str] = set()
    for conflict in conflicts:
        if _clean(conflict.get("offer_id")) != oid:
            continue
        if _clean(conflict.get("resolution_status")) in {"AUTO_RESOLVED", "OPERATOR_RESOLVED"}:
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
        matching_resolution = next(
            (
                conflict
                for conflict in active_conflicts
                if _clean(conflict.get("offer_id")) == offer_id
                and _resolution_field(_clean(conflict.get("field"))) == field
                and _clean(conflict.get("resolution_status")) in {"AUTO_RESOLVED", "OPERATOR_RESOLVED"}
            ),
            None,
        )
        if matching_resolution:
            candidate_id = _clean(matching_resolution.get("canonical_candidate_id"))
            candidate = next(
                (
                    row
                    for row in matching_resolution.get("candidate_evidence") or []
                    if _clean(row.get("candidate_id")) == candidate_id
                ),
                None,
            )
            evidence = list((candidate or {}).get("evidence") or [])
            strongest = max(
                evidence,
                key=lambda row: (PROVENANCE_QUALITY_RANK[_quality(row)], _clean(row.get("event_id"))),
                default={},
            )
            if strongest:
                item = dict(strongest)
                item["value"] = matching_resolution.get("canonical_value")
            item.update(
                {
                    "canonical_status": "VERIFIED",
                    "resolution_status": matching_resolution.get("resolution_status"),
                    "resolution_basis": matching_resolution.get("resolution_basis"),
                    "resolution_version": matching_resolution.get("resolution_version"),
                    "resolution_event_id": matching_resolution.get("resolution_event_id"),
                    "canonicality_origin_kind": (
                        "operator_decision"
                        if matching_resolution.get("resolution_status") == "OPERATOR_RESOLVED"
                        else "deterministic_policy"
                    ),
                }
            )
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
        if item.get("resolution_status"):
            reasons.append(
                f"{field} canonical via {item.get('resolution_status')}: {item.get('resolution_basis') or 'recorded resolution'}"
            )
        else:
            reasons.append(_field_trust_reason(field, _clean(item.get("canonical_status") or "INCOMPLETE")))
    return reasons


def project_latest_offer_for_case(events: list[dict[str, Any]], *, case_id: str) -> dict[str, Any] | None:
    cid = _clean(case_id)
    by_offer: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    observation_events = sorted(
        (event for event in events if _clean(event.get("event_type")) in OFFER_OBSERVATION_EVENT_TYPES),
        key=lambda row: (_clean(row.get("occurred_at")), _clean(row.get("event_id"))),
    )
    for event in observation_events:
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
    latest_id = max(order, key=lambda oid: (str(by_offer[oid].get("created_at") or ""), oid))
    latest = by_offer[latest_id]
    conflicts = [item for item in detect_offer_conflicts_for_case(events, case_id=cid) if _clean(item.get("offer_id")) == latest_id]
    truth_resolution: dict[str, dict[str, Any]] = {}
    for conflict in conflicts:
        projection_field = _resolution_field(_clean(conflict.get("field")))
        truth_resolution[projection_field] = {
            key: conflict.get(key)
            for key in (
                "conflict_id",
                "current_value",
                "canonical_value",
                "resolution_status",
                "resolution_basis",
                "requires_operator",
                "resolution_version",
                "resolved_by",
                "resolved_at",
                "explanation",
            )
        }
        if _clean(conflict.get("resolution_status")) not in {"AUTO_RESOLVED", "OPERATOR_RESOLVED"}:
            continue
        canonical_value = conflict.get("canonical_value")
        if conflict.get("field") == "document_id":
            latest.setdefault("document", {})["document_id"] = canonical_value
        elif conflict.get("field") == "document_url":
            latest.setdefault("document", {})["url"] = canonical_value
        else:
            latest[conflict["field"]] = canonical_value
    if conflicts:
        latest["truth_resolution"] = truth_resolution
        latest["resolution_history"] = conflicts
    return latest


def fetch_latest_offer_for_case(database_url: str, case_id: str) -> dict[str, Any] | None:
    return project_latest_offer_for_case(fetch_offer_events_for_case(database_url, case_id), case_id=case_id)


def _resolution_event_exists(
    events: list[dict[str, Any]],
    *,
    event_type: str,
    conflict_id: str,
    resolution_version: str,
    resolution_status: str,
    canonical_candidate_id: str = "",
) -> dict[str, Any] | None:
    for event in events:
        if _clean(event.get("event_type")) != event_type:
            continue
        payload = dict(event.get("payload") or {})
        if (
            _clean(payload.get("conflict_id")) == conflict_id
            and _clean(payload.get("resolution_version")) == resolution_version
            and _clean(payload.get("resolution_status")) == resolution_status
            and (
                not canonical_candidate_id
                or _clean(payload.get("canonical_candidate_id")) == canonical_candidate_id
            )
        ):
            return event
    return None


def _resolution_payload(conflict: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": "topinstal.offer_truth_resolution.v1",
        "conflict_id": conflict.get("conflict_id"),
        "case_id": conflict.get("case_id"),
        "offer_id": conflict.get("offer_id"),
        "field": conflict.get("field"),
        "candidate_values": conflict.get("candidate_values"),
        "candidate_evidence": conflict.get("candidate_evidence"),
        "resolution_status": conflict.get("resolution_status"),
        "resolution_basis": conflict.get("resolution_basis"),
        "canonical_value": conflict.get("canonical_value"),
        "canonical_candidate_id": conflict.get("canonical_candidate_id"),
        "canonical_event_id": conflict.get("canonical_event_id"),
        "resolved_by": conflict.get("resolved_by"),
        "resolved_at": conflict.get("resolved_at"),
        "requires_operator": conflict.get("requires_operator"),
        "resolution_version": conflict.get("resolution_version"),
        "previous_resolution": conflict.get("previous_resolution"),
        "explanation": conflict.get("explanation"),
    }
    return {key: value for key, value in payload.items() if value not in ("", None, {}, [])}


def reconcile_offer_truth_resolutions(
    database_url: str,
    *,
    case_id: str,
    offer_id: str,
    engagement_id: str,
    events: list[dict[str, Any]] | None = None,
    publisher: Callable[..., str | None] = publish_os_event,
) -> dict[str, Any]:
    """Persist deterministic resolution/detection audit events once per conflict revision."""
    current_events = list(events) if events is not None else fetch_offer_events_for_case(database_url, case_id)
    published: list[str] = []
    conflicts = [
        conflict
        for conflict in detect_offer_conflicts_for_case(current_events, case_id=case_id)
        if _clean(conflict.get("offer_id")) == _clean(offer_id)
    ]
    for conflict in conflicts:
        status = _clean(conflict.get("resolution_status"))
        event_type = OFFER_CONFLICT_RESOLVED_EVENT if status == "AUTO_RESOLVED" else OFFER_CONFLICT_DETECTED_EVENT
        if status not in {"AUTO_RESOLVED", "OPERATOR_REQUIRED"}:
            continue
        existing = _resolution_event_exists(
            current_events,
            event_type=event_type,
            conflict_id=_clean(conflict.get("conflict_id")),
            resolution_version=_clean(conflict.get("resolution_version")),
            resolution_status=status,
            canonical_candidate_id=_clean(conflict.get("canonical_candidate_id")),
        )
        if existing:
            continue
        payload = _resolution_payload(conflict)
        event_id = publisher(
            database_url=database_url,
            event_type=event_type,
            engagement_id=_clean(engagement_id),
            source_repo="gmail-agent",
            payload=payload,
            correlation={
                "case_id": _clean(case_id),
                "offer_id": _clean(offer_id),
                "conflict_id": _clean(conflict.get("conflict_id")),
                "resolution_version": _clean(conflict.get("resolution_version")),
            },
            case_id=_clean(case_id),
            success=True,
        )
        if event_id:
            published.append(event_id)
    return {"ok": True, "published": published, "conflicts": conflicts}


def record_operator_offer_resolution(
    *,
    database_url: str,
    case_id: str,
    offer_id: str,
    conflict_id: str,
    expected_revision: str,
    candidate_id: str,
    principal_id: str,
    reason: str = "",
    events: list[dict[str, Any]] | None = None,
    publisher: Callable[..., str | None] = publish_os_event,
) -> dict[str, Any]:
    """Record an authenticated operator decision without rewriting source observations."""
    cid = _clean(case_id)
    oid = _clean(offer_id)
    conflict_key = _clean(conflict_id)
    revision = _clean(expected_revision)
    selected_id = _clean(candidate_id)
    principal = _clean(principal_id)
    if not cid or not oid or not conflict_key:
        raise OfferObservationError("conflict_identity_required", "case_id, offer_id and conflict_id are required")
    if not principal:
        raise OfferObservationError("mutation_principal_required", "verified mutation principal is required")
    current_events = list(events) if events is not None else fetch_offer_events_for_case(database_url, cid)
    conflicts = detect_offer_conflicts_for_case(current_events, case_id=cid)
    conflict = next(
        (
            item
            for item in conflicts
            if _clean(item.get("conflict_id")) == conflict_key
            and _clean(item.get("case_id")) == cid
            and _clean(item.get("offer_id")) == oid
        ),
        None,
    )
    if conflict is None:
        raise OfferObservationError("conflict_not_found", "conflict does not belong to this case and offer")
    current_revision = _clean(conflict.get("resolution_version"))
    if not revision or revision != current_revision:
        raise OfferObservationError("stale_conflict_revision", "stale conflict revision")
    candidate = next(
        (item for item in conflict.get("candidate_evidence") or [] if _clean(item.get("candidate_id")) == selected_id),
        None,
    )
    if candidate is None:
        raise OfferObservationError("candidate_not_in_conflict", "candidate does not belong to conflict")
    existing = _resolution_event_exists(
        current_events,
        event_type=OFFER_CONFLICT_RESOLVED_EVENT,
        conflict_id=conflict_key,
        resolution_version=current_revision,
        resolution_status="OPERATOR_RESOLVED",
        canonical_candidate_id=selected_id,
    )
    if existing:
        return {
            "ok": True,
            "event_id": existing.get("event_id"),
            "idempotent": True,
            "resolution_status": "OPERATOR_RESOLVED",
            "canonical_value": candidate.get("value"),
        }
    if _clean(conflict.get("resolution_status")) != "OPERATOR_REQUIRED":
        raise OfferObservationError(
            "conflict_not_operator_required",
            "operator resolution is allowed only for a conflict requiring operator review",
        )
    previous_resolution = next(
        (
            {
                "event_id": event.get("event_id"),
                "resolution_status": (event.get("payload") or {}).get("resolution_status"),
                "canonical_value": (event.get("payload") or {}).get("canonical_value"),
                "resolved_by": (event.get("payload") or {}).get("resolved_by"),
                "resolved_at": event.get("occurred_at"),
            }
            for event in reversed(_resolution_events_for_conflict(current_events, conflict_key))
            if _clean((event.get("payload") or {}).get("resolution_status")) in {"AUTO_RESOLVED", "OPERATOR_RESOLVED"}
        ),
        None,
    )
    now = datetime.now(timezone.utc).isoformat()
    operator_resolution = dict(conflict)
    operator_resolution.update(
        {
            "resolution_status": "OPERATOR_RESOLVED",
            "resolution_basis": "OPERATOR_SELECTION",
            "canonical_value": candidate.get("value"),
            "canonical_candidate_id": candidate.get("candidate_id"),
            "canonical_event_id": (candidate.get("event_ids") or [None])[0],
            "resolved_by": principal,
            "resolved_at": now,
            "requires_operator": False,
            "previous_resolution": previous_resolution,
            "operator_reason": _clean(reason),
            "origin_kind": "operator_decision",
            "provenance_quality": "PROVEN",
        }
    )
    operator_resolution["explanation"] = {
        "basis": "OPERATOR_SELECTION",
        "winning_evidence": candidate.get("evidence") or [],
        "losing_evidence": [
            evidence
            for item in conflict.get("candidate_evidence") or []
            if item.get("candidate_id") != candidate.get("candidate_id")
            for evidence in item.get("evidence") or []
        ],
        "policy_rule": "AUTHENTICATED_OPERATOR_SELECTED_EXISTING_CANDIDATE",
        "human_summary": f"Wartość {candidate.get('value')} jest kanoniczna, ponieważ wybrał ją uwierzytelniony operator.",
    }
    payload = _resolution_payload(operator_resolution)
    payload.update(
        {
            "operator_reason": _clean(reason),
            "origin_kind": "operator_decision",
            "provenance_quality": "PROVEN",
        }
    )
    engagement_id = ""
    for event in current_events:
        if _event_offer_id(event) == oid and _clean(event.get("engagement_id")):
            engagement_id = _clean(event.get("engagement_id"))
            break
    event_id = publisher(
        database_url=database_url,
        event_type=OFFER_CONFLICT_RESOLVED_EVENT,
        engagement_id=engagement_id,
        source_repo="gmail-agent",
        payload=payload,
        correlation={
            "case_id": cid,
            "offer_id": oid,
            "conflict_id": conflict_key,
            "resolution_version": current_revision,
        },
        case_id=cid,
        user_id=principal,
        success=True,
    )
    if not event_id:
        raise OfferObservationError("resolution_publish_failed", "operator resolution could not be persisted")
    return {
        "ok": True,
        "event_id": event_id,
        "idempotent": False,
        "resolution_status": "OPERATOR_RESOLVED",
        "canonical_value": candidate.get("value"),
        "resolution_version": current_revision,
    }


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
