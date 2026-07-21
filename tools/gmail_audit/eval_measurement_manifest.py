"""Build the frozen final-eval measurement manifest.

This is eval tooling only. It records hashes for the corpus, rubric, scorer,
judge contract, harness, and capture contract so RUN-A/RUN-B can prove parity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from eval_measurement_scoring import canonical_json_sha256


MANIFEST_VERSION = "final-eval-measurement-manifest.v1"
SCORER_FILES = (
    "eval_measurement_scoring.py",
    "eval_final_rescore.py",
    "eval_understanding_judge.py",
)
JUDGE_FILES = (
    "judge-contract.json",
    "judge-prompt-final.txt",
    "judge-output-schema.json",
    "judge-config.json",
    "judge-hashes.json",
)
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def json_file_canonical_sha256(path: Path) -> str:
    return canonical_json_sha256(json.loads(path.read_text(encoding="utf-8-sig")))


def directory_manifest(path: Path) -> dict[str, Any]:
    files = []
    for file_path in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = file_path.relative_to(path)
        if any(part in EXCLUDED_PARTS for part in rel.parts):
            continue
        if file_path.suffix in EXCLUDED_SUFFIXES:
            continue
        files.append({"path": rel.as_posix(), "sha256": file_sha256(file_path), "bytes": file_path.stat().st_size})
    return {
        "path": str(path),
        "files": files,
        "sha256": canonical_json_sha256({"files": files}),
    }


def build_measurement_manifest(
    *,
    corpus: Path,
    metric_definitions: Path,
    harness_dir: Path,
    judge_dir: Path,
    scorer_dir: Path,
) -> dict[str, Any]:
    scorer_hashes = {name: file_sha256(scorer_dir / name) for name in SCORER_FILES}
    judge_hashes = {name: file_sha256(judge_dir / name) for name in JUDGE_FILES if (judge_dir / name).is_file()}
    harness = directory_manifest(harness_dir)
    components = {
        "corpus_sha256": json_file_canonical_sha256(corpus),
        "rubric_sha256": file_sha256(metric_definitions),
        "scorer_code_sha256": canonical_json_sha256(scorer_hashes),
        "judge_contract_sha256": canonical_json_sha256(judge_hashes),
        "harness_sha256": harness["sha256"],
        "capture_contract_sha256": file_sha256(scorer_dir / "eval_final_rescore.py"),
    }
    payload = {
        "manifest_version": MANIFEST_VERSION,
        "status": "FROZEN",
        "scope": "final_eval_measurement_contract",
        "components": components,
        "scorer_files": scorer_hashes,
        "judge_files": judge_hashes,
        "harness": harness,
        "rules": {
            "understanding_semantics": "frozen_semantic_judge",
            "extraction": "deterministic_field_level",
            "planner_action": "deterministic",
            "safety": "deterministic_gates",
            "draft": "deterministic_contract_unless_separate_frozen_judge_required",
            "judge_errors_are_not_capability_failures": True,
            "manual_scoring_required": False,
            "no_measurement_changes_between_run_a_and_run_b": True,
        },
    }
    payload["manifest_sha256"] = canonical_json_sha256(payload)
    return payload


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build final eval measurement manifest.")
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--metric-definitions", required=True)
    parser.add_argument("--harness-dir", required=True)
    parser.add_argument("--judge-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    scorer_dir = Path(__file__).resolve().parent
    manifest = build_measurement_manifest(
        corpus=Path(args.corpus),
        metric_definitions=Path(args.metric_definitions),
        harness_dir=Path(args.harness_dir),
        judge_dir=Path(args.judge_dir),
        scorer_dir=scorer_dir,
    )
    write_json(Path(args.out), manifest)
    print(json.dumps({"manifest_sha256": manifest["manifest_sha256"], "status": manifest["status"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
