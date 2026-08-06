"""Harness tests for AI-OS bounded runtime support."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
TOOL_DIR = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import aios_bounded_runtime_support as harness

_MISSING_ENV = Path("/nonexistent/.env.playwright.local")


def test_default_node_b_url_uses_8766() -> None:
    with patch.dict(os.environ, {}, clear=True), patch.object(harness, "PLAYWRIGHT_ENV_PATH", _MISSING_ENV):
        urls = harness.resolve_bounded_runtime_urls()
    assert urls.node_b_base == "http://127.0.0.1:8766"


def test_daszek_app_url_defaults_to_daszek_path() -> None:
    urls = harness.BoundedRuntimeUrls(node_b_base="http://127.0.0.1:8766", daszek_base="http://127.0.0.1:8090")
    with patch.dict(os.environ, {}, clear=True):
        assert harness.daszek_app_url(urls) == "http://127.0.0.1:8090/daszek/"


def test_aios_daszek_url_does_not_double_daszek_path() -> None:
    urls = harness.BoundedRuntimeUrls(node_b_base="http://127.0.0.1:8766", daszek_base="http://127.0.0.1:8090")
    with patch.dict(os.environ, {"AIOS_DASZEK_URL": "http://127.0.0.1:8090/daszek/"}, clear=False):
        assert harness.daszek_app_url(urls) == "http://127.0.0.1:8090/daszek/"


def test_missing_credentials_standard_mode_skips_with_reason() -> None:
    with patch.dict(os.environ, {}, clear=True), patch.object(
        harness, "PLAYWRIGHT_ENV_PATH", _MISSING_ENV
    ), patch.object(harness, "runtime_proof_required", return_value=False):
        with pytest.raises(pytest.skip.Exception) as exc:
            harness.load_daszek_credentials()
    assert "RUNTIME_CREDENTIALS_MISSING" in str(exc.value)


def test_missing_credentials_required_mode_fails() -> None:
    with patch.dict(os.environ, {}, clear=True), patch.object(
        harness, "PLAYWRIGHT_ENV_PATH", _MISSING_ENV
    ), patch.object(harness, "runtime_proof_required", return_value=True):
        with pytest.raises(BaseException) as exc:
            harness.load_daszek_credentials()
    assert "RUNTIME_CREDENTIALS_MISSING" in str(exc.value)


def test_require_bounded_runtime_skips_without_proof_flag() -> None:
    """Gate A must not run Playwright journeys unless AIOS_RUNTIME_PROOF_REQUIRED=1."""
    urls = harness.BoundedRuntimeUrls(node_b_base="http://127.0.0.1:8766", daszek_base="http://127.0.0.1:8090")
    with patch.object(harness, "runtime_proof_required", return_value=False):
        with pytest.raises(pytest.skip.Exception) as exc:
            harness.require_bounded_runtime(urls=urls)
    assert "AIOS_RUNTIME_PROOF_REQUIRED" in str(exc.value)


def test_missing_node_b_required_mode_fails() -> None:
    urls = harness.BoundedRuntimeUrls(node_b_base="http://127.0.0.1:1", daszek_base="http://127.0.0.1:8090")
    with patch.object(harness, "runtime_proof_required", return_value=True), patch.object(harness, "_probe", return_value=False):
        with pytest.raises(BaseException) as exc:
            harness.require_bounded_runtime(urls=urls)
    assert "RUNTIME_NODE_B_UNAVAILABLE" in str(exc.value)


def test_tracked_harness_has_no_literal_fallback_credentials() -> None:
    """Tracked source must not embed local sandbox passwords (gitignored env file may)."""
    support_source = (TESTS_DIR / "aios_bounded_runtime_support.py").read_text(encoding="utf-8")
    for needle in ("konrad123", 'login = "konrad"', 'password = "konrad123"'):
        assert needle not in support_source


def test_manifest_does_not_store_password_or_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOS_PHASE3_PROOF_DIR", str(tmp_path))
    monkeypatch.delenv("AIOS_RUNTIME_PROOF_REQUIRED", raising=False)
    manifest = harness.begin_proof_manifest()
    harness.record_journey_result(
        manifest,
        "3.5",
        {
            "status": "PASS",
            "correlation_id": "sig_demo",
            "case_id": "case_demo",
            "draft_id": "draft_demo",
            "approval_receipt_id": "draft_demo",
            "communication_sent_count": 0,
        },
    )
    out = harness.finalize_proof_manifest(manifest)
    payload = out.read_text(encoding="utf-8")
    for forbidden in ("password", "csrf_token", "Authorization", "cookie"):
        assert forbidden not in payload.lower()


def test_working_tree_dirtiness_distinguishes_tracked_and_untracked() -> None:
    """Provenance must not collapse untracked-only dirt into a silent clean tree."""
    dirtiness = harness._working_tree_dirtiness()
    assert set(dirtiness) >= {
        "working_tree_dirty",
        "tracked_working_tree_dirty",
        "untracked_paths_present",
        "untracked_paths_count",
    }
    assert dirtiness["working_tree_dirty"] is (
        dirtiness["tracked_working_tree_dirty"] or dirtiness["untracked_paths_present"]
    )
    assert dirtiness["untracked_paths_count"] >= 0
    if dirtiness["untracked_paths_present"]:
        assert dirtiness["untracked_paths_count"] > 0
        assert dirtiness["working_tree_dirty"] is True


def test_record_browser_artifacts_appends_paths() -> None:
    manifest = {"trace_paths": [], "screenshot_paths": []}
    harness.record_browser_artifacts(
        manifest,
        trace_path="C:/tmp/trace.zip",
        screenshot_path="C:/tmp/shot.png",
    )
    assert manifest["trace_paths"] == ["C:/tmp/trace.zip"]
    assert manifest["screenshot_paths"] == ["C:/tmp/shot.png"]


def test_finalize_required_autofills_test_command_and_writes_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIOS_PHASE3_PROOF_DIR", str(tmp_path))
    monkeypatch.setenv("AIOS_RUNTIME_PROOF_REQUIRED", "1")
    monkeypatch.setenv("AIOS_PHASE3_SUMMARY_TRACKED_PATH", str(tmp_path / "tracked-summary.json"))
    monkeypatch.delenv("AIOS_PHASE3_TEST_COMMAND", raising=False)
    manifest = harness.begin_proof_manifest()
    manifest["test_command"] = ""
    harness.record_browser_artifacts(
        manifest,
        trace_path=str(tmp_path / "trace.zip"),
        screenshot_path=str(tmp_path / "shot.png"),
    )
    harness.record_journey_result(manifest, "3.5", {"status": "PASS"})
    out = harness.finalize_proof_manifest(manifest)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["test_command"].strip()
    summary_path = out.parent / "proof-manifest.summary.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["manifest_version"] == harness.PROOF_SUMMARY_SCHEMA_VERSION
    assert summary["full_manifest_sha256"]
    assert summary["journey_verdicts"]["3.5"] == "PASS"
    assert summary["counts"]["trace_paths"] == 1
    assert summary["counts"]["screenshot_paths"] == 1
    tracked = tmp_path / "tracked-summary.json"
    assert tracked.is_file()
    assert "password" not in summary_path.read_text(encoding="utf-8").lower()


def test_finalize_required_fails_without_browser_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIOS_PHASE3_PROOF_DIR", str(tmp_path))
    monkeypatch.setenv("AIOS_RUNTIME_PROOF_REQUIRED", "1")
    monkeypatch.setenv("AIOS_PHASE3_TEST_COMMAND", "pytest tools/gmail_audit/tests -q")
    monkeypatch.setenv("AIOS_PHASE3_SUMMARY_TRACKED_PATH", str(tmp_path / "tracked-summary.json"))
    manifest = harness.begin_proof_manifest()
    harness.record_journey_result(manifest, "3.5", {"status": "PASS"})
    with pytest.raises(AssertionError, match="trace_paths"):
        harness.finalize_proof_manifest(manifest)


def test_finalize_required_fails_without_screenshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIOS_PHASE3_PROOF_DIR", str(tmp_path))
    monkeypatch.setenv("AIOS_RUNTIME_PROOF_REQUIRED", "1")
    monkeypatch.setenv("AIOS_PHASE3_TEST_COMMAND", "pytest tools/gmail_audit/tests -q")
    monkeypatch.setenv("AIOS_PHASE3_SUMMARY_TRACKED_PATH", str(tmp_path / "tracked-summary.json"))
    manifest = harness.begin_proof_manifest()
    harness.record_browser_artifacts(manifest, trace_path=str(tmp_path / "trace.zip"))
    harness.record_journey_result(manifest, "3.6_complaint", {"status": "PASS"})
    with pytest.raises(AssertionError, match="screenshot_paths"):
        harness.finalize_proof_manifest(manifest)


def test_finalize_required_restores_missing_dirtiness_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIOS_PHASE3_PROOF_DIR", str(tmp_path))
    monkeypatch.setenv("AIOS_RUNTIME_PROOF_REQUIRED", "1")
    monkeypatch.setenv("AIOS_PHASE3_TEST_COMMAND", "pytest focused")
    monkeypatch.setenv("AIOS_PHASE3_SUMMARY_TRACKED_PATH", str(tmp_path / "tracked-summary.json"))
    manifest = harness.begin_proof_manifest()
    for field in (
        "working_tree_dirty",
        "tracked_working_tree_dirty",
        "untracked_paths_present",
        "untracked_paths_count",
    ):
        manifest.pop(field, None)
    out = harness.finalize_proof_manifest(manifest)
    payload = json.loads(out.read_text(encoding="utf-8"))
    for field in (
        "working_tree_dirty",
        "tracked_working_tree_dirty",
        "untracked_paths_present",
        "untracked_paths_count",
    ):
        assert field in payload


def test_build_proof_manifest_summary_omits_secrets() -> None:
    summary = harness.build_proof_manifest_summary(
        {
            "proof_id": "demo",
            "completed_at": "2026-08-06T00:00:00Z",
            "git_head_sha": "abc",
            "test_command": "pytest",
            "journeys": {"3.5": {"status": "PASS"}},
            "trace_paths": ["t.zip"],
            "screenshot_paths": ["s.png"],
            "live_send_invocations": 0,
            "working_tree_dirty": False,
            "tracked_working_tree_dirty": False,
            "untracked_paths_present": False,
            "pytest_result": {"passed": 1, "skipped": 0, "failed": 0},
        },
        full_manifest_sha256="deadbeef",
    )
    blob = json.dumps(summary).lower()
    for forbidden in ("password", "csrf_token", "authorization", "cookie"):
        assert forbidden not in blob
    assert summary["status"] == "PASS"
    assert summary["full_manifest_sha256"] == "deadbeef"


def test_x1_01_is_browser_journey_key_for_ph3_06() -> None:
    assert "x1_01" in harness._BROWSER_JOURNEY_KEYS


def test_finalize_required_requires_artifacts_for_x1_01_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIOS_PHASE3_PROOF_DIR", str(tmp_path))
    monkeypatch.setenv("AIOS_RUNTIME_PROOF_REQUIRED", "1")
    monkeypatch.setenv("AIOS_PHASE3_TEST_COMMAND", "pytest tools/gmail_audit/tests -q")
    monkeypatch.setenv("AIOS_PHASE3_SUMMARY_TRACKED_PATH", str(tmp_path / "tracked-summary.json"))
    manifest = harness.begin_proof_manifest()
    harness.record_journey_result(manifest, "x1_01", {"status": "PASS"})
    with pytest.raises(AssertionError, match="trace_paths"):
        harness.finalize_proof_manifest(manifest)


def test_playwright_apply_feed_visibility_rejects_invalid_mode() -> None:
    with pytest.raises(ValueError, match="unsupported override mode"):
        harness.playwright_apply_feed_visibility_override(
            object(),
            engagement_id="eng_x",
            case_id="case_x",
            mode="attention_required",
        )
