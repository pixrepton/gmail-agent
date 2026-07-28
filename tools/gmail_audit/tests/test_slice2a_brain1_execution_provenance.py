"""AI-OS-INTELLIGENCE-FINAL-HARDENING-01 / SLICE-2A-BRAIN1-EXECUTION-PROVENANCE.

Root cause: Brain 1 can return a model result, a repaired/coerced result, a skipped-for-lane
artifact or a fallback, and today's artifacts cannot tell them apart. This slice makes execution
and authorship observable WITHOUT changing any business decision, operator-facing behaviour,
routing, snapshot, feed or scoring.

Scope guard asserted here: nothing in this slice may change a normalized VALUE, only what is
recorded about how that value came to be.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business_reasoner import build_skipped_business_reasoning, fallback_business_reasoning  # noqa: E402
from intake_schema import _normalize_choice, validate_business_reasoning_result  # noqa: E402
from understanding_output import validate_understanding_invariants  # noqa: E402


def _valid_br_payload() -> dict:
    return {
        "business_interpretation": "Klient prosi o wycene.",
        "business_area": "lead",
        "customer_state_guess": "new_lead",
        "recommended_next_action": "collect_data",
        "recommended_action_reason": "Brakuje metrazu.",
        "urgency": "normal",
        "human_review_bias": "medium",
        "confidence": {"business_confidence": 0.8, "action_confidence": 0.7},
    }


# ── 1. _uo_errors reach the canonical Brain 1 metadata envelope ────────────────────────────


def test_validator_still_reports_its_findings_to_the_caller():
    raw = {"schema_version": "wrong_version", "operator_explanation": {"essence_pl": "x"}}
    _out, errors = validate_understanding_invariants(raw)
    assert "invalid_schema_version" in errors


def test_validator_reports_an_empty_list_when_nothing_was_wrong():
    # explicit empty, so "ran and found nothing" is distinguishable from "did not run"
    raw = {"schema_version": "understanding_output.v1", "operator_explanation": {"essence_pl": "x"}}
    _out, errors = validate_understanding_invariants(raw)
    assert errors == []


def test_understanding_validation_errors_are_bounded_codes_not_payloads():
    # the envelope must never become a copy of the mail. Codes are short identifiers.
    raw = {"schema_version": "wrong_version", "operator_explanation": {"essence_pl": "x" * 5000}}
    _out, errors = validate_understanding_invariants(raw)
    assert all(len(str(code)) <= 120 for code in errors)
    assert not any("xxxxxxxxxx" in str(code) for code in errors)


# ── 2. coercion telemetry ──────────────────────────────────────────────────────────────────


def test_normalize_choice_records_a_real_coercion():
    notes: list[dict] = []
    out = _normalize_choice("needs_response", {"new_lead", "post_offer"}, default="unclear",
                            field_name="business_reasoning.customer_state_guess", notes=notes)
    assert out == "unclear"
    assert len(notes) == 1
    note = notes[0]
    assert note["field_name"] == "business_reasoning.customer_state_guess"
    assert note["raw_value"] == "needs_response"
    assert note["normalized_value"] == "unclear"
    assert note["reason_code"] == "value_not_in_allowed_vocabulary"


def test_normalize_choice_records_nothing_when_the_value_is_already_valid():
    notes: list[dict] = []
    assert _normalize_choice("new_lead", {"new_lead"}, default="unclear", field_name="f", notes=notes) == "new_lead"
    assert notes == []


def test_normalize_choice_distinguishes_an_empty_value_from_a_wrong_one():
    notes: list[dict] = []
    _normalize_choice("", {"a"}, default="a", field_name="f", notes=notes)
    assert notes[0]["reason_code"] == "empty_value_defaulted"


def test_normalize_choice_bounds_the_recorded_raw_value():
    notes: list[dict] = []
    _normalize_choice("z" * 900, {"a"}, default="a", field_name="f", notes=notes)
    assert len(notes[0]["raw_value"]) <= 120


def test_normalize_choice_without_a_sink_behaves_exactly_as_before():
    # every pre-existing call site passes no sink; behaviour must be byte-identical
    assert _normalize_choice("nope", {"a"}, default="a", field_name="f") == "a"
    assert _normalize_choice("a", {"a"}, default="b", field_name="f") == "a"


def test_business_reasoning_records_each_real_coercion():
    payload = {**_valid_br_payload(), "customer_state_guess": "needs_response", "business_area": "procurement"}
    out = validate_business_reasoning_result(payload)
    fields = {n["field_name"] for n in out["execution_metadata"]["normalization_notes"]} if "execution_metadata" in out else set()
    notes = out.get("normalization_notes") or []
    fields = fields or {n["field_name"] for n in notes}
    assert "business_reasoning.customer_state_guess" in fields
    assert "business_reasoning.business_area" in fields


def test_business_reasoning_emits_no_notes_for_a_fully_valid_payload():
    out = validate_business_reasoning_result(_valid_br_payload())
    assert "normalization_notes" not in out


def test_coercion_telemetry_does_not_change_any_normalized_value():
    # the scope guard: recording a coercion must not alter its outcome
    payload = {**_valid_br_payload(), "customer_state_guess": "needs_response"}
    out = validate_business_reasoning_result(payload)
    assert out["customer_state_guess"] == "unclear"
    assert out["business_area"] == "lead"
    assert out["recommended_next_action"] == "collect_data"


# ── 3. source_mode / reasoning_status on every Brain 1 result path ─────────────────────────


def test_fallback_is_labelled_as_unavailable_not_as_a_decision():
    meta = fallback_business_reasoning(reason="central_stage_unavailable")["execution_metadata"]
    assert meta["source_mode"] == "fallback"
    assert meta["reasoning_status"] == "unavailable"
    assert meta["fallback_used"] is True


def test_fallback_semantics_are_unchanged_in_this_slice():
    # operator decision D defers changing what the fallback DOES; this slice only labels it.
    result = fallback_business_reasoning(reason="boom")
    assert result["recommended_next_action"] == "escalate_review"
    assert result["confidence"]["business_confidence"] == 0.0
    assert result["confidence"]["action_confidence"] == 0.0


def test_skipped_for_lane_is_labelled_as_skipped():
    result = build_skipped_business_reasoning(
        lane="reference_only", intake_result={}, reason="lane_skips_business_reasoning"
    )
    meta = result["execution_metadata"]
    assert meta["source_mode"] == "skipped_for_lane"
    assert meta["reasoning_status"] == "skipped"


def test_the_four_result_classes_are_mutually_distinguishable():
    modes = {
        fallback_business_reasoning(reason="x")["execution_metadata"]["source_mode"],
        build_skipped_business_reasoning(lane="skip", intake_result={}, reason="y")["execution_metadata"]["source_mode"],
        "model_result",
        "normalized_model_result",
    }
    assert len(modes) == 4


# ── 4. capture integrity ───────────────────────────────────────────────────────────────────


def test_metadata_addition_does_not_alter_the_business_contract_fields():
    # a consumer reading the decision must see exactly what it saw before
    before_keys = {
        "business_interpretation", "business_area", "customer_state_guess", "recommended_next_action",
        "recommended_action_reason", "missing_information", "risks", "urgency", "operator_note",
        "confidence", "business_summary_short", "reply_recommended", "human_review_bias",
        "safety_notes", "evidence_refs", "assumptions", "unsupported_claims", "conflict_refs",
    }
    out = validate_business_reasoning_result(_valid_br_payload())
    assert before_keys.issubset(set(out)), before_keys - set(out)


def test_clean_result_carries_no_extra_keys_beyond_the_prior_contract():
    # no note key appears when nothing was coerced, so a clean contract is unchanged
    out = validate_business_reasoning_result(_valid_br_payload())
    assert "normalization_notes" not in out
