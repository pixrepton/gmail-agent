from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER = REPO_ROOT / "scripts" / "run-row4a-local-proof.ps1"


def _source() -> str:
    return RUNNER.read_text(encoding="utf-8")


def test_runner_exposes_all_failure_simulation_switches() -> None:
    source = _source()

    assert "SimulateFailureAfterWorkerStop" in source
    assert "SimulateFailureAfterProofApiStart" in source
    assert "SimulateFailureBeforeBrowserProof" in source


def test_runner_keeps_browser_proof_inside_restore_lifecycle() -> None:
    source = _source()

    browser_marker = '[row4a] browser proof'
    restore_marker = '[row4a] restore background gmail-agent worker state'

    assert browser_marker in source
    assert restore_marker in source
    assert source.index(browser_marker) < source.index(restore_marker)


def test_runner_uses_python_browser_harness() -> None:
    source = _source()

    assert "row4a_browser_proof.py" in source
    assert "& python $browserHarness" in source


def test_runner_can_capture_pre_replay_baseline_without_mutating_old_proof() -> None:
    source = _source()

    assert "PreviousProofDir" in source
    assert "pre-replay-baseline.json" in source
    assert "OPERATOR_ROW4_HANDOFF.json" in source
