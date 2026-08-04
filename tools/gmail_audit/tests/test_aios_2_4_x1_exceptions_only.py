"""AI-OS Roadmap 2.4 — X1 exceptions-only desk (bounded).

Contract asserted here:

* `case_timeline_only` has a real consumer (own feed bucket), distinct from `hidden` and from desk;
* membership "why" (`why_on_desk_reason_codes` / `feed_visibility_mode`) is projected next to the
  business prose `why_on_desk`, never merged into it;
* soft exceptions preference reorders the desk (HITL / pending readiness first) without removing
  cards; the hard `exceptions_only` filter is opt-in;
* `apply_operator_visibility_override` is auditable, cannot persist `attention_required`, cannot
  silence real outstanding HITL work, and does not freeze later monotonic promotion.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daszek_engagement_feed.build import (  # noqa: E402
    build_case_timeline_only_items,
    build_engagement_feed_envelope,
    build_feed_from_engagement_snapshots,
)
from daszek_engagement_feed.case import (  # noqa: E402
    feed_visibility_mode_from_snapshot,
    snapshot_to_feed_case,
    why_on_desk_reason_codes_from_snapshot,
)
from daszek_engagement_feed.desk import snapshot_to_desk_item  # noqa: E402
from feed_visibility import (  # noqa: E402
    VISIBILITY_ATTENTION_REQUIRED,
    VISIBILITY_CASE_TIMELINE_ONLY,
    VISIBILITY_HIDDEN,
    VISIBILITY_MAIN_FEED,
    apply_operator_visibility_override,
    classify_signal_for_feed,
    effective_visibility_mode,
    is_case_timeline_only,
    is_main_feed_member,
    merge_feed_visibility,
)
from llm_contracts.case_readiness import CaseReadinessState  # noqa: E402
from llm_contracts.engagement_snapshot_v2 import (  # noqa: E402
    CaseUnderstandingProjection,
    EngagementSnapshotV2,
    FeedVisibility,
    HitlGate,
    OperationalStatus,
)
from operator_desk_priority import (  # noqa: E402
    case_readiness_from_snapshot,
    desk_priority_rank,
    order_desk_snapshots,
)


def _classify(lane: str, *, triage_class: str = "", reasons: list[str] | None = None) -> FeedVisibility:
    return FeedVisibility(
        **classify_signal_for_feed(
            preclassification_result={"lane": lane, "reasons": reasons or []},
            triage_result={"triage_class": triage_class} if triage_class else None,
        )
    )


def _snapshot(
    *,
    engagement_id: str = "eng_x1",
    case_id: str = "case_x1",
    visibility: FeedVisibility | None = None,
    hitl_required: bool = False,
    status_code: str = "enriching",
    why_pl: str = "",
) -> EngagementSnapshotV2:
    understanding = None
    if why_pl:
        understanding = CaseUnderstandingProjection(
            essence_pl="x",
            why_pl=why_pl,
            recommended_next_step_pl="",
        )
    return EngagementSnapshotV2(
        engagement_id=engagement_id,
        case_id=case_id,
        version=1,
        operational_status=OperationalStatus(code=status_code, steps_remaining=1, blocking=False),
        hitl_gate=HitlGate(required=hitl_required, reason="draft" if hitl_required else ""),
        feed_visibility=visibility,
        case_understanding=understanding,
    )


# ── case_timeline_only consumer ────────────────────────────────────────────────────────────


def test_case_timeline_only_predicate_is_distinct_from_hidden_and_main_feed():
    timeline = _snapshot(visibility=_classify("reference_only", triage_class="reference_only"))
    hidden = _snapshot(visibility=_classify("skip", triage_class="ignore"))
    main = _snapshot(visibility=_classify("intake_llm"))

    assert is_case_timeline_only(timeline) is True
    assert is_main_feed_member(timeline) is False
    assert is_case_timeline_only(hidden) is False
    assert is_main_feed_member(hidden) is False
    assert is_case_timeline_only(main) is False
    assert is_main_feed_member(main) is True


def test_case_timeline_only_bucket_is_not_desk_work():
    snap = _snapshot(
        engagement_id="ref_1",
        case_id="case_ref",
        visibility=_classify("reference_only", reasons=["fwd_invoice"]),
    )
    items = build_case_timeline_only_items([snap], meta_by_case={"case_ref": {"subject": "FW: faktura"}})
    assert len(items) == 1
    row = items[0]
    assert row["case_id"] == "case_ref"
    assert row["feed_visibility_mode"] == VISIBILITY_CASE_TIMELINE_ONLY
    assert row["main_feed_member"] is False
    assert row["read_only"] is True
    assert "note_id" not in row
    assert "presence_mode" not in row
    assert "fwd_invoice" in row["why_on_desk_reason_codes"]
    # Desk mapper is status-driven and may still shape a row; membership keeps it out of the
    # main feed path. The timeline bucket above is the consumer that makes the distinction real.
    assert is_main_feed_member(snap) is False


def test_engagement_feed_envelope_keeps_timeline_only_out_of_desk_and_cases():
    main = _snapshot(engagement_id="m1", visibility=_classify("intake_llm"), hitl_required=True)
    timeline = _snapshot(
        engagement_id="t1",
        case_id="case_t",
        visibility=_classify("reference_only"),
    )
    feed_core = build_feed_from_engagement_snapshots([main])
    envelope = build_engagement_feed_envelope(
        feed_core,
        snapshot_id="snap-x1",
        case_timeline_only=build_case_timeline_only_items([timeline]),
    )
    feed = envelope["feed"]
    desk_ids = {item.get("engagement_id") for item in feed["desk"]}
    case_ids = {item.get("engagement_id") for item in feed["cases"]}
    timeline_ids = {item["engagement_id"] for item in feed["case_timeline_only"]}
    assert "t1" in timeline_ids
    assert "t1" not in desk_ids
    assert "t1" not in case_ids
    assert "m1" in desk_ids
    assert feed["feed_meta"]["case_timeline_only_count"] == 1


# ── why_you / membership reasons ───────────────────────────────────────────────────────────


def test_membership_why_is_separate_from_business_why_on_desk():
    snap = _snapshot(
        visibility=_classify("intake_llm", reasons=["lane:intake_llm"]),
        hitl_required=True,
        why_pl="Klient pyta o ofertę na pompę ciepła.",
    )
    case_row = snapshot_to_feed_case(snap)
    desk_row = snapshot_to_desk_item(snap)
    assert case_row["why_on_desk"] == "Klient pyta o ofertę na pompę ciepła."
    assert "lane:intake_llm" in case_row["why_on_desk_reason_codes"]
    assert any(r.startswith("operator_override:") for r in case_row["why_on_desk_reason_codes"])
    assert case_row["feed_visibility_mode"] == VISIBILITY_ATTENTION_REQUIRED
    assert desk_row is not None
    assert desk_row["why_on_desk"] == case_row["why_on_desk"]
    assert desk_row["why_on_desk_reason_codes"] == case_row["why_on_desk_reason_codes"]
    # helpers used by both surfaces stay consistent
    assert why_on_desk_reason_codes_from_snapshot(snap) == case_row["why_on_desk_reason_codes"]
    assert feed_visibility_mode_from_snapshot(snap) == VISIBILITY_ATTENTION_REQUIRED


def test_membership_why_can_exist_without_business_prose():
    snap = _snapshot(visibility=_classify("skip", reasons=["obvious_noise"]), hitl_required=True)
    row = snapshot_to_feed_case(snap)
    assert row["why_on_desk"] == ""
    assert row["why_on_desk_reason_codes"]
    assert row["feed_visibility_mode"] == VISIBILITY_ATTENTION_REQUIRED


# ── soft exceptions preference + opt-in hard filter ────────────────────────────────────────


def test_soft_exceptions_reorder_without_dropping_cards():
    # `pending_operator` itself is an executive attention override (rank 0), so use
    # `ready_for_quote` to exercise the softer pending-readiness band (rank 1).
    quiet = _snapshot(engagement_id="quiet", status_code="enriching", visibility=_classify("intake_llm"))
    decision = _snapshot(
        engagement_id="decision",
        status_code="ready_for_quote",
        visibility=_classify("intake_llm"),
    )
    hitl = _snapshot(
        engagement_id="hitl",
        status_code="enriching",
        visibility=_classify("intake_llm"),
        hitl_required=True,
    )
    # store order: quiet first (would be wrong on an exceptions-first desk)
    ordered = order_desk_snapshots([quiet, decision, hitl], exceptions_only=False)
    assert [s.engagement_id for s in ordered] == ["hitl", "decision", "quiet"]
    assert len(ordered) == 3
    assert case_readiness_from_snapshot(decision)["state"] == CaseReadinessState.READY_FOR_DECISION.value


def test_exceptions_only_hard_filter_is_opt_in():
    quiet = _snapshot(engagement_id="quiet", status_code="enriching", visibility=_classify("intake_llm"))
    hitl = _snapshot(
        engagement_id="hitl",
        visibility=_classify("intake_llm"),
        hitl_required=True,
    )
    soft = order_desk_snapshots([quiet, hitl], exceptions_only=False)
    hard = order_desk_snapshots([quiet, hitl], exceptions_only=True)
    assert [s.engagement_id for s in soft] == ["hitl", "quiet"]
    assert [s.engagement_id for s in hard] == ["hitl"]
    assert case_readiness_from_snapshot(quiet)["state"] == CaseReadinessState.NO_ACTION_REQUIRED.value


def test_desk_priority_rank_puts_attention_ahead_of_pending_readiness():
    assert desk_priority_rank(
        visibility_mode=VISIBILITY_ATTENTION_REQUIRED,
        readiness_state=CaseReadinessState.NO_ACTION_REQUIRED.value,
    ) < desk_priority_rank(
        visibility_mode=VISIBILITY_MAIN_FEED,
        readiness_state=CaseReadinessState.READY_FOR_APPROVAL.value,
    )


# ── operator_override path ─────────────────────────────────────────────────────────────────


def test_operator_override_reclassifies_with_audit_trail():
    stored = _classify("intake_llm", reasons=["lane:intake_llm"])
    out = apply_operator_visibility_override(stored, mode=VISIBILITY_HIDDEN, reason="noise_after_review")
    assert out["mode"] == VISIBILITY_HIDDEN
    assert out["operator_override"] is True
    assert any(r.startswith("operator_reclassified:main_feed->hidden") for r in out["reason_codes"])
    assert any("noise_after_review" in r for r in out["reason_codes"])
    # original routing provenance survives
    assert "lane:intake_llm" in out["reason_codes"]


def test_operator_override_rejects_persisting_attention_required():
    stored = _classify("intake_llm")
    with pytest.raises(ValueError, match="operator override mode"):
        apply_operator_visibility_override(stored, mode=VISIBILITY_ATTENTION_REQUIRED)


def test_operator_hide_cannot_silence_outstanding_hitl_work():
    stored = FeedVisibility(**apply_operator_visibility_override(_classify("intake_llm"), mode=VISIBILITY_HIDDEN))
    snap = _snapshot(visibility=stored, hitl_required=True)
    mode, reasons = effective_visibility_mode(snap)
    assert mode == VISIBILITY_ATTENTION_REQUIRED
    assert is_main_feed_member(snap) is True
    assert any(r.startswith("operator_override:pending_hitl_gate") for r in reasons)
    assert stored.operator_override is True


def test_operator_hide_does_not_freeze_later_monotonic_promotion():
    hidden = FeedVisibility(
        **apply_operator_visibility_override(
            _classify("skip", triage_class="ignore", reasons=["newsletter"]),
            mode=VISIBILITY_HIDDEN,
            reason="ack_noise",
        )
    )
    incoming = classify_signal_for_feed(
        preclassification_result={"lane": "intake_llm", "reasons": ["real_inquiry"]},
        triage_result={"triage_class": "business_signal"},
    )
    merged = merge_feed_visibility(hidden, incoming)
    assert merged is not None
    assert merged["mode"] == VISIBILITY_MAIN_FEED
    assert merged["operator_override"] is True  # prior operator act remains recorded
    assert any(r.startswith("promoted:hidden->main_feed") for r in merged["reason_codes"])


def test_operator_override_to_timeline_only_stays_out_of_main_feed_without_work():
    stored = FeedVisibility(
        **apply_operator_visibility_override(_classify("intake_llm"), mode=VISIBILITY_CASE_TIMELINE_ONLY)
    )
    snap = _snapshot(visibility=stored, hitl_required=False, status_code="enriching")
    assert is_case_timeline_only(snap) is True
    assert is_main_feed_member(snap) is False
