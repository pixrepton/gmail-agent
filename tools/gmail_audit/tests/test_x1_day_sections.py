"""X1 v0 — Karta dnia: RED/GREEN coverage for feed.day.sections composition.

RED-1: active feed builder emits non-empty feed.day.sections when source data exists.
RED-2: pending decision maps into "decyzje czekające" with honest waiting-time semantics.
RED-3: today calendar boundary (incl. all-day and overnight-crossing events).
RED-4: new cases since yesterday — Case semantics, not Signal semantics.
RED-5: no data about stagnation never produces a fake "zagrożone" section.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.store import build_initial_snapshot
from daszek_engagement_feed import build_operational_feed_from_engagement_store
from daszek_engagement_feed.day import OPERATOR_TIMEZONE, compose_day_sections
from mailbox_memory.inmemory import InMemoryMailboxMemoryStore


def _snapshot(*, case_id: str = "case_x1_1") -> object:
    return build_initial_snapshot(
        case_id=case_id,
        engagement_id=f"eng_{case_id}",
        trace_id=f"sig_{case_id}",
    )


class _FakeConnCtx:
    def __enter__(self):
        return object()

    def __exit__(self, *exc: object) -> bool:
        return False


class _DecisionOnlyStore:
    """Store with just enough surface to exercise the decision-queue path."""

    def _connect(self):
        return _FakeConnCtx()


def _noon_today_warsaw() -> datetime:
    return datetime.now(OPERATOR_TIMEZONE).replace(hour=12, minute=0, second=0, microsecond=0)


# ── RED-1 — feed.day is not empty when source data exists ──────────────────


def test_red1_active_feed_builder_emits_non_empty_day_sections_when_calendar_data_exists() -> None:
    store = InMemoryMailboxMemoryStore()
    now = _noon_today_warsaw()
    today_start_at = now.replace(hour=10, minute=0).astimezone(timezone.utc).isoformat()
    store.upsert_calendar_event(
        {
            "calendar_event_id": "ev-today-1",
            "case_id": "case_x1_1",
            "summary": "Wizyta montażowa",
            "start_at": today_start_at,
            "end_at": now.replace(hour=11, minute=0).astimezone(timezone.utc).isoformat(),
        }
    )
    snap = _snapshot(case_id="case_x1_1")

    envelope = build_operational_feed_from_engagement_store(
        _InMemoryOperatorStoreOf([snap]),
        case_ids=["case_x1_1"],
        mailbox_store=store,
    )

    sections = envelope["feed"]["day"]["sections"]
    assert sections != [], "feed.day.sections must not be empty when source data exists"


# ── RED-2 — decyzje czekające, honest waiting-time semantics ───────────────


def test_red2_pending_decision_maps_with_honest_waiting_semantics() -> None:
    fake_queue = [
        {
            "proposal_id": "prop-1",
            "engagement_id": "eng-1",
            "case_id": "case_dec_1",
            "created_at": "2026-07-15T10:00:00Z",
            "proposal_type": "draft_reply",
            "summary_pl": "Draft odpowiedzi do klienta.",
            "source_pipeline": "gmail",
            "hours_waiting": 6.5,
            "priority": "high",
        }
    ]
    with patch("divergence_loop.fetch_decision_queue", return_value=fake_queue):
        day = compose_day_sections(_DecisionOnlyStore(), [])

    decisions = next((s for s in day["sections"] if s["key"] == "decyzje_czekajace"), None)
    assert decisions is not None, "pending decision must produce a decyzje_czekajace section"
    item = decisions["items"][0]
    assert item["case_id"] == "case_dec_1"
    assert "6.5" in item["why_on_desk"]
    # No fabricated business priority label anywhere on the item.
    assert "priority" not in item
    assert "ai" not in str(item).lower()


def test_red2_decisions_keep_source_order_oldest_first() -> None:
    fake_queue = [
        {"proposal_id": "p-old", "case_id": "c1", "created_at": "2026-07-14T00:00:00Z",
         "proposal_type": "draft_reply", "hours_waiting": 40.0, "priority": "critical"},
        {"proposal_id": "p-new", "case_id": "c2", "created_at": "2026-07-16T00:00:00Z",
         "proposal_type": "draft_reply", "hours_waiting": 2.0, "priority": "normal"},
    ]
    with patch("divergence_loop.fetch_decision_queue", return_value=fake_queue):
        day = compose_day_sections(_DecisionOnlyStore(), [])

    items = day["sections"][0]["items"]
    assert [i["note_id"] for i in items] == ["decision-p-old", "decision-p-new"], (
        "v0 must not re-sort decisions by a new priority heuristic — "
        "preserve created_at ASC exactly as fetch_decision_queue returns it"
    )


# ── RED-3 — today calendar boundary ─────────────────────────────────────────


def test_red3_event_today_is_included_event_outside_today_is_excluded() -> None:
    store = InMemoryMailboxMemoryStore()
    now = _noon_today_warsaw()
    store.upsert_calendar_event(
        {
            "calendar_event_id": "ev-today",
            "case_id": "case_x1_1",
            "summary": "Dzisiaj",
            "start_at": now.replace(hour=9).astimezone(timezone.utc).isoformat(),
            "end_at": now.replace(hour=10).astimezone(timezone.utc).isoformat(),
        }
    )
    store.upsert_calendar_event(
        {
            "calendar_event_id": "ev-tomorrow",
            "case_id": "case_x1_1",
            "summary": "Jutro",
            "start_at": (now + timedelta(days=1)).replace(hour=9).astimezone(timezone.utc).isoformat(),
            "end_at": (now + timedelta(days=1)).replace(hour=10).astimezone(timezone.utc).isoformat(),
        }
    )

    day = compose_day_sections(store, [_snapshot(case_id="case_x1_1")], now=now)

    visits = next((s for s in day["sections"] if s["key"] == "wizyty_dzis"), None)
    assert visits is not None
    titles = {i["title"] for i in visits["items"]}
    assert "Dzisiaj" in titles
    assert "Jutro" not in titles


def test_red3_overnight_event_crossing_into_today_is_included() -> None:
    store = InMemoryMailboxMemoryStore()
    now = _noon_today_warsaw()
    yesterday_2300 = (now - timedelta(days=1)).replace(hour=23, minute=0)
    today_0100 = now.replace(hour=1, minute=0)
    store.upsert_calendar_event(
        {
            "calendar_event_id": "ev-overnight",
            "case_id": "case_x1_1",
            "summary": "Nocna zmiana",
            "start_at": yesterday_2300.astimezone(timezone.utc).isoformat(),
            "end_at": today_0100.astimezone(timezone.utc).isoformat(),
        }
    )

    day = compose_day_sections(store, [_snapshot(case_id="case_x1_1")], now=now)

    visits = next((s for s in day["sections"] if s["key"] == "wizyty_dzis"), None)
    assert visits is not None, "event starting before midnight and continuing into today must be included"
    assert visits["items"][0]["title"] == "Nocna zmiana"


def test_red3_all_day_event_today_is_included() -> None:
    store = InMemoryMailboxMemoryStore()
    now = _noon_today_warsaw()
    today_date = now.date().isoformat()
    store.upsert_calendar_event(
        {
            "calendar_event_id": "ev-allday",
            "case_id": "case_x1_1",
            "summary": "Całodniowe dzisiaj",
            "start_at": today_date,
            "end_at": "",
        }
    )

    day = compose_day_sections(store, [_snapshot(case_id="case_x1_1")], now=now)

    visits = next((s for s in day["sections"] if s["key"] == "wizyty_dzis"), None)
    assert visits is not None
    assert visits["items"][0]["title"] == "Całodniowe dzisiaj"


def test_red3_all_day_event_yesterday_is_excluded() -> None:
    store = InMemoryMailboxMemoryStore()
    now = _noon_today_warsaw()
    yesterday_date = (now - timedelta(days=1)).date().isoformat()
    store.upsert_calendar_event(
        {
            "calendar_event_id": "ev-allday-yesterday",
            "case_id": "case_x1_1",
            "summary": "Całodniowe wczoraj",
            "start_at": yesterday_date,
            "end_at": "",
        }
    )

    day = compose_day_sections(store, [_snapshot(case_id="case_x1_1")], now=now)

    visits = next((s for s in day["sections"] if s["key"] == "wizyty_dzis"), None)
    assert visits is None


# ── RED-4 — new cases since yesterday (Case semantics, not Signal) ─────────


def test_red4_new_case_maps_into_section_old_case_does_not() -> None:
    fake_delta = {
        "ok": True,
        "delta": {
            "new_cases": 1,
            "new_cases_list": [{"case_id": "case_new_1", "client": "Jan Kowalski"}],
            "since": "2026-07-15T00:00:00Z",
        },
    }
    with patch("agent_runtime.business_pulse.get_daily_delta", return_value=fake_delta):
        day = compose_day_sections(_DecisionOnlyStore(), [])

    new_cases = next((s for s in day["sections"] if s["key"] == "nowe_sprawy"), None)
    assert new_cases is not None
    assert new_cases["title"] == "Nowe sprawy od wczoraj"
    assert [i["case_id"] for i in new_cases["items"]] == ["case_new_1"]


def test_red4_no_new_cases_produces_no_section() -> None:
    fake_delta = {"ok": True, "delta": {"new_cases": 0, "new_cases_list": [], "since": "2026-07-15T00:00:00Z"}}
    with patch("agent_runtime.business_pulse.get_daily_delta", return_value=fake_delta):
        day = compose_day_sections(_DecisionOnlyStore(), [])

    assert all(s["key"] != "nowe_sprawy" for s in day["sections"])


# ── RED-5 — no fabricated stagnation section ────────────────────────────────


def test_red5_no_data_never_produces_a_stagnation_section() -> None:
    with patch("divergence_loop.fetch_decision_queue", return_value=[]), \
         patch("agent_runtime.business_pulse.get_daily_delta", return_value={"ok": False, "error": "no db"}):
        day = compose_day_sections(_DecisionOnlyStore(), [_snapshot()])

    assert day["sections"] == []
    blob = str(day).lower()
    for forbidden in ("stagnac", "zagrożon", "zagrozon", "at_risk", "at-risk"):
        assert forbidden not in blob


def test_red5_composed_sections_never_contain_stagnation_keys_even_with_full_data() -> None:
    fake_queue = [{
        "proposal_id": "p1", "case_id": "c1", "created_at": "2026-07-15T00:00:00Z",
        "proposal_type": "draft_reply", "hours_waiting": 3.0, "priority": "normal",
    }]
    fake_delta = {"ok": True, "delta": {"new_cases": 1, "new_cases_list": [{"case_id": "c2", "client": "X"}], "since": ""}}
    with patch("divergence_loop.fetch_decision_queue", return_value=fake_queue), \
         patch("agent_runtime.business_pulse.get_daily_delta", return_value=fake_delta):
        day = compose_day_sections(_DecisionOnlyStore(), [])

    keys = {s["key"] for s in day["sections"]}
    assert keys == {"decyzje_czekajace", "nowe_sprawy"}


# ── helpers ──────────────────────────────────────────────────────────────


class _InMemoryOperatorStoreOf:
    """Minimal OperatorEngagementStore stand-in exposing only what
    build_operational_feed_from_engagement_store needs for these tests."""

    def __init__(self, snapshots: list[object]) -> None:
        self._by_case = {s.case_id: s for s in snapshots}

    def load_snapshots_for_case_ids(self, case_ids: list[str]) -> list[object]:
        return [self._by_case[cid] for cid in case_ids if cid in self._by_case]
