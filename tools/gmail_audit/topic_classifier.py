"""Deterministic TopicResult v1 classifier for Decision Pipeline P0."""

from __future__ import annotations

import hashlib
from typing import Any

from evidence_ref import evidence_ref_from_message, normalize_evidence_ref

TOPIC_RESULT_SCHEMA_VERSION = "topic_result.v1"
TOPIC_SCHEMA_VERSION = TOPIC_RESULT_SCHEMA_VERSION


def build_topic_result(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    business_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intake = intake_result if isinstance(intake_result, dict) else {}
    business = business_result if isinstance(business_result, dict) else {}
    area = str(intake.get("business_area") or business.get("business_area") or "").strip().lower()
    case_family = str((intake.get("case_assessment") or {}).get("case_family") or "").strip().lower()

    if area in {"service", "serwis"}:
        topic_id, label, confidence = "service_request", "Zgłoszenie serwisowe", 0.86
    elif area in {"sales", "lead", "offer"} or case_family in {"lead_opportunity", "heat_pump"}:
        topic_id, label, confidence = "sales_or_offer", "Sprzedaż / oferta", 0.72
    elif area in {"procurement", "logistics"}:
        topic_id, label, confidence = "operations", "Operacje / logistyka", 0.72
    elif area:
        topic_id, label, confidence = _slug(area), area.replace("_", " ").title(), 0.62
    else:
        subject = ""
        sm = snapshot.get("source_message") if isinstance(snapshot.get("source_message"), dict) else {}
        if isinstance(sm, dict):
            subject = str(sm.get("subject") or "").lower()
        if any(word in subject for word in ("awaria", "serwis", "usterka", "nie grzeje")):
            topic_id, label, confidence = "service_request", "Zgłoszenie serwisowe", 0.66
        else:
            topic_id, label, confidence = "general_inbox", "Ogólna sprawa inbox", 0.45

    reason_codes = [f"business_area:{area}"] if area else ["subject_lexical_or_unknown"]
    if case_family:
        reason_codes.append(f"case_family:{case_family}")
    evidence_refs = _message_evidence(snapshot, confidence=0.7)
    tid = "topic_" + hashlib.sha256(f"{topic_id}|{label}".encode("utf-8")).hexdigest()[:16]
    return {
        "schema_version": TOPIC_RESULT_SCHEMA_VERSION,
        "topic_result_id": tid,
        "topic_id": topic_id,
        "topic": topic_id,
        "label_pl": label,
        "topic_label_pl": label,
        "confidence": confidence,
        "secondary_topics": [],
        "reason_codes": reason_codes,
        "evidence_refs": evidence_refs,
        "requires_rich_context": topic_id in {"service_request", "sales_or_offer", "general_inbox"},
        "requires_operator_review": confidence < 0.55 or topic_id == "general_inbox",
        "source": "deterministic_p0",
    }


def _slug(value: str) -> str:
    out = "".join(ch if ch.isalnum() else "_" for ch in value.lower()).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or "general_inbox"


def _message_evidence(snapshot: dict[str, Any], *, confidence: float) -> list[dict[str, Any]]:
    sm = snapshot.get("source_message") if isinstance(snapshot.get("source_message"), dict) else {}
    mid = str(sm.get("message_id") or "").strip()
    if not mid:
        return []
    return [
        normalize_evidence_ref(
            evidence_ref_from_message(
                message_id=mid,
                source_timestamp=str(sm.get("date") or "").strip(),
                confidence=confidence,
            )
        )
    ]


__all__ = ["TOPIC_RESULT_SCHEMA_VERSION", "TOPIC_SCHEMA_VERSION", "build_topic_result"]
