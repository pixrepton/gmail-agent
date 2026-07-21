from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from eval_final_rescore import quality_breakdown, qualification_after_rescore, rescore_final_run, score_final_case  # noqa: E402


def _case_output(**overrides):
    payload = {
        "id": "DRAFT-01",
        "stage_reached": "full",
        "extraction": {"intent": "reply"},
        "understanding": {"summary": "Klient pyta o serwis. Potrzebna odpowiedz."},
        "planner_classification": "CLEAN_PASS",
        "planner": {"turns_raw": []},
        "rubric_scores": {"planner": {"unsafe_non_escalation": False, "correct_escalation_rate": 1.0}},
    }
    payload.update(overrides)
    return payload


def _corpus_case(**ground_truth_overrides):
    ground_truth = {
        "understanding": {"must": ["klient", "odpowiedz"]},
        "draft_expected": True,
        "draft": {"must": ["Dzien dobry"], "relevance_terms": ["serwis"], "must_not": ["umowiona wizyta"]},
    }
    ground_truth.update(ground_truth_overrides)
    return {"id": "DRAFT-01", "ground_truth": ground_truth}


def test_missing_draft_text_is_capture_gap_not_metadata_score() -> None:
    row = score_final_case(_case_output(draft={"draft_enabled": False, "drafts": []}), _corpus_case())

    assert row["primary_outcome"] == "HARNESS"
    assert row["component_status"]["draft"]["status"] == "UNSCORABLE_WITH_PROVEN_CAPTURE_GAP"
    assert row["component_status"]["draft"]["reason"] == "draft_text_missing"


def test_explicit_draft_parse_failure_is_capability_not_capture_gap() -> None:
    row = score_final_case(
        _case_output(
            draft={
                "draft_enabled": False,
                "drafts": [],
                "do_not_send_reasons": ["ReplyDraftResult must be a JSON object."],
                "execution_metadata": {
                    "fallback_used": True,
                    "parse_status": "fallback",
                    "error": "ReplyDraftResult must be a JSON object.",
                },
            }
        ),
        _corpus_case(),
    )

    assert row["primary_outcome"] == "CAPABILITY"
    assert row["component_status"]["draft"]["status"] == "DRAFT_GENERATION_FAILURE"
    assert row["component_status"]["draft"]["scored"] is True
    assert row["component_status"]["draft"]["passed"] is False
    assert row["capture_gap"] == []


def test_raw_draft_response_json_can_be_scored_after_contract_validation_failure() -> None:
    row = score_final_case(
        _case_output(
            draft={
                "draft_enabled": False,
                "drafts": [],
                "execution_metadata": {
                    "parse_status": "pydantic_failed",
                    "response_json": {
                        "short_operational": {
                            "body": "Dzien dobry, odpowiadamy w sprawie serwisu. Nie mamy jeszcze umowionej wizyty."
                        }
                    },
                },
            }
        ),
        _corpus_case(),
    )

    assert row["component_status"]["draft"]["status"] == "SCORED"
    assert not row["capture_gap"]


def test_not_applicable_draft_is_not_missing() -> None:
    row = score_final_case(_case_output(draft=None), _corpus_case(draft_expected=False, draft={}))

    assert row["component_status"]["draft"]["status"] == "NOT_APPLICABLE"
    assert not row["capture_gap"]


def test_duplicate_rag_guard_is_nonblocking_for_quality_scoring() -> None:
    output = _case_output(
        draft={"drafts": [{"body": "Dzien dobry, pytanie o serwis.", "variant": "short"}]},
        planner={
            "turns_raw": [
                {
                    "tool_name": "search_rag_knowledge",
                    "tool_status": "error",
                    "turn_summary_pl": "duplicate_rag_research_stop: Research RAG objective already covered in this run.",
                }
            ]
        },
    )
    row = score_final_case(output, _corpus_case())

    assert row["base_primary_outcome"] == "CLEAN_PASS"
    assert row["nonblocking_tool_errors"][0]["tool_name"] == "search_rag_knowledge"


