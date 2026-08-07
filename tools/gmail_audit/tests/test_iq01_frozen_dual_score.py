"""IQ-01 — pin frozen Fresh38 capture + protocol/dual-score entrypoint (Gate A)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "measurement_contract_v1"
CAPTURE = FIXTURE_DIR / "fresh-full38-results.json"
MANIFEST = FIXTURE_DIR / "fixture-manifest.json"

# Workspace root: gmail-agent/tools/gmail_audit/tests → parents[4]
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
IQ01_DIR = WORKSPACE_ROOT / "knowledge" / "eval" / "understanding-to-decision-quality-01"
PROTOCOL = IQ01_DIR / "PROTOCOL.md"
SYNTHETIC_RUNNER = IQ01_DIR / "run_iq01_eval.py"
FROZEN_RUNNER = IQ01_DIR / "run_iq01_frozen_dual_score.py"

PINNED_SHA256 = "c04f295293e750856548ac35b4a9126d5946da4e74b6b5df4efebaea33bf736c"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_iq01_frozen_capture_sha256_matches_manifest() -> None:
    assert CAPTURE.is_file()
    assert MANIFEST.is_file()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    entry = manifest["files"]["fresh-full38-results.json"]
    assert entry["sha256"] == PINNED_SHA256
    assert _sha256(CAPTURE) == PINNED_SHA256
    assert CAPTURE.stat().st_size == int(entry["bytes"])


def test_iq01_protocol_and_runners_exist() -> None:
    assert PROTOCOL.is_file(), f"missing {PROTOCOL}"
    assert SYNTHETIC_RUNNER.is_file(), f"missing {SYNTHETIC_RUNNER}"
    assert FROZEN_RUNNER.is_file(), f"missing {FROZEN_RUNNER}"
    protocol_text = PROTOCOL.read_text(encoding="utf-8")
    assert PINNED_SHA256 in protocol_text
    assert "machine_proposed" in protocol_text
    assert "adjudicated" in protocol_text
    assert "dual-score" in protocol_text.lower() or "Dual-score" in protocol_text
    assert r"C:\top-code-session-scratch\iq01-eval" in protocol_text


def test_iq01_frozen_dual_score_dry_run() -> None:
    """Dry mode: verify sha256 + extract Understanding count without dirty writes."""
    if not FROZEN_RUNNER.is_file():
        pytest.skip("frozen dual-score runner not present in this checkout layout")
    proc = subprocess.run(
        [sys.executable, str(FROZEN_RUNNER), "--dry-run"],
        cwd=str(WORKSPACE_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    # stdout starts with a JSON preview object
    preview_raw = proc.stdout.strip().split("dry-run:", 1)[0].strip()
    preview = json.loads(preview_raw)
    assert preview["verified"] is True
    assert preview["sha256"] == PINNED_SHA256
    assert preview["capture_case_count"] == 38
    assert preview["extracted_understanding_count"] == 36
    assert preview["labeling_status"] == "machine_proposed"
    assert preview["adjudicated_complete"] is False
    assert preview["baseline_gap"] is False
