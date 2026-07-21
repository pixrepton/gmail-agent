from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from eval_measurement_manifest import build_measurement_manifest  # noqa: E402


def test_measurement_manifest_hashes_frozen_components(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"cases": [{"id": "A"}]}), encoding="utf-8")
    metric_definitions = tmp_path / "metric-definitions.md"
    metric_definitions.write_text("rubric", encoding="utf-8")
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "run_recovery.py").write_text("print('run')", encoding="utf-8")
    (harness / "__pycache__").mkdir()
    (harness / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
    judge = tmp_path / "judge"
    judge.mkdir()
    for name in (
        "judge-contract.json",
        "judge-prompt-final.txt",
        "judge-output-schema.json",
        "judge-config.json",
        "judge-hashes.json",
    ):
        (judge / name).write_text(name, encoding="utf-8")

    manifest = build_measurement_manifest(
        corpus=corpus,
        metric_definitions=metric_definitions,
        harness_dir=harness,
        judge_dir=judge,
        scorer_dir=TOOLS_DIR,
    )

    assert manifest["status"] == "FROZEN"
    assert manifest["rules"]["manual_scoring_required"] is False
    assert manifest["components"]["corpus_sha256"]
    assert manifest["manifest_sha256"]
    assert [item["path"] for item in manifest["harness"]["files"]] == ["run_recovery.py"]
