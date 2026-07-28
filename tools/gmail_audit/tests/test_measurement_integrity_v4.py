from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import eval_final_rescore_versioned as versioned  # noqa: E402


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "measurement_contract_v1"
CORPUS_V1 = FIXTURE_DIR / "corpus-v1.json"
FROZEN_CAPTURE = FIXTURE_DIR / "fresh-full38-results.json"
FROZEN_JUDGE = FIXTURE_DIR / "FRESH-FINAL-judge.json"
FIXED_TIMESTAMP = "2026-07-24T00:00:00Z"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _row(output: dict, case_id: str) -> dict:
    return next(row for row in output["cases"] if row["case_id"] == case_id)


@pytest.fixture(scope="module")
def frozen_outputs() -> dict:
    results = _load_json(FROZEN_CAPTURE)
    corpus = _load_json(CORPUS_V1)
    judge = _load_json(FROZEN_JUDGE)
    common = {
        "understanding_judge": judge,
        "source_sut_capture_sha256": _file_sha256(FROZEN_CAPTURE),
        "frozen_judge_result_sha256": _file_sha256(FROZEN_JUDGE),
        "timestamp": FIXED_TIMESTAMP,
    }
    return {
        version: versioned.rescore_final_run_versioned(
            results,
            corpus,
            measurement_contract_version=version,
            **common,
        )
        for version in ("v1", "v2", "v3", "v4")
    }


def test_v4_contract_is_explicit() -> None:
    assert versioned.CONTRACT_V4 == "v4"
    assert "v4" in versioned.SUPPORTED_CONTRACTS


def test_ground_truth_only_applicable_dimensions_ignores_output_non_emptiness() -> None:
    # Ground truth never mentions risk -- output non-emptiness must not matter,
    # because the frozen-judge-only variant of this function does not even accept
    # an actual_understanding argument to echo.
    case_no_risk_signal = {
        "ground_truth": {"understanding": {"must": ["rozpoznanie nowego zapytania"], "must_not": []}},
        "input": {},
    }
    assert "risks" not in versioned._ground_truth_only_applicable_dimensions(case_no_risk_signal)

    # Ground truth genuinely calls for a risk assessment -- must stay applicable
    # regardless of whether the SUT said anything.
    case_with_risk_signal = {
        "ground_truth": {"understanding": {"must": ["oznaczenie ryzyka pilnego zgloszenia"], "must_not": []}},
        "input": {},
    }
    assert "risks" in versioned._ground_truth_only_applicable_dimensions(case_with_risk_signal)


def _judge_row(*, applicable: bool, verdict: str = "BORDERLINE") -> dict:
    return {
        "case_id": "SYNTHETIC",
        "status": "SCORED",
        "overall_verdict": verdict if applicable else "CLEAR_PASS",
        "unsafe_misinterpretation": False,
        "dimensions": {
            "essence": {"applicable": True, "verdict": "PASS", "reason_code": "", "evidence": "", "score": 1.0},
            "risks": {
                "applicable": applicable,
                "verdict": verdict,
                "reason_code": "model_flagged_it" if applicable else "not_applicable",
                "evidence": "klient wspomnial o mozliwym problemie",
                "score": 0.65 if applicable else 1.0,
            },
        },
    }


def test_apply_v4_risk_grounding_narrows_output_only_applicability() -> None:
    corpus_case_without_risk_ground_truth = {
        "ground_truth": {"understanding": {"must": ["rozpoznanie zapytania o oferte"], "must_not": []}},
        "input": {},
    }
    judge_row = _judge_row(applicable=True, verdict="BORDERLINE")

    adjusted = versioned._apply_v4_risk_grounding(judge_row, corpus_case_without_risk_ground_truth)

    assert adjusted is not judge_row
    assert adjusted["dimensions"]["risks"]["applicable"] is False
    assert adjusted["dimensions"]["risks"]["reason_code"] == "not_applicable_v4_ground_truth_only"
    assert adjusted["dimensions"]["risks"]["v1_verdict_superseded"] == {
        "applicable": True,
        "verdict": "BORDERLINE",
        "reason_code": "model_flagged_it",
    }
    # essence was already PASS and risks is now excluded -> overall must recompute to CLEAR_PASS.
    assert adjusted["overall_verdict"] == "CLEAR_PASS"
    assert adjusted["score"] == 1.0
    assert adjusted["passed"] is True
    assert adjusted["v4_risk_applicability_corrected"] == "narrowed"

    # The original captured judge row is untouched (v1/v2/v3 stay reproducible).
    assert judge_row["dimensions"]["risks"]["applicable"] is True


def test_apply_v4_risk_grounding_never_fabricates_missing_real_judgment() -> None:
    corpus_case_requiring_risk = {
        "ground_truth": {"understanding": {"must": ["oznaczenie ryzyka pilnego przypadku"], "must_not": []}},
        "input": {},
    }
    judge_row = _judge_row(applicable=False)

    flagged = versioned._apply_v4_risk_grounding(judge_row, corpus_case_requiring_risk)

    assert flagged["v4_risk_applicability_corrected"] == "needs_rejudge"
    # No verdict is invented: the dimension itself is untouched.
    assert flagged["dimensions"]["risks"] == judge_row["dimensions"]["risks"]
    assert flagged["overall_verdict"] == judge_row["overall_verdict"]


