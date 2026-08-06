"""X1-02 — Playwright: exceptions_only toggle changes feed + preference survives reload.

Skips in Gate A unless AIOS_RUNTIME_PROOF_REQUIRED=1 and bounded stack is up
(same harness pattern as Phase 3.5 / 3.6).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

TOOL_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from aios_bounded_runtime_support import (  # noqa: E402
    load_daszek_credentials,
    playwright_dismiss_onboarding,
    playwright_login_daszek,
    require_bounded_runtime,
    traced_journey_page,
)
from aios_canonical_runtime_ingress import (  # noqa: E402
    push_engagement_feed_for_ingress,
    run_canonical_runtime_ingress_from_fixture,
)

STORAGE_KEY = "daszek-exceptions-only-view"
FEED_LATEST_FRAGMENT = "/operational-feed-snapshots/latest"


def _daszek_credentials() -> tuple[str, str]:
    return load_daszek_credentials()


def _wait_desk_toggle(page: Any) -> Any:
    from playwright.sync_api import expect as _expect

    tab = page.locator('.view-tab[data-view="desk"]')
    if tab.count() > 0:
        tab.first.click(timeout=10_000)
    playwright_dismiss_onboarding(page)
    toggle = page.locator("[data-exceptions-only-toggle]")
    _expect(toggle).to_be_visible(timeout=30_000)
    return toggle


def _capture_latest_feed(page: Any, *, expect_exceptions_only: bool) -> dict[str, Any]:
    """Trigger a fresh desk load and capture the latest operational-feed response."""
    with page.expect_response(
        lambda resp: FEED_LATEST_FRAGMENT in resp.url and resp.request.method == "GET",
        timeout=60_000,
    ) as pending:
        refresh = page.locator("[data-refresh-operational-feed]")
        if refresh.count() > 0:
            refresh.first.click(timeout=5_000)
        else:
            # Fallback: re-toggle change handler / soft navigation to desk.
            tab = page.locator('.view-tab[data-view="desk"]')
            if tab.count() > 0:
                tab.first.click(timeout=5_000)
            page.wait_for_timeout(300)
            page.evaluate("() => window.loadAllData && window.loadAllData()")
    response = pending.value
    assert response.ok, f"feed latest HTTP {response.status}: {response.url}"
    url = response.url
    has_flag = "exceptions_only=1" in url or "exceptions_only=true" in url.lower()
    assert has_flag is expect_exceptions_only, f"url={url} expected_exceptions_only={expect_exceptions_only}"
    body = response.json()
    assert body.get("ok") is True, body
    return {"url": url, "body": body}


@pytest.mark.playwright
def test_playwright_exceptions_only_toggle_preference_bounded() -> None:
    urls = require_bounded_runtime()
    login, password = _daszek_credentials()

    # Seed a mixed desk (quiet + actionable) so OFF vs ON can diverge when Node B is live.
    ingress = run_canonical_runtime_ingress_from_fixture("post_offer_question")
    push = push_engagement_feed_for_ingress(ingress)
    assert push.get("ok") is True, push

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            with traced_journey_page(browser, journey_key="x1-02") as page:
                # Fresh Playwright context starts empty; do not use add_init_script to
                # clear STORAGE_KEY — init scripts re-run on reload and would wipe the
                # preference the test proves survives.
                playwright_login_daszek(
                    page, login=login, password=password, daszek_base=urls.daszek_base
                )
                toggle = _wait_desk_toggle(page)
                expect(toggle).not_to_be_checked()

                soft = _capture_latest_feed(page, expect_exceptions_only=False)
                assert soft["body"].get("live_preview") is not True
                assert soft["body"].get("exceptions_only") is not True

                with page.expect_response(
                    lambda resp: FEED_LATEST_FRAGMENT in resp.url
                    and ("exceptions_only=1" in resp.url or "exceptions_only=true" in resp.url.lower())
                    and resp.request.method == "GET",
                    timeout=60_000,
                ) as pending_on:
                    toggle.check()
                on_resp = pending_on.value
                assert on_resp.ok, f"exceptions_only feed HTTP {on_resp.status}"
                on_body = on_resp.json()
                assert on_body.get("ok") is True, on_body
                # Toggle switches Daszek from stored snapshot → Node B live exceptions_only preview.
                assert on_body.get("exceptions_only") is True, on_body
                assert on_body.get("live_preview") is True, on_body
                assert "exceptions_only=1" in on_resp.url or "exceptions_only=true" in on_resp.url.lower()
                # Visible UI hint after re-render.
                expect(page.locator(".desk-toggle-exceptions input")).to_be_checked()
                expect(page.get_by_text("Podgląd na żywo z Node B", exact=False)).to_be_visible(timeout=15_000)

                stored = page.evaluate(f"() => localStorage.getItem('{STORAGE_KEY}')")
                assert stored == "1"

                # Preference survives session reload.
                page.reload(wait_until="domcontentloaded", timeout=90_000)
                expect(page.locator("#main-screen")).to_be_visible(timeout=60_000)
                playwright_dismiss_onboarding(page)
                toggle_after = _wait_desk_toggle(page)
                expect(toggle_after).to_be_checked()
                assert page.evaluate(f"() => localStorage.getItem('{STORAGE_KEY}')") == "1"

                reloaded = _capture_latest_feed(page, expect_exceptions_only=True)
                assert (
                    reloaded["body"].get("exceptions_only") is True
                    or reloaded["body"].get("live_preview") is True
                    or "exceptions_only=1" in reloaded["url"]
                ), reloaded
        finally:
            browser.close()
