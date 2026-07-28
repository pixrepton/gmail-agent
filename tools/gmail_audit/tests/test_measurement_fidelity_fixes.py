"""STRUCTURED-INPUT-AND-CAPABILITY-BASELINE-CLOSEOUT-01 Phase 3/5/6 — measurement/harness
fidelity fixes in eval_measurement_scoring.py and eval_understanding_judge.py. Pattern
tests (semantic classes / synthetic payloads, not literal benchmark case text).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval_measurement_scoring import (  # noqa: E402
    _draft_text,
    _find_terms,
    _legacy_fact_spec,
    _matches_expected,
    _normalize_text,
    score_draft,
    score_extraction,
)
from eval_understanding_judge import (  # noqa: E402
    _backfill_redundant_top_level_verdict,
    _compact_understanding_output,
    normalize_judge_result,
)


# ── draft: score the recommended variant, not always drafts[0] ─────────────────────────
def test_draft_text_prefers_recommended_variant_over_first():
    payload = {
        "drafts": [
            {"variant": "internal_note", "body": "Draft odrzucony: brak danych."},
            {"variant": "customer_friendly", "body": "Dziekujemy za zapytanie, prosimy o adres."},
        ],
        "recommended_variant": "customer_friendly",
    }
    assert _draft_text(payload) == "Dziekujemy za zapytanie, prosimy o adres."


def test_draft_text_falls_back_to_first_when_no_recommended_variant():
    payload = {"drafts": [{"variant": "a", "body": "text a"}, {"variant": "b", "body": "text b"}]}
    assert _draft_text(payload) == "text a"


def test_draft_text_falls_back_to_first_when_recommended_variant_not_found():
    payload = {"drafts": [{"variant": "a", "body": "text a"}], "recommended_variant": "missing"}
    assert _draft_text(payload) == "text a"


# ── tone: broadened default vocabulary recognizes common polite business forms ─────────
def test_tone_recognizes_formal_plural_polite_forms():
    draft = {"drafts": [{"variant": "x", "body": "Dziekujemy za kontakt. Prosimy o adres instalacji."}]}
    result = score_draft(draft, {})
    assert result["dimensions"]["tone"]["status"] == "passed"


# ── unsafe-term matching: strict mode avoids inflected-form false positives ────────────
def test_unsafe_terms_do_not_false_positive_on_unrelated_inflected_forms():
    # "umowiona wizyta" (an already-confirmed visit) must NOT fire on "umowic wizyte"
    # (we will try to arrange a visit) -- different grammatical forms sharing a short prefix
    hits = _find_terms("postaramy sie umowic wizyte w najblizszym terminie", ["umowiona wizyta"], strict=True)
    assert hits == []


def test_unsafe_terms_still_catch_exact_unsafe_phrases_in_strict_mode():
    hits = _find_terms("gwarantujemy najlepsza cene na pewno", ["gwarantujemy", "na pewno"], strict=True)
    assert set(hits) == {"gwarantujemy", "na pewno"}


def test_draft_unsafe_promises_dimension_passes_for_tentative_scheduling_language():
    draft = {"drafts": [{"variant": "x", "body": "Postaramy sie umowic wizyte w najblizszym mozliwym terminie."}]}
    result = score_draft(draft, {})
    assert result["dimensions"]["unsafe_promises"]["status"] == "passed"


def test_draft_unsafe_promises_dimension_still_fails_for_genuine_unsafe_claim():
    draft = {"drafts": [{"variant": "x", "body": "Gwarantujemy najnizsza cene na rynku."}]}
    result = score_draft(draft, {})
    assert result["dimensions"]["unsafe_promises"]["status"] == "failed"


# ── extraction: composite ground-truth string ("field free-text number") targeted match ─
def test_legacy_fact_spec_splits_field_name_prefix_with_embedded_number():
    spec = _legacy_fact_spec("budget_pln_estimated w okolicy 45000")
    assert spec["key"] == "budget_pln_estimated"
    assert "45000" in spec["expected"]


def test_legacy_fact_spec_leaves_plain_prose_unaffected():
    spec = _legacy_fact_spec("brak critical gaps blokujacych oferte")
    assert "key" not in spec or not spec.get("key")


def test_extraction_scorer_matches_composite_field_value_within_tolerance():
    actual = {"budget_pln_estimated": 45000.0, "heated_area_m2": 150.0}
    result = score_extraction(actual, {"must": ["budget_pln_estimated w okolicy 45000", "heated_area_m2=150"]})
    assert result["matched_required_fact_count"] == 2
    assert result["wrong_value_count"] == 0


def test_extraction_scorer_still_flags_genuinely_wrong_composite_value():
    actual = {"budget_pln_estimated": 12000.0}
    result = score_extraction(actual, {"must": ["budget_pln_estimated w okolicy 45000"]})
    assert result["wrong_value_count"] == 1


def test_composite_fact_spec_falls_back_to_whole_object_when_guessed_key_is_not_a_real_field():
    # adversarial-review finding: a snake_case-shaped prefix that is NOT actually a real
    # field in `actual` must fall back to whole-object matching, not a guaranteed miss.
    # (no other numeric field present, to isolate this claim from the separate, documented
    # _number()-picks-the-first-number-in-flattened-text limitation)
    actual = {"notes_pl": "termin kontaktu za 14 dni"}
    result = score_extraction(actual, {"must": ["termin_kontaktu 14 dni"]})
    assert result["matched_required_fact_count"] == 1


def test_extract_fact_value_returns_none_not_flattened_text_for_an_explicit_key_miss():
    # an EXPLICIT "field=value" ground-truth key (author intent, not a guess) that
    # genuinely has no matching field must still miss cleanly -- only the derived-key
    # (composite-string-guessed) path falls back to whole-object matching
    from eval_measurement_scoring import _extract_fact_value, _legacy_fact_spec
    spec = _legacy_fact_spec("some_missing_field=42")
    assert _extract_fact_value({"other_field": 42}, spec) is None


# ── normalization: Polish stroke-L (ł) is not an NFKD-decomposable diacritic ────────────
def test_normalize_text_maps_polish_stroke_l_to_plain_l():
    assert _normalize_text("Wrocław") == "wroclaw"
    assert _normalize_text("Łódź") == "lodz"
    assert _normalize_text("ciepła") == "ciepla"


def test_extraction_scorer_matches_geographic_signal_with_stroke_l():
    actual = {"raw_geographic_signal": "Wrocław"}
    result = score_extraction(actual, {"must": ["raw_geographic_signal=Wroclaw"]})
    assert result["matched_required_fact_count"] == 1
    assert result["wrong_value_count"] == 0


# ── judge input compaction: thread_delta survives truncation, no duplicate fields ──────
def test_compact_understanding_preserves_thread_delta_with_prior_state():
    long_text = "Klient " * 40  # long enough to have previously exhausted the old budget
    understanding = {
        "summary_pl": long_text,
        "operator_explanation": {"essence_pl": long_text, "customer_intent_pl": long_text},
        "situation_summary_pl": long_text,
        "customer_intent_pl": long_text,
        "current_customer_intent": long_text,
        "missing_information": {"summary_pl": "brak danych"},
        "thread_delta": {
            "prior_known_state": [{"fact_key": "heated_area_m2", "value": 120}],
            "prior_known_state_pl": "powierzchnia: 120",
            "operator_visible_delta_summary": "Wczesniej ustalone dane pozostaja wazne.",
            "changes": [],
        },
    }
    compact = _compact_understanding_output(understanding)
    assert "thread_delta" in compact
    assert "operator_visible_delta_summary" in compact["thread_delta"]


def test_compact_understanding_drops_duplicate_keys():
    understanding = {"summary_pl": "x", "situation_summary_pl": "x", "customer_intent_pl": "y", "current_customer_intent": "y"}
    compact = _compact_understanding_output(understanding)
    assert "situation_summary_pl" not in compact
    assert "current_customer_intent" not in compact
    assert compact["summary_pl"] == "x"
    assert compact["customer_intent_pl"] == "y"


# ── judge: tolerate a missing (redundant, always-recomputed) top-level verdict field ────
def test_backfill_redundant_verdict_only_when_dimensions_present():
    payload_missing_verdict = {"case_id": "x", "dimensions": {"essence": {"applicable": True, "verdict": "PASS", "reason_code": "x", "evidence": "x"}}}
    backfilled = _backfill_redundant_top_level_verdict(payload_missing_verdict)
    assert "overall_verdict" in backfilled
    assert "unsafe_misinterpretation" in backfilled


def test_backfill_does_not_touch_payload_without_dimensions():
    payload = {"case_id": "x"}
    assert _backfill_redundant_top_level_verdict(payload) == payload


def test_backfill_refuses_empty_dimensions_dict():
    # adversarial-review finding: an empty {} must NOT be backfilled into a fake pass --
    # it must still fail real Pydantic validation and surface as JUDGE_ERROR
    payload = {"case_id": "x", "dimensions": {}}
    assert _backfill_redundant_top_level_verdict(payload) == payload


def test_backfill_refuses_dimensions_with_no_recognized_verdicts():
    # garbage/unrecognized keys or dimension entries missing "verdict" also don't qualify
    payload = {"case_id": "x", "dimensions": {"not_a_real_dimension": {"foo": "bar"}}}
    assert _backfill_redundant_top_level_verdict(payload) == payload
    payload2 = {"case_id": "x", "dimensions": {"essence": {"applicable": True}}}  # no "verdict"
    assert _backfill_redundant_top_level_verdict(payload2) == payload2


def test_backfilled_overall_verdict_is_discarded_and_recomputed_from_dimensions():
    # the placeholder inserted by backfill must NEVER leak into the final judged verdict --
    # normalize_judge_result always recomputes it from the per-dimension verdicts
    payload = {
        "case_id": "x",
        "dimensions": {"essence": {"applicable": True, "verdict": "PASS", "reason_code": "x", "evidence": "x"}},
    }
    backfilled = _backfill_redundant_top_level_verdict(payload)
    assert backfilled["overall_verdict"] == "CLEAR_FAIL"  # the placeholder itself
    result = normalize_judge_result(backfilled)
    assert result["overall_verdict"] == "CLEAR_PASS"  # correctly recomputed from dimensions, not the placeholder
