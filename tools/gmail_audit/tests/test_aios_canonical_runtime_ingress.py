"""In-process canonical ingress proofs for AI-OS 3.5 / 3.6."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
TOOL_DIR = TESTS_DIR.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from aios_canonical_runtime_ingress import (
    DirectDatabaseSeedForbidden,
    assert_no_direct_database_seed,
    canonical_runtime_ingress_scope,
    run_canonical_runtime_ingress_from_fixture,
    run_canonical_runtime_ingress_from_snapshot,
    run_canonical_runtime_noise_ingress,
)

COMPLAINT_BODY = (
    "Zglaszam reklamacje montazu klimatyzacji. Urzadzenie nie chlodzi od tygodnia, "
    "prosze o pilna wizyte serwisowa i potwierdzenie terminu naprawy gwarancyjnej."
)


def _complaint_snapshot(*, message_id: str) -> dict:
    return {
        "mailbox": "ops@topinstal.local",
        "source_message": {
            "message_id": message_id,
            "thread_id": f"thread-complaint-{message_id}",
            "date": "2026-08-04T09:30:00+02:00",
            "from": "klient@example.com",
            "to": ["ops@topinstal.local"],
            "subject": "Reklamacja montazu klimatyzacji",
            "snippet": COMPLAINT_BODY[:80],
            "body": COMPLAINT_BODY,
            "labels": ["INBOX"],
        },
        "context_messages": [],
    }


def test_customer_fixture_canonical_ingress_creates_hitl_without_direct_seed() -> None:
    result = run_canonical_runtime_ingress_from_fixture("post_offer_question")
    assert result.seed_method == "canonical_runtime_ingress"
    assert result.direct_database_seed_used is False
    assert result.case_id
    assert result.draft_id
    assert result.hitl_id
    snap = result.store.load_snapshot(result.engagement_id)
    assert snap is not None
    assert snap.hitl_gate.required is True
    assert len([a for a in snap.actions if a.enabled]) == 1


def test_complaint_canonical_ingress_creates_hitl_without_direct_seed() -> None:
    result = run_canonical_runtime_ingress_from_snapshot(
        _complaint_snapshot(message_id=f"msg-complaint-{uuid.uuid4().hex[:10]}")
    )
    assert result.seed_method == "canonical_runtime_ingress"
    assert result.direct_database_seed_used is False
    assert "reklamac" in COMPLAINT_BODY.lower()


def test_noise_canonical_ingress_rejects_case_and_hitl() -> None:
    noise = run_canonical_runtime_noise_ingress(unique_suffix=f"proc-{uuid.uuid4().hex[:8]}")
    assert noise["seed_method"] == "canonical_runtime_ingress"
    assert noise["direct_database_seed_used"] is False
    assert noise["case_created"] is False
    assert noise["hitl_created"] is False


def test_direct_database_seed_guard_raises_in_ingress_scope() -> None:
    with canonical_runtime_ingress_scope():
        with pytest.raises(DirectDatabaseSeedForbidden):
            assert_no_direct_database_seed("insert_snapshot")
