"""AI-OS Roadmap 3.5 — Playwright customer email journey (Daszek approval)."""

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
    playwright_approve_hitl_without_send,
    playwright_login_daszek,
    record_journey_result,
    require_bounded_runtime,
    traced_journey_page,
    wait_for_ready_for_manual_send,
)
from aios_canonical_runtime_ingress import (  # noqa: E402
    push_engagement_feed_for_ingress,
    run_canonical_runtime_ingress_from_fixture,
)

FIXTURE_NAME = "post_offer_question"


def _daszek_credentials() -> tuple[str, str]:
    return load_daszek_credentials()


@pytest.mark.playwright
def test_playwright_customer_email_journey_approval_bounded() -> None:
    urls = require_bounded_runtime()
    login, password = _daszek_credentials()

    ingress = run_canonical_runtime_ingress_from_fixture(FIXTURE_NAME)
    assert ingress.seed_method == "canonical_runtime_ingress"
    assert ingress.direct_database_seed_used is False
    assert ingress.case_id
    assert ingress.draft_id
    assert ingress.hitl_id
    assert ingress.draft_body
    assert ingress.preclassification_lane != "skip"

    push = push_engagement_feed_for_ingress(ingress)
    assert push.get("ok") is True, push
    assert push.get("skipped") is not True, push

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            with traced_journey_page(browser, journey_key="3.5") as page:
                playwright_login_daszek(page, login=login, password=password, daszek_base=urls.daszek_base)
                playwright_approve_hitl_without_send(
                    page,
                    engagement_id=ingress.engagement_id,
                    case_id=ingress.case_id,
                    draft_hint=ingress.draft_body,
                )

                final = wait_for_ready_for_manual_send(ingress.store, ingress.engagement_id)
                assert final.communication_receipt.state == "ready_for_manual_send"
                assert final.communication_receipt.gmail_message_id == ""
                assert final.hitl_gate.required is False

                manifest = get_active_manifest()
                payload = ingress.manifest_payload()
                payload.update(
                    {
                        "status": "PASS",
                        "correlation_id": ingress.message_id,
                        "approval_receipt_id": ingress.draft_id,
                        "communication_sent_count": 0,
                        "feed_push_ok": bool(push.get("ok")),
                    }
                )
                record_journey_result(manifest, "3.5", payload)
                manifest["live_send_invocations"] = 0
                manifest["seed_method"] = ingress.seed_method
                manifest["direct_database_seed_used"] = False
        finally:
            browser.close()
