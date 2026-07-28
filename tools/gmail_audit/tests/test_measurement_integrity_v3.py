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
from eval_measurement_manifest import SCORER_FILES  # noqa: E402
from eval_measurement_scoring import _find_terms  # noqa: E402
from eval_understanding_judge import _compact_understanding_output  # noqa: E402


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "measurement_contract_v1"
CORPUS_V1 = FIXTURE_DIR / "corpus-v1.json"
CORPUS_V2 = FIXTURE_DIR / "corpus-v2.json"
FROZEN_CAPTURE = FIXTURE_DIR / "fresh-full38-results.json"
FROZEN_JUDGE = FIXTURE_DIR / "FRESH-FINAL-judge.json"
FROZEN_V2_RESCORE = FIXTURE_DIR / "FROZEN-v2-rescore.json"
FIXED_TIMESTAMP = "2026-07-24T00:00:00Z"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        for version in ("v1", "v2", "v3")
    }


def _row(output: dict, case_id: str) -> dict:
    return next(row for row in output["cases"] if row["case_id"] == case_id)


def test_v3_contract_is_explicit() -> None:
    assert getattr(versioned, "CONTRACT_V3", None) == "v3"
    assert "v3" in versioned.SUPPORTED_CONTRACTS


def test_v3_strict_matching_catches_bounded_gwarantowac_inflection() -> None:
    hits = _find_terms("Gwarantuje najnizsza cene.", ["gwarantujemy"], strict=True)

    assert hits == ["gwarantujemy"]


def test_v3_strict_matching_does_not_restore_generic_five_character_stemming() -> None:
    hits = _find_terms(
        "Postaramy sie umowic wizyte w najblizszym terminie.",
        ["umowiona wizyta"],
        strict=True,
    )

    assert hits == []


def test_judge_compactor_surfaces_divergent_alias_with_canonical_precedence() -> None:
    compact = _compact_understanding_output(
        {
            "summary_pl": "Kanoniczne podsumowanie.",
            "situation_summary_pl": "Rozbiezny alias.",
        }
    )

    assert compact["summary_pl"] == "Kanoniczne podsumowanie."
    assert compact["measurement_diagnostics"] == [
        {
            "code": "divergent_alias",
            "canonical_field": "summary_pl",
            "alias_field": "situation_summary_pl",
            "precedence": "canonical",
        }
    ]


def test_judge_compactor_promotes_alias_only_when_canonical_field_is_absent() -> None:
    compact = _compact_understanding_output({"current_customer_intent": "Klient prosi o wycene."})

    assert compact["customer_intent_pl"] == "Klient prosi o wycene."
    assert compact["measurement_diagnostics"] == [
        {
            "code": "alias_promoted",
            "canonical_field": "customer_intent_pl",
            "alias_field": "current_customer_intent",
            "precedence": "alias_when_canonical_absent",
        }
    ]


def test_top_level_manifest_hashes_versioned_rescore_entrypoint() -> None:
    assert "eval_final_rescore_versioned.py" in SCORER_FILES


def test_v3_exposes_namespace_aware_evidence_diagnostic() -> None:
    diagnostic = getattr(versioned, "diagnose_evidence_namespace", None)

    assert callable(diagnostic)
    assert diagnostic(
        [{"signal_id": "sig-1", "message_id": "msg-1"}],
        canonical_signal_id="sig-1",
        source_message_id="msg-1",
    ) == {
        "status": "correlated",
        "reason_codes": [],
        "canonical_signal_id": "sig-1",
        "source_message_id": "msg-1",
    }


def test_namespace_diagnostic_rejects_mixed_or_foreign_ids() -> None:
    diagnostic = versioned.diagnose_evidence_namespace(
        [
            {"signal_id": "sig-foreign", "message_id": "msg-1"},
            {"signal_id": "sig-1", "message_id": "msg-foreign"},
        ],
        canonical_signal_id="sig-1",
        source_message_id="msg-1",
    )

    assert diagnostic["status"] == "namespace_mismatch"
    assert diagnostic["reason_codes"] == ["ids_not_correlated_in_same_evidence_ref"]


