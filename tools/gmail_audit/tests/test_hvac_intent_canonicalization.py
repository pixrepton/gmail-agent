"""STRUCTURED-INPUT-AND-CAPABILITY-BASELINE-CLOSEOUT-01 Phase 3 — hvac_intent canonical
vocabulary. Pattern tests (semantic classes, not literal benchmark case text).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_contracts.signal_extraction import (  # noqa: E402
    HVAC_INTENT_CANONICAL_VALUES,
    SignalExtractionResult,
    canonicalize_hvac_intent,
)


def test_all_canonical_values_are_in_the_fixed_vocabulary():
    for raw in ("wycena", "wycena_oferta", "awaria", "przeglad", "rabat", "wroce za miesiac", "", "asdkjaslkdj"):
        canonical, _ = canonicalize_hvac_intent(raw)
        assert canonical in HVAC_INTENT_CANONICAL_VALUES


def test_request_for_quotation_pl_and_en():
    for raw in ("Prosze o wycene pompy ciepla", "wymiana na pompe ciepla", "request for quotation", "I'd like a quote"):
        canonical, evidence = canonicalize_hvac_intent(raw)
        assert canonical == "wycena_oferta"
        assert evidence == raw


def test_price_negotiation():
    for raw in ("Czy jest mozliwy rabat?", "Cena jest za wysoka, prosze o nizsza", "Can you offer a discount?"):
        canonical, _ = canonicalize_hvac_intent(raw)
        assert canonical == "negocjacja_ceny"


def test_deferral_not_confused_with_refusal_or_acceptance():
    for raw in ("Musze to jeszcze przemyslec z zona, wrocimy za miesiac", "I need to think it over, will get back to you"):
        canonical, _ = canonicalize_hvac_intent(raw)
        assert canonical == "odroczenie_decyzji"


def test_service_or_malfunction():
    for raw in ("Pompa ciepla nie dziala od tygodnia", "the unit is broken and leaking"):
        canonical, _ = canonicalize_hvac_intent(raw)
        assert canonical == "awaria_naprawa"


def test_no_intent_detected_maps_to_unknown_not_a_fabricated_class():
    canonical, evidence = canonicalize_hvac_intent("")
    assert canonical == "nieznane"
    assert evidence == ""
    # a genuinely unrelated free-text remainder that matches no rule also stays unknown,
    # never invented as a new ad-hoc class
    canonical2, evidence2 = canonicalize_hvac_intent("Prosze o kontakt telefoniczny w sprawie faktury")
    assert canonical2 in HVAC_INTENT_CANONICAL_VALUES


def test_building_type_field_present_only_in_attachment_scenario_is_not_lost():
    # canonicalization must never mutate/drop other fields on the result model
    result = SignalExtractionResult(hvac_intent="wycena", building_type="dom jednorodzinny", heated_area_m2=150.0)
    assert result.building_type == "dom jednorodzinny"
    assert result.heated_area_m2 == 150.0


def test_conflicting_mail_vs_prior_context_still_canonicalizes_deterministically():
    # canonicalization is a pure function of the CURRENT raw text; it does not consult or
    # get confused by unrelated prior-context text
    canonical_a, _ = canonicalize_hvac_intent("wycena pompy ciepla")
    canonical_b, _ = canonicalize_hvac_intent("wycena pompy ciepla")
    assert canonical_a == canonical_b == "wycena_oferta"


def test_multiword_synonyms_are_not_treated_as_new_classes():
    # many different phrasings of the same request all collapse to ONE canonical class,
    # never spawning ad-hoc per-phrasing classes
    variants = ("wycena", "prosze o wycene", "interesuje mnie oferta", "ile kosztuje pompa ciepla", "request for quotation")
    canonical_values = {canonicalize_hvac_intent(v)[0] for v in variants}
    assert canonical_values == {"wycena_oferta"}


def test_canonicalization_handles_polish_stroke_l_diacritic():
    # "ciepła" (with stroke-L) must match the same phrase rules written in plain ASCII
    canonical, _ = canonicalize_hvac_intent("Chciałbym kupić pompę ciepła")
    assert canonical == "wycena_oferta"


def test_efficiency_question_recognized_via_polish_synonym():
    # "efektywność" (efficiency) is a standard PL synonym alongside "wydajnosc"/"sprawnosc";
    # observed live-production gap: this exact phrasing fell through to "nieznane"
    canonical, _ = canonicalize_hvac_intent(
        "Zapytanie o efektywność Panasonic Aquarea T-CAP w niskich temperaturach"
    )
    assert canonical == "pytanie_techniczne"


def test_malfunction_with_discount_request_classifies_as_service_not_price_negotiation():
    # adversarial-review finding: a real malfunction report that also asks for a repair
    # discount must classify as service/repair, not sales/price-negotiation
    canonical, _ = canonicalize_hvac_intent("Klimatyzacja nie dziala, prosze o rabat na naprawe")
    assert canonical == "awaria_naprawa"


def test_word_boundary_prevents_substring_collision_inside_unrelated_words():
    # adversarial-review finding: "fault" must not match inside "default"
    canonical, _ = canonicalize_hvac_intent("Prosimy przywrocic ustawienia domyslne (default) na urzadzeniu")
    assert canonical != "awaria_naprawa"


def test_unknown_sentinel_matches_the_scorers_existing_is_unknown_vocabulary():
    # eval_measurement_scoring._is_unknown() recognizes {"unknown","none","null","nieznane","brak"}
    # (normalized/casefolded) as a "no value" sentinel. hvac_intent's own "no signal detected"
    # canonical value MUST be exactly one of these, or the extraction scorer would count a
    # correctly-abstained "no intent detected" as a wrong_value (extra penalty) instead of the
    # intended missing_unknown (recall-only, no penalty).
    canonical, _ = canonicalize_hvac_intent("")
    assert canonical in {"unknown", "none", "null", "nieznane", "brak"}


def test_raw_evidence_is_never_discarded():
    raw = "inquiry about low-temperature efficiency of Panasonic Aquarea T-CAP"
    canonical, evidence = canonicalize_hvac_intent(raw)
    assert canonical == "pytanie_techniczne"
    assert evidence == raw
