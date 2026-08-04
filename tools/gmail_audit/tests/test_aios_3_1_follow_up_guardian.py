"""AI-OS Roadmap 3.1 -- Follow-up Guardian.

Contract asserted here:

* silence past a state's SLA budget (roadmap 2.1's `stagnation_sot`, reused unchanged) produces
  a NEW operator-facing proposal where NONE existed before -- the gap `SUFITY.md` named as
  Journey D ("cisza N dni -> propozycja nie istnieje w kodzie w ogole");
* the proposal is an ordinary `ActionItem` with `enabled=True`, so `feed_visibility`'s existing
  `_has_pending_operator_work` (roadmap 2.4) promotes the case to `attention_required` on X1
  with ZERO changes to visibility logic itself;
* a case within its SLA budget, or already carrying an active guardian proposal, is left alone
  (no duplicate proposals, no false positives on healthy waiting);
* a case whose `operational_status.code` has no lifecycle/SLA mapping is `not_evaluable`, never
  guessed into `stagnating`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot  # noqa: E402
from feed_visibility import is_main_feed_member  # noqa: E402
from follow_up_guardian import (  # noqa: E402
    FOLLOW_UP_ACTION_ID,
    build_follow_up_action_item,
    evaluate_follow_up_candidate,
    has_active_follow_up_proposal,
    hours_since_updated_at,
    run_follow_up_guardian_tick,
)


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ── pure helpers ────────────────────────────────────────────────────────────────────────────


def test_hours_since_updated_at_measures_real_elapsed_time():
    hours = hours_since_updated_at(_iso(50), now=datetime.now(timezone.utc))
    assert hours is not None
    assert 49.9 <= hours <= 50.1


def test_hours_since_updated_at_unmeasurable_on_missing_or_bad_value():
    assert hours_since_updated_at("") is None
    assert hours_since_updated_at("not-a-timestamp") is None


def test_evaluate_follow_up_candidate_none_when_within_sla():
    # qualification (enriching) SLA budget is 48h
    verdict = evaluate_follow_up_candidate(operational_status_code="enriching", hours_since_update=10.0)
    assert verdict is None


def test_evaluate_follow_up_candidate_stagnating_past_sla():
    verdict = evaluate_follow_up_candidate(operational_status_code="enriching", hours_since_update=60.0)
    assert verdict is not None
    assert verdict["status"] == "stagnating"
    assert verdict["sla_hours"] == 48


def test_evaluate_follow_up_candidate_unevaluable_code_never_guessed():
    # OperationalStatus.code has no such literal / lifecycle mapping today
    verdict = evaluate_follow_up_candidate(operational_status_code="unknown_code", hours_since_update=1000.0)
    assert verdict is None


def test_evaluate_follow_up_candidate_unmeasured_hours_never_flagged():
    verdict = evaluate_follow_up_candidate(operational_status_code="enriching", hours_since_update=None)
    assert verdict is None


def test_build_follow_up_action_item_shape():
    verdict = evaluate_follow_up_candidate(operational_status_code="ready_for_quote", hours_since_update=30.0)
    assert verdict is not None
    item = build_follow_up_action_item(
        case_id="case_1", operational_status_code="ready_for_quote", verdict=verdict
    )
    assert item["id"] == FOLLOW_UP_ACTION_ID
    assert item["enabled"] is True
    assert item["source_signal_id"] == ""
    assert item["case_id"] == "case_1"
    assert "follow-up" in item["payload_pl"].lower()


# ── tick orchestration (InMemoryOperatorEngagementStore) ──────────────────────────────────────


def _seed(
    store: InMemoryOperatorEngagementStore,
    *,
    engagement_id: str,
    case_id: str,
    status_code: str,
    hours_ago: float,
) -> None:
    snapshot = build_initial_snapshot(
        case_id=case_id, engagement_id=engagement_id, trace_id=f"trace_{engagement_id}"
    )
    snapshot = snapshot.model_copy(
        update={"operational_status": snapshot.operational_status.model_copy(update={"code": status_code})}
    )
    store.insert_snapshot(snapshot)
    store._rows[engagement_id]["updated_at"] = _iso(hours_ago)


def test_tick_proposes_follow_up_for_newly_stagnating_case():
    store = InMemoryOperatorEngagementStore()
    _seed(store, engagement_id="eng_stale", case_id="case_stale", status_code="enriching", hours_ago=60.0)

    result = run_follow_up_guardian_tick(store)

    assert result["ok"] is True
    assert "case_stale" in result["proposed_case_ids"]
    snap = store.load_snapshot("eng_stale")
    assert has_active_follow_up_proposal(snap) is True
    assert is_main_feed_member(snap) is True  # roadmap 2.4 promotion, unmodified


def test_tick_leaves_healthy_waiting_case_untouched():
    store = InMemoryOperatorEngagementStore()
    _seed(store, engagement_id="eng_fresh", case_id="case_fresh", status_code="enriching", hours_ago=2.0)

    result = run_follow_up_guardian_tick(store)

    assert result["proposed_case_ids"] == []
    assert result["skipped_not_stagnating"] == 1
    snap = store.load_snapshot("eng_fresh")
    assert has_active_follow_up_proposal(snap) is False


def test_tick_does_not_duplicate_proposal_on_repeat_ticks():
    store = InMemoryOperatorEngagementStore()
    _seed(store, engagement_id="eng_repeat", case_id="case_repeat", status_code="enriching", hours_ago=60.0)

    first = run_follow_up_guardian_tick(store)
    assert first["proposed_count"] == 1

    second = run_follow_up_guardian_tick(store)
    assert second["proposed_count"] == 0
    assert second["skipped_already_proposed"] == 1

    snap = store.load_snapshot("eng_repeat")
    guardian_actions = [a for a in snap.actions if a.id == FOLLOW_UP_ACTION_ID]
    assert len(guardian_actions) == 1


def test_tick_skips_staging_snapshots_without_case_id():
    store = InMemoryOperatorEngagementStore()
    from agent_runtime.store import build_staging_snapshot

    staging = build_staging_snapshot(engagement_id="stg_1", trace_id="t1")
    store.insert_snapshot(staging)
    store._rows["stg_1"]["updated_at"] = _iso(500.0)

    result = run_follow_up_guardian_tick(store)

    assert result["proposed_case_ids"] == []
    assert result["checked"] == 1
