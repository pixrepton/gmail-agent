"""Risk assessment for case intelligence."""
from __future__ import annotations
from typing import Any
from .validators import _bounded_float
from .constants import RISK_TYPES, RISK_SEVERITIES, RISK_TYPE_LABELS_PL


# A risk is "grounded" only when it is derived from a specific concrete case fact
# or deterministic state, and it exposes that basis (and evidence refs where a
# source reference is technically available). Free-text business-reasoner risk
# strings carry no supporting fact of their own, so they default to ungrounded
# hypotheses: neither case severity nor overall case urgency grounds them.
_UNGROUNDED_HYPOTHESIS = "business_reasoner_hypothesis"


def _severity_rank(value: Any) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(str(value or "").strip(), 0)


def _grounding(
    basis: str,
    *,
    grounded: bool,
    supporting_fact_pl: str = "",
    evidence_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "grounded": bool(grounded),
        "basis": basis,
        "supporting_fact_pl": str(supporting_fact_pl or "")[:240],
        "evidence_refs": [r for r in (evidence_refs or []) if isinstance(r, dict)][:8],
    }


def _risk_item(
    *,
    risk_type: str,
    severity: str,
    reason_pl: str,
    confidence: float,
    watch: str,
    grounding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "risk_type": risk_type,
        "severity": severity,
        "reason_pl": reason_pl,
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "what_to_watch_for": watch,
        "grounding": grounding if isinstance(grounding, dict) else _grounding(_UNGROUNDED_HYPOTHESIS, grounded=False),
    }


def _dedupe_risk_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_type: dict[str, dict[str, Any]] = {}
    for item in items:
        risk_type = str(item.get("risk_type") or "").strip()
        if not risk_type:
            continue
        existing = by_type.get(risk_type)
        if existing is None:
            by_type[risk_type] = item
            continue
        new_rank = _severity_rank(item.get("severity"))
        old_rank = _severity_rank(existing.get("severity"))
        new_grounded = bool((item.get("grounding") or {}).get("grounded"))
        old_grounded = bool((existing.get("grounding") or {}).get("grounded"))
        # Prefer higher severity; on a tie prefer the grounded risk.
        if new_rank > old_rank or (new_rank == old_rank and new_grounded and not old_grounded):
            by_type[risk_type] = item
    return sorted(by_type.values(), key=lambda item: (_severity_rank(item.get("severity")), _bounded_float(item.get("confidence"), default=0.0)), reverse=True)


# RC-IQ-R3: never surface a raw internal signal token in an operator-facing risk
# reason. Known attachment/document tokens get an operator sentence; a bare snake_case
# token (internal id, no spaces) is replaced with a neutral verification prompt; genuine
# human free text is kept.
_RISK_SIGNAL_LABEL_PL = {
    "financial_document_present": "Klient przeslal dokument finansowy (np. fakture) wymagajacy przetworzenia.",
    "low_confidence_extraction": "Odczyt zalacznika jest niepewny i wymaga recznej weryfikacji.",
    "unrecognized_attachment": "W wiadomosci jest nierozpoznany zalacznik wymagajacy sprawdzenia.",
}


def _humanize_risk_signal(raw_risk: str) -> str:
    text = str(raw_risk or "").strip()
    if text.lower() in _RISK_SIGNAL_LABEL_PL:
        return _RISK_SIGNAL_LABEL_PL[text.lower()]
    if not text:
        return "Sygnal wymaga weryfikacji operatora."
    if "_" in text and " " not in text:  # internal snake_case token — do not leak verbatim
        return "Pojawil sie sygnal wymagajacy weryfikacji przez operatora przed dalszym ruchem."
    return f"Zwroc uwage: {text}."


