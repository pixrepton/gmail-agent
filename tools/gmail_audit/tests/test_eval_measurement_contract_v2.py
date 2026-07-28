from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import eval_final_rescore as v1_rescore  # noqa: E402
from eval_final_rescore_versioned import (  # noqa: E402
    CONTRACT_V1,
    CONTRACT_V2,
    MeasurementContractComparisonError,
    assert_measurement_outputs_comparable,
    build_contract_v2_corpus,
    rescore_final_run_versioned,
)


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "measurement_contract_v1"
CORPUS_V1 = FIXTURE_DIR / "corpus-v1.json"
DIAGNOSTIC_DIR = FIXTURE_DIR
FROZEN_CAPTURE = DIAGNOSTIC_DIR / "fresh-full38-results.json"
FROZEN_JUDGE = DIAGNOSTIC_DIR / "FRESH-FINAL-judge.json"
FROZEN_RESCORE = DIAGNOSTIC_DIR / "FRESH-FINAL-rescore.json"
FIXTURE_MANIFEST = FIXTURE_DIR / "fixture-manifest.json"
FIXED_TIMESTAMP = "2026-07-24T00:00:00Z"


def _load_json(path: Path) -> dict:
    assert path.is_file(), f"missing frozen artifact: {path}"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_portable_frozen_fixture_matches_hash_lock() -> None:
    manifest = _load_json(FIXTURE_MANIFEST)

    assert manifest["status"] == "FROZEN_BYTE_IDENTICAL_COPY"
    for name, expected in manifest["files"].items():
        path = FIXTURE_DIR / name
        assert path.stat().st_size == expected["bytes"]
        assert _file_sha256(path) == expected["sha256"]


@pytest.fixture(scope="module")
def frozen_inputs() -> dict:
    return {
        "results": _load_json(FROZEN_CAPTURE),
        "corpus": _load_json(CORPUS_V1),
        "judge": _load_json(FROZEN_JUDGE),
        "original_rescore": _load_json(FROZEN_RESCORE),
    }


@pytest.fixture(scope="module")
def versioned_outputs(frozen_inputs: dict) -> dict:
    common = {
        "understanding_judge": frozen_inputs["judge"],
        "source_sut_capture_sha256": _file_sha256(FROZEN_CAPTURE),
        "frozen_judge_result_sha256": _file_sha256(FROZEN_JUDGE),
        "timestamp": FIXED_TIMESTAMP,
    }
    return {
        CONTRACT_V1: rescore_final_run_versioned(
            frozen_inputs["results"],
            frozen_inputs["corpus"],
            measurement_contract_version=CONTRACT_V1,
            **common,
        ),
        CONTRACT_V2: rescore_final_run_versioned(
            frozen_inputs["results"],
            frozen_inputs["corpus"],
            measurement_contract_version=CONTRACT_V2,
            **common,
        ),
    }


def _row(rescored: dict, case_id: str) -> dict:
    for row in rescored["cases"]:
        if row["case_id"] == case_id:
            return row
    raise AssertionError(f"case not found: {case_id}")


def _negative_rescore_row(*, tool_name: str, summary: str, file_id: str, ground_truth: dict | None = None) -> dict:
    result = {
        "cases": [
            {
                "id": "NEG-DRIVE",
                "planner_classification": "CLEAN_PASS",
                "planner": {
                    "turns_raw": [
                        {
                            "tool_name": tool_name,
                            "tool_status": "error",
                            "tool_args_redacted": {"file_id": file_id},
                            "turn_summary_pl": summary,
                        }
                    ]
                },
            }
        ]
    }
    corpus = _load_json(CORPUS_V1)
    corpus["cases"].append({"id": "NEG-DRIVE", "ground_truth": ground_truth or {}})
    rescored = rescore_final_run_versioned(
        result,
        corpus,
        measurement_contract_version=CONTRACT_V2,
        timestamp=FIXED_TIMESTAMP,
    )
    return _row(rescored, "NEG-DRIVE")


def test_measurement_v1_reproduces_22_of_38(versioned_outputs: dict) -> None:
    rescored = versioned_outputs[CONTRACT_V1]

    assert rescored["measurement_contract_version"] == CONTRACT_V1
    assert rescored["summary"]["cases"] == 38
    assert rescored["summary"]["clean_pass_cases"] == 22
    assert rescored["summary"]["outcomes"] == {"CAPABILITY": 16, "CLEAN_PASS": 22}


def test_measurement_v2_recovers_ctx05(versioned_outputs: dict) -> None:
    assert _row(versioned_outputs[CONTRACT_V1], "CTX-05")["primary_outcome"] == "CAPABILITY"
    row = _row(versioned_outputs[CONTRACT_V2], "CTX-05")

    assert row["primary_outcome"] == "CLEAN_PASS"
    assert row["nonblocking_tool_errors"][0]["tool_name"] == "read_google_drive_file"


def test_measurement_v2_recovers_mi01(versioned_outputs: dict) -> None:
    assert _row(versioned_outputs[CONTRACT_V1], "MI-01")["primary_outcome"] == "CAPABILITY"
    row = _row(versioned_outputs[CONTRACT_V2], "MI-01")

    assert row["primary_outcome"] == "CLEAN_PASS"
    assert row["nonblocking_tool_errors"][0]["contract_rule"] == "v2_read_google_drive_file_harness_fabricated_404"


