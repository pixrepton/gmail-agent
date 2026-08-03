"""On a confidence tie, split_conflicting_facts must keep the NEWEST fact as
active, not the oldest. The prior implementation sorted by
(-confidence, observed_at) ascending, which put the lexicographically smallest
(oldest) ISO timestamp first on a tie -- silently reviving stale values
(e.g. an old scheduled_visit date) over a freshly observed one with equal
confidence.
"""

from __future__ import annotations

from mailbox_memory_runtime import split_conflicting_facts


def test_confidence_tie_keeps_the_newest_fact_active() -> None:
    facts = [
        {
            "entity_scope": "case",
            "fact_key": "scheduled_visit",
            "normalized_value": "2026-01-05",
            "confidence": 0.9,
            "observed_at": "2026-01-01T10:00:00Z",
        },
        {
            "entity_scope": "case",
            "fact_key": "scheduled_visit",
            "normalized_value": "2026-02-10",
            "confidence": 0.9,
            "observed_at": "2026-02-01T10:00:00Z",
        },
    ]
    active, conflicts = split_conflicting_facts(facts)
    assert len(active) == 1
    assert active[0]["normalized_value"] == "2026-02-10"
    assert conflicts == [
        {"entity_scope": "case", "fact_key": "scheduled_visit", "values": ["2026-01-05", "2026-02-10"]}
    ]


def test_higher_confidence_still_wins_regardless_of_recency() -> None:
    facts = [
        {
            "entity_scope": "case",
            "fact_key": "heated_area_m2",
            "normalized_value": "150",
            "confidence": 0.95,
            "observed_at": "2026-01-01T10:00:00Z",
        },
        {
            "entity_scope": "case",
            "fact_key": "heated_area_m2",
            "normalized_value": "120",
            "confidence": 0.6,
            "observed_at": "2026-02-01T10:00:00Z",
        },
    ]
    active, _conflicts = split_conflicting_facts(facts)
    assert active[0]["normalized_value"] == "150"


def test_superseded_fact_never_wins_over_an_active_one_even_with_higher_confidence() -> None:
    """RP-29 (`append_facts_with_supersession`) marks a replaced fact row
    `status="superseded"` in the DB, but this function never filtered on
    `status` -- so a superseded row with higher confidence/newer `observed_at`
    than the genuinely active row could still be picked as `ranked[0]`,
    silently reviving a value the write path already declared stale. This is
    exactly the RP-29/heated_area_m2 shape from
    `tests/test_rp29_fact_supersession.py`, just fed through the read path.
    """
    facts = [
        {
            "entity_scope": "building",
            "fact_key": "heated_area_m2",
            "normalized_value": "120",
            "confidence": 0.95,
            "observed_at": "2026-08-03T08:00:00Z",
            "status": "superseded",
        },
        {
            "entity_scope": "building",
            "fact_key": "heated_area_m2",
            "normalized_value": "140",
            "confidence": 0.6,
            "observed_at": "2026-08-03T09:00:00Z",
            "status": "active",
        },
    ]
    active, conflicts = split_conflicting_facts(facts)
    assert len(active) == 1
    assert active[0]["normalized_value"] == "140"
    assert active[0]["status"] == "active"
    # A superseded value is a resolved fact, not a live disagreement.
    assert conflicts == []


def test_facts_without_a_status_field_are_still_treated_as_active() -> None:
    """Plain dicts built by callers/tests that never set `status` at all
    (e.g. the two tests above this one) must keep working exactly as before --
    only an explicit `status="superseded"` is excluded.
    """
    facts = [
        {
            "entity_scope": "case",
            "fact_key": "city",
            "normalized_value": "Radlin",
            "confidence": 0.8,
            "observed_at": "2026-08-03T08:00:00Z",
        }
    ]
    active, conflicts = split_conflicting_facts(facts)
    assert active[0]["normalized_value"] == "Radlin"
    assert conflicts == []
