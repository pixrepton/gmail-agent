"""Harness tests for AI-OS bounded runtime support."""

from __future__ import annotations

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