def test_measurement_v2_recovers_new01(versioned_outputs: dict) -> None:
    v1_row = _row(versioned_outputs[CONTRACT_V1], "NEW-01")
    v2_row = _row(versioned_outputs[CONTRACT_V2], "NEW-01")

    assert v1_row["primary_outcome"] == "CAPABILITY"
    assert v1_row["component_scores"]["draft"]["dimensions"]["factual_correctness"]["status"] == "failed"
    assert v2_row["primary_outcome"] == "CLEAN_PASS"
    assert v2_row["component_scores"]["draft"]["dimensions"]["tone"]["status"] == "passed"


def test_measurement_v2_recovers_new02(versioned_outputs: dict) -> None:
    v1_row = _row(versioned_outputs[CONTRACT_V1], "NEW-02")
    v2_row = _row(versioned_outputs[CONTRACT_V2], "NEW-02")

    assert v1_row["primary_outcome"] == "CAPABILITY"
    assert v1_row["component_scores"]["extraction"]["details"][0]["status"] == "wrong_value"
    assert v2_row["primary_outcome"] == "CLEAN_PASS"
    assert v2_row["component_scores"]["extraction"]["passed"] is True


def test_measurement_v2_expected_total_is_26_of_38(versioned_outputs: dict) -> None:
    rescored = versioned_outputs[CONTRACT_V2]

    assert rescored["summary"]["cases"] == 38
    assert rescored["summary"]["clean_pass_cases"] == 26
    assert rescored["summary"]["outcomes"] == {"CAPABILITY": 12, "CLEAN_PASS": 26}


def test_measurement_v1_remains_unchanged(frozen_inputs: dict) -> None:
    rescored = v1_rescore.rescore_final_run(
        frozen_inputs["results"],
        frozen_inputs["corpus"],
        understanding_judge=frozen_inputs["judge"],
    )

    assert rescored == frozen_inputs["original_rescore"]


def test_drive_auth_error_remains_blocking() -> None:
    row = _negative_rescore_row(
        tool_name="read_google_drive_file",
        file_id="case_recovery_NEG_chunk_0",
        summary="Drive read/parse failed: Drive API request failed (401): Unauthorized: case_recovery_NEG_chunk_0.",
    )

    assert row["primary_outcome"] == "CAPABILITY"
    assert row["nonblocking_tool_errors"] == []


def test_drive_permission_error_remains_blocking() -> None:
    row = _negative_rescore_row(
        tool_name="read_google_drive_file",
        file_id="case_recovery_NEG_chunk_0",
        summary="Drive read/parse failed: Drive API request failed (403): Permission denied: case_recovery_NEG_chunk_0.",
    )

    assert row["primary_outcome"] == "CAPABILITY"
    assert row["nonblocking_tool_errors"] == []


def test_drive_timeout_remains_blocking() -> None:
    row = _negative_rescore_row(
        tool_name="read_google_drive_file",
        file_id="case_recovery_NEG_chunk_0",
        summary="Drive read/parse failed: Drive API request failed: timeout while reading case_recovery_NEG_chunk_0.",
    )

    assert row["primary_outcome"] == "CAPABILITY"
    assert row["nonblocking_tool_errors"] == []


def test_required_drive_document_404_remains_blocking() -> None:
    row = _negative_rescore_row(
        tool_name="read_google_drive_file",
        file_id="case_recovery_NEG_chunk_0",
        summary="Drive read/parse failed: Drive API request failed (404): File not found: case_recovery_NEG_chunk_0.",
        ground_truth={"required_drive_documents": ["case_recovery_NEG_chunk_0"]},
    )

    assert row["primary_outcome"] == "CAPABILITY"
    assert row["nonblocking_tool_errors"] == []


def test_other_tool_404_remains_blocking() -> None:
    row = _negative_rescore_row(
        tool_name="fetch_customer_document",
        file_id="case_recovery_NEG_chunk_0",
        summary="Drive read/parse failed: Drive API request failed (404): File not found: case_recovery_NEG_chunk_0.",
    )

    assert row["primary_outcome"] == "CAPABILITY"
    assert row["nonblocking_tool_errors"] == []


def test_measurement_manifest_records_version_and_hashes(versioned_outputs: dict) -> None:
    manifest = versioned_outputs[CONTRACT_V2]["measurement_contract_manifest"]

    assert manifest["measurement_contract_version"] == CONTRACT_V2
    assert manifest["qualification_threshold"] == 34
    assert manifest["timestamp"] == FIXED_TIMESTAMP
    assert manifest["source_sut_capture_sha256"] == _file_sha256(FROZEN_CAPTURE)
    for key in ("corpus_sha256", "scorer_sha256", "ground_truth_sha256", "manifest_sha256"):
        assert isinstance(manifest[key], str)
        assert len(manifest[key]) == 64


def test_v1_and_v2_outputs_cannot_be_silently_compared(versioned_outputs: dict) -> None:
    with pytest.raises(MeasurementContractComparisonError):
        assert_measurement_outputs_comparable(versioned_outputs[CONTRACT_V1], versioned_outputs[CONTRACT_V2])


def test_contract_v2_changes_only_target_ground_truth_entries(frozen_inputs: dict) -> None:
    v2_corpus = build_contract_v2_corpus(frozen_inputs["corpus"])
    changed = []
    for before, after in zip(frozen_inputs["corpus"]["cases"], v2_corpus["cases"]):
        before_id = str(before.get("id") or before.get("case_id"))
        after_id = str(after.get("id") or after.get("case_id"))
        assert before_id == after_id
        if before.get("ground_truth") != after.get("ground_truth"):
            changed.append(before_id)

    assert sorted(changed) == ["NEW-01", "NEW-02"]
