"""AI-OS Roadmap X1-01 — Playwright feed-visibility reclassify (Daszek → Node B).

Journey (bounded):
  operator reclassifies card → POST …/feed-visibility/override → CAS save
  → receipt with requested vs effective → refreshed Daszek projection.

Gate A: skips unless ``AIOS_RUNTIME_PROOF_REQUIRED=1`` (same pattern as 3.5/3.6).
Live run also needs local stack + ``.env.playwright.local`` credentials.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

TOOL_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from aios_bounded_runtime_support import (  # noqa: E402
    get_active_manifest,
    load_daszek_credentials,
    playwright_apply_feed_visibility_override,
    playwright_login_daszek,
    record_journey_result,
    require_bounded_runtime,
    traced_journey_page,
)
from aios_canonical_runtime_ingress import (  # noqa: E402
    push_engagement_feed_for_ingress,
    run_canonical_runtime_ingress_from_fixture,
)
from feed_visibility import (  # noqa: E402
    VISIBILITY_ATTENTION_REQUIRED,
    VISIBILITY_CASE_TIMELINE_ONLY,
    VISIBILITY_HIDDEN,
    VISIBILITY_MAIN_FEED,
)

FIXTURE_NAME = "post_offer_question"
OVERRIDE_MODE = VISIBILITY_CASE_TIMELINE_ONLY
_LEGAL_EFFECTIVE = {
    VISIBILITY_HIDDEN,
    VISIBILITY_CASE_TIMELINE_ONLY,
    VISIBILITY_MAIN_FEED,
    VISIBILITY_ATTENTION_REQUIRED,
}


def _daszek_credentials() -> tuple[str, str]:
    return load_daszek_credentials()


@pytest.mark.playwright
def test_playwright_feed_visibility_reclassify_cas_receipt_bounded() -> None:
    urls = require_bounded_runtime()
    login, password = _daszek_credentials()

    ingress = run_canonical_runtime_ingress_from_fixture(FIXTURE_NAME)
    assert ingress.seed_method == "canonical_runtime_ingress"
    assert ingress.direct_database_seed_used is False
    assert ingress.case_id
    assert ingress.engagement_id

    before = ingress.store.load_snapshot(ingress.engagement_id)
    assert before is not None
    version_before = int(before.version)

    push = push_engagement_feed_for_ingress(ingress)
    assert push.get("ok") is True, push
    assert push.get("skipped") is not True, push

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            with traced_journey_page(browser, journey_key="x1_01") as page:
                playwright_login_daszek(
                    page, login=login, password=password, daszek_base=urls.daszek_base
                )
                receipt = playwright_apply_feed_visibility_override(
                    page,
                    engagement_id=ingress.engagement_id,
                    case_id=ingress.case_id,
                    mode=OVERRIDE_MODE,
                    reason="x1-01-reclassify",
                )

                assert receipt.get("ok") is True
                assert receipt["requested_override_mode"] == OVERRIDE_MODE
                assert receipt["stored_feed_visibility_mode"] == OVERRIDE_MODE
                assert receipt["effective_feed_visibility_mode"] in _LEGAL_EFFECTIVE
                assert receipt["feed_visibility_mode"] == receipt["effective_feed_visibility_mode"]
                # HITL-pending ingress typically yields attention_required ≠ requested.
                if receipt["effective_feed_visibility_mode"] == VISIBILITY_ATTENTION_REQUIRED:
                    assert (
                        receipt["requested_override_mode"]
                        != receipt["effective_feed_visibility_mode"]
                    )

                after = ingress.store.load_snapshot(ingress.engagement_id)
                assert after is not None
                assert after.feed_visibility is not None
                assert after.feed_visibility.mode == OVERRIDE_MODE
                assert after.feed_visibility.operator_override is True
                # CAS path: successful mutate bumps optimistic-lock version.
                assert int(after.version) > version_before
                assert int(receipt.get("version") or after.version) == int(after.version)

                manifest = get_active_manifest()
                payload = ingress.manifest_payload()
                payload.update(
                    {
                        "status": "PASS",
                        "correlation_id": ingress.message_id,
                        "requested_override_mode": receipt["requested_override_mode"],
                        "effective_feed_visibility_mode": receipt[
                            "effective_feed_visibility_mode"
                        ],
                        "stored_feed_visibility_mode": receipt["stored_feed_visibility_mode"],
                        "version_before": version_before,
                        "version_after": int(after.version),
                        "feed_push_ok": bool(push.get("ok")),
                        "cas_version_bumped": True,
                    }
                )
                record_journey_result(manifest, "x1_01", payload)
                manifest["live_send_invocations"] = 0
                manifest["seed_method"] = ingress.seed_method
                manifest["direct_database_seed_used"] = False
        finally:
            browser.close()