def _map_raw_risk_to_item(
    raw_risk: str,
    *,
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    grounding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lowered = raw_risk.lower()
    priority = str(intake_result.get("priority") or "medium")
    severity = "high" if priority in {"critical", "high"} or str(business_result.get("urgency") or "") == "high" else "medium"

    if "weak_case_link" in lowered or "manual_review" in lowered or "reasoning_unavailable" in lowered:
        return _risk_item(risk_type="interpretation_risk", severity=severity,
            reason_pl="System nie ma jeszcze wystarczajaco pewnego zrozumienia lub linku sprawy.",
            confidence=0.74, watch="Czy pojawi sie potwierdzenie sprawy albo mocniejszy kontekst.", grounding=grounding)
    if "scope" in lowered:
        return _risk_item(risk_type="lead_loss_risk", severity="medium",
            reason_pl="Zakres sprawy lub leada nie jest jeszcze wystarczajaco doprecyzowany.",
            confidence=0.68, watch="Czy klient poda dane potrzebne do kwalifikacji.", grounding=grounding)
    if "visit" in lowered or "delay" in lowered:
        return _risk_item(risk_type="operational_delay_risk", severity=severity,
            reason_pl="Brak potwierdzenia terminu moze opoznic dalszy ruch operacyjny.",
            confidence=0.72, watch="Czy termin zostanie potwierdzony lub skorygowany.", grounding=grounding)
    if "supplier" in lowered:
        return _risk_item(risk_type="supplier_dependency_risk", severity="medium",
            reason_pl="Dalszy przebieg zalezy od ruchu po stronie dostawcy.",
            confidence=0.7, watch="Czy dostawca potwierdzi termin lub status.", grounding=grounding)
    if "finance" in lowered or "payment" in lowered:
        return _risk_item(risk_type="finance_risk", severity="medium",
            reason_pl="Sprawa ma komponent finansowy wymagajacy potwierdzenia.",
            confidence=0.66, watch="Czy pojawi sie potwierdzenie platnosci lub rozliczenia.", grounding=grounding)
    # RC-IQ-R3: an unanswered customer question is a concrete, grounded risk — describe
    # it in operator language and reference the actual pending question (from grounding),
    # never as the raw internal token.
    if "unanswered_customer_question" in lowered or "unresolved_question" in lowered:
        supporting = str((grounding or {}).get("supporting_fact_pl") or "").strip()
        detail = f' Pytanie czeka na odpowiedz: "{supporting[:180]}".' if supporting else ""
        return _risk_item(risk_type="interpretation_risk", severity=severity,
            reason_pl=("Klient ma niezalatwione pytanie wymagajace odpowiedzi przed dalszym ruchem." + detail).strip(),
            confidence=0.7, watch="Czy pytanie klienta zostanie odpowiedziane w kolejnym ruchu.", grounding=grounding)
    return _risk_item(risk_type="interpretation_risk", severity="medium",
        reason_pl=_humanize_risk_signal(raw_risk),
        confidence=0.6, watch="Czy kolejne sygnaly potwierdza ten kierunek.", grounding=grounding)


def build_risk_assessment(
    *,
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    missing_info: dict[str, Any],
    current_note_state: dict[str, Any],
    attachment_intelligence: dict[str, Any] | None = None,
    thread_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # (raw_risk_text, grounding) entries. Business-reasoner strings carry no fact
    # of their own -> ungrounded hypotheses. Attachment findings and concrete
    # unresolved questions are grounded in a specific fact.
    entries: list[tuple[str, dict[str, Any]]] = []
    seen_raw: set[str] = set()
    for item in business_result.get("risks") or []:
        text = str(item).strip()
        if text and text not in seen_raw:
            seen_raw.add(text)
            entries.append((text, _grounding(_UNGROUNDED_HYPOTHESIS, grounded=False)))
    for flag in (attachment_intelligence or {}).get("combined_risk_flags") or []:
        text = str(flag).strip()
        if text and text not in seen_raw:
            seen_raw.add(text)
            entries.append((text, _grounding("attachment_finding", grounded=True, supporting_fact_pl=text)))
    tm = thread_memory or {}
    unresolved_questions = [str(q).strip() for q in (tm.get("unresolved_questions") or []) if str(q).strip()]
    # An "unanswered question" risk is only material when a concrete question exists.
    # A bare boolean flag with no identifiable question is an ungrounded generic risk.
    if tm.get("has_unanswered_question") and unresolved_questions:
        entries.append(
            ("unanswered_customer_question", _grounding("unresolved_question", grounded=True, supporting_fact_pl=unresolved_questions[0]))
        )
    raw_present = bool(entries)
    risks: list[dict[str, Any]] = []
    for text, grounding in entries:
        risks.append(_map_raw_risk_to_item(text, intake_result=intake_result, business_result=business_result, grounding=grounding))

    critical_missing = [str(x).strip() for x in (missing_info.get("critical") or []) if str(x).strip()]
    if critical_missing and str((intake_result.get("case_assessment") or {}).get("case_family") or "") == "lead_opportunity":
        risks.append(_risk_item(risk_type="lead_loss_risk", severity="medium",
            reason_pl="Lead jest aktywny, ale bez krytycznych danych moze utkac na etapie kwalifikacji.",
            confidence=0.72, watch="Czy klient odpowie z danymi potrzebnymi do kolejnego kroku.",
            grounding=_grounding("missing_critical_fields", grounded=True, supporting_fact_pl=", ".join(critical_missing[:3]))))
    existing_risk_types = {str(item.get("risk_type") or "") for item in risks}
    state_detected = str((intake_result.get("case_assessment") or {}).get("state_detected") or "")
    detected_delivery_state = state_detected in {"delivery_at_risk", "ordered", "delayed", "received"}
    if str(intake_result.get("business_area") or "") in {"procurement", "logistics"} and (
        raw_present or detected_delivery_state
    ) and not existing_risk_types.intersection({"logistics_risk", "supplier_dependency_risk"}):
        # Only a concrete detected delivery state grounds a logistics risk; presence
        # of a generic business-reasoner string does not.
        risks.append(_risk_item(risk_type="logistics_risk",
            severity="medium" if str(intake_result.get("priority") or "low") != "critical" else "high",
            reason_pl="Temat dotyczy logistyki lub dostawy i moze wplynac na kolejne dzialania operacyjne.",
            confidence=0.7, watch="Czy pojawi sie nowy termin dostawy albo potwierdzenie odbioru.",
            grounding=_grounding("detected_delivery_state", grounded=detected_delivery_state, supporting_fact_pl=state_detected if detected_delivery_state else "")))
    try:
        age_days = float(current_note_state.get("age_days") or 0.0)
    except (TypeError, ValueError):
        age_days = 0.0
    if age_days >= 5:
        risks.append(_risk_item(risk_type="aging_risk", severity="medium",
            reason_pl="Temat zalega juz kilka dni bez wyraznego zamkniecia.",
            confidence=0.65, watch="Czy pojawia sie realny postep, czy tylko zaleganie bez ruchu.",
            grounding=_grounding("age_days", grounded=True, supporting_fact_pl=f"{age_days:.0f} dni bez zamkniecia")))

    risks = _dedupe_risk_items(risks)
    grounded_risks = [r for r in risks if (r.get("grounding") or {}).get("grounded")]
    if grounded_risks:
        summary_pl = "Najwazniejsze ryzyko: " + grounded_risks[0]["reason_pl"]
    else:
        summary_pl = "Na ten moment nie widac wyraznych, potwierdzonych ryzyk operacyjnych."
    return {"summary_pl": summary_pl, "risks": risks}
