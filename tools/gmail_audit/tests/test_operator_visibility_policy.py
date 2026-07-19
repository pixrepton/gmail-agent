"""CEL operator visibility policy tests."""

from __future__ import annotations

from operator_visibility_policy import (
    apply_desk_composition_visibility,
    desk_card_spec_for_case,
    should_show_on_desk,
    should_suppress_desk_and_tasks,
)


def test_no_desk_when_case_is_clean() -> None:
    feed_case = {"case_id": "c1", "title": "OK", "status": "open", "badges": {}}
    assert should_show_on_desk(feed_case) is False
    assert desk_card_spec_for_case(feed_case) is None


def test_desk_on_blocking_conflict() -> None:
    feed_case = {
        "case_id": "c2",
        "title": "Sprawa X",
        "badges": {"blocking_conflict": True},
        "conflicting_facts": [{"summary": "Konflikt A/B", "projection_summary": "Konflikt A/B"}],
    }
    spec = desk_card_spec_for_case(feed_case)
    assert spec is not None
    assert spec.kind == "conflict"
    assert "Sprzeczność" in spec.title


def test_desk_on_blocking_gap() -> None:
    feed_case = {
        "case_id": "c3",
        "title": "Sprawa Y",
        "has_blocking_gaps": True,
        "completeness_gaps": [{"summary": "Brak adresu"}],
    }
    spec = desk_card_spec_for_case(feed_case)
    assert spec is not None
    assert spec.kind == "gap"


def test_suppress_desk_for_promotional_preclassification_lane() -> None:
    assert should_suppress_desk_and_tasks(preclassification_result={"lane": "promotional"}) is True
    assert should_suppress_desk_and_tasks(preclassification_result={"lane": "no_action"}) is True


def test_suppress_desk_for_wait_marketing_business_reasoning() -> None:
    assert should_suppress_desk_and_tasks(
        business_result={"recommended_next_action": "wait", "business_area": "marketing"},
    ) is True
    assert should_suppress_desk_and_tasks(
        business_result={"recommended_next_action": "wait", "business_area": "lead"},
    ) is False


def test_desk_tasks_suppressed_flag_blocks_desk_card() -> None:
    feed_case = {
        "case_id": "c-spam",
        "title": "Oferta",
        "badges": {"needs_operator_review": True},
        "desk_tasks_suppressed": True,
    }
    assert should_show_on_desk(feed_case) is False
    assert desk_card_spec_for_case(feed_case) is None


def test_apply_desk_composition_visibility_marks_suppressed_case() -> None:
    feed_case = {"case_id": "c4", "badges": {"needs_operator_review": True}}
    apply_desk_composition_visibility(
        feed_case,
        {
            "should_surface": False,
            "surface_zone": "case_only",
            "desk_tasks_suppressed": True,
            "desk_suppression_reason": "non_business_noise",
        },
    )
    assert feed_case["desk_tasks_suppressed"] is True
    assert feed_case["badges"]["needs_operator_review"] is False
    assert feed_case["surface_zone"] == "case_only"
