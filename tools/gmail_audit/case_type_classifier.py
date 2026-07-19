"""Deterministic CaseTypeResult v1 classifier for Decision Pipeline P0."""

from __future__ import annotations

import hashlib
from typing import Any

from evidence_ref import evidence_ref_from_message, normalize_evidence_ref

CASE_TYPE_RESULT_SCHEMA_VERSION = "case_type_result.v1"
CASE_TYPE_SCHEMA_VERSION = CASE_TYPE_RESULT_SCHEMA_VERSION


def build_case_type_result(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intake = intake_result if isinstance(intake_result, dict) else {}
    link = case_link_result if isinstance(case_link_result, dict) else {}
    try:
        link_confidence = max(0.0, min(1.0, float(link.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        link_confidence = 0.0
    family = str((intake.get("case_assessment") or {}).get("case_family") or "").strip().lower()
    area = str(intake.get("business_area") or "").strip().lower()

    if area in {"service", "serwis"}:
        case_type = "service_case"
        label = "Sprawa serwisowa"
        confidence = 0.82
    elif family == "lead_opportunity":
        case_type = "sales_lead"
        label = "Lead / szansa sprzedażowa"
        confidence = 0.78
    elif family:
        case_type = _slug(family)
        label = family.replace("_", " ").title()
        confidence = 0.66
    else:
        case_type = "inbox_case"
        label = "Sprawa inbox"
        confidence = 0.45

    link_decision = str(link.get("decision") or "").strip().lower()
    if link_decision in {"linked", "existing_case", "same_case"}:
        link_status = "linked"
        requires_adjudication = link_confidence < 0.75
    elif link_decision in {"pending_adjudication", "competing_links", "weak_link", "link_conflict"}:
        link_status = "pending_or_conflict"
        requires_adjudication = True
    elif link_decision in {"unlinked", "new_case", "no_link"}:
        link_status = "unlinked"
        requires_adjudication = False
    else:
        link_status = link_decision or "unknown"
        requires_adjudication = link_status == "unknown"

    candidates = [
        str((candidate or {}).get("case_key") or (candidate or {}).get("case_id") or "").strip()
        for candidate in (link.get("candidates") or [])
        if isinstance(candidate, dict)
    ]
    candidates = [x for x in candidates if x][:5]
    evidence_refs = _message_evidence(snapshot, confidence=0.7)
    rid = "ctype_" + hashlib.sha256(f"{case_type}|{link_status}".encode("utf-8")).hexdigest()[:16]
    return {
        "schema_version": CASE_TYPE_RESULT_SCHEMA_VERSION,
        "case_type_result_id": rid,
        "case_type": case_type,
        "label_pl": label,
        "case_link_status": link_status,
        "case_link_confidence": link_confidence,
        "case_link_reason": "Case link status derived from intake linker output.",
        "requires_adjudication": requires_adjudication,
        "candidate_case_ids": candidates,
        "evidence_refs": evidence_refs,
        "confidence": confidence,
        "source": "deterministic_p0",
    }


def _slug(value: str) -> str:
    out = "".join(ch if ch.isalnum() else "_" for ch in value.lower()).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or "inbox_case"


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


__all__ = ["CASE_TYPE_RESULT_SCHEMA_VERSION", "CASE_TYPE_SCHEMA_VERSION", "build_case_type_result"]
