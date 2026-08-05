"""AI-OS 3.6 — runtime noise control via canonical Gmail signal ingress."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from agent_runtime.store import InMemoryOperatorEngagementStore
from daszek_engagement_feed.build import _list_main_feed_snapshots
from feed_visibility import VISIBILITY_HIDDEN, classify_signal_for_feed, is_main_feed_member
from fixture_helpers import build_fixture_snapshot, load_fixture
from observation_triage import triage_gmail_observation
from preclassifier import preclassify_snapshot

from aios_bounded_runtime_support import get_active_manifest, record_journey_result
from aios_canonical_runtime_ingress import run_canonical_runtime_noise_ingress


def test_noise_canonical_runtime_intake_rejects_operator_surface() -> None:
    message_payload, _expected = load_fixture("obvious_noise")
    snapshot = build_fixture_snapshot(message_payload)
    pre = preclassify_snapshot(snapshot)
    assert pre["lane"] == "skip"

    triage = triage_gmail_observation(
        __import__("gmail_signal_adapter", fromlist=["build_gmail_raw_observation"]).build_gmail_raw_observation(
            snapshot=snapshot,
            created_by_runtime="test_aios_36",
        )
    )
    assert triage["triage_class"] == "ignore"

    noise = run_canonical_runtime_noise_ingress()
    assert noise["seed_method"] == "canonical_runtime_ingress"
    assert noise["direct_database_seed_used"] is False
    assert noise["case_created"] is False
    assert noise["hitl_created"] is False

    visibility = classify_signal_for_feed(preclassification_result=pre, triage_result=triage)
    assert visibility["mode"] == VISIBILITY_HIDDEN

    eng_store = InMemoryOperatorEngagementStore()
    assert _list_main_feed_snapshots(eng_store.list_recent_snapshots, limit=50) == []
    for snap in eng_store.list_recent_snapshots(limit=50):
        assert snap.hitl_gate.required is False
        assert not any(action.enabled for action in snap.actions)
        assert not is_main_feed_member(snap)

    manifest = get_active_manifest()
    record_journey_result(
        manifest,
        "3.6_noise_control",
        {
            **noise,
            "status": "PASS",
            "correlation_id": noise["ingress_receipt_id"],
            "classification": "noise",
            "visible_on_x1": False,
        },
    )
