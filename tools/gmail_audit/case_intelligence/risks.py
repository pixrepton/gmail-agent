"""Risk assessment for case intelligence."""
from __future__ import annotations
from typing import Any
from .validators import _bounded_float
from .constants import RISK_TYPES, RISK_SEVERITIES, RISK_TYPE_LABELS_PL


def _severity_rank(value: Any) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(str(value or "").strip(), 0)


def _risk_item(*, risk_type: str, severity: str, reason_pl: str, confidence: float, watch: str) -> dict[str, Any]:
    return {
        "risk_type": risk_type,
        "severity": severity,
        "reason_pl": reason_pl,
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "what_to_watch_for": watch,
    }


def _dedupe_risk_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_type: dict[str, dict[str, Any]] = {}
    for item in items:
        risk_type = str(item.get("risk_type") or "").strip()
        if not risk_type:
            continue
        existing = by_type.get(risk_type)
        if not existing or _severity_rank(item.get("severity")) > _severity_rank(existing.get("severity")):
            by_type[risk_type] = item
    return sorted(by_type.values(), key=lambda item: (_severity_rank(item.get("severity")), _bounded_float(item.get("confidence"), default=0.0)), reverse=True)


def _map_raw_risk_to_item(raw_risk: str, *, intake_result: dict[str, Any], business_result: dict[str, Any]) -> dict[str, Any]:
    lowered = raw_risk.lower()
    priority = str(intake_result.get("priority") or "medium")
    severity = "high" if priority in {"critical", "high"} or str(business_result.get("urgency") or "") == "high" else "medium"

    if "weak_case_link" in lowered or "manual_review" in lowered or "reasoning_unavailable" in lowered:
        return _risk_item(risk_type="interpretation_risk", severity=severity,
            reason_pl="System nie ma jeszcze wystarczajaco pewnego zrozumienia lub linku sprawy.",
            confidence=0.74, watch="Czy pojawi sie potwierdzenie sprawy albo mocniejszy kontekst.")
    if "scope" in lowered:
        return _risk_item(risk_type="lead_loss_risk", severity="medium",
            reason_pl="Zakres sprawy lub leada nie jest jeszcze wystarczajaco doprecyzowany.",
            confidence=0.68, watch="Czy klient poda dane potrzebne do kwalifikacji.")
    if "visit" in lowered or "delay" in lowered:
        return _risk_item(risk_type="operational_delay_risk", severity=severity,
            reason_pl="Brak potwierdzenia terminu moze opoznic dalszy ruch operacyjny.",
            confidence=0.72, watch="Czy termin zostanie potwierdzony lub skorygowany.")
    if "supplier" in lowered:
        return _risk_item(risk_type="supplier_dependency_risk", severity="medium",
            reason_pl="Dalszy przebieg zalezy od ruchu po stronie dostawcy.",
            confidence=0.7, watch="Czy dostawca potwierdzi termin lub status.")
    if "finance" in lowered or "payment" in lowered:
        return _risk_item(risk_type="finance_risk", severity="medium",
            reason_pl="Sprawa ma komponent finansowy wymagajacy potwierdzenia.",
            confidence=0.66, watch="Czy pojawi sie potwierdzenie platnosci lub rozliczenia.")
    return _risk_item(risk_type="interpretation_risk", severity="medium",
        reason_pl=f"System wykryl sygnal ryzyka: {raw_risk}.",
        confidence=0.6, watch="Czy kolejne sygnaly potwierdza ten kierunek.")


def build_risk_assessment(
    *,
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    missing_info: dict[str, Any],
    current_note_state: dict[str, Any],
    attachment_intelligence: dict[str, Any] | None = None,
    thread_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_risks = [str(item).strip() for item in (business_result.get("risks") or []) if str(item).strip()]
    for flag in (attachment_intelligence or {}).get("combined_risk_flags") or []:
        text = str(flag).strip()
        if text and text not in raw_risks:
            raw_risks.append(text)
    if (thread_memory or {}).get("has_unanswered_question"):
        raw_risks.append("unanswered_customer_question")
    risks: list[dict[str, Any]] = []
    for item in raw_risks:
        risks.append(_map_raw_risk_to_item(item, intake_result=intake_result, business_result=business_result))

    if missing_info.get("critical") and str((intake_result.get("case_assessment") or {}).get("case_family") or "") == "lead_opportunity":
        risks.append(_risk_item(risk_type="lead_loss_risk", severity="medium",
            reason_pl="Lead jest aktywny, ale bez krytycznych danych moze utkac na etapie kwalifikacji.",
            confidence=0.72, watch="Czy klient odpowie z danymi potrzebnymi do kolejnego kroku."))
    existing_risk_types = {str(item.get("risk_type") or "") for item in risks}
    state_detected = str((intake_result.get("case_assessment") or {}).get("state_detected") or "")
    if str(intake_result.get("business_area") or "") in {"procurement", "logistics"} and (
        raw_risks or state_detected in {"delivery_at_risk", "ordered", "delayed", "received"}
    ) and not existing_risk_types.intersection({"logistics_risk", "supplier_dependency_risk"}):
        risks.append(_risk_item(risk_type="logistics_risk",
            severity="medium" if str(intake_result.get("priority") or "low") != "critical" else "high",
            reason_pl="Temat dotyczy logistyki lub dostawy i moze wplynac na kolejne dzialania operacyjne.",
            confidence=0.7, watch="Czy pojawi sie nowy termin dostawy albo potwierdzenie odbioru."))
    try:
        age_days = float(current_note_state.get("age_days") or 0.0)
    except (TypeError, ValueError):
        age_days = 0.0
    if age_days >= 5:
        risks.append(_risk_item(risk_type="aging_risk", severity="medium",
            reason_pl="Temat zalega juz kilka dni bez wyraznego zamkniecia.",
            confidence=0.65, watch="Czy pojawia sie realny postep, czy tylko zaleganie bez ruchu."))

    risks = _dedupe_risk_items(risks)
    summary_pl = "Najwazniejsze ryzyko: " + risks[0]["reason_pl"] if risks else "Na ten moment nie widac wyraznych ryzyk operacyjnych."
    return {"summary_pl": summary_pl, "risks": risks}
