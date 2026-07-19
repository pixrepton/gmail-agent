"""Scenario-style assertions for guidance normalization (deterministic, no LLM)."""

from __future__ import annotations

from case_intelligence import _normalize_case_guidance


def test_scenario_client_waiting_follow_up() -> None:
    cg = _normalize_case_guidance(
        {
            "operational_status": "follow_up_needed",
            "waiting_for": "client",
            "reason_summary_pl": "Ostatni ruch był po stronie firmy; klient milczy.",
            "blocker_summary_pl": "",
            "momentum": "slowing",
            "stagnation_flag": False,
            "stagnation_reason_pl": "",
            "business_readiness": "ready_for_followup",
            "operator_attention_class": "keep_visible",
            "next_step_hint_pl": "Warto wrócić do klienta z krótkim przypomnieniem.",
            "confidence": 0.55,
            "source_mode": "llm_reasoned",
        },
        source_mode="llm_reasoned",
    )
    assert cg["operational_status"] == "follow_up_needed"
    assert cg["waiting_for"] == "client"


def test_scenario_needs_data_waiting() -> None:
    cg = _normalize_case_guidance(
        {
            "operational_status": "waiting",
            "waiting_for": "document",
            "reason_summary_pl": "Brakuje dokumentacji technicznej.",
            "blocker_summary_pl": "Brak PDF z instalacji.",
            "momentum": "stalled",
            "stagnation_flag": True,
            "stagnation_reason_pl": "Brak odpowiedzi od tygodnia.",
            "business_readiness": "needs_data",
            "operator_attention_class": "watch",
            "next_step_hint_pl": "Czekamy na dokument od klienta.",
            "confidence": 0.4,
            "source_mode": "llm_reasoned",
        },
        source_mode="llm_reasoned",
    )
    assert cg["business_readiness"] == "needs_data"
    assert cg["stagnation_flag"] is True


def test_scenario_ready_for_offer() -> None:
    cg = _normalize_case_guidance(
        {
            "operational_status": "ready",
            "waiting_for": "none",
            "reason_summary_pl": "Dane kompletne pod kalkulację.",
            "blocker_summary_pl": "",
            "momentum": "growing",
            "stagnation_flag": False,
            "stagnation_reason_pl": "",
            "business_readiness": "ready_for_offer",
            "operator_attention_class": "act_soon",
            "next_step_hint_pl": "Sprawa gotowa do przygotowania oferty.",
            "confidence": 0.82,
            "source_mode": "llm_reasoned",
        },
        source_mode="llm_reasoned",
    )
    assert cg["operational_status"] == "ready"
    assert cg["business_readiness"] == "ready_for_offer"


def test_scenario_stagnating() -> None:
    cg = _normalize_case_guidance(
        {
            "operational_status": "stagnating",
            "waiting_for": "unknown",
            "reason_summary_pl": "Brak ruchu mimo kilku sygnałów.",
            "blocker_summary_pl": "",
            "momentum": "stalled",
            "stagnation_flag": True,
            "stagnation_reason_pl": "Długi czas bez odpowiedzi stron.",
            "business_readiness": "not_ready",
            "operator_attention_class": "watch",
            "next_step_hint_pl": "Warto ocenić czy temat nadal aktualny.",
            "confidence": 0.5,
            "source_mode": "llm_reasoned",
        },
        source_mode="llm_reasoned",
    )
    assert cg["operational_status"] == "stagnating"
    assert cg["momentum"] == "stalled"


def test_scenario_blocked() -> None:
    cg = _normalize_case_guidance(
        {
            "operational_status": "blocked",
            "waiting_for": "schedule",
            "reason_summary_pl": "Realizacja zależy od potwierdzenia terminu.",
            "blocker_summary_pl": "Klient nie potwierdził wizyty.",
            "momentum": "slowing",
            "stagnation_flag": False,
            "stagnation_reason_pl": "",
            "business_readiness": "not_ready",
            "operator_attention_class": "act_soon",
            "next_step_hint_pl": "Potrzebny potwierdzony termin.",
            "confidence": 0.63,
            "source_mode": "llm_reasoned",
        },
        source_mode="llm_reasoned",
    )
    assert cg["operational_status"] == "blocked"
    assert cg["waiting_for"] == "schedule"


def test_scenario_watching_low_energy() -> None:
    cg = _normalize_case_guidance(
        {
            "operational_status": "watching",
            "waiting_for": "unknown",
            "reason_summary_pl": "Niski priorytet, brak pilnych sygnałów.",
            "blocker_summary_pl": "",
            "momentum": "steady",
            "stagnation_flag": False,
            "stagnation_reason_pl": "",
            "business_readiness": "not_ready",
            "operator_attention_class": "case_only_ok",
            "next_step_hint_pl": "Można trzymać tylko w pamięci sprawy.",
            "confidence": 0.25,
            "source_mode": "llm_reasoned",
        },
        source_mode="llm_reasoned",
    )
    assert cg["operational_status"] == "watching"
    assert cg["operator_attention_class"] == "case_only_ok"
