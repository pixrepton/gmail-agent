"""Bounded runtime harness for AI-OS Phase 3 Playwright proofs (3.5 / 3.6)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
import requests

TOOL_DIR = Path(__file__).resolve().parent.parent
GMAIL_AGENT_REPO_ROOT = TOOL_DIR.parent.parent
WORKSPACE_ROOT = GMAIL_AGENT_REPO_ROOT.parent
PLAYWRIGHT_ENV_PATH = TOOL_DIR / ".env.playwright.local"
DEFAULT_NODE_B_URL = "http://127.0.0.1:8766"
DEFAULT_DASZEK_URL = "http://127.0.0.1:8090/daszek/"
DEFAULT_TRACKED_SUMMARY_PATH = WORKSPACE_ROOT / "knowledge" / "eval" / "PHASE3_RUNTIME_PROOF_SUMMARY.json"
PROOF_SUMMARY_SCHEMA_VERSION = "phase3-runtime-proof-summary.v1"
_BROWSER_JOURNEY_KEYS = frozenset({"3.5", "3.6_complaint", "x1_01"})
_DIRTINESS_FIELDS = (
    "working_tree_dirty",
    "tracked_working_tree_dirty",
    "untracked_paths_present",
    "untracked_paths_count",
)


@dataclass(frozen=True, slots=True)
class BoundedRuntimeUrls:
    node_b_base: str
    daszek_base: str


def runtime_proof_required() -> bool:
    return os.getenv("AIOS_RUNTIME_PROOF_REQUIRED", "").strip().lower() in {"1", "true", "yes"}


def _load_playwright_env_file_only() -> None:
    if not PLAYWRIGHT_ENV_PATH.is_file():
        return
    try:
        from dotenv import dotenv_values

        for key, value in dotenv_values(PLAYWRIGHT_ENV_PATH).items():
            if value is None:
                continue
            env_key = str(key or "").strip()
            if not env_key:
                continue
            os.environ.setdefault(env_key, str(value))
    except Exception:
        return


def _wordpress_root_from_daszek_url(raw: str) -> str:
    """Normalize Daszek app URL or WP root to WordPress origin (no /daszek suffix)."""
    value = str(raw or "").strip().rstrip("/")
    if value.endswith("/daszek"):
        value = value[: -len("/daszek")]
    return value.rstrip("/")


def resolve_bounded_runtime_urls() -> BoundedRuntimeUrls:
    _load_playwright_env_file_only()
    node_b = (
        os.getenv("AIOS_NODE_B_URL")
        or os.getenv("NODE_B_REGISTRY_BASE_URL")
        or os.getenv("GMAIL_AGENT_API_BASE_URL")
        or DEFAULT_NODE_B_URL
    ).rstrip("/")
    daszek_raw = (
        os.getenv("AIOS_DASZEK_URL")
        or os.getenv("DASZEK_BASE_URL")
        or DEFAULT_DASZEK_URL
    )
    return BoundedRuntimeUrls(node_b_base=node_b, daszek_base=_wordpress_root_from_daszek_url(daszek_raw))


def enforce_runtime_dependency(reason_code: str, message: str) -> None:
    if runtime_proof_required():
        pytest.fail(f"{reason_code}: {message}")
    pytest.skip(f"{reason_code}: {message}")


def _probe(url: str, *, timeout_sec: float = 3.0) -> bool:
    try:
        response = requests.get(url, timeout=timeout_sec)
        return response.status_code < 500
    except Exception:
        return False


def _probe_browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


def _probe_daszek_login_page(daszek_base: str) -> bool:
    app_url = daszek_app_url(BoundedRuntimeUrls(node_b_base="", daszek_base=daszek_base))
    try:
        response = requests.get(app_url, timeout=8)
        if response.status_code >= 500:
            return False
        body = response.text.lower()
        return "login-form" in body or 'id="login"' in body
    except Exception:
        return False


def _probe_daszek_login_api(daszek_base: str, login: str, password: str) -> bool:
    base = daszek_base.rstrip("/")
    try:
        response = requests.post(
            f"{base}/wp-json/daszek/v1/login",
            json={"login": login, "password": password},
            timeout=12,
        )
        payload = response.json() if response.content else {}
        return response.status_code < 400 and bool(payload.get("ok"))
    except Exception:
        return False


def bounded_runtime_preflight(*, urls: BoundedRuntimeUrls | None = None) -> dict[str, Any]:
    resolved = urls or resolve_bounded_runtime_urls()
    login = ""
    password = ""
    credentials_present = False
    login_ok = False
    try:
        login, password = peek_daszek_credentials()
        credentials_present = bool(login and password)
    except Exception:
        credentials_present = False

    node_b_ok = _probe(f"{resolved.node_b_base}/health")
    daszek_ok = _probe(resolved.daszek_base)
    login_page_ok = _probe_daszek_login_page(resolved.daszek_base) if daszek_ok else False
    browser_ok = _probe_browser_available()
    if credentials_present:
        login_ok = _probe_daszek_login_api(resolved.daszek_base, login, password)

    return {
        "node_b_url": resolved.node_b_base,
        "daszek_url": daszek_app_url(resolved),
        "runtime_preflight": {
            "node_b": "PASS" if node_b_ok else "FAIL",
            "daszek": "PASS" if daszek_ok else "FAIL",
            "login_page": "PASS" if login_page_ok else "FAIL",
            "browser": "PASS" if browser_ok else "FAIL",
            "credentials": "PASS" if credentials_present else "FAIL",
            "login": "PASS" if login_ok else ("SKIP" if not credentials_present else "FAIL"),
        },
        "ready": all(
            [
                node_b_ok,
                daszek_ok,
                login_page_ok,
                browser_ok,
                credentials_present,
                login_ok if credentials_present else False,
            ]
        ),
    }


def runtime_health(urls: BoundedRuntimeUrls | None = None) -> dict[str, Any]:
    return bounded_runtime_preflight(urls=urls)


def peek_daszek_credentials() -> tuple[str, str]:
    _load_playwright_env_file_only()
    login = (
        os.getenv("AIOS_DASZEK_TEST_LOGIN")
        or os.getenv("DASZEK_LOGIN")
        or ""
    ).strip()
    password = (
        os.getenv("AIOS_DASZEK_TEST_PASSWORD")
        or os.getenv("DASZEK_PASSWORD")
        or ""
    ).strip()
    return login, password


def load_daszek_credentials() -> tuple[str, str]:
    login, password = peek_daszek_credentials()
    if not login or not password:
        enforce_runtime_dependency(
            "RUNTIME_CREDENTIALS_MISSING",
            "Set AIOS_DASZEK_TEST_LOGIN/AIOS_DASZEK_TEST_PASSWORD or DASZEK_LOGIN/DASZEK_PASSWORD "
            f"(optional file: {PLAYWRIGHT_ENV_PATH.name}, gitignored).",
        )
    return login, password


def require_bounded_runtime(*, urls: BoundedRuntimeUrls | None = None) -> BoundedRuntimeUrls:
    if not runtime_proof_required():
        pytest.skip("AIOS_RUNTIME_PROOF_REQUIRED not set — bounded Playwright proof skipped in Gate A")
    resolved = urls or resolve_bounded_runtime_urls()
    preflight = bounded_runtime_preflight(urls=resolved)
    if not _probe(f"{resolved.node_b_base}/health"):
        enforce_runtime_dependency(
            "RUNTIME_NODE_B_UNAVAILABLE",
            f"Node B unavailable at {resolved.node_b_base}",
        )
    if not _probe(resolved.daszek_base):
        enforce_runtime_dependency(
            "RUNTIME_DASZEK_UNAVAILABLE",
            f"Daszek unavailable at {resolved.daszek_base}",
        )
    if not _probe_daszek_login_page(resolved.daszek_base):
        enforce_runtime_dependency(
            "RUNTIME_DASZEK_UNAVAILABLE",
            f"Daszek login page unavailable at {daszek_app_url(resolved)}",
        )
    if not _probe_browser_available():
        enforce_runtime_dependency("RUNTIME_BROWSER_MISSING", "Playwright Chromium is unavailable")
    load_daszek_credentials()
    if not preflight["runtime_preflight"]["login"] == "PASS":
        login, password = peek_daszek_credentials()
        if login and password and not _probe_daszek_login_api(resolved.daszek_base, login, password):
            enforce_runtime_dependency("RUNTIME_CREDENTIALS_MISSING", "Daszek login API rejected credentials")
    return resolved


def daszek_app_url(urls: BoundedRuntimeUrls) -> str:
    configured = (os.getenv("AIOS_DASZEK_URL") or "").strip()
    if configured:
        root = _wordpress_root_from_daszek_url(configured)
        return f"{root}/daszek/"
    return f"{urls.daszek_base.rstrip('/')}/daszek/"


def playwright_dismiss_onboarding(page: Any) -> None:
    """Close Daszek first-run overlay so HITL clicks are not intercepted."""
    page.evaluate(
        """() => {
            try { localStorage.setItem('daszek-onboarding-done', '1'); } catch (_) {}
            const overlay = document.getElementById('onboarding-overlay');
            const backdrop = document.getElementById('onboarding-backdrop');
            if (overlay) overlay.classList.remove('onboarding-overlay--visible');
            if (backdrop) backdrop.classList.remove('onboarding-backdrop--visible');
            if (overlay) overlay.remove();
            if (backdrop) backdrop.remove();
        }"""
    )
    skip = page.locator("#onboarding-skip")
    if skip.count() > 0:
        try:
            skip.first.click(timeout=1_500)
        except Exception:
            pass
    finish = page.locator("#onboarding-finish")
    if finish.count() > 0:
        try:
            finish.first.click(timeout=1_500)
        except Exception:
            pass
    page.wait_for_timeout(200)


def playwright_login_daszek(page: Any, *, login: str, password: str, daszek_base: str) -> None:
    from playwright.sync_api import expect

    app_url = daszek_app_url(BoundedRuntimeUrls(node_b_base="", daszek_base=daszek_base))
    page.add_init_script(
        "try { localStorage.setItem('daszek-onboarding-done', '1'); } catch (e) {}"
    )
    page.goto(app_url, wait_until="domcontentloaded", timeout=90_000)
    page.locator("#login").fill(login)
    page.locator("#password").fill(password)
    page.locator('#login-form button[type="submit"]').click()
    expect(page.locator("#main-screen")).to_be_visible(timeout=60_000)
    playwright_dismiss_onboarding(page)


def playwright_open_case_detail(page: Any, *, case_id: str) -> None:
    from playwright.sync_api import expect

    playwright_dismiss_onboarding(page)
    # Desk first; Cases tab as fallback when feed projects into cases list only.
    for view in ("desk", "cases"):
        playwright_dismiss_onboarding(page)
        tab = page.locator(f'.view-tab[data-view="{view}"]')
        if tab.count() > 0:
            tab.first.click(timeout=10_000)
            page.wait_for_timeout(1200)
        card = page.locator(f'[data-open-case="{case_id}"]')
        if card.count() > 0:
            playwright_dismiss_onboarding(page)
            card.first.click(timeout=15_000, force=True)
            expect(page.locator("#detail-panel")).to_be_visible(timeout=30_000)
            return
        # Soft refresh operational feed when available.
        refresh = page.locator("[data-refresh-operational-feed]")
        if refresh.count() > 0:
            refresh.first.click(timeout=5_000)
            page.wait_for_timeout(1500)
    raise TimeoutError(f"Daszek card for case_id={case_id} not found on desk/cases after feed push")


def playwright_apply_feed_visibility_override(
    page: Any,
    *,
    engagement_id: str,
    case_id: str,
    mode: str,
    reason: str = "x1-01-playwright",
) -> dict[str, Any]:
    """Daszek UI: reclassify card → POST feed-visibility/override → receipt.

    Returns the Node B / proxy JSON body (requested vs effective fields).
    Server-side CAS uses the current snapshot version when the UI omits
    ``expected_version`` (proxy still forwards it when present).
    """
    from playwright.sync_api import expect

    target_mode = str(mode or "").strip()
    if target_mode not in {"hidden", "case_timeline_only", "main_feed"}:
        raise ValueError(f"unsupported override mode: {target_mode!r}")

    playwright_open_case_detail(page, case_id=case_id)
    panel = page.locator("#detail-panel")
    apply_btn = panel.locator(f'[data-feed-visibility-apply="{engagement_id}"]')
    expect(apply_btn).to_be_visible(timeout=30_000)
    expect(panel.locator("[data-feed-visibility-mode]")).to_be_visible(timeout=15_000)
    apply_btn.scroll_into_view_if_needed()
    playwright_dismiss_onboarding(page)
    page.locator("#global-error").evaluate(
        "el => { el.style.display = 'none'; el.textContent = ''; }"
    )

    with page.expect_response(
        lambda response: (
            "/feed-visibility/override" in response.url
            and response.request.method == "POST"
        ),
        timeout=60_000,
    ) as response_info:
        # Atomic set+click in one evaluate: avoids stale/duplicate selects and
        # mid-flight detail re-renders clearing the mode before submit.
        page.evaluate(
            """([eng, mode, reasonText]) => {
                const btn = document.querySelector('[data-feed-visibility-apply=\"' + eng + '\"]');
                if (!btn) throw new Error('apply button missing for ' + eng);
                // app.js submit reads document.querySelector (first match) — set all.
                document.querySelectorAll('[data-feed-visibility-mode]').forEach((modeEl) => {
                    modeEl.value = mode;
                    modeEl.dispatchEvent(new Event('change', { bubbles: true }));
                });
                document.querySelectorAll('[data-feed-visibility-reason]').forEach((reasonEl) => {
                    if (reasonText) {
                        reasonEl.value = String(reasonText).slice(0, 80);
                        reasonEl.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                });
                const modeEl = document.querySelector('[data-feed-visibility-mode]');
                if (!modeEl || modeEl.value !== mode) {
                    throw new Error('mode select rejected value ' + mode);
                }
                btn.click();
            }""",
            [engagement_id, target_mode, str(reason or "").strip()],
        )
    response = response_info.value
    # Client-only validation shows global-error without a fetch; after a response exists,
    # prefer HTTP body over the toast (toast text is often generic WP proxy wording).
    if response.status >= 400:
        body = ""
        try:
            body = response.text()[:800]
        except Exception:
            body = "<unreadable>"
        ui_err = ""
        err = page.locator("#global-error")
        if err.count() > 0 and err.first.is_visible():
            ui_err = (err.first.inner_text() or "").strip()
        raise AssertionError(
            f"feed-visibility override HTTP {response.status}: {body}"
            + (f" | ui={ui_err}" if ui_err else "")
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AssertionError(f"feed-visibility override returned non-JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"feed-visibility override payload must be object, got {type(payload)!r}")
    if payload.get("ok") is False:
        raise AssertionError(f"feed-visibility override rejected: {payload}")

    requested = payload.get("requested_override_mode")
    effective = payload.get("effective_feed_visibility_mode")
    if requested != target_mode:
        raise AssertionError(
            f"requested_override_mode mismatch: expected {target_mode!r}, got {requested!r}"
        )
    if not effective:
        raise AssertionError(f"missing effective_feed_visibility_mode in receipt: {payload}")
    # Compatibility alias must track effective, not requested.
    alias = payload.get("feed_visibility_mode")
    if alias is not None and alias != effective:
        raise AssertionError(
            f"feed_visibility_mode alias must equal effective ({effective!r}), got {alias!r}"
        )

    # Projection refresh: UI reloads detail and shows effective desk mode.
    expect(panel).to_contain_text("Efektywny widok na biurku", timeout=30_000)
    expect(panel).to_contain_text(str(effective), timeout=30_000)
    error = page.locator("#global-error, #error-banner, .toast-error, [data-error]")
    if error.count() > 0 and error.first.is_visible():
        text = (error.first.inner_text() or "").strip()
        if text:
            raise AssertionError(f"Daszek showed error after feed-visibility override: {text}")
    return payload


def playwright_approve_hitl_without_send(
    page: Any,
    *,
    engagement_id: str,
    case_id: str,
    draft_hint: str = "",
) -> None:
    from playwright.sync_api import expect

    playwright_open_case_detail(page, case_id=case_id)
    panel = page.locator("#detail-panel")
    if draft_hint:
        expect(panel).to_contain_text(draft_hint[:48], timeout=30_000)
    approve = panel.locator(f'[data-hitl-approve="{engagement_id}"]')
    expect(approve).to_be_visible(timeout=30_000)
    playwright_dismiss_onboarding(page)
    with page.expect_response(
        lambda response: "/agent-hitl/approve" in response.url and response.request.method == "POST",
        timeout=60_000,
    ) as response_info:
        approve.click(force=True)
    response = response_info.value
    if response.status >= 400:
        body = ""
        try:
            body = response.text()[:500]
        except Exception:
            body = "<unreadable>"
        raise AssertionError(f"HITL approve HTTP {response.status}: {body}")
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict) and payload.get("ok") is False:
        raise AssertionError(f"HITL approve rejected: {payload}")
    # Wait until UI leaves pending state / toast confirms.
    page.wait_for_timeout(800)
    error = page.locator("#error-banner, .toast-error, [data-error]")
    if error.count() > 0 and error.first.is_visible():
        raise AssertionError(f"Daszek showed error after HITL approve: {error.first.inner_text()}")


def wait_for_ready_for_manual_send(store: Any, engagement_id: str, *, timeout_sec: float = 20.0) -> Any:
    """Poll operator store until UI/Node B approve lands on the snapshot."""
    import time

    deadline = time.time() + timeout_sec
    last = None
    while time.time() < deadline:
        last = store.load_snapshot(engagement_id)
        receipt = getattr(last, "communication_receipt", None) if last is not None else None
        hitl = getattr(last, "hitl_gate", None) if last is not None else None
        if (
            receipt is not None
            and getattr(receipt, "state", None) == "ready_for_manual_send"
            and hitl is not None
            and getattr(hitl, "required", True) is False
        ):
            return last
        time.sleep(0.4)
    raise AssertionError(
        "Snapshot did not reach ready_for_manual_send after UI HITL approve: "
        f"engagement_id={engagement_id} receipt={getattr(last, 'communication_receipt', None)!r} "
        f"hitl={getattr(last, 'hitl_gate', None)!r}"
    )


def proof_artifacts_root() -> Path:
    custom = os.getenv("AIOS_PHASE3_PROOF_DIR", "").strip()
    if custom:
        return Path(custom)
    return TOOL_DIR / "artifacts" / "phase3-runtime-proof"


_ACTIVE_MANIFEST: dict[str, Any] | None = None


def set_active_manifest(manifest: dict[str, Any] | None) -> None:
    global _ACTIVE_MANIFEST
    _ACTIVE_MANIFEST = manifest


def _run_git(*args: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd or GMAIL_AGENT_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()
    except Exception:
        return ""


def _tracked_diff_sha256() -> str:
    diff = _run_git("diff", "HEAD")
    if not diff and _run_git("diff", "--cached"):
        diff = _run_git("diff", "--cached")
    if not diff:
        return ""
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def _working_tree_dirtiness() -> dict[str, Any]:
    """Distinguish tracked dirt from untracked paths for proof provenance.

    ``working_tree_dirty`` is the full porcelain signal (tracked + untracked).
    Explicit tracked/untracked fields avoid the ambiguity where only a tracked
    hash looked clean while untracked proof helpers still existed.
    """
    porcelain = _run_git("status", "--porcelain")
    lines = [line for line in porcelain.splitlines() if line.strip()]
    untracked: list[str] = []
    tracked_dirty = False
    for line in lines:
        # porcelain: "?? path" = untracked; otherwise XY path = tracked change
        if line.startswith("?? ") or line.startswith("!! "):
            untracked.append(line[3:].strip())
        else:
            tracked_dirty = True
    return {
        "working_tree_dirty": bool(lines),
        "tracked_working_tree_dirty": tracked_dirty,
        "untracked_paths_present": bool(untracked),
        "untracked_paths_count": len(untracked),
    }


def _node_b_image_metadata() -> tuple[str, str]:
    image_ref = os.getenv("AIOS_NODE_B_IMAGE", "gmail-agent-nodeb-api").strip() or "gmail-agent-nodeb-api"
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}} {{.Created}}", image_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return "", ""
        parts = (result.stdout or "").strip().split(" ", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        if parts:
            return parts[0], ""
    except Exception:
        pass
    return "", ""


def _resolve_intelligence_mode() -> str:
    if os.getenv("AIOS_LIVE_LLM_INTELLIGENCE", "").strip().lower() in {"1", "true", "yes"}:
        return "live_llm_intelligence"
    return "deterministic_test_double"


def _resolve_runtime_wiring_mode() -> str:
    return "production_runtime_wiring"


def _parse_gate_a_result_from_env() -> dict[str, int] | None:
    raw = os.getenv("AIOS_GATE_A_RESULT_JSON", "").strip()
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return {
                    "passed": int(payload.get("passed", 0)),
                    "skipped": int(payload.get("skipped", 0)),
                    "failed": int(payload.get("failed", 0)),
                }
        except Exception:
            pass
    passed = os.getenv("AIOS_GATE_A_PASSED", "").strip()
    if passed:
        try:
            return {
                "passed": int(passed),
                "skipped": int(os.getenv("AIOS_GATE_A_SKIPPED", "0") or 0),
                "failed": int(os.getenv("AIOS_GATE_A_FAILED", "0") or 0),
            }
        except ValueError:
            return None
    return None


def record_pytest_session_result(manifest: dict[str, Any], *, passed: int, skipped: int, failed: int) -> None:
    manifest["pytest_result"] = {"passed": passed, "skipped": skipped, "failed": failed}


def record_gate_a_result(manifest: dict[str, Any], *, passed: int, skipped: int, failed: int) -> None:
    manifest["gate_a_result"] = {"passed": passed, "skipped": skipped, "failed": failed}


def begin_proof_manifest() -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    proof_id = f"phase3-runtime-{stamp}-{uuid.uuid4().hex[:8]}"
    urls = resolve_bounded_runtime_urls()
    image_id, image_created = _node_b_image_metadata()
    gate_a = _parse_gate_a_result_from_env()
    dirtiness = _working_tree_dirtiness()
    manifest: dict[str, Any] = {
        "manifest_schema_version": "1",
        "proof_id": proof_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": "",
        "git_head_sha": _run_git("rev-parse", "HEAD"),
        "working_tree_dirty": dirtiness["working_tree_dirty"],
        "tracked_working_tree_dirty": dirtiness["tracked_working_tree_dirty"],
        "untracked_paths_present": dirtiness["untracked_paths_present"],
        "untracked_paths_count": dirtiness["untracked_paths_count"],
        "tracked_diff_sha256": _tracked_diff_sha256(),
        "node_b_image_id": image_id,
        "node_b_image_created_at": image_created,
        "node_b_url": urls.node_b_base,
        "daszek_url": daszek_app_url(urls),
        "runtime_preflight": bounded_runtime_preflight(urls=urls)["runtime_preflight"],
        "runtime_wiring_mode": _resolve_runtime_wiring_mode(),
        "intelligence_mode": _resolve_intelligence_mode(),
        "live_gmail_send_enabled": False,
        "test_command": os.getenv("AIOS_PHASE3_TEST_COMMAND", "").strip(),
        "journeys": {},
        "live_send_invocations": 0,
        "trace_paths": [],
        "screenshot_paths": [],
    }
    if gate_a is not None:
        manifest["gate_a_result"] = gate_a
    return manifest


def get_active_manifest() -> dict[str, Any]:
    if _ACTIVE_MANIFEST is not None:
        return _ACTIVE_MANIFEST
    return begin_proof_manifest()


def record_journey_result(manifest: dict[str, Any], journey_key: str, payload: dict[str, Any]) -> None:
    journeys = manifest.setdefault("journeys", {})
    if not isinstance(journeys, dict):
        journeys = {}
        manifest["journeys"] = journeys
    journeys[journey_key] = payload


def record_browser_artifacts(
    manifest: dict[str, Any],
    *,
    trace_path: Path | str | None = None,
    screenshot_path: Path | str | None = None,
) -> None:
    """Append browser artifact paths onto the proof manifest (PH3-06)."""
    if trace_path:
        traces = manifest.setdefault("trace_paths", [])
        if not isinstance(traces, list):
            traces = []
            manifest["trace_paths"] = traces
        traces.append(str(trace_path))
    if screenshot_path:
        shots = manifest.setdefault("screenshot_paths", [])
        if not isinstance(shots, list):
            shots = []
            manifest["screenshot_paths"] = shots
        shots.append(str(screenshot_path))


def _browser_artifacts_dir(manifest: dict[str, Any], journey_key: str) -> Path:
    proof_id = str(manifest.get("proof_id") or "adhoc").strip() or "adhoc"
    safe_key = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in journey_key) or "journey"
    out = proof_artifacts_root() / "_browser" / proof_id / safe_key
    out.mkdir(parents=True, exist_ok=True)
    return out


@contextmanager
def traced_journey_page(
    browser: Any,
    *,
    journey_key: str,
    manifest: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Open a Playwright page with tracing + end-of-journey screenshot (PH3-06)."""
    active = manifest if manifest is not None else get_active_manifest()
    art_dir = _browser_artifacts_dir(active, journey_key)
    trace_path = art_dir / "trace.zip"
    screenshot_path = art_dir / "screenshot.png"
    context = browser.new_context()
    context.tracing.start(screenshots=True, snapshots=True, sources=False)
    page = context.new_page()
    shot_path: Path | None = screenshot_path
    trace_file: Path | None = trace_path
    try:
        yield page
    finally:
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            shot_path = None
        try:
            context.tracing.stop(path=str(trace_path))
        except Exception:
            trace_file = None
        try:
            context.close()
        except Exception:
            pass
        record_browser_artifacts(
            active,
            trace_path=trace_file if trace_file is not None and trace_file.is_file() else None,
            screenshot_path=shot_path if shot_path is not None and shot_path.is_file() else None,
        )


def _default_test_command() -> str:
    env_cmd = os.getenv("AIOS_PHASE3_TEST_COMMAND", "").strip()
    if env_cmd:
        return env_cmd
    argv = [str(part) for part in sys.argv if str(part).strip()]
    if argv:
        return " ".join(argv)
    return "python -m pytest tools/gmail_audit/tests -q"


def _ensure_dirtiness_fields(manifest: dict[str, Any]) -> None:
    missing = [field for field in _DIRTINESS_FIELDS if field not in manifest]
    if not missing:
        return
    dirtiness = _working_tree_dirtiness()
    for field in missing:
        manifest[field] = dirtiness[field]


def _has_browser_journeys(manifest: dict[str, Any]) -> bool:
    journeys = manifest.get("journeys")
    if not isinstance(journeys, dict):
        return False
    return bool(_BROWSER_JOURNEY_KEYS.intersection(journeys.keys()))


def _validate_runtime_proof_manifest(manifest: dict[str, Any]) -> None:
    """GOV-08: when runtime proof is required, refuse incomplete manifests."""
    test_command = str(manifest.get("test_command") or "").strip()
    if not test_command:
        raise AssertionError("GOV-08: test_command must be non-empty when AIOS_RUNTIME_PROOF_REQUIRED=1")
    missing = [field for field in _DIRTINESS_FIELDS if field not in manifest]
    if missing:
        raise AssertionError(f"GOV-08: missing dirtiness fields: {', '.join(missing)}")
    if _has_browser_journeys(manifest):
        traces = manifest.get("trace_paths")
        shots = manifest.get("screenshot_paths")
        if not isinstance(traces, list) or not traces:
            raise AssertionError("GOV-08/PH3-06: browser journeys require non-empty trace_paths")
        if not isinstance(shots, list) or not shots:
            raise AssertionError("GOV-08/PH3-06: browser journeys require non-empty screenshot_paths")


def tracked_proof_summary_path() -> Path:
    custom = os.getenv("AIOS_PHASE3_SUMMARY_TRACKED_PATH", "").strip()
    if custom:
        return Path(custom)
    return DEFAULT_TRACKED_SUMMARY_PATH


def build_proof_manifest_summary(
    manifest: dict[str, Any],
    *,
    full_manifest_path: Path | None = None,
    full_manifest_sha256: str = "",
) -> dict[str, Any]:
    """Non-secret summary for GOV-07 (no passwords/tokens/cookies/PII bodies)."""
    journeys = manifest.get("journeys") if isinstance(manifest.get("journeys"), dict) else {}
    journey_verdicts: dict[str, str] = {}
    for key, payload in journeys.items():
        if isinstance(payload, dict):
            journey_verdicts[str(key)] = str(payload.get("status") or "UNKNOWN")
        else:
            journey_verdicts[str(key)] = "UNKNOWN"
    traces = manifest.get("trace_paths") if isinstance(manifest.get("trace_paths"), list) else []
    shots = manifest.get("screenshot_paths") if isinstance(manifest.get("screenshot_paths"), list) else []
    pytest_result = manifest.get("pytest_result") if isinstance(manifest.get("pytest_result"), dict) else {}
    failed = int(pytest_result.get("failed", 0) or 0) if pytest_result else 0
    if failed > 0:
        status = "FAIL"
    elif journey_verdicts and all(v == "PASS" for v in journey_verdicts.values()):
        status = "PASS"
    elif journey_verdicts:
        status = "PARTIAL"
    else:
        status = "NO_JOURNEYS"
    summary: dict[str, Any] = {
        "manifest_version": PROOF_SUMMARY_SCHEMA_VERSION,
        "status": status,
        "proof_id": manifest.get("proof_id", ""),
        "completed_at": manifest.get("completed_at", ""),
        "git_head_sha": manifest.get("git_head_sha", ""),
        "test_command": manifest.get("test_command", ""),
        "full_manifest_sha256": full_manifest_sha256,
        "journey_verdicts": journey_verdicts,
        "counts": {
            "journeys": len(journey_verdicts),
            "trace_paths": len(traces),
            "screenshot_paths": len(shots),
            "live_send_invocations": int(manifest.get("live_send_invocations", 0) or 0),
        },
        "working_tree_dirty": bool(manifest.get("working_tree_dirty", False)),
        "tracked_working_tree_dirty": bool(manifest.get("tracked_working_tree_dirty", False)),
        "untracked_paths_present": bool(manifest.get("untracked_paths_present", False)),
        "pytest_result": pytest_result or {"passed": 0, "skipped": 0, "failed": 0},
    }
    if full_manifest_path is not None:
        summary["full_manifest_path"] = str(full_manifest_path)
    gate_a = manifest.get("gate_a_result")
    if isinstance(gate_a, dict):
        summary["gate_a_result"] = gate_a
    return summary


def write_proof_manifest_summary(
    summary: dict[str, Any],
    *,
    artifact_path: Path,
    write_tracked: bool = False,
) -> tuple[Path, Path | None]:
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    artifact_path.write_text(payload, encoding="utf-8")
    tracked_path: Path | None = None
    if write_tracked:
        tracked_path = tracked_proof_summary_path()
        tracked_path.parent.mkdir(parents=True, exist_ok=True)
        tracked_path.write_text(payload, encoding="utf-8")
        os.environ["AIOS_PHASE3_PROOF_SUMMARY_PATH"] = str(tracked_path)
    return artifact_path, tracked_path


def finalize_proof_manifest(manifest: dict[str, Any]) -> Path:
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    if not str(manifest.get("test_command") or "").strip():
        manifest["test_command"] = _default_test_command()
    _ensure_dirtiness_fields(manifest)
    if "pytest_result" not in manifest:
        manifest["pytest_result"] = {"passed": 0, "skipped": 0, "failed": 0}
    if "gate_a_result" not in manifest:
        gate_a = _parse_gate_a_result_from_env()
        if gate_a is not None:
            manifest["gate_a_result"] = gate_a
    if runtime_proof_required():
        _validate_runtime_proof_manifest(manifest)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = proof_artifacts_root() / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "proof-manifest.json"
    raw = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    out_path.write_text(raw, encoding="utf-8")
    os.environ["AIOS_PHASE3_PROOF_MANIFEST_PATH"] = str(out_path)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    summary = build_proof_manifest_summary(
        manifest,
        full_manifest_path=out_path,
        full_manifest_sha256=digest,
    )
    write_proof_manifest_summary(
        summary,
        artifact_path=out_dir / "proof-manifest.summary.json",
        write_tracked=runtime_proof_required(),
    )
    return out_path


__all__ = [
    "BoundedRuntimeUrls",
    "DEFAULT_DASZEK_URL",
    "DEFAULT_NODE_B_URL",
    "DEFAULT_TRACKED_SUMMARY_PATH",
    "PLAYWRIGHT_ENV_PATH",
    "PROOF_SUMMARY_SCHEMA_VERSION",
    "finalize_proof_manifest",
    "begin_proof_manifest",
    "bounded_runtime_preflight",
    "build_proof_manifest_summary",
    "daszek_app_url",
    "enforce_runtime_dependency",
    "get_active_manifest",
    "set_active_manifest",
    "load_daszek_credentials",
    "peek_daszek_credentials",
    "playwright_dismiss_onboarding",
    "playwright_login_daszek",
    "playwright_open_case_detail",
    "playwright_apply_feed_visibility_override",
    "playwright_approve_hitl_without_send",
    "wait_for_ready_for_manual_send",
    "proof_artifacts_root",
    "record_browser_artifacts",
    "record_journey_result",
    "record_gate_a_result",
    "record_pytest_session_result",
    "require_bounded_runtime",
    "resolve_bounded_runtime_urls",
    "runtime_health",
    "runtime_proof_required",
    "traced_journey_page",
    "tracked_proof_summary_path",
    "write_proof_manifest_summary",
]
