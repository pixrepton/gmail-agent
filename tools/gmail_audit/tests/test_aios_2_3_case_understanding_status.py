"""SLICE-2C / Roadmap 2.3 — `case_understanding_status` never touches feed membership.

The failure this closes: "our reasoning about this case is degraded" and "this case does not belong
on the operator's desk" are different statements with different owners. If understanding quality
could gate membership, a reasoning failure would silently remove real operator work from the desk;
if it could create membership, every degraded reasoning run would manufacture a card.

Contract asserted here:

* the status is derived from `case_understanding_provenance`, never fabricated;
* `feed_visibility` does not import or read it — asserted structurally AND behaviourally;
* `degraded` / `unavailable` neither hides an existing card nor creates a new one;
* the field is optional, so snapshots written before this slice still validate;
* the golden JSON schema is in sync with the model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from case_understanding_status import (  # noqa: E402
    STATUS_DEGRADED,
    STATUS_OK,
    STATUS_REASONING_NOT_REQUIRED,
    STATUS_UNAVAILABLE,
    build_case_understanding_status,
)
from feed_visibility import (  # noqa: E402
    VISIBILITY_ATTENTION_REQUIRED,
    VISIBILITY_HIDDEN,
    VISIBILITY_MAIN_FEED,
    classify_signal_for_feed,
    effective_visibility_mode,
    is_main_feed_member,
)
from llm_contracts.engagement_snapshot_v2 import (  # noqa: E402
    CaseUnderstandingStatusV1,
    EngagementSnapshotV2,
    FeedVisibility,
    HitlGate,
    OperationalStatus,
)


def _provenance(**overrides) -> dict:
    base = {
        "schema_version": "v1",
        "availability": "available",
        "source_mode": "model_result",
        "validation_state": "clean",
        "source_signal_id": "sig-1",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "reason_codes": [],
        "normalization_count": 0,
        "validation_error_count": 0,
    }
    base.update(overrides)
    return base


def _snapshot(
    *,
    status: CaseUnderstandingStatusV1 | None = None,
    visibility: FeedVisibility | None = None,
    hitl_required: bool = False,
    status_code: str = "enriching",
) -> EngagementSnapshotV2:
    return EngagementSnapshotV2(
        engagement_id="eng-2c",
        case_id="case-2c",
        version=1,
        operational_status=OperationalStatus(code=status_code, steps_remaining=1, blocking=False),
        hitl_gate=HitlGate(required=hitl_required, reason="draft" if hitl_required else ""),
        feed_visibility=visibility,
        case_understanding_status=status,
    )


# ── derivation ─────────────────────────────────────────────────────────────────────────────


def test_available_model_result_is_ok():
    out = build_case_understanding_status(_provenance(), now_iso="2026-01-01T01:00:00+00:00")
    assert out["status"] == STATUS_OK
    assert out["source"] == "model_result"
    assert out["age_seconds"] == 3600
    assert out["source_signal_id"] == "sig-1"


def test_fallback_source_is_degraded():
    out = build_case_understanding_status(_provenance(source_mode="fallback"))
    assert out["status"] == STATUS_DEGRADED
    assert out["reason"]


def test_corrected_validation_is_recorded_but_is_not_a_downgrade():
    # mirrors CaseUnderstandingProvenance: `corrected` is NOT `degraded`
    out = build_case_understanding_status(_provenance(validation_state="corrected"))
    assert out["status"] == STATUS_OK
    assert "normalized:corrected" in out["reason_codes"]


def test_unavailable_and_not_required_are_distinct():
    assert build_case_understanding_status(_provenance(availability="unavailable"))["status"] == STATUS_UNAVAILABLE
    skipped = build_case_understanding_status(
        _provenance(availability="not_required", source_mode="skipped_for_lane")
    )
    assert skipped["status"] == STATUS_REASONING_NOT_REQUIRED


def test_missing_or_unknown_provenance_never_fabricates_a_status():
    assert build_case_understanding_status(None) is None
    assert build_case_understanding_status({}) is None
    assert build_case_understanding_status(_provenance(availability="")) is None
    assert build_case_understanding_status(_provenance(availability="some_future_value")) is None


def test_age_is_none_when_it_cannot_be_computed():
    out = build_case_understanding_status(_provenance(observed_at=""))
    assert out["age_seconds"] is None


# ── hard rule: no membership authority ─────────────────────────────────────────────────────


def test_feed_visibility_does_not_read_case_understanding_status():
    import feed_visibility

    source = Path(feed_visibility.__file__).read_text(encoding="utf-8")
    assert "case_understanding_status" not in source
    assert "case_understanding" not in source


def test_degraded_status_does_not_hide_a_visible_card():
    visible = FeedVisibility(**classify_signal_for_feed(preclassification_result={"lane": "intake_llm"}))
    degraded = CaseUnderstandingStatusV1(status="degraded", source="fallback")
    snap = _snapshot(status=degraded, visibility=visible)
    mode, reasons = effective_visibility_mode(snap)
    assert mode == VISIBILITY_MAIN_FEED
    assert is_main_feed_member(snap) is True
    assert not any("understanding" in str(r) for r in reasons)


def test_unavailable_status_does_not_create_membership_for_a_hidden_card():
    hidden = FeedVisibility(
        **classify_signal_for_feed(
            preclassification_result={"lane": "skip"}, triage_result={"triage_class": "ignore"}
        )
    )
    unavailable = CaseUnderstandingStatusV1(status="unavailable")
    snap = _snapshot(status=unavailable, visibility=hidden)
    assert effective_visibility_mode(snap)[0] == VISIBILITY_HIDDEN
    assert is_main_feed_member(snap) is False


def test_membership_is_identical_with_and_without_the_status_field():
    for code, hitl in (("enriching", False), ("pending_operator", False), ("enriching", True)):
        for status in (None, CaseUnderstandingStatusV1(status="unavailable"), CaseUnderstandingStatusV1(status="ok")):
            without = _snapshot(status=None, hitl_required=hitl, status_code=code)
            with_status = _snapshot(status=status, hitl_required=hitl, status_code=code)
            assert effective_visibility_mode(without) == effective_visibility_mode(with_status)


def test_real_operator_work_still_wins_regardless_of_understanding_status():
    hidden = FeedVisibility(**classify_signal_for_feed(preclassification_result={"lane": "skip"}))
    snap = _snapshot(
        status=CaseUnderstandingStatusV1(status="unavailable"),
        visibility=hidden,
        hitl_required=True,
    )
    assert effective_visibility_mode(snap)[0] == VISIBILITY_ATTENTION_REQUIRED


# ── contract compatibility ─────────────────────────────────────────────────────────────────


def test_status_field_is_optional_so_legacy_snapshots_still_validate():
    data = _snapshot().model_dump()
    data.pop("case_understanding_status", None)
    assert EngagementSnapshotV2.model_validate(data).case_understanding_status is None


def test_golden_schema_is_in_sync_with_the_model():
    from llm_contracts.engagement_snapshot_v2 import engagement_snapshot_v2_json_schema

    golden = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "engagement_snapshot_v2.schema.json"
    on_disk = json.loads(golden.read_text(encoding="utf-8"))
    assert "CaseUnderstandingStatusV1" in on_disk.get("$defs", {})
    assert on_disk == engagement_snapshot_v2_json_schema()


# ── writer lockstep ────────────────────────────────────────────────────────────────────────


def test_writer_sets_and_clears_the_status_together_with_the_provenance():
    from agent_runtime.graph import _ground_current_signal

    snap = _snapshot()
    grounded = _ground_current_signal(
        snap,
        {
            "subject": "Nowa wiadomosc",
            "case_understanding_projection": {"essence_pl": "cos sie zmienilo"},
            "case_understanding_provenance": _provenance(source_mode="fallback"),
        },
    )
    assert grounded.case_understanding_provenance is not None
    assert grounded.case_understanding_status is not None
    assert grounded.case_understanding_status.status == "degraded"

    # next turn with no correlated Understanding: both are cleared, never left stale
    cleared = _ground_current_signal(grounded, {"subject": "Kolejna wiadomosc"})
    assert cleared.case_understanding is None
    assert cleared.case_understanding_provenance is None
    assert cleared.case_understanding_status is None


def test_a_tool_delta_may_not_author_the_status():
    from agent_runtime.graph import _strip_protected_snapshot_fields

    out = _strip_protected_snapshot_fields(
        delta_source={"case_understanding_status": {"status": "ok"}, "case_kind": "inne"},
        tool_name="some_tool",
    )
    assert "case_understanding_status" not in out
    assert out["case_kind"] == "inne"


def test_daszek_case_row_may_display_the_status_without_it_affecting_membership():
    from daszek_engagement_feed.case import snapshot_to_feed_case
    from daszek_engagement_feed.desk import snapshot_to_desk_item

    snap = _snapshot(status=CaseUnderstandingStatusV1(status="degraded", source="fallback", reason="x"))
    row = snapshot_to_feed_case(snap)
    assert row["case_understanding_status"]["status"] == "degraded"
    # the desk item is still produced by operational state alone
    assert snapshot_to_desk_item(snap) is not None
    assert is_main_feed_member(snap) is True
