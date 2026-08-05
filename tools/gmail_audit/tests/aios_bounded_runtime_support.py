"""Bounded runtime harness for AI-OS Phase 3 Playwright proofs (3.5 / 3.6)."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import requests

TOOL_DIR = Path(__file__).resolve().parent.parent
PLAYWRIGHT_ENV_PATH = TOOL_DIR / ".env.playwright.local"
DEFAULT_NODE_B_URL = "http://127.0.0.1:8766"
DEFAULT_DASZEK_URL = "http://127.0.0.1:8090/daszek/"


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


def begin_proof_manifest() -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    proof_id = f"phase3-runtime-{stamp}-{uuid.uuid4().hex[:8]}"
    urls = resolve_bounded_runtime_urls()
    return {
        "proof_id": proof_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": "",
        "node_b_url": urls.node_b_base,
        "daszek_url": daszek_app_url(urls),
        "runtime_preflight": bounded_runtime_preflight(urls=urls)["runtime_preflight"],
        "journeys": {},
        "live_send_invocations": 0,
        "trace_paths": [],
        "screenshot_paths": [],
    }


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


def finalize_proof_manifest(manifest: dict[str, Any]) -> Path:
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = proof_artifacts_root() / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "proof-manifest.json"
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.environ["AIOS_PHASE3_PROOF_MANIFEST_PATH"] = str(out_path)
    return out_path


__all__ = [
    "BoundedRuntimeUrls",
    "DEFAULT_DASZEK_URL",
    "DEFAULT_NODE_B_URL",
    "PLAYWRIGHT_ENV_PATH",
    "finalize_proof_manifest",
    "begin_proof_manifest",
    "bounded_runtime_preflight",
    "daszek_app_url",
    "enforce_runtime_dependency",
    "get_active_manifest",
    "set_active_manifest",
    "load_daszek_credentials",
    "peek_daszek_credentials",
    "playwright_dismiss_onboarding",
    "playwright_login_daszek",
    "playwright_open_case_detail",
    "playwright_approve_hitl_without_send",
    "wait_for_ready_for_manual_send",
    "proof_artifacts_root",
    "record_journey_result",
    "require_bounded_runtime",
    "resolve_bounded_runtime_urls",
    "runtime_health",
    "runtime_proof_required",
]
