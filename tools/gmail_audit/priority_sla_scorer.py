"""Deterministic PrioritySlaResult v1 scorer for Decision Pipeline P0."""

from __future__ import annotations

import hashlib
from typing import Any

from evidence_ref import evidence_ref_from_message, normalize_evidence_ref

PRIORITY_SLA_RESULT_SCHEMA_VERSION = "priority_sla_result.v1"
PRIORITY_SCHEMA_VERSION = PRIORITY_SLA_RESULT_SCHEMA_VERSION


def build_priority_sla_result(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    missing_info: dict[str, Any] | None = None,
    topic_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ = snapshot
    intake = intake_result if isinstance(intake_result, dict) else {}
    missing = missing_info if isinstance(missing_info, dict) else {}
    topic = topic_result if isinstance(topic_result, dict) else {}

    priority_raw = str(intake.get("priority") or "").strip().lower()
    if priority_raw in {"critical", "urgent", "high", "medium", "low"}:
        priority = "high" if priority_raw in {"critical", "urgent"} else priority_raw
    elif topic.get("topic_id") == "service_request":
        priority = "medium"
    else:
        priority = "low"

    critical_missing = bool(missing.get("critical"))
    requires_same_day = priority == "high" or (topic.get("topic_id") == "service_request" and not critical_missing)
    if priority == "high":
        sla_risk = "elevated"
        sla_risk_score = 0.72
    elif critical_missing:
        sla_risk = "blocked_by_missing_info"
        sla_risk_score = 0.62
    else:
        sla_risk = "normal"
        sla_risk_score = 0.35

    reason = "Priorytet z intake."
    if topic.get("topic_id") == "service_request":
        reason = "Zgłoszenie serwisowe wymaga widoczności operatora."
    if critical_missing:
        reason = "Braki danych ograniczają gotowość dalszej akcji."

    rid = "psla_" + hashlib.sha256(f"{priority}|{sla_risk}|{reason}".encode("utf-8")).hexdigest()[:16]
    evidence_refs = _message_evidence(snapshot, confidence=0.65)
    return {
        "schema_version": PRIORITY_SLA_RESULT_SCHEMA_VERSION,
        "priority_sla_result_id": rid,
        "priority": priority,
        "urgency": priority_raw if priority_raw else priority,
        "sla_risk": sla_risk,
        "sla_risk_score": sla_risk_score,
        "priority_reason": reason,
        "requires_same_day_attention": bool(requires_same_day),
        "deadline_detected": False,
        "deadline_pressure": critical_missing,
        "customer_waiting": priority in {"high", "medium"},
        "customer_waiting_time": "",
        "business_impact": "medium" if priority in {"high", "medium"} else "low",
        "business_value_signal": "normal",
        "relationship_risk": "low",
        "service_risk": "high" if topic.get("topic_id") == "service_request" else "normal",
        "complaint_risk": "low",
        "reason_codes": ["intake_priority", "missing_info_critical" if critical_missing else "missing_info_ok"],
        "evidence_refs": evidence_refs,
        "confidence": 0.72 if priority_raw else 0.55,
        "source": "deterministic_p0",
    }


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


__all__ = ["PRIORITY_SLA_RESULT_SCHEMA_VERSION", "PRIORITY_SCHEMA_VERSION", "build_priority_sla_result"]
