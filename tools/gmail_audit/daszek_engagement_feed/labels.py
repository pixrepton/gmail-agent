"""Operator-facing labels for engagement feed rows (case_kind → UI)."""

from __future__ import annotations

from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

CASE_KIND_META: dict[str, tuple[str, str, str]] = {
    "wycena_oferta": ("lead_opportunity", "Wycena / oferta", "sales"),
    "zapytanie_klienta": ("lead_opportunity", "Zapytanie klienta", "sales"),
    "awaria_naprawa": ("platform_service_security", "Awaria / serwis", "service"),
    "przeglad_konserwacja": ("platform_service_security", "Przegląd / konserwacja", "service"),
    "faktura_sprzedaz": ("finance_settlement", "Faktura sprzedażowa", "finance"),
    "faktura_zakup": ("finance_settlement", "Faktura zakupowa", "finance"),
    "ksiegowosc": ("finance_settlement", "Księgowość", "finance"),
    "zakupy_materialow": ("procurement_delivery", "Zakupy materiałów", "procurement"),
    "szkolenie": ("marketing_performance_review", "Szkolenie / webinar", "marketing_growth"),
    "inne": ("internal_coordination", "Sprawa wewnętrzna", "operations"),
    "niezaklasyfikowane": ("unknown", "Sprawa ogólna", "operations"),
}

OPERATIONAL_STATUS_LABELS: dict[str, str] = {
    "pending_operator": "Oczekuje operatora",
    "ready_for_quote": "Gotowe do oferty",
    "enriching": "Uzupełnianie danych",
    "raw_inquiry": "Nowe zapytanie",
    "node_a_error": "Błąd kalkulacji",
}


def case_kind_ui_meta(case_kind: str) -> tuple[str, str, str]:
    key = str(case_kind or "niezaklasyfikowane").strip() or "niezaklasyfikowane"
    return CASE_KIND_META.get(key, CASE_KIND_META["niezaklasyfikowane"])


def operational_status_label(code: str) -> str:
    raw = str(code or "").strip()
    return OPERATIONAL_STATUS_LABELS.get(raw, raw.replace("_", " ").capitalize() if raw else "Bez stanu")


def primary_next_action_pl(snapshot: EngagementSnapshotV2) -> str:
    if snapshot.hitl_gate.required:
        if any(a.id == "draft_reply" and a.enabled for a in snapshot.actions):
            return "Sprawdź draft i zatwierdź odpowiedź do klienta."
        if snapshot.gaps:
            return str(snapshot.gaps[0].ask_pl or "Wymagana decyzja operatora.")
        return "Wymagana decyzja operatora (HITL)."
    code = str(snapshot.operational_status.code or "").strip()
    if code == "ready_for_quote":
        return "Wyślij ofertę lub przygotuj draft odpowiedzi."
    if code == "raw_inquiry" and snapshot.gaps:
        return str(snapshot.gaps[0].ask_pl or "Uzupełnij brakujące dane od klienta.")
    if code == "raw_inquiry":
        return "Przeanalizuj zapytanie i zbierz dane do wyceny lub serwisu."
    if code == "enriching":
        return "Agent zbiera dane — sprawdź dziennik kroków."
    if code == "node_a_error":
        return "Sprawdź dostępność kalkulacji (kalk-top) i spróbuj ponownie."
    return ""
