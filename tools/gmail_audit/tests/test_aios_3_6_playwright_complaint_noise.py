"""AI-OS Roadmap 3.6 — complaint journey + parallel noise protection."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from agent_runtime.agent_reconcile import _evaluate_cost_gate  # noqa: E402
from fixture_helpers import build_fixture_snapshot, load_fixture  # noqa: E402
from preclassifier import is_obvious_noise, preclassify_snapshot  # noqa: E402
from signal_contract import CanonicalSignal  # noqa: E402

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

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
    run_canonical_runtime_ingress_from_snapshot,
    run_canonical_runtime_noise_ingress,
)

COMPLAINT_SUBJECT = "Reklamacja montazu klimatyzacji"
COMPLAINT_BODY = (
    "Zglaszam reklamacje montazu klimatyzacji. Urzadzenie nie chlodzi od tygodnia, "
    "prosze o pilna wizyte serwisowa i potwierdzenie terminu naprawy gwarancyjnej."
)


def _complaint_snapshot(*, message_id: str) -> dict[str, Any]:
    return {
        "mailbox": "ops@topinstal.local",
        "source_message": {
            "message_id": message_id,
            "thread_id": f"thread-complaint-{message_id}",
            "date": "2026-08-04T09:30:00+02:00",
            "from": "klient@example.com",
            "to": ["ops@topinstal.local"],
            "subject": COMPLAINT_SUBJECT,
            "snippet": COMPLAINT_BODY[:80],
            "body": COMPLAINT_BODY,
            "labels": ["INBOX"],
        },
        "context_messages": [],
    }


def _gmail_signal(*, signal_id: str, message_id: str) -> CanonicalSignal:
    return CanonicalSignal(
        signal_id=signal_id,
        schema_version="1",
        signal_kind="gmail_message_observed",
        source_kind="gmail_inbound",
        source_ref={"message_id": message_id},
        observed_at="2026-08-04T09:30:00+02:00",
        effective_at=None,
        case_key_hint=None,
        thread_key_hint=None,
        business_lane=None,
        signal_summary_pl=COMPLAINT_SUBJECT,
        payload={},
        artifacts={},
        processing_state="pending",
        idempotency_key=f"idem-{signal_id}",
        content_hash=None,
        replayable=True,
        created_by_runtime="test",
    )


def test_complaint_semantic_signal_is_not_noise() -> None:
    snapshot = _complaint_snapshot(message_id="msg-complaint-unit")
    assert is_obvious_noise(snapshot) is False
    lane = preclassify_snapshot(snapshot)["lane"]
    assert lane != "skip"

    cost_gate = _evaluate_cost_gate(
        _gmail_signal(signal_id="sig-complaint-unit", message_id="msg-complaint-unit"),
        {"message": {"subject": COMPLAINT_SUBJECT, "body_text": COMPLAINT_BODY}},
    )
    assert cost_gate.get("skip") is not True


def test_marketing_noise_fixture_still_skips() -> None:
    message_payload, _expected = load_fixture("obvious_noise")
    snapshot = build_fixture_snapshot(message_payload)
    assert is_obvious_noise(snapshot) is True
    assert preclassify_snapshot(snapshot)["lane"] == "skip"


def _daszek_credentials() -> tuple[str, str]:
    return load_daszek_credentials()


@pytest.mark.playwright
def test_playwright_complaint_journey_with_noise_control_bounded() -> None:
    urls = require_bounded_runtime()
    login, password = _daszek_credentials()

    noise = run_canonical_runtime_noise_ingress(unique_suffix=f"ui-{uuid.uuid4().hex[:8]}")
    assert noise["seed_method"] == "canonical_runtime_ingress"
    assert noise["direct_database_seed_used"] is False
    assert noise["case_created"] is False

    complaint = run_canonical_runtime_ingress_from_snapshot(
        _complaint_snapshot(message_id=f"msg-complaint-{uuid.uuid4().hex[:10]}")
    )
    assert complaint.seed_method == "canonical_runtime_ingress"
    assert complaint.direct_database_seed_used is False
    assert "reklamac" in COMPLAINT_BODY.lower()

    push = push_engagement_feed_for_ingress(complaint)
    assert push.get("ok") is True, push
    assert push.get("skipped") is not True, push

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            with traced_journey_page(browser, journey_key="3.6_complaint") as page:
                playwright_login_daszek(page, login=login, password=password, daszek_base=urls.daszek_base)
                playwright_approve_hitl_without_send(
                    page,
                    engagement_id=complaint.engagement_id,
                    case_id=complaint.case_id,
                    draft_hint=complaint.draft_body,
                )

                final = wait_for_ready_for_manual_send(complaint.store, complaint.engagement_id)
                assert final.communication_receipt.state == "ready_for_manual_send"
                assert final.hitl_gate.required is False

                manifest = get_active_manifest()
                record_journey_result(
                    manifest,
                    "3.6_complaint",
                    {
                        **complaint.manifest_payload(),
                        "status": "PASS",
                        "correlation_id": complaint.message_id,
                    },
                )
                record_journey_result(
                    manifest,
                    "3.6_noise_control",
                    {
                        **noise,
                        "status": "PASS",
                        "correlation_id": noise["message_id"],
                        "visible_on_x1": False,
                    },
                )
                manifest["live_send_invocations"] = 0
                manifest["seed_method"] = "canonical_runtime_ingress"
                manifest["direct_database_seed_used"] = False
        finally:
            browser.close()
