from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import eval_final_rescore_versioned as versioned  # noqa: E402


FIXED_TIMESTAMP = "2026-08-09T00:00:00Z"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "measurement_contract_v1"
CORPUS_V1 = FIXTURE_DIR / "corpus-v1.json"


def _ctx04_corpus() -> dict:
    return json.loads(CORPUS_V1.read_text(encoding="utf-8-sig"))


def _ctx04_results(*, with_budget_context: bool) -> dict:
    if with_budget_context:
        understanding = {
            "customer_intent_pl": (
                "Klient potwierdza gotowosc do otrzymania finalnej propozycji; "
                "w kontekscie sprawy nadal obowiazuje budzet 40-50 tys. PLN."
            ),
            "thread_delta": {
                "prior_known_state": [
                    {
                        "fact_key": "budget_pln_estimated",
                        "label_pl": "budzet (PLN)",
                        "value": "40000-50000",
                    }
                ],
                "prior_known_state_pl": "budzet (PLN): 40000-50000",
            },
        }
    else:
        understanding = {
            "customer_intent_pl": "Klient prosi o finalna propozycje po ustaleniach technicznych.",
            "thread_delta": {"prior_known_state": []},
        }
    return {"mode": "synthetic", "cases": [{"id": "CTX-04", "stage_reached": "full", "understanding": understanding}]}


def _ctx04_false_negative_judge() -> dict:
    return {
        "cases": [
            {
                "case_id": "CTX-04",
                "status": "SCORED",
                "overall_verdict": "CLEAR_FAIL",
                "unsafe_misinterpretation": False,
                "score": 0.0,
                "passed": False,
                "dimensions": {
                    "essence": {
                        "applicable": True,
                        "verdict": "FAIL",
                        "reason_code": "missing_budget_context",
                        "evidence": "no budget mention",
                        "score": 0.0,
                        "status": "failed",
                    },
                    "intent": {
                        "applicable": False,
                        "verdict": "PASS",
                        "reason_code": "not_applicable",
                        "evidence": "",
                        "score": 1.0,
                        "status": "passed",
                    },
                    "current_state_change": {
                        "applicable": True,
                        "verdict": "FAIL",
                        "reason_code": "no_proposal_details",
                        "evidence": "no proposal provided",
                        "score": 0.0,
                        "status": "failed",
                    },
                    "gaps": {
                        "applicable": False,
                        "verdict": "PASS",
                        "reason_code": "not_applicable",
                        "evidence": "",
                        "score": 1.0,
                        "status": "passed",
                    },
                    "risks": {
                        "applicable": False,
                        "verdict": "PASS",
                        "reason_code": "not_applicable",
                        "evidence": "",
                        "score": 1.0,
                        "status": "passed",
                    },
                    "contradictions": {
                        "applicable": False,
                        "verdict": "PASS",
                        "reason_code": "not_applicable",
                        "evidence": "",
                        "score": 1.0,
                        "status": "passed",
                    },
                    "recommended_next_step": {
                        "applicable": False,
                        "verdict": "PASS",
                        "reason_code": "not_applicable",
                        "evidence": "",
                        "score": 1.0,
                        "status": "passed",
                    },
                },
            }
        ]
    }


def _row(output: dict, case_id: str = "CTX-04") -> dict:
    return next(row for row in output["cases"] if row["case_id"] == case_id)


def _rescore(version: str, *, with_budget_context: bool = True) -> tuple[dict, dict]:
    results = _ctx04_results(with_budget_context=with_budget_context)
    original_results = copy.deepcopy(results)
    rescored = versioned.rescore_final_run_versioned(
        results,
        _ctx04_corpus(),
        understanding_judge=_ctx04_false_negative_judge(),
        measurement_contract_version=version,
        timestamp=FIXED_TIMESTAMP,
    )
    assert results == original_results
    return rescored, _row(rescored)


def test_v5_contract_is_explicit() -> None:
    assert versioned.CONTRACT_V5 == "v5"
    assert "v5" in versioned.SUPPORTED_CONTRACTS


def test_v5_adjudicates_ctx04_budget_context_false_negative() -> None:
    v4, v4_row = _rescore("v4")
    v5, v5_row = _rescore("v5")

    assert v4["summary"]["outcomes"] == {"CAPABILITY": 1}
    assert v4_row["primary_outcome"] == "CAPABILITY"
    assert v5["summary"]["outcomes"] == {"CLEAN_PASS": 1}
    assert v5_row["primary_outcome"] == "CLEAN_PASS"
    assert v5_row["v5_budget_context_adjudication"] == {
        "status": "corrected",
        "dimensions": ["essence", "current_state_change"],
        "budget_context_present": True,
        "runtime_changed": False,
    }

    dims = v5_row["component_scores"]["understanding"]["dimensions"]
    assert dims["essence"]["reason_code"] == "budget_context_present_v5_adjudication"
    assert dims["current_state_change"]["reason_code"] == "understanding_proposal_request_present_v5_adjudication"
    assert dims["essence"]["v5_verdict_superseded"]["reason_code"] == "missing_budget_context"


def test_v5_does_not_rescue_missing_budget_context() -> None:
    _, row = _rescore("v5", with_budget_context=False)

    assert row["primary_outcome"] == "CAPABILITY"
    assert "v5_budget_context_adjudication" not in row


def test_v5_manifest_declares_eval_only_change() -> None:
    _, row = _rescore("v5")
    del row
    v4_corpus = versioned.build_contract_v4_corpus(_ctx04_corpus())
    v5_corpus = versioned.build_contract_v5_corpus(_ctx04_corpus())
    rescored, _ = _rescore("v5")
    manifest = rescored["measurement_contract_manifest"]

    assert manifest["contract_changed_from"] == "v4"
    assert manifest["ground_truth_changed_from_v4"] is False
    assert {
        str(case.get("id") or case.get("case_id")): case.get("ground_truth") for case in v4_corpus["cases"]
    } == {
        str(case.get("id") or case.get("case_id")): case.get("ground_truth") for case in v5_corpus["cases"]
    }
    with pytest.raises(versioned.MeasurementContractComparisonError):
        versioned.assert_measurement_outputs_comparable(
            versioned.rescore_final_run_versioned(
                _ctx04_results(with_budget_context=True),
                _ctx04_corpus(),
                understanding_judge=_ctx04_false_negative_judge(),
                measurement_contract_version="v4",
                timestamp=FIXED_TIMESTAMP,
            ),
            rescored,
        )
