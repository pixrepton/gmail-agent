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
    """A superseded value is a settled fact, not a live disagreement. The
    active selection must still ignore it even with higher confidence/newer
    `observed_at` (RP-29), and it must not surface as a conflict unless the
    supersession reason marks an ambiguous customer message change.
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
    assert conflicts == []


def test_explicit_correction_supersession_is_not_a_conflict() -> None:
    """A write path that marks a supersession as an explicit correction means the
    old value was declared wrong, not a competing live value -- no conflict."""
    facts = [
        {
            "entity_scope": "building",
            "fact_key": "heated_area_m2",
            "normalized_value": "120",
            "confidence": 0.95,
            "observed_at": "2026-08-03T08:00:00Z",
            "status": "superseded",
            "metadata": {"supersede_reason": "explicit_correction"},
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
    assert active[0]["normalized_value"] == "140"
    assert conflicts == []


def test_customer_message_value_change_is_a_conflict() -> None:
    """CTX-03: a new customer message replaces a prior value with a different
    value (`supersede_reason="replace_message_facts"`), and neither value is
    declared wrong — surface 120 vs 160 as a conflict instead of hiding it."""
    facts = [
        {
            "entity_scope": "case",
            "fact_key": "heated_area_m2",
            "normalized_value": "120",
            "confidence": 0.9,
            "observed_at": "2026-07-10T00:00:00Z",
            "status": "superseded",
            "metadata": {"supersede_reason": "replace_message_facts"},
        },
        {
            "entity_scope": "case",
            "fact_key": "heated_area_m2",
            "normalized_value": "160",
            "confidence": 0.7,
            "observed_at": "2026-07-17T00:00:00Z",
            "status": "active",
        },
    ]
    active, conflicts = split_conflicting_facts(facts)
    assert active[0]["normalized_value"] == "160"
    assert conflicts == [
        {"entity_scope": "case", "fact_key": "heated_area_m2", "values": ["120", "160"]}
    ]


def test_repeated_same_value_is_not_a_conflict() -> None:
    facts = [
        {
            "entity_scope": "building",
            "fact_key": "heated_area_m2",
            "normalized_value": "140",
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
    assert active[0]["normalized_value"] == "140"
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