def test_budget_exceeded_remains_capability() -> None:
    output = _case_output(
        draft={"drafts": [{"body": "Dzien dobry, pytanie o serwis.", "variant": "short"}]},
        planner={"turns_raw": [{"tool_name": "search_rag_knowledge", "tool_status": "budget_exceeded"}]},
    )
    row = score_final_case(output, _corpus_case())

    assert row["base_primary_outcome"] == "CAPABILITY"
    assert row["primary_outcome"] == "CAPABILITY"


def test_quality_failure_overrides_stale_clean_pass() -> None:
    output = _case_output(draft={"drafts": [{"body": "Faktura przekazana do ksiegowosci.", "variant": "short"}]})
    row = score_final_case(output, _corpus_case())

    assert row["primary_outcome"] == "CAPABILITY"
    assert row["component_status"]["draft"]["scored"] is True
    assert row["component_status"]["draft"]["passed"] is False


def test_capture_gap_blocks_qualification_and_points_to_fresh_run_a() -> None:
    rows = {
        "cases": [
            score_final_case(_case_output(draft={"draft_enabled": False, "drafts": []}), _corpus_case()),
        ]
    }
    breakdown = quality_breakdown(rows)
    qualification = qualification_after_rescore(rows, breakdown)

    assert qualification["verdict"] == "NOT QUALIFIED — CAPTURE GAP"
    assert qualification["next_step"] == "FRESH RUN-A"


def test_understanding_judge_result_scores_legacy_semantics() -> None:
    output = _case_output(draft={"drafts": [{"body": "Dzien dobry, pytanie o serwis.", "variant": "short"}]})
    judge = {
        "status": "SCORED",
        "overall_verdict": "CLEAR_PASS",
        "unsafe_misinterpretation": False,
        "dimensions": {"essence": {"applicable": True, "verdict": "PASS", "score": 1.0}},
    }

    row = score_final_case(output, _corpus_case(), understanding_judge=judge)

    assert row["component_status"]["understanding"]["status"] == "SCORED"
    assert row["component_scores"]["understanding"]["scorer_type"] == "llm_judge"


def test_judge_error_is_measurement_blocker_not_capability_failure() -> None:
    output = _case_output(draft={"drafts": [{"body": "Dzien dobry, pytanie o serwis.", "variant": "short"}]})
    judge = {"status": "JUDGE_UNAVAILABLE", "case_id": "DRAFT-01"}

    row = score_final_case(output, _corpus_case(), understanding_judge=judge)
    breakdown = quality_breakdown({"cases": [row]})
    qualification = qualification_after_rescore({"cases": [row]}, breakdown)

    assert row["component_status"]["understanding"]["status"] == "JUDGE_UNAVAILABLE"
    assert qualification["verdict"] == "NOT QUALIFIED — JUDGE ERROR"


def test_missing_frozen_judge_row_does_not_fallback_to_deterministic() -> None:
    output = _case_output(draft={"drafts": [{"body": "Dzien dobry, pytanie o serwis.", "variant": "short"}]})
    output["understanding"] = {"summary_pl": "Pytanie o serwis"}
    corpus = {"cases": [_corpus_case()]}

    rescored = rescore_final_run({"cases": [output]}, corpus, understanding_judge={"cases": []})
    row = rescored["cases"][0]

    assert row["component_status"]["understanding"]["status"] == "JUDGE_UNAVAILABLE"
    assert row["component_status"]["understanding"]["reason"] == "understanding_judge_unresolved"


def test_rescore_final_run_maps_judge_by_case_id() -> None:
    results = {"cases": [_case_output(id="DRAFT-01", draft={"drafts": [{"body": "Dzien dobry, pytanie o serwis."}]})]}
    corpus = {"cases": [_corpus_case()]}
    judge_results = {
        "cases": [
            {
                "case_id": "DRAFT-01",
                "status": "SCORED",
                "overall_verdict": "CLEAR_PASS",
                "unsafe_misinterpretation": False,
                "dimensions": {"essence": {"applicable": True, "verdict": "PASS", "score": 1.0}},
            }
        ]
    }

    rescored = rescore_final_run(results, corpus, understanding_judge=judge_results)

    assert rescored["cases"][0]["component_scores"]["understanding"]["scorer_type"] == "llm_judge"
