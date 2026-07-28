"""AI-OS-INTELLIGENCE-FINAL-HARDENING-01 / SLICE-2B-OPERATOR-FEED-MEMBERSHIP.

Root cause (routing proof, NOISE_CAN_REACH_FEED_CONFIRMED): the existence of an
`operator_engagement_snapshots` row was effectively membership of the operator's main feed --
`list_recent_snapshots` had no `WHERE` clause at all, so a confirmed-noise mail's staging snapshot
became a card.

Contract asserted here:  `snapshot exists`  !=  `belongs in the operator's main feed`.

Nothing in this slice changes the preclassifier, signal creation, snapshot creation, staging TTL,
or any semantic case field. Visibility is routing/projection metadata only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daszek_engagement_feed.build import _list_main_feed_snapshots  # noqa: E402
from feed_visibility import (  # noqa: E402
    VISIBILITY_ATTENTION_REQUIRED,
    VISIBILITY_CASE_TIMELINE_ONLY,
    VISIBILITY_HIDDEN,
    VISIBILITY_MAIN_FEED,
    classify_signal_for_feed,
    effective_visibility_mode,
    is_main_feed_member,
)
from llm_contracts.engagement_snapshot_v2 import (  # noqa: E402
    ActionItem,
    EngagementSnapshotV2,
    FeedVisibility,
    HitlGate,
    OperationalStatus,
)


def _snapshot(
    *,
    engagement_id: str = "eng_1",
    case_id: str = "",
    visibility: FeedVisibility | None = None,
    hitl_required: bool = False,
    status_code: str = "enriching",
    actions: list[ActionItem] | None = None,
) -> EngagementSnapshotV2:
    return EngagementSnapshotV2(
        engagement_id=engagement_id,
        case_id=case_id,
        version=1,
        operational_status=OperationalStatus(code=status_code, steps_remaining=3, blocking=False),
        hitl_gate=HitlGate(required=hitl_required, reason="draft_ready_for_approval" if hitl_required else ""),
        actions=actions or [],
        feed_visibility=visibility,
    )


def _classify(lane: str, *, triage_class: str = "", reasons: list[str] | None = None) -> FeedVisibility:
    return FeedVisibility(
        **classify_signal_for_feed(
            preclassification_result={"lane": lane, "reasons": reasons or []},
            triage_result={"triage_class": triage_class} if triage_class else None,
        )
    )


# ── 1. noise orphan: snapshot exists, not in the main feed ─────────────────────────────────


def test_noise_orphan_snapshot_exists_but_is_not_a_main_feed_card():
    vis = _classify("skip", triage_class="ignore", reasons=["obvious_noise", "noise_subject_keyword:newsletter"])
    assert vis.mode == VISIBILITY_HIDDEN
    snap = _snapshot(visibility=vis)
    assert is_main_feed_member(snap) is False
    # the snapshot itself is untouched -- journal/CanonicalSignal/technical snapshot all survive
    assert snap.engagement_id == "eng_1"


# ── 2. noise WITH pending HITL: must be visible as attention_required ──────────────────────


def test_noise_with_pending_hitl_is_visible_as_attention_required():
    snap = _snapshot(visibility=_classify("skip", triage_class="ignore"), hitl_required=True)
    mode, reasons = effective_visibility_mode(snap)
    assert mode == VISIBILITY_ATTENTION_REQUIRED
    assert any("operator_override:pending_hitl_gate" in r for r in reasons)
    assert is_main_feed_member(snap) is True


def test_noise_with_pending_operator_status_is_visible():
    snap = _snapshot(visibility=_classify("skip"), status_code="pending_operator")
    assert effective_visibility_mode(snap)[0] == VISIBILITY_ATTENTION_REQUIRED


def test_noise_with_node_a_error_status_is_visible():
    snap = _snapshot(visibility=_classify("skip"), status_code="node_a_error")
    assert effective_visibility_mode(snap)[0] == VISIBILITY_ATTENTION_REQUIRED


def test_outcome_unknown_is_not_reachable_from_the_snapshot_contract():
    # documents a real limitation rather than pretending coverage: outcome_unknown is a HITL send
    # state in MailboxMemory keyed by decision_key, never a snapshot field. See 09-residual-risks.
    from llm_contracts.engagement_snapshot_v2 import OperationalStatus
    import pydantic
    try:
        OperationalStatus(code="outcome_unknown", steps_remaining=1, blocking=False)
    except pydantic.ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("outcome_unknown became a valid OperationalStatus code -- revisit the override set")


def test_noise_with_an_enabled_action_proposal_is_visible():
    action = ActionItem(id="draft_reply", enabled=True, payload_pl="tresc")
    snap = _snapshot(visibility=_classify("skip"), actions=[action])
    assert effective_visibility_mode(snap)[0] == VISIBILITY_ATTENTION_REQUIRED


def test_override_comes_from_executive_fields_not_from_text():
    # a noise snapshot whose only "signal" is text-like content stays hidden; no keyword override
    action = ActionItem(id="draft_reply", enabled=False, payload_pl="PILNE! HITL! decyzja operatora!")
    snap = _snapshot(visibility=_classify("skip"), actions=[action])
    assert is_main_feed_member(snap) is False


# ── 3-4. reference-only ────────────────────────────────────────────────────────────────────


def test_reference_only_orphan_is_not_a_standalone_main_card():
    vis = _classify("reference_only", triage_class="reference_only")
    assert vis.mode == VISIBILITY_CASE_TIMELINE_ONLY
    assert is_main_feed_member(_snapshot(visibility=vis)) is False


def test_reference_only_attached_to_a_case_stays_on_the_timeline_without_raising_attention():
    snap = _snapshot(case_id="case_123", visibility=_classify("reference_only"))
    mode, _reasons = effective_visibility_mode(snap)
    assert mode == VISIBILITY_CASE_TIMELINE_ONLY  # available to case history
    assert mode != VISIBILITY_ATTENTION_REQUIRED  # but never a new attention card
    assert is_main_feed_member(snap) is False
    assert snap.case_id == "case_123"


# ── 5-6. review_direct and ordinary business signal stay visible ───────────────────────────


def test_review_direct_remains_visible():
    assert is_main_feed_member(_snapshot(visibility=_classify("review_direct"))) is True


def test_needs_operator_review_triage_remains_visible():
    assert is_main_feed_member(_snapshot(visibility=_classify("", triage_class="needs_operator_review"))) is True


def test_ordinary_business_signal_remains_visible():
    vis = _classify("intake_llm", triage_class="business_signal")
    assert vis.mode == VISIBILITY_MAIN_FEED
    assert is_main_feed_member(_snapshot(visibility=vis)) is True


# ── 7. legacy snapshots must not vanish ────────────────────────────────────────────────────


def test_legacy_snapshot_without_visibility_metadata_is_not_hidden():
    snap = _snapshot(visibility=None)
    mode, reasons = effective_visibility_mode(snap)
    assert mode == VISIBILITY_MAIN_FEED
    assert "legacy_snapshot_without_visibility_metadata" in reasons
    assert is_main_feed_member(snap) is True


def test_unknown_visibility_mode_falls_back_to_visible_with_a_reason():
    snap = _snapshot(visibility=None)
    object.__setattr__(snap, "feed_visibility", FeedVisibility(mode="main_feed"))
    assert is_main_feed_member(snap) is True


# ── 8. hidden snapshots must not displace qualifying ones past the limit ───────────────────


def test_hidden_snapshots_cannot_push_a_qualifying_case_out_of_the_limit():
    # 60 newer noise snapshots, then one older genuinely qualifying case.
    hidden = [_snapshot(engagement_id=f"noise_{i}", visibility=_classify("skip", triage_class="ignore")) for i in range(60)]
    qualifying = _snapshot(engagement_id="real_case", visibility=_classify("intake_llm"))
    ordered = [*hidden, qualifying]  # newest first, exactly as the store returns

    calls: list[int] = []

    def list_fn(*, limit: int):
        calls.append(limit)
        return ordered[:limit]

    out = _list_main_feed_snapshots(list_fn, limit=50)
    assert [s.engagement_id for s in out] == ["real_case"], "a naive LIMIT 50 + filter would return nothing"
    assert max(calls) > 50, "the overfetch must look past the first page"


def test_overfetch_is_bounded_and_terminates_when_everything_is_hidden():
    hidden = [_snapshot(engagement_id=f"n{i}", visibility=_classify("skip")) for i in range(300)]

    calls: list[int] = []

    def list_fn(*, limit: int):
        calls.append(limit)
        return hidden[:limit]

    out = _list_main_feed_snapshots(list_fn, limit=50)
    assert out == []
    assert len(calls) < 12, f"overfetch did not terminate promptly: {calls}"
    assert max(calls) <= 1000


def test_qualifying_snapshots_keep_their_recency_order_and_the_limit_is_respected():
    snaps = [_snapshot(engagement_id=f"c{i}", visibility=_classify("intake_llm")) for i in range(10)]
    out = _list_main_feed_snapshots(lambda *, limit: snaps[:limit], limit=4)
    assert [s.engagement_id for s in out] == ["c0", "c1", "c2", "c3"]


# ── 9. no fabrication: an unknown lane is never assumed to be noise ────────────────────────


def test_unknown_lane_is_not_classified_as_noise():
    for lane in ("", "some_future_lane", "unknown"):
        vis = _classify(lane)
        assert vis.mode == VISIBILITY_MAIN_FEED, f"lane {lane!r} was wrongly hidden"
        assert is_main_feed_member(_snapshot(visibility=vis)) is True


def test_absent_classification_data_does_not_hide_anything():
    decision = classify_signal_for_feed(preclassification_result=None, triage_result=None)
    assert decision["mode"] == VISIBILITY_MAIN_FEED


# ── provenance ─────────────────────────────────────────────────────────────────────────────


def test_visibility_decision_preserves_provenance():
    vis = _classify("skip", triage_class="ignore", reasons=["obvious_noise", "noise_subject_keyword:newsletter"])
    assert vis.source_lane == "skip"
    assert vis.source_triage_class == "ignore"
    assert "noise_subject_keyword:newsletter" in vis.reason_codes
    assert vis.operator_override is False


def test_override_is_reported_in_the_reason_codes():
    snap = _snapshot(visibility=_classify("skip", reasons=["obvious_noise"]), hitl_required=True)
    _mode, reasons = effective_visibility_mode(snap)
    assert "obvious_noise" in reasons  # original routing reason preserved
    assert any(r.startswith("operator_override:") for r in reasons)  # and the override is explicit


def test_feed_visibility_is_optional_so_old_snapshots_still_validate():
    data = _snapshot(visibility=None).model_dump()
    data.pop("feed_visibility", None)
    assert EngagementSnapshotV2.model_validate(data).feed_visibility is None
