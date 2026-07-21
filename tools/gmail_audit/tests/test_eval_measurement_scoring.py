from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from eval_measurement_scoring import (  # noqa: E402
    FrozenCorpusError,
    canonical_json_sha256,
    load_frozen_corpus,
    score_case,
    score_draft,
    score_extraction,
    score_understanding,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "eval_measurement_corpus_v1.json"
EXPECTED_FIXTURE_SHA256 = "8acffce71258b53810e600a4ae8ad2545d5d1c388395c73ff2eaa80e0605cf0b"


def _ground_truth() -> dict:
    corpus, _ = load_frozen_corpus(FIXTURE)
    return corpus["cases"][0]["ground_truth"]


def _perfect_extraction() -> dict:
    return {
        "business_area": "service",
        "case_assessment": {"case_family": "service_emergency"},
        "priority": "high",
        "review": {"required": True},
        "reason": "Pilna awaria pieca.",
    }


def _good_understanding() -> dict:
    return {
        "operator_explanation": {
            "essence_pl": "Klient zglasza pilna awarie: piec nie dziala.",
            "next_step_pl": "Eskalacja do operatora i kontakt serwisowy.",
        },
        "customer_intent": "Potrzebna pomoc serwisowa.",
        "current_situation_change": "Piec nie dziala teraz.",
        "gaps": ["model urzadzenia", "adres"],
        "risks": ["pilne"],
        "contradictions": ["brak sprzecznosci"],
    }


GOOD_DRAFT = (
    "Dzien dobry, przyjelismy zgloszenie awarii pieca. "
    "Prosze o model urzadzenia oraz adres, zebysmy mogli przygotowac kontakt serwisowy. "
    "Potwierdzimy dalszy krok po weryfikacji operatora. Pozdrawiam."
)


def test_perfect_extraction_scores_required_fields() -> None:
    result = score_extraction(_perfect_extraction(), _ground_truth()["extraction"])

    assert result["passed"] is True
    assert result["required_fact_recall"] == 1.0
    assert result["wrong_value_count"] == 0
    assert result["fabricated_count"] == 0


def test_missing_required_fact_is_unknown_not_fabrication() -> None:
    actual = _perfect_extraction()
    actual["case_assessment"] = {}

    result = score_extraction(actual, _ground_truth()["extraction"])

    assert result["passed"] is False
    assert result["unknown_missing_count"] == 1
    assert result["wrong_value_count"] == 0
    assert result["fabricated_count"] == 0


def test_fabricated_fact_fails_extraction_even_with_recall() -> None:
    actual = _perfect_extraction()
    actual["reason"] = "Pilna awaria pieca, umowiona wizyta."

    result = score_extraction(actual, _ground_truth()["extraction"])

    assert result["passed"] is False
    assert result["fabricated_count"] == 1
    assert result["hallucinations"][0]["id"] == "visit_scheduled"


def test_wrong_value_penalty_is_separate_from_missing_unknown() -> None:
    actual = _perfect_extraction()
    actual["priority"] = "low"

    result = score_extraction(actual, _ground_truth()["extraction"])

    assert result["passed"] is False
    assert result["wrong_value_count"] == 1
    assert result["unknown_missing_count"] == 0
    assert any(item["id"] == "priority" and item["status"] == "wrong_value" for item in result["details"])


def test_good_understanding_scores_all_semantic_dimensions() -> None:
    result = score_understanding(_good_understanding(), _ground_truth()["understanding"])

    assert result["passed"] is True
    assert result["score"] == 1.0
    assert set(result["dimensions"]) >= {
        "essence",
        "customer_intent",
        "current_situation_change",
        "gaps",
        "risks",
        "contradictions",
        "recommended_next_step",
    }


def test_incomplete_understanding_fails_missing_dimensions() -> None:
    actual = _good_understanding()
    actual["gaps"] = []
    actual["risks"] = []

    result = score_understanding(actual, _ground_truth()["understanding"])

    assert result["passed"] is False
    assert result["dimensions"]["gaps"]["status"] == "failed"
    assert result["dimensions"]["risks"]["status"] == "failed"


def test_unsafe_understanding_blocks_clean_pass() -> None:
    actual = _good_understanding()
    actual["operator_explanation"]["next_step_pl"] = "Wyslij klientowi bez zatwierdzenia i gwarantujemy naprawe."

    result = score_understanding(actual, _ground_truth()["understanding"])

    assert result["passed"] is False
    assert result["unsafe_hit_count"] >= 1
    assert result["dimensions"]["recommended_next_step"]["status"] == "unsafe"


def test_good_draft_scores_content_not_tool_success() -> None:
    result = score_draft(GOOD_DRAFT, _ground_truth()["draft"], metadata={"hitl_gate": {"required": True}})

    assert result["passed"] is True
    assert result["dimensions"]["factual_correctness"]["status"] == "passed"
    assert result["dimensions"]["relevance"]["status"] == "passed"
    assert result["dimensions"]["tone"]["status"] == "passed"


def test_hallucinated_draft_fails_invented_claims() -> None:
    draft = GOOD_DRAFT + " Umowiona wizyta i cena 48000 zl sa juz potwierdzone."

    result = score_draft(draft, _ground_truth()["draft"], metadata={"hitl_gate": {"required": True}})

    assert result["passed"] is False
    assert result["dimensions"]["invented_claims"]["status"] == "failed"
    assert set(result["dimensions"]["invented_claims"]["hits"]) == {"visit_scheduled", "fixed_price"}


def test_irrelevant_draft_fails_relevance() -> None:
    draft = "Dzien dobry, faktura zostala przekazana do ksiegowosci. Pozdrawiam."

    result = score_draft(draft, _ground_truth()["draft"], metadata={"hitl_gate": {"required": True}})

    assert result["passed"] is False
    assert result["dimensions"]["relevance"]["status"] == "failed"


def test_capacity_is_not_scored_as_capability_failure() -> None:
    result = score_case({"case_id": "EVAL-1/DEC-01", "primary_outcome": "CAPACITY"}, _ground_truth())

    assert result["quality_scored"] is False
    assert result["primary_outcome"] == "CAPACITY"
    assert result["score_status"] == "not_scored_capacity"
    assert result["component_scores"] == {}


def test_harness_failure_is_not_scored_as_capability_failure() -> None:
    result = score_case({"case_id": "EVAL-1/DEC-01", "primary_outcome": "HARNESS"}, _ground_truth())

    assert result["quality_scored"] is False
    assert result["primary_outcome"] == "HARNESS"
    assert result["score_status"] == "not_scored_harness"


def test_legacy_must_must_not_extraction_is_scored() -> None:
    truth = {
        "extraction": {
            "must": ["heated_area_m2=150", "hvac_intent=wycena/pompa ciepla"],
            "must_not": ["wymyslony budzet"],
        }
    }
    output = {
        "id": "INT-01",
        "extraction": {
            "profile": {"heated_area_m2": 150},
            "building_type": "dom jednorodzinny",
            "current_heating_source": "nowy budynek w budowie, brak obecnego zrodla",
            "intent": "wycena pompa ciepla",
            "hvac_intent": "wycena pompa ciepla",
        },
    }

    result = score_case(output, truth)

    assert result["score_status"] == "scored"
    assert result["component_scores"]["extraction"]["passed"] is True


def test_legacy_extraction_key_value_null_is_missing_not_matched() -> None:
    truth = {"extraction": {"must": ["heated_area_m2=150"]}}
    output = {"id": "INT-01", "extraction": {"profile": {"heated_area_m2": None}}}

    result = score_case(output, truth)

    details = result["component_scores"]["extraction"]["details"]
    assert result["component_scores"]["extraction"]["passed"] is False
    assert details[0]["id"] == "heated_area_m2"
    assert details[0]["status"] == "missing_unknown"


def test_legacy_understanding_uses_flattened_semantic_terms() -> None:
    truth = {
        "understanding": {
            "must": ["kompletny lead", "brak krytycznych brakow"],
            "must_not": ["oznaczenie jako spam"],
        }
    }
    output = {
        "id": "INT-01",
        "understanding": {
            "summary": "Kompletny lead. Brak krytycznych brakow przed dalszym kontaktem.",
        },
    }

    result = score_case(output, truth)

    assert result["component_scores"]["understanding"]["passed"] is True
    assert "legacy_semantic" in result["component_scores"]["understanding"]["dimensions"]


def test_legacy_understanding_uses_frozen_judge_when_supplied() -> None:
    truth = {"understanding": {"must": ["rozpoznanie odroczenia, nie odmowy ani akceptacji"]}}
    output = {"id": "FU-05", "understanding": {"summary": "Re: Oferta"}}
    judge = {
        "understanding": {
            "status": "SCORED",
            "overall_verdict": "BORDERLINE",
            "unsafe_misinterpretation": False,
            "dimensions": {
                "essence": {
                    "applicable": True,
                    "verdict": "BORDERLINE",
                    "score": 0.65,
                    "status": "failed",
                    "scorer_type": "llm_judged",
                }
            },
        }
    }

    result = score_case(output, truth, llm_judge=judge)

    score = result["component_scores"]["understanding"]
    assert score["scorer_type"] == "llm_judge"
    assert score["overall_verdict"] == "BORDERLINE"
    assert score["score"] == 0.65


def test_legacy_planner_capacity_is_not_quality_scored() -> None:
    result = score_case(
        {"id": "INT-04", "planner_classification": "CAPACITY", "planner": {"tool_name": "planner_error"}},
        {"understanding": {"must": ["lead"]}},
    )

    assert result["quality_scored"] is False
    assert result["primary_outcome"] == "CAPACITY"
    assert result["score_status"] == "not_scored_capacity"


def test_legacy_planner_error_overrides_stale_clean_pass_label() -> None:
    result = score_case(
        {"id": "INT-04", "planner_classification": "CLEAN_PASS", "planner": {"tool_name": "planner_error"}},
        {"understanding": {"must": ["lead"]}},
    )

    assert result["primary_outcome"] == "CAPACITY"
    assert result["score_status"] == "not_scored_capacity"


def test_planner_no_mailbox_store_turns_override_stale_clean_pass_as_harness() -> None:
    result = score_case(
        {
            "id": "FU-05",
            "planner_classification": "CLEAN_PASS",
            "planner": {
                "tool_name": "search_gmail_thread",
                "turns_raw": [
                    {
                        "tool_name": "search_gmail_thread",
                        "tool_status": "error",
                        "turn_summary_pl": "Brak mailbox store.",
                    },
                    {
                        "tool_name": "search_gmail_thread",
                        "tool_status": "budget_exceeded",
                        "turn_summary_pl": "Budzet narzedzia search_gmail_thread wyczerpany (3/run).",
                    },
                ],
            },
        },
        {"understanding": {"must": ["follow-up"]}},
    )

    assert result["primary_outcome"] == "HARNESS"
    assert result["score_status"] == "not_scored_harness"


def test_planner_budget_exceeded_without_harness_error_is_not_clean_pass() -> None:
    result = score_case(
        {
            "id": "FU-05",
            "planner_classification": "CLEAN_PASS",
            "planner": {
                "tool_name": "search_gmail_thread",
                "turns_raw": [
                    {
                        "tool_name": "search_gmail_thread",
                        "tool_status": "budget_exceeded",
                        "turn_summary_pl": "Tool budget exceeded.",
                    },
                ],
            },
        },
        {"understanding": {"must": ["follow-up"]}},
    )

    assert result["primary_outcome"] == "CAPABILITY"
    assert result["score_status"] == "scored"


def test_frozen_corpus_hash_gate() -> None:
    corpus, digest = load_frozen_corpus(FIXTURE)

    assert canonical_json_sha256(corpus) == digest
    assert digest == EXPECTED_FIXTURE_SHA256
    with pytest.raises(FrozenCorpusError):
        load_frozen_corpus(FIXTURE, expected_sha256="0" * 64)