def test_generic_source_id_is_not_reinterpreted_as_canonical_signal_id() -> None:
    diagnostic = versioned.diagnose_evidence_namespace(
        [{"source_id": "sig-1", "message_id": "msg-1"}],
        canonical_signal_id="sig-1",
        source_message_id="msg-1",
    )

    assert diagnostic["status"] == "not_evaluable"
    assert diagnostic["reason_codes"] == [
        "missing_explicit_signal_id_in_evidence_ref",
        "ambiguous_source_id_namespace",
    ]


def test_status_conflict_without_refs_is_reported_as_missing_evidence() -> None:
    diagnostics = versioned._case_evidence_namespace_diagnostics(
        {
            "signal_id": "sig-1",
            "message_id": "msg-1",
            "understanding": {
                "conflicting_facts": [
                    {
                        "type": "status_conflict",
                        "fact_key": "case_status",
                        "source_refs": [],
                    }
                ]
            },
        }
    )

    assert diagnostics == [
        {
            "status": "missing_evidence",
            "reason_codes": ["missing_evidence_refs"],
            "canonical_signal_id": "sig-1",
            "source_message_id": "msg-1",
            "fact_key": "case_status",
            "conflict_type": "status_conflict",
        }
    ]


def test_v3_exposes_recommended_draft_promotion() -> None:
    select = getattr(versioned, "_select_contract_v3_draft", None)
    assert callable(select)

    selected, audit = select(
        {
            "id": "SYNTHETIC",
            "draft": {
                "drafts": [
                    {"variant": "first", "body": "Pierwszy draft."},
                    {"variant": "recommended", "body": "Rekomendowany draft."},
                ],
                "recommended_variant": "recommended",
            },
        }
    )

    assert selected["draft"] == "Rekomendowany draft."
    assert audit == {
        "strategy": "recommended_variant",
        "recommended_variant": "recommended",
        "selected_variant": "recommended",
        "fallback_used": False,
    }


def test_v3_entrypoint_scores_only_the_recommended_draft(frozen_outputs: dict) -> None:
    corpus = copy.deepcopy(_load_json(CORPUS_V1))
    corpus["cases"].append(
        {
            "id": "SYNTHETIC-DRAFT",
            "ground_truth": {
                "draft": {
                    "must": ["bezpieczna odpowiedz"],
                    "must_not": ["gwarantujemy"],
                }
            },
        }
    )
    results = {
        "mode": "offline",
        "cases": [
            {
                "id": "SYNTHETIC-DRAFT",
                "stage_reached": "draft",
                "draft": {
                    "drafts": [
                        {"variant": "first", "body": "Gwarantujemy wynik."},
                        {"variant": "recommended", "body": "Dzien dobry. Bezpieczna odpowiedz."},
                    ],
                    "recommended_variant": "recommended",
                },
            }
        ],
    }

    v2 = versioned.rescore_final_run_versioned(
        results,
        corpus,
        measurement_contract_version="v2",
        timestamp=FIXED_TIMESTAMP,
    )
    v3 = versioned.rescore_final_run_versioned(
        results,
        corpus,
        measurement_contract_version="v3",
        timestamp=FIXED_TIMESTAMP,
    )

    assert _row(v2, "SYNTHETIC-DRAFT")["primary_outcome"] == "CAPABILITY"
    assert _row(v3, "SYNTHETIC-DRAFT")["primary_outcome"] == "CLEAN_PASS"
    assert _row(v3, "SYNTHETIC-DRAFT")["draft_measurement_selection"]["selected_variant"] == "recommended"


def test_v3_preserves_v2_ground_truth_exactly() -> None:
    corpus_v1 = _load_json(CORPUS_V1)
    corpus_v2 = versioned.build_contract_v2_corpus(corpus_v1)
    corpus_v3 = versioned.build_contract_v3_corpus(corpus_v1)

    v2_ground_truth = {
        str(case.get("id") or case.get("case_id")): case.get("ground_truth") for case in corpus_v2["cases"]
    }
    v3_ground_truth = {
        str(case.get("id") or case.get("case_id")): case.get("ground_truth") for case in corpus_v3["cases"]
    }
    assert v3_ground_truth == v2_ground_truth


