"""Versioned offline final-run rescoring for measurement-contract corrections.

The original eval_final_rescore.py remains the frozen v1 scorer. This module is
the explicit entrypoint for versioned measurement corrections.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import eval_final_rescore as v1_rescore
from eval_measurement_scoring import canonical_json_sha256


CONTRACT_V1 = "v1"
CONTRACT_V2 = "v2"
CONTRACT_V3 = "v3"
SUPPORTED_CONTRACTS = {CONTRACT_V1, CONTRACT_V2, CONTRACT_V3}
QUALIFICATION_THRESHOLD = 34

NEW01_BAD_DRAFT_MUST = "ton profesjonalny, adekwatny do prostego zapytania"
NEW02_BAD_EXTRACTION_MUST = "hvac_intent zawiera element techniczny/pytanie, nie tylko 'wycena'"
NEW02_V2_EXTRACTION_MUST = "hvac_intent=techniczne/pytanie"

COMPARISON_POLICY = (
    "measurement outputs are comparable only when measurement_contract_version, "
    "corpus hash, ground-truth hash, scorer hash, and threshold match"
)


class MeasurementContractError(ValueError):
    """Raised for invalid or unsupported measurement contract use."""


class MeasurementContractComparisonError(MeasurementContractError):
    """Raised when two measurement outputs cannot be compared silently."""


def normalize_contract_version(version: str) -> str:
    normalized = str(version or "").strip().lower()
    if normalized not in SUPPORTED_CONTRACTS:
        raise MeasurementContractError(
            f"Unsupported measurement contract version {version!r}; expected one of {sorted(SUPPORTED_CONTRACTS)}"
        )
    return normalized


def build_contract_v2_corpus(corpus_v1: dict[str, Any]) -> dict[str, Any]:
    """Return the v2 corpus derived from v1 without mutating the frozen input."""

    corpus = copy.deepcopy(corpus_v1)
    corpus["schema_version"] = "eval_measurement_corpus.v2"
    corpus["measurement_contract_version"] = CONTRACT_V2
    corpus["derived_from"] = {
        "measurement_contract_version": CONTRACT_V1,
        "corpus_sha256": canonical_json_sha256(corpus_v1),
    }
    corpus["contract_v2_changes"] = [
        {
            "case_id": "NEW-01",
            "field": "ground_truth.draft.must",
            "change": "move style/tone requirement out of factual literal matching",
        },
        {
            "case_id": "NEW-02",
            "field": "ground_truth.extraction.must",
            "change": "replace natural-language requirement sentence with field=value fact spec",
        },
    ]

    _patch_new01_ground_truth(corpus)
    _patch_new02_ground_truth(corpus)
    return corpus


def build_contract_v3_corpus(corpus_v1: dict[str, Any]) -> dict[str, Any]:
    """Derive v3 from v2 without changing any ground-truth entry."""

    corpus_v2 = build_contract_v2_corpus(corpus_v1)
    corpus = copy.deepcopy(corpus_v2)
    corpus["schema_version"] = "eval_measurement_corpus.v3"
    corpus["measurement_contract_version"] = CONTRACT_V3
    corpus["derived_from"] = {
        "measurement_contract_version": CONTRACT_V2,
        "corpus_sha256": canonical_json_sha256(corpus_v2),
    }
    corpus["contract_v3_changes"] = [
        "version scorer semantics instead of leaking v3 fixes into v1/v2",
        "score the recommended draft variant at the final-rescore entrypoint",
        "use bounded strict unsafe-term morphology",
        "surface divergent Understanding aliases and evidence namespace diagnostics",
    ]
    return corpus


def rescore_final_run_versioned(
    results: dict[str, Any],
    corpus: dict[str, Any],
    *,
    understanding_judge: dict[str, Any] | None = None,
    measurement_contract_version: str,
    source_sut_capture_sha256: str | None = None,
    frozen_judge_result_sha256: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    version = normalize_contract_version(measurement_contract_version)
    effective_corpus = _effective_corpus(corpus, version)

    if version == CONTRACT_V1:
        rescored = v1_rescore.rescore_final_run(results, effective_corpus, understanding_judge=understanding_judge)
    elif version == CONTRACT_V2:
        rescored = _rescore_final_run_v2(results, effective_corpus, understanding_judge=understanding_judge)
    else:
        rescored = _rescore_final_run_v3(results, effective_corpus, understanding_judge=understanding_judge)

    metadata = build_measurement_manifest(
        effective_corpus,
        measurement_contract_version=version,
        source_sut_capture_sha256=source_sut_capture_sha256 or canonical_json_sha256(results),
        frozen_judge_result_sha256=frozen_judge_result_sha256,
        timestamp=timestamp,
    )
    rescored["measurement_contract_version"] = version
    rescored["measurement_contract_manifest"] = metadata
    return rescored


def build_measurement_manifest(
    corpus: dict[str, Any],
    *,
    measurement_contract_version: str,
    source_sut_capture_sha256: str,
    frozen_judge_result_sha256: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    version = normalize_contract_version(measurement_contract_version)
    manifest = {
        "measurement_contract_version": version,
        "corpus_sha256": canonical_json_sha256(corpus),
        "ground_truth_sha256": _ground_truth_sha256(corpus),
        "scorer_sha256": _scorer_sha256(version),
        "qualification_threshold": QUALIFICATION_THRESHOLD,
        "timestamp": timestamp or _utc_timestamp(),
        "source_sut_capture_sha256": source_sut_capture_sha256,
        "comparison_policy": COMPARISON_POLICY,
    }
    if frozen_judge_result_sha256:
        manifest["frozen_judge_result_sha256"] = frozen_judge_result_sha256
    if version == CONTRACT_V2:
        manifest["contract_changed_from"] = CONTRACT_V1
        manifest["contract_change_reason"] = "measurement correction for CTX-05, MI-01, NEW-01, NEW-02 only"
    elif version == CONTRACT_V3:
        manifest["contract_changed_from"] = CONTRACT_V2
        manifest["contract_change_reason"] = [
            "restore frozen v1/v2 scorer semantics",
            "promote recommended draft variant before final scoring",
            "bounded strict unsafe-term morphology",
            "divergent alias and evidence namespace diagnostics",
            "top-level manifest coverage for the versioned rescore entrypoint",
        ]
        manifest["ground_truth_changed_from_v2"] = False
    manifest["manifest_sha256"] = canonical_json_sha256(manifest)
    return manifest


def assert_measurement_outputs_comparable(left: dict[str, Any], right: dict[str, Any]) -> None:
    left_manifest = _manifest(left)
    right_manifest = _manifest(right)
    compared_fields = (
        "measurement_contract_version",
        "corpus_sha256",
        "ground_truth_sha256",
        "scorer_sha256",
        "qualification_threshold",
    )
    differences = {
        field: {"left": left_manifest.get(field), "right": right_manifest.get(field)}
        for field in compared_fields
        if left_manifest.get(field) != right_manifest.get(field)
    }
    if differences:
        raise MeasurementContractComparisonError(
            "Measurement outputs use different contracts or hashes; explicit contract-change comparison required"
        )


def _rescore_final_run_v2(
    results: dict[str, Any],
    corpus: dict[str, Any],
    *,
    understanding_judge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cases_by_id = {str(case.get("id") or case.get("case_id")): case for case in corpus.get("cases", [])}
    judge_by_case = v1_rescore._judge_by_case(understanding_judge)
    judge_supplied = isinstance(understanding_judge, dict)
    rows = []
    for case_output in results.get("cases", []):
        case_id = str(case_output.get("id") or case_output.get("case_id") or "")
        corpus_case = cases_by_id.get(case_id)
        if not corpus_case:
            rows.append(v1_rescore._missing_ground_truth_row(case_id, case_output))
            continue
        judge_row = judge_by_case.get(case_id)
        ground_truth = corpus_case.get("ground_truth") if isinstance(corpus_case.get("ground_truth"), dict) else {}
        if judge_supplied and isinstance(ground_truth.get("understanding"), dict) and not judge_row:
            judge_row = {"case_id": case_id, "status": "JUDGE_UNAVAILABLE", "error": "missing_frozen_judge_result"}
        rows.append(_score_final_case_v2(case_output, corpus_case, understanding_judge=judge_row))

    summary = v1_rescore._summary(rows)
    return {
        "source_mode": results.get("mode"),
        "sentinel_only": bool(results.get("sentinel_only")),
        "corpus_canonical_sha256": canonical_json_sha256(corpus),
        "summary": summary,
        "cases": rows,
    }


def _score_final_case_v2(
    case_output: dict[str, Any],
    corpus_case: dict[str, Any],
    *,
    understanding_judge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sanitized, v2_nonblocking = _strip_contract_v2_nonblocking_errors(case_output, corpus_case)
    row = v1_rescore.score_final_case(
        sanitized,
        corpus_case,
        understanding_judge=understanding_judge,
        measurement_contract_version=CONTRACT_V2,
    )
    row["nonblocking_tool_errors"] = v2_nonblocking + list(row.get("nonblocking_tool_errors") or [])
    row["measurement_contract_version"] = CONTRACT_V2
    return row


def _rescore_final_run_v3(
    results: dict[str, Any],
    corpus: dict[str, Any],
    *,
    understanding_judge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cases_by_id = {str(case.get("id") or case.get("case_id")): case for case in corpus.get("cases", [])}
    judge_by_case = v1_rescore._judge_by_case(understanding_judge)
    judge_supplied = isinstance(understanding_judge, dict)
    rows = []
    for case_output in results.get("cases", []):
        case_id = str(case_output.get("id") or case_output.get("case_id") or "")
        corpus_case = cases_by_id.get(case_id)
        if not corpus_case:
            rows.append(v1_rescore._missing_ground_truth_row(case_id, case_output))
            continue
        judge_row = judge_by_case.get(case_id)
        ground_truth = corpus_case.get("ground_truth") if isinstance(corpus_case.get("ground_truth"), dict) else {}
        if judge_supplied and isinstance(ground_truth.get("understanding"), dict) and not judge_row:
            judge_row = {"case_id": case_id, "status": "JUDGE_UNAVAILABLE", "error": "missing_frozen_judge_result"}
        rows.append(_score_final_case_v3(case_output, corpus_case, understanding_judge=judge_row))

    summary = v1_rescore._summary(rows)
    return {
        "source_mode": results.get("mode"),
        "sentinel_only": bool(results.get("sentinel_only")),
        "corpus_canonical_sha256": canonical_json_sha256(corpus),
        "summary": summary,
        "cases": rows,
    }


def _score_final_case_v3(
    case_output: dict[str, Any],
    corpus_case: dict[str, Any],
    *,
    understanding_judge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected, draft_selection = _select_contract_v3_draft(case_output)
    sanitized, v2_nonblocking = _strip_contract_v2_nonblocking_errors(selected, corpus_case)
    row = v1_rescore.score_final_case(
        sanitized,
        corpus_case,
        understanding_judge=understanding_judge,
        measurement_contract_version=CONTRACT_V3,
    )
    row["nonblocking_tool_errors"] = v2_nonblocking + list(row.get("nonblocking_tool_errors") or [])
    row["measurement_contract_version"] = CONTRACT_V3
    row["draft_measurement_selection"] = draft_selection
    evidence_diagnostics = _case_evidence_namespace_diagnostics(case_output)
    if evidence_diagnostics:
        row["evidence_namespace_diagnostics"] = evidence_diagnostics
    return row


def _select_contract_v3_draft(case_output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    selected = copy.deepcopy(case_output)
    draft = selected.get("draft")
    audit = {
        "strategy": "unavailable",
        "recommended_variant": "",
        "selected_variant": "",
        "fallback_used": False,
    }
    if isinstance(draft, str):
        audit["strategy"] = "direct_body"
        return selected, audit
    if not isinstance(draft, dict):
        if isinstance(selected.get("planner"), dict) and selected["planner"].get("generate_draft_reply_body"):
            audit["strategy"] = "planner_capture_fallback"
            audit["fallback_used"] = True
        return selected, audit

    drafts = draft.get("drafts")
    recommended = str(draft.get("recommended_variant") or "").strip()
    audit["recommended_variant"] = recommended
    if isinstance(drafts, list):
        if recommended:
            for item in drafts:
                if not isinstance(item, dict) or str(item.get("variant") or "").strip() != recommended:
                    continue
                body = str(item.get("body") or item.get("payload_pl") or "").strip()
                if body:
                    selected["draft"] = body
                    audit.update(
                        {
                            "strategy": "recommended_variant",
                            "selected_variant": recommended,
                            "fallback_used": False,
                        }
                    )
                    return selected, audit
        for item in drafts:
            if not isinstance(item, dict):
                continue
            body = str(item.get("body") or item.get("payload_pl") or "").strip()
            if body:
                selected["draft"] = body
                audit.update(
                    {
                        "strategy": "first_variant_fallback",
                        "selected_variant": str(item.get("variant") or "").strip(),
                        "fallback_used": True,
                    }
                )
                return selected, audit

    if isinstance(draft.get("execution_metadata"), dict):
        audit["strategy"] = "legacy_execution_metadata_fallback"
        audit["fallback_used"] = True
    return selected, audit


def diagnose_evidence_namespace(
    source_refs: list[dict[str, Any]] | None,
    *,
    canonical_signal_id: str,
    source_message_id: str,
) -> dict[str, Any]:
    signal_id = str(canonical_signal_id or "").strip()
    message_id = str(source_message_id or "").strip()
    result = {
        "status": "not_evaluable",
        "reason_codes": [],
        "canonical_signal_id": signal_id,
        "source_message_id": message_id,
    }
    refs = [item for item in (source_refs or []) if isinstance(item, dict)]
    if not refs:
        result["status"] = "missing_evidence"
        result["reason_codes"] = ["missing_evidence_refs"]
        return result
    if not signal_id or not message_id:
        missing = []
        if not signal_id:
            missing.append("missing_canonical_signal_id")
        if not message_id:
            missing.append("missing_source_message_id")
        result["reason_codes"] = missing
        return result

    explicit_signal_ids = [
        str(ref.get("signal_id") or ref.get("source_signal_id") or "").strip()
        for ref in refs
        if str(ref.get("signal_id") or ref.get("source_signal_id") or "").strip()
    ]
    explicit_message_ids = [
        str(ref.get("message_id") or ref.get("source_message_id") or "").strip()
        for ref in refs
        if str(ref.get("message_id") or ref.get("source_message_id") or "").strip()
    ]
    if not explicit_signal_ids:
        result["reason_codes"] = ["missing_explicit_signal_id_in_evidence_ref"]
        if any(str(ref.get("source_id") or "").strip() for ref in refs):
            result["reason_codes"].append("ambiguous_source_id_namespace")
        return result
    if not explicit_message_ids:
        result["reason_codes"] = ["missing_message_id_in_evidence_ref"]
        return result

    for ref in refs:
        ref_signal_id = str(ref.get("signal_id") or ref.get("source_signal_id") or "").strip()
        ref_message_id = str(ref.get("message_id") or ref.get("source_message_id") or "").strip()
        if ref_signal_id == signal_id and ref_message_id == message_id:
            result["status"] = "correlated"
            return result

    reason_codes = []
    if not any(
        str(ref.get("signal_id") or ref.get("source_signal_id") or "").strip() == signal_id
        for ref in refs
    ):
        reason_codes.append("canonical_signal_id_mismatch")
    if not any(
        str(ref.get("message_id") or ref.get("source_message_id") or "").strip() == message_id for ref in refs
    ):
        reason_codes.append("source_message_id_mismatch")
    if not reason_codes:
        reason_codes.append("ids_not_correlated_in_same_evidence_ref")
    result["status"] = "namespace_mismatch"
    result["reason_codes"] = reason_codes
    return result


def _case_evidence_namespace_diagnostics(case_output: dict[str, Any]) -> list[dict[str, Any]]:
    understanding = case_output.get("understanding") or case_output.get("understanding_output")
    if not isinstance(understanding, dict):
        return []
    conflicts = understanding.get("conflicting_facts")
    if not isinstance(conflicts, list):
        return []
    canonical_signal_id = str(
        case_output.get("signal_id")
        or case_output.get("canonical_signal_id")
        or understanding.get("canonical_signal_id")
        or ""
    )
    source_message_id = str(
        case_output.get("message_id")
        or case_output.get("source_message_id")
        or understanding.get("source_message_id")
        or ""
    )
    diagnostics = []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        refs = conflict.get("evidence_refs") or conflict.get("source_refs")
        diagnostic = diagnose_evidence_namespace(
            refs if isinstance(refs, list) else [],
            canonical_signal_id=canonical_signal_id,
            source_message_id=source_message_id,
        )
        diagnostic["fact_key"] = str(conflict.get("fact_key") or "")
        diagnostic["conflict_type"] = str(conflict.get("type") or "fact_conflict")
        diagnostics.append(diagnostic)
    return diagnostics


def _strip_contract_v2_nonblocking_errors(
    case_output: dict[str, Any],
    corpus_case: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sanitized = copy.deepcopy(case_output)
    planner = sanitized.get("planner")
    if not isinstance(planner, dict) or not isinstance(planner.get("turns_raw"), list):
        return sanitized, []

    kept_turns: list[Any] = []
    nonblocking: list[dict[str, Any]] = []
    for turn in planner.get("turns_raw") or []:
        if isinstance(turn, dict) and is_contract_v2_nonblocking_drive_read_404(turn, corpus_case):
            nonblocking.append(
                {
                    "tool_name": turn.get("tool_name"),
                    "tool_status": turn.get("tool_status") or turn.get("status"),
                    "summary": v1_rescore._preview(turn.get("turn_summary_pl") or turn.get("summary") or ""),
                    "contract_rule": "v2_read_google_drive_file_harness_fabricated_404",
                }
            )
            continue
        kept_turns.append(turn)
    planner["turns_raw"] = kept_turns
    return sanitized, nonblocking


def is_contract_v2_nonblocking_drive_read_404(turn: dict[str, Any], corpus_case: dict[str, Any]) -> bool:
    status = str(turn.get("tool_status") or turn.get("status") or "").strip().lower()
    if status != "error":
        return False
    if str(turn.get("tool_name") or "").strip() != "read_google_drive_file":
        return False

    text = v1_rescore._normalize(
        " ".join(str(turn.get(key) or "") for key in ("turn_summary_pl", "summary", "tool_args_redacted", "error"))
    )
    if any(blocker in text for blocker in ("auth", "unauthorized", "permission", "forbidden", "timeout")):
        return False
    if "drive api request failed" not in text:
        return False
    if "file not found" not in text and "404" not in text:
        return False
    if not _is_harness_fabricated_drive_file_id(turn, text):
        return False
    return not _ground_truth_requires_drive_document(corpus_case)


def _is_harness_fabricated_drive_file_id(turn: dict[str, Any], normalized_text: str) -> bool:
    args = turn.get("tool_args_redacted")
    file_id = ""
    if isinstance(args, dict):
        file_id = str(args.get("file_id") or "")
    normalized_file_id = v1_rescore._normalize(file_id)
    return (
        normalized_file_id.startswith("case recovery ")
        and " chunk " in normalized_file_id
        and normalized_file_id in normalized_text
    )


def _ground_truth_requires_drive_document(corpus_case: dict[str, Any]) -> bool:
    ground_truth = corpus_case.get("ground_truth") if isinstance(corpus_case.get("ground_truth"), dict) else {}
    required_keys = {
        "required_drive_documents",
        "required_drive_file_ids",
        "required_google_drive_files",
        "must_read_drive_documents",
    }
    for key in required_keys:
        value = ground_truth.get(key)
        if value:
            return True
    text = v1_rescore._normalize(json.dumps(ground_truth, ensure_ascii=False, sort_keys=True))
    return any(
        marker in text
        for marker in (
            "required drive document",
            "required google drive file",
            "wymagany dokument drive",
            "wymagany plik drive",
        )
    )


def _patch_new01_ground_truth(corpus: dict[str, Any]) -> None:
    case = _case_by_id(corpus, "NEW-01")
    draft = ((case.get("ground_truth") or {}).get("draft") or {})
    must = list(draft.get("must") or [])
    if NEW01_BAD_DRAFT_MUST not in must:
        raise MeasurementContractError("NEW-01 v1 bad draft.must entry not found")
    draft["must"] = [item for item in must if item != NEW01_BAD_DRAFT_MUST]
    draft.setdefault("tone_terms", ["dzien dobry", "prosze", "pozdrawiam"])


def _patch_new02_ground_truth(corpus: dict[str, Any]) -> None:
    case = _case_by_id(corpus, "NEW-02")
    extraction = ((case.get("ground_truth") or {}).get("extraction") or {})
    must = list(extraction.get("must") or [])
    if NEW02_BAD_EXTRACTION_MUST not in must:
        raise MeasurementContractError("NEW-02 v1 bad extraction.must entry not found")
    extraction["must"] = [NEW02_V2_EXTRACTION_MUST if item == NEW02_BAD_EXTRACTION_MUST else item for item in must]


def _case_by_id(corpus: dict[str, Any], case_id: str) -> dict[str, Any]:
    for case in corpus.get("cases", []):
        if str(case.get("id") or case.get("case_id")) == case_id:
            return case
    raise MeasurementContractError(f"Corpus case {case_id} not found")


def _effective_corpus(corpus: dict[str, Any], version: str) -> dict[str, Any]:
    if version == CONTRACT_V1:
        return copy.deepcopy(corpus)
    if version == CONTRACT_V2:
        return build_contract_v2_corpus(corpus)
    if version == CONTRACT_V3:
        return build_contract_v3_corpus(corpus)
    raise AssertionError(version)


def _manifest(output: dict[str, Any]) -> dict[str, Any]:
    manifest = output.get("measurement_contract_manifest")
    if not isinstance(manifest, dict):
        raise MeasurementContractComparisonError("Measurement output lacks measurement_contract_manifest")
    return manifest


def _ground_truth_sha256(corpus: dict[str, Any]) -> str:
    payload = [
        {"case_id": str(case.get("id") or case.get("case_id")), "ground_truth": case.get("ground_truth") or {}}
        for case in corpus.get("cases", [])
    ]
    return canonical_json_sha256(payload)


def _scorer_sha256(version: str) -> str:
    here = Path(__file__).resolve()
    scorer_files = [here.parent / "eval_measurement_scoring.py", here.parent / "eval_final_rescore.py"]
    if version in {CONTRACT_V2, CONTRACT_V3}:
        scorer_files.append(here)
    if version == CONTRACT_V3:
        scorer_files.append(here.parent / "eval_understanding_judge.py")
    hashes = {path.name: _file_sha256(path) for path in scorer_files}
    return canonical_json_sha256(hashes)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _metadata_payload(payload: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    out["measurement_contract_version"] = manifest["measurement_contract_version"]
    out["measurement_contract_manifest"] = manifest
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline rescore captured final eval results with an explicit contract.")
    parser.add_argument("--measurement-contract-version", required=True, choices=sorted(SUPPORTED_CONTRACTS))
    parser.add_argument("--run-results", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--summary-out", required=True)
    parser.add_argument("--breakdown-out", required=True)
    parser.add_argument("--qualification-out", required=True)
    parser.add_argument("--understanding-judge-results")
    parser.add_argument("--effective-corpus-out")
    args = parser.parse_args(argv)

    results_path = Path(args.run_results)
    judge_path = Path(args.understanding_judge_results) if args.understanding_judge_results else None
    results = v1_rescore.load_json(results_path)
    corpus_v1 = v1_rescore.load_json(args.corpus)
    judge_results = v1_rescore.load_json(judge_path) if judge_path else None

    rescored = rescore_final_run_versioned(
        results,
        corpus_v1,
        understanding_judge=judge_results,
        measurement_contract_version=args.measurement_contract_version,
        source_sut_capture_sha256=_file_sha256(results_path),
        frozen_judge_result_sha256=_file_sha256(judge_path) if judge_path else None,
    )
    manifest = rescored["measurement_contract_manifest"]
    breakdown = v1_rescore.quality_breakdown(rescored)
    qualification = v1_rescore.qualification_after_rescore(rescored, breakdown)

    v1_rescore.write_json(args.out, rescored)
    v1_rescore.write_json(args.summary_out, _metadata_payload(rescored["summary"], manifest))
    v1_rescore.write_json(args.breakdown_out, _metadata_payload(breakdown, manifest))
    v1_rescore.write_json(args.qualification_out, _metadata_payload(qualification, manifest))
    if args.effective_corpus_out:
        v1_rescore.write_json(args.effective_corpus_out, _effective_corpus(corpus_v1, manifest["measurement_contract_version"]))

    print(json.dumps({"summary": rescored["summary"], "qualification": qualification}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
