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
  guessed into `stagnating`;
* FG-03: mailbox cases in closed set (`closed|done|archived|resolved|cancelled|merged`) never
  receive a stagnation proposal even when the engagement snapshot looks stagnating.
* FG-02: durable `lifecycle_state_since` is preferred over row `updated_at` for hours-in-state;
  unrelated snapshot saves must not reset the stagnation clock.
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
    # Backdate both row clock and durable since (FG-02): insert stamps since=now.
    aged = _iso(hours_ago)
    store._rows[engagement_id]["updated_at"] = aged
    payload = store._rows[engagement_id]["snapshot_data"]
    assert isinstance(payload, dict)
    payload["lifecycle_state_since"] = aged


def test_save_snapshot_bumps_lifecycle_state_since_only_when_status_code_changes():
    from llm_contracts.engagement_snapshot_v2 import ActionItem

    store = InMemoryOperatorEngagementStore()
    snapshot = build_initial_snapshot(
        case_id="case_since", engagement_id="eng_since", trace_id="t_since"
    )
    store.insert_snapshot(snapshot)
    first = store.load_snapshot("eng_since")
    assert first is not None
    # Freeze a clearly older since so a same-second NOW() stamp is distinguishable.
    old_since = _iso(48.0)
    store._rows["eng_since"]["snapshot_data"]["lifecycle_state_since"] = old_since
    first = store.load_snapshot("eng_since")
    assert first is not None
    assert first.lifecycle_state_since == old_since

    # Unrelated mutation (actions) must not reset since, even though updated_at moves.
    with_actions = first.model_copy(
        update={
            "actions": [
                ActionItem(
                    id="noise",
                    enabled=True,
                    payload_pl="x",
                    case_id="case_since",
                )
            ]
        }
    )
    store.save_snapshot(with_actions, expected_version=first.version)
    after_noise = store.load_snapshot("eng_since")
    assert after_noise is not None
    assert after_noise.lifecycle_state_since == old_since
    assert store._rows["eng_since"]["updated_at"]

    # Status code change must bump since away from the frozen value.
    changed = after_noise.model_copy(
        update={
            "operational_status": after_noise.operational_status.model_copy(
                update={"code": "enriching"}
            )
        }
    )
    store.save_snapshot(changed, expected_version=after_noise.version)
    after_code = store.load_snapshot("eng_since")
    assert after_code is not None
    assert after_code.lifecycle_state_since != old_since
    assert after_code.lifecycle_state_since


def test_tick_stagnating_with_fresh_updated_at_when_lifecycle_state_since_is_old():
    """FG-02: guardian prefers since over updated_at — fresh save must not clear stagnation."""
    store = InMemoryOperatorEngagementStore()
    _seed(
        store,
        engagement_id="eng_proxy",
        case_id="case_proxy",
        status_code="enriching",
        hours_ago=60.0,
    )

    # Simulate an unrelated recent write that only bumps row updated_at.
    store._rows["eng_proxy"]["updated_at"] = _iso(0.1)

    result = run_follow_up_guardian_tick(store)

    assert result["ok"] is True
    assert "case_proxy" in result["proposed_case_ids"]
    snap = store.load_snapshot("eng_proxy")
    assert has_active_follow_up_proposal(snap) is True


def test_hours_in_lifecycle_state_prefers_since_over_updated_at():
    from follow_up_guardian import hours_in_lifecycle_state

    store = InMemoryOperatorEngagementStore()
    _seed(
        store,
        engagement_id="eng_hours",
        case_id="case_hours",
        status_code="enriching",
        hours_ago=60.0,
    )
    snap = store.load_snapshot("eng_hours")
    assert snap is not None
    hours = hours_in_lifecycle_state(snap, _iso(0.5), now=datetime.now(timezone.utc))
    assert hours is not None
    assert 49.0 <= hours <= 61.0


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
    aged = _iso(500.0)
    store._rows["stg_1"]["updated_at"] = aged
    payload = store._rows["stg_1"]["snapshot_data"]
    assert isinstance(payload, dict)
    payload["lifecycle_state_since"] = aged

    result = run_follow_up_guardian_tick(store)

    assert result["proposed_case_ids"] == []
    assert result["checked"] == 1


# ── FG-03: closed / merged mailbox cases must not get stagnation proposals ────


class _FakeMailboxStore:
    """Minimal mailbox SoT stub: only `fetch_case` for guardian status resolution."""

    def __init__(self, statuses: dict[str, str]):
        self._statuses = {str(k): str(v) for k, v in statuses.items()}

    def fetch_case(self, case_id: str) -> dict | None:
        cid = str(case_id or "").strip()
        if cid not in self._statuses:
            return None
        return {"case_id": cid, "status": self._statuses[cid]}


@pytest.mark.parametrize("closed_status", ["closed", "merged", "cancelled", "done", "archived", "resolved"])
def test_tick_skips_closed_or_merged_case_no_proposal(closed_status: str):
    store = InMemoryOperatorEngagementStore()
    _seed(
        store,
        engagement_id="eng_closed",
        case_id="case_closed",
        status_code="enriching",
        hours_ago=60.0,
    )
    mailbox = _FakeMailboxStore({"case_closed": closed_status})

    result = run_follow_up_guardian_tick(store, mailbox_store=mailbox)

    assert result["proposed_count"] == 0
    assert result["proposed_case_ids"] == []
    assert result["skipped_closed"] == 1
    snap = store.load_snapshot("eng_closed")
    assert has_active_follow_up_proposal(snap) is False


def test_tick_still_proposes_for_open_stagnating_case_with_mailbox_status():
    store = InMemoryOperatorEngagementStore()
    _seed(
        store,
        engagement_id="eng_open",
        case_id="case_open",
        status_code="enriching",
        hours_ago=60.0,
    )
    mailbox = _FakeMailboxStore({"case_open": "active"})

    result = run_follow_up_guardian_tick(store, mailbox_store=mailbox)

    assert result["proposed_count"] == 1
    assert "case_open" in result["proposed_case_ids"]
    assert result.get("skipped_closed", 0) == 0
    snap = store.load_snapshot("eng_open")
    assert has_active_follow_up_proposal(snap) is True


def test_fg01_option_b_waiting_case_status_uses_waiting_client_sla():
    """FG-01 Option B: mailbox `waiting` maps to WAITING_CLIENT (168h), not QUALIFICATION (48h)."""
    within_waiting_sla = evaluate_follow_up_candidate(
        operational_status_code="pending_operator",
        hours_since_update=60.0,
        case_status="waiting",
    )
    assert within_waiting_sla is None

    past_waiting_sla = evaluate_follow_up_candidate(
        operational_status_code="pending_operator",
        hours_since_update=200.0,
        case_status="waiting",
    )
    assert past_waiting_sla is not None
    assert past_waiting_sla["status"] == "stagnating"
    assert past_waiting_sla["sla_hours"] == 168
    assert past_waiting_sla["lifecycle_state"] == "waiting_for_client"


def test_fg01_option_b_tick_joins_mailbox_waiting_status():
    store = InMemoryOperatorEngagementStore()
    _seed(
        store,
        engagement_id="eng_wait",
        case_id="case_wait",
        status_code="pending_operator",
        hours_ago=60.0,
    )
    mailbox = _FakeMailboxStore({"case_wait": "waiting"})

    # 60h is past QUALIFICATION(48h) but within WAITING_CLIENT(168h) → no proposal.
    result = run_follow_up_guardian_tick(store, mailbox_store=mailbox)
    assert result["proposed_count"] == 0
    assert result["skipped_not_stagnating"] >= 1