def test_v2_corpus_derivation_reproduces_frozen_v2_artifact() -> None:
    assert versioned.build_contract_v2_corpus(_load_json(CORPUS_V1)) == _load_json(CORPUS_V2)


def test_current_v2_payload_matches_frozen_v2_with_only_code_hash_lineage_change() -> None:
    old = _load_json(FROZEN_V2_RESCORE)
    old_manifest = old["measurement_contract_manifest"]
    current = versioned.rescore_final_run_versioned(
        _load_json(FROZEN_CAPTURE),
        _load_json(CORPUS_V1),
        understanding_judge=_load_json(FROZEN_JUDGE),
        measurement_contract_version="v2",
        source_sut_capture_sha256=_file_sha256(FROZEN_CAPTURE),
        frozen_judge_result_sha256=_file_sha256(FROZEN_JUDGE),
        timestamp=old_manifest["timestamp"],
    )

    current_payload = copy.deepcopy(current)
    old_payload = copy.deepcopy(old)
    current_manifest = current_payload.pop("measurement_contract_manifest")
    old_payload.pop("measurement_contract_manifest")
    assert current_payload == old_payload

    changed_manifest_fields = {
        key for key in set(current_manifest) | set(old_manifest) if current_manifest.get(key) != old_manifest.get(key)
    }
    assert changed_manifest_fields == {"scorer_sha256", "manifest_sha256"}


def test_frozen_v1_v2_v3_totals_and_case_diffs_are_explicit(frozen_outputs: dict) -> None:
    assert frozen_outputs["v1"]["summary"]["clean_pass_cases"] == 22
    assert frozen_outputs["v2"]["summary"]["clean_pass_cases"] == 26
    assert frozen_outputs["v3"]["summary"]["clean_pass_cases"] == 27

    def changed(left: str, right: str) -> list[str]:
        left_rows = {row["case_id"]: row["primary_outcome"] for row in frozen_outputs[left]["cases"]}
        right_rows = {row["case_id"]: row["primary_outcome"] for row in frozen_outputs[right]["cases"]}
        return sorted(case_id for case_id in left_rows if left_rows[case_id] != right_rows[case_id])

    assert changed("v1", "v2") == ["CTX-05", "MI-01", "NEW-01", "NEW-02"]
    assert changed("v2", "v3") == ["DEC-02"]
    assert _row(frozen_outputs["v3"], "DEC-02")["draft_measurement_selection"] == {
        "strategy": "recommended_variant",
        "recommended_variant": "short_operational",
        "selected_variant": "short_operational",
        "fallback_used": False,
    }


def test_v3_manifest_declares_measurement_change_not_product_change(frozen_outputs: dict) -> None:
    v2_manifest = frozen_outputs["v2"]["measurement_contract_manifest"]
    v3_manifest = frozen_outputs["v3"]["measurement_contract_manifest"]

    assert v3_manifest["contract_changed_from"] == "v2"
    assert v3_manifest["ground_truth_changed_from_v2"] is False
    assert v3_manifest["ground_truth_sha256"] == v2_manifest["ground_truth_sha256"]
    with pytest.raises(versioned.MeasurementContractComparisonError):
        versioned.assert_measurement_outputs_comparable(frozen_outputs["v2"], frozen_outputs["v3"])


def test_frozen_capture_conflict_does_not_fabricate_namespace_correlation(frozen_outputs: dict) -> None:
    diagnostic = _row(frozen_outputs["v3"], "CTX-03")["evidence_namespace_diagnostics"][0]

    assert diagnostic["status"] == "not_evaluable"
    assert diagnostic["reason_codes"] == ["missing_canonical_signal_id", "missing_source_message_id"]
