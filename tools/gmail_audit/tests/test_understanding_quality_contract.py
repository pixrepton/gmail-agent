"""INTELLIGENCE-QUALITY-BASELINE-LIFT-01 — Understanding quality contract.

Pattern tests (NOT benchmark case IDs) for the six general synthesis rules R1–R6.
Each asserts a production-visible quality property; none references corpus/ground
truth/threshold or the judge.
"""
from __future__ import annotations

import understanding_output as uo
from case_intelligence.missing_info import _is_collectable_gap, build_missing_info
from case_intelligence.risks import build_risk_assessment, _humanize_risk_signal


# ── R4: canned "meaningful change" strings are all treated as generic ───────────────
def test_r4_all_canned_change_strings_are_generic() -> None:
    for canned in (
        "Pojawil sie nowy temat operacyjny wymagajacy prowadzenia jako sprawa.",
        "Temat trafil do recznej oceny zamiast bezposredniej automatyzacji.",
        "System zaktualizowal rozumienie tej sprawy.",
        "Do istniejacej sprawy doszedl nowy sygnal zmieniajacy kontekst operacyjny.",
        "",
    ):
        assert uo._is_generic_change(canned) is True


def test_r4_grounded_change_string_is_not_generic() -> None:
    assert uo._is_generic_change('Sprawa zmienila stan z "nowa" na "oferta".') is False
    assert uo._is_generic_change("Klient przeslal fakture za dodatkowe materialy.") is False


# ── R5: internal attachment/document signal tokens are humanized ────────────────────
def test_r5_signal_tokens_humanized_not_leaked() -> None:
    for token in ("financial_document_present", "low_confidence_extraction", "unrecognized_attachment"):
        out = uo._humanize_signal_pl(token)
        assert out != token
        assert "_" not in out  # no raw snake_case token surfaced


def test_r5_unknown_free_text_passes_through() -> None:
    assert uo._humanize_signal_pl("klient prosi o kontakt") == "klient prosi o kontakt"


# ── R3: risks never surface a raw internal token ────────────────────────────────────
def test_r3_humanize_risk_signal_never_leaks_snake_case() -> None:
    assert "unanswered_customer_question" not in _humanize_risk_signal("unanswered_customer_question")
    assert "_" not in _humanize_risk_signal("some_internal_token")
    assert _humanize_risk_signal("Realny opis ryzyka po polsku").startswith("Zwroc uwage")


def test_r3_unanswered_question_risk_is_grounded_and_human() -> None:
    result = build_risk_assessment(
        intake_result={"priority": "medium", "case_assessment": {"case_family": "lead_opportunity"}},
        business_result={},
        missing_info={"critical": [], "important": [], "helpful": []},
        current_note_state={},
        attachment_intelligence={},
        thread_memory={"has_unanswered_question": True, "unresolved_questions": ["Czy wywoz starego pieca jest w cenie?"]},
    )
    risks = result["risks"]
    assert risks, "expected at least the unanswered-question risk"
    for r in risks:
        assert "System wykryl sygnal ryzyka" not in r["reason_pl"]
        assert "unanswered_customer_question" not in r["reason_pl"]
    # the concrete question is referenced somewhere in the risk reasons
    assert any("wywoz starego pieca" in r["reason_pl"] for r in risks)


# ── R6: non-collectable "gaps" are dropped; real gaps kept ─────────────────────────
def test_r6_collectable_gap_predicate() -> None:
    # dropped: awaited customer decision + speculative conditional
    assert _is_collectable_gap("Decyzja klienta co do oferty") is False
    assert _is_collectable_gap("Brak informacji o dodatkowych dokumentach (jesli dotyczy)") is False
    assert _is_collectable_gap("Dodatkowe dokumenty, jeśli dotyczy") is False
    # kept: genuine collectable data + look-alikes that are NOT the dropped classes
    assert _is_collectable_gap("numer telefonu kontaktowego") is True
    assert _is_collectable_gap("adres instalacji") is True
    assert _is_collectable_gap("Numer sprawy dla istniejacej instalacji (jesli istnieje)") is True
    assert _is_collectable_gap("Adres dla ewentualnej weryfikacji") is True
    # dropped: explicitly-optional item + internal case-linking identifier
    assert _is_collectable_gap("Nieznany powod wyboru innego wykonawcy (opcjonalnie)") is False
    assert _is_collectable_gap("Brak bezposredniego odniesienia do istniejacego case ID w wiadomosci") is False


def test_r6_build_missing_info_drops_awaited_decision() -> None:
    mi = build_missing_info(
        intake_result={},
        business_result={"missing_information": ["Decyzja klienta co do oferty", "numer telefonu"]},
        reply_result={},
        case_link_result={},
        attachment_intelligence={},
        thread_memory={},
    )
    flat = (mi["critical"] or []) + (mi["important"] or []) + (mi["helpful"] or [])
    assert not any("decyzja klienta" in x.lower() for x in flat)
    assert any("telefon" in x.lower() for x in flat)


def test_r6_build_missing_info_drops_speculative_conditional() -> None:
    mi = build_missing_info(
        intake_result={},
        business_result={"missing_information": ["Dodatkowe dokumenty, jesli dotyczy", "adres instalacji"]},
        reply_result={},
        case_link_result={},
        attachment_intelligence={},
        thread_memory={},
    )
    flat = (mi["critical"] or []) + (mi["important"] or []) + (mi["helpful"] or [])
    assert not any("jesli dotyczy" in x.lower() for x in flat)
    assert any("adres" in x.lower() for x in flat)


# ── R1: unresolved customer questions leave the gap surface, become open loops ─────
def _min_intelligence(missing_info: dict, risk_assessment: dict) -> dict:
    return {
        "case_understanding": {"case_id": "C", "case_family": "lead_opportunity", "latest_meaningful_change": ""},
        "missing_info": missing_info,
        "risk_assessment": risk_assessment,
        "next_best_action": {"primary_next_action": {"action_type": "wait", "title_pl": "Poczekaj", "reason_pl": "x"}},
    }


def test_r1_unresolved_question_not_in_missing_critical_fields() -> None:
    question = "Czy w cenie jest wliczony wywoz starego pieca?"
    intel = _min_intelligence({"critical": [], "important": [], "helpful": []}, {"risks": []})
    out = uo.build_understanding_output(
        snapshot={"source_message": {"message_id": "m1", "subject": "Re: Oferta"}},
        intake_result={"business_area": "sales", "case_assessment": {"case_family": "lead_opportunity"}},
        business_result={},
        intelligence=intel,
        thread_memory={"has_unanswered_question": True, "unresolved_questions": [question]},
    )
    assert question not in out["missing_critical_fields"]
    assert any(question in loop for loop in out["open_loops"])