def test_apply_v4_risk_grounding_leaves_genuinely_applicable_alone() -> None:
    corpus_case_requiring_risk = {
        "ground_truth": {"understanding": {"must": ["oznaczenie ryzyka pilnego przypadku"], "must_not": []}},
        "input": {},
    }
    judge_row = _judge_row(applicable=True, verdict="BORDERLINE")

    result = versioned._apply_v4_risk_grounding(judge_row, corpus_case_requiring_risk)

    assert result == judge_row
    assert "v4_risk_applicability_corrected" not in result


def test_apply_v4_risk_grounding_passes_through_unavailable_judge() -> None:
    unavailable = {"case_id": "SYNTHETIC", "status": "JUDGE_UNAVAILABLE"}
    assert versioned._apply_v4_risk_grounding(unavailable, {}) == unavailable
    assert versioned._apply_v4_risk_grounding(None, {}) is None


def test_v4_narrows_doc03_and_ctx01_risks_without_changing_the_total(frozen_outputs: dict) -> None:
    # Confirmed against the real frozen fixture: DOC-03 and CTX-01 are the only two
    # cases where 'risks' was applicable purely via output non-emptiness (ground
    # truth never mentions risk for either), and both also carry an independently
    # applicable 'gaps: BORDERLINE' -- so narrowing 'risks' must not silently
    # inflate the score; v4's clean_pass total must equal v3's.
    v3_doc03 = _row(frozen_outputs["v3"], "DOC-03")
    v4_doc03 = _row(frozen_outputs["v4"], "DOC-03")
    v3_ctx01 = _row(frozen_outputs["v3"], "CTX-01")
    v4_ctx01 = _row(frozen_outputs["v4"], "CTX-01")

    for v3_row, v4_row in ((v3_doc03, v4_doc03), (v3_ctx01, v4_ctx01)):
        v3_dims = v3_row["component_scores"]["understanding"]["dimensions"]
        v4_dims = v4_row["component_scores"]["understanding"]["dimensions"]
        assert v3_dims["risks"]["applicable"] is True
        assert v4_dims["risks"]["applicable"] is False
        assert v4_dims["risks"]["reason_code"] == "not_applicable_v4_ground_truth_only"
        # gaps was never touched by the v4 correction -- stays whatever v3 judged.
        assert v4_dims["gaps"]["applicable"] == v3_dims["gaps"]["applicable"]
        assert v4_dims["gaps"]["verdict"] == v3_dims["gaps"]["verdict"]
        # Both cases stay non-CLEAR_PASS because gaps alone still keeps them BORDERLINE.
        assert v3_row["primary_outcome"] == v4_row["primary_outcome"]

    assert frozen_outputs["v4"]["summary"]["clean_pass_cases"] == frozen_outputs["v3"]["summary"]["clean_pass_cases"]


def test_v4_manifest_declares_measurement_change_not_product_change(frozen_outputs: dict) -> None:
    v3_manifest = frozen_outputs["v3"]["measurement_contract_manifest"]
    v4_manifest = frozen_outputs["v4"]["measurement_contract_manifest"]

    assert v4_manifest["contract_changed_from"] == "v3"
    assert v4_manifest["ground_truth_changed_from_v3"] is False
    assert v4_manifest["ground_truth_sha256"] == v3_manifest["ground_truth_sha256"]
    with pytest.raises(versioned.MeasurementContractComparisonError):
        versioned.assert_measurement_outputs_comparable(frozen_outputs["v3"], frozen_outputs["v4"])


def test_v4_preserves_v3_ground_truth_exactly() -> None:
    corpus_v1 = _load_json(CORPUS_V1)
    corpus_v3 = versioned.build_contract_v3_corpus(corpus_v1)
    corpus_v4 = versioned.build_contract_v4_corpus(corpus_v1)

    v3_ground_truth = {
        str(case.get("id") or case.get("case_id")): case.get("ground_truth") for case in corpus_v3["cases"]
    }
    v4_ground_truth = {
        str(case.get("id") or case.get("case_id")): case.get("ground_truth") for case in corpus_v4["cases"]
    }
    assert v4_ground_truth == v3_ground_truth


def test_v1_v2_v3_totals_are_unchanged_by_v4_existing(frozen_outputs: dict) -> None:
    # v4 must be purely additive -- the frozen v1/v2/v3 totals from
    # test_measurement_integrity_v3.py must reproduce identically here.
    assert frozen_outputs["v1"]["summary"]["clean_pass_cases"] == 22
    assert frozen_outputs["v2"]["summary"]["clean_pass_cases"] == 26
    assert frozen_outputs["v3"]["summary"]["clean_pass_cases"] == 27
