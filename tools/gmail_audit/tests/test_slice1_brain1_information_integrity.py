"""AI-OS-INTELLIGENCE-FINAL-HARDENING-01 / SLICE-1-BRAIN1-INFORMATION-INTEGRITY.

One root cause: Brain 1 holds information it currently deletes, ignores, or passes on in an
impoverished form. Four changes, each with a RED assertion and a counter-case proving no
fabrication and no provenance loss.

Authority model in force (operator-approved): Brain 1 is the sole author of case semantics.
These tests only make Brain 1 stop discarding its own evidence -- they change no vocabulary,
no ownership, no routing, and no measurement contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context_assembler import _facts_dict_from_active_facts  # noqa: E402
from signal_extractor import signal_extraction_failed  # noqa: E402
from understanding_output import (  # noqa: E402
    _facts_invalidated,
    _similar_case_hints,
    build_understanding_output,
    validate_understanding_invariants,
)

# ── change 1 — operator_explanation must survive validation ────────────────────────────────

_OPERATOR_EXPLANATION_KEYS = (
    "essence_pl",
    "customer_intent_pl",
    "what_arrived_pl",
    "what_is_new_pl",
    "what_is_missing_pl",
    "what_is_risk_pl",
    "what_system_suggests_pl",
    "why_pl",
    "what_we_dont_know_pl",
    "operator_should_check_pl",
)


def _full_operator_explanation() -> dict:
    return {
        "essence_pl": "Klient pyta o pompe ciepla dla domu 150 m2.",
        "customer_intent_pl": "Chce wyceny.",
        "what_arrived_pl": "Nowe zapytanie ofertowe.",
        "what_is_new_pl": "Podal metraz i lokalizacje.",
        "what_is_missing_pl": "Brakuje dokladnego adresu i numeru telefonu.",
        "what_is_risk_pl": "Bez adresu nie da sie umowic wizyty.",
        "what_system_suggests_pl": "Zebrac brakujace dane.",
        "why_pl": "Lead aktywny, ale niekompletny.",
        "what_we_dont_know_pl": "Nie wiemy, czy budynek jest w budowie.",
        "operator_should_check_pl": "Sprawdz link sprawy i brakujace dane.",
    }


def test_validation_preserves_every_operator_explanation_field():
    # RED before the fix: validate_understanding_invariants REPLACED the whole dict with
    # {essence_pl, customer_intent_pl}, deleting the eight explanatory fields -- including
    # what_is_missing_pl and what_is_risk_pl, i.e. the explanations for exactly the judge
    # dimensions (gaps, risks) that the measurement work found dominant.
    raw = {"schema_version": "understanding_output.v1", "operator_explanation": _full_operator_explanation()}
    out, _errors = validate_understanding_invariants(raw)
    oe = out["operator_explanation"]
    missing = [key for key in _OPERATOR_EXPLANATION_KEYS if key not in oe]
    assert missing == [], f"validation dropped operator_explanation fields: {missing}"


def test_validation_still_sanitizes_each_preserved_field():
    # counter-case: preserving fields must NOT bypass the projection-safety sanitisation.
    # A phone number must not survive into the operator feed (the PII filter is load-bearing;
    # a prior wave hit a false positive here and fixed it WITHOUT weakening the filter).
    raw = {
        "schema_version": "understanding_output.v1",
        "operator_explanation": {**_full_operator_explanation(), "what_is_missing_pl": "Zadzwon: 501 234 567"},
    }
    out, _errors = validate_understanding_invariants(raw)
    assert "501 234 567" not in out["operator_explanation"]["what_is_missing_pl"]


def test_validation_does_not_invent_absent_operator_explanation_fields():
    # counter-case: no fabrication. A field absent from the producer must not appear with a
    # placeholder value; it may be absent or empty, never invented.
    raw = {
        "schema_version": "understanding_output.v1",
        "operator_explanation": {"essence_pl": "x", "customer_intent_pl": "y"},
    }
    out, _errors = validate_understanding_invariants(raw)
    for key, value in out["operator_explanation"].items():
        if key in {"essence_pl", "customer_intent_pl"}:
            continue
        assert str(value or "") == "", f"{key} was invented with value {value!r}"


def test_validation_errors_are_returned_for_execution_metadata():
    # the caller must be able to record what validation changed; today gmail_intake discards
    # the error list entirely, so a destructive normalisation is unreportable.
    raw = {"schema_version": "wrong_version", "operator_explanation": _full_operator_explanation()}
    _out, errors = validate_understanding_invariants(raw)
    assert "invalid_schema_version" in errors


# ── change 2 (B5) — three dead fields wired from REAL data only ────────────────────────────


def test_facts_invalidated_derives_superseded_values_from_real_conflicts():
    # RED before the fix: hardcoded []. The real contract's invalidation semantics are in
    # split_conflicting_facts: for one fact_key it keeps ranked[0] (highest confidence, then
    # observed_at) as active and reports the full value set as a conflict. Any conflicted value
    # that is not the active one is, by that contract, superseded.
    active_facts = [{"fact_key": "heated_area_m2", "normalized_value": "150", "entity_scope": "case"}]
    conflicting = [{"entity_scope": "case", "fact_key": "heated_area_m2", "values": ["120", "150"]}]
    rows = _facts_invalidated({"active_facts": active_facts, "conflicting_facts": conflicting})
    assert [r["fact_key"] for r in rows] == ["heated_area_m2"]
    assert rows[0]["superseded_value"] == "120"
    assert rows[0]["current_value"] == "150"


def test_facts_invalidated_is_empty_without_conflicts():
    assert _facts_invalidated({"active_facts": [{"fact_key": "a", "normalized_value": "1"}], "conflicting_facts": []}) == []
    assert _facts_invalidated({}) == []


def test_facts_invalidated_never_invents_a_supersession_when_only_one_value_exists():
    # counter-case: a "conflict" row carrying a single value is not a supersession.
    conflicting = [{"entity_scope": "case", "fact_key": "city", "values": ["Rybnik"]}]
    active_facts = [{"fact_key": "city", "normalized_value": "Rybnik", "entity_scope": "case"}]
    assert _facts_invalidated({"active_facts": active_facts, "conflicting_facts": conflicting}) == []


def test_similar_case_hints_come_only_from_real_precedent_refs():
    # RED before the fix: hardcoded [].
    pack = {"precedent_evidence_refs": [{"source_type": "case", "source_id": "case_recovery_X"}]}
    hints = _similar_case_hints(pack)
    assert len(hints) == 1
    assert hints[0].get("source_id") == "case_recovery_X"


def test_similar_case_hints_empty_when_no_precedents():
    assert _similar_case_hints({}) == []
    assert _similar_case_hints({"precedent_evidence_refs": []}) == []


def test_understanding_output_wires_all_three_previously_dead_fields():
    # end-to-end through the real producer: the three fields must reflect pack data, and must
    # stay empty when the pack carries none.
    pack = {
        "case_id": "case_x",
        "active_facts": [{"fact_key": "heated_area_m2", "normalized_value": "150", "entity_scope": "case"}],
        "conflicting_facts": [{"entity_scope": "case", "fact_key": "heated_area_m2", "values": ["120", "150"]}],
        "precedent_evidence_refs": [{"source_type": "case", "source_id": "case_prev"}],
    }
    out = build_understanding_output(
        snapshot={"source_message": {"message_id": "m1"}},
        intake_result={},
        business_result={},
        intelligence={},
        case_context_pack=pack,
    )
    assert out["facts_disputed"], "facts_disputed must reflect real conflicting_facts"
    assert out["facts_invalidated"], "facts_invalidated must reflect real supersessions"
    assert out["similar_case_hints"], "similar_case_hints must reflect real precedent refs"


def test_understanding_output_keeps_the_three_fields_empty_without_source_data():
    out = build_understanding_output(
        snapshot={"source_message": {"message_id": "m1"}},
        intake_result={},
        business_result={},
        intelligence={},
        case_context_pack={"case_id": "case_x"},
    )
    assert out["facts_disputed"] == []
    assert out["facts_invalidated"] == []
    assert out["similar_case_hints"] == []


# ── change 3 (B3) — facts reach the LLM with provenance ────────────────────────────────────


def test_facts_dict_carries_confidence_and_source_for_the_llm():
    # RED before the fix: bare {key: value}. The deterministic layer reads observed_at and
    # source_ref from these same rows (understanding_output._prior_known_state_rows), so the
    # model deciding the action knew strictly less than the projection describing it.
    rows = [
        {
            "fact_key": "heated_area_m2",
            "value": "150",
            "confidence": 0.82,
            "source_ref": "msg_1",
            "observed_at": "2026-07-20T10:00:00Z",
        }
    ]
    facts = _facts_dict_from_active_facts(rows)
    rendered = str(facts["heated_area_m2"])
    assert "150" in rendered
    assert "0.8" in rendered, f"confidence missing from {rendered!r}"
    assert "msg_1" in rendered, f"source_ref missing from {rendered!r}"
    assert "2026-07-20" in rendered, f"observed_at missing from {rendered!r}"


def test_facts_dict_omits_provenance_parts_that_do_not_exist():
    # counter-case: no fabrication. A row without confidence/source must not gain invented ones.
    facts = _facts_dict_from_active_facts([{"fact_key": "city", "value": "Rybnik"}])
    rendered = str(facts["city"])
    assert rendered.strip() == "Rybnik", f"provenance was invented: {rendered!r}"
    assert "conf" not in rendered
    assert "src" not in rendered


def test_facts_dict_value_is_never_lost_to_the_annotation():
    # the value must remain readable/parsable, not be replaced by metadata
    facts = _facts_dict_from_active_facts(
        [{"fact_key": "budget_pln_estimated", "value": "45000", "confidence": 0.5}]
    )
    assert "45000" in str(facts["budget_pln_estimated"])


def test_facts_dict_stays_within_a_bounded_per_fact_budget():
    # context budget must not be blown by provenance: a long source_ref cannot expand a fact
    # entry without bound.
    rows = [{"fact_key": "k", "value": "v", "confidence": 0.9, "source_ref": "s" * 500}]
    rendered = str(_facts_dict_from_active_facts(rows)["k"])
    assert len(rendered) <= 200, f"per-fact rendering exceeded the budget: {len(rendered)}"


# ── change 4 — a failed signal extraction is a gap, never evidence ──────────────────────────


def test_failure_marker_is_recognised_as_failure_not_as_signals():
    # RED before the fix: these dicts are TRUTHY, so `if hvac_signals:` treated them as evidence
    # and an internal error string entered the Intake prompt.
    assert signal_extraction_failed({"parse_status": "extraction_failed", "error_reason": "boom"}) is True
    assert signal_extraction_failed({"parse_status": "empty_result", "error_reason": "no signal"}) is True


def test_real_extraction_result_is_not_treated_as_a_failure():
    # counter-case: a genuine result must stay evidence
    assert signal_extraction_failed({"hvac_intent": "wycena_oferta", "building_type": "dom"}) is False
    assert signal_extraction_failed({}) is False
    assert signal_extraction_failed(None) is False


def test_intake_payload_never_receives_the_error_dict_as_hvac_signals():
    # the consumer contract: on failure, hvac_signals is absent and the reason is kept separately
    failure = {"parse_status": "extraction_failed", "error_reason": "central_stage_unavailable"}
    stage_payload: dict = {}
    if signal_extraction_failed(failure):
        stage_payload["hvac_signals_error"] = {
            "parse_status": failure["parse_status"],
            "error_reason": failure["error_reason"],
        }
    elif failure:
        stage_payload["hvac_signals"] = failure
    assert "hvac_signals" not in stage_payload
    assert stage_payload["hvac_signals_error"]["error_reason"] == "central_stage_unavailable"
