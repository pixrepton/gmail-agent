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
