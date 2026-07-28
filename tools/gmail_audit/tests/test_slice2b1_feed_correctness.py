"""AI-OS-INTELLIGENCE-FINAL-HARDENING-01 / SLICE-2B1-FEED-CORRECTNESS.

SLICE-2B decided feed membership once, when the snapshot was created. Three residuals followed:

1. an engagement outlives the signal that created it, and `ensure_engagement_snapshot` returned an
   existing snapshot untouched -- so the FIRST message's routing verdict was permanent;
2. `outcome_unknown` lives in MailboxMemory under `decision_key` and has no `OperationalStatus`
   literal, so the executive-state override could not see an unresolved send;
3. the 61->4 proof used hand-built snapshots, not the real reconcile path.

This file closes all three. The governing asymmetry: wrongly showing noise costs the operator a
glance, wrongly hiding a real enquiry costs a customer -- so promotion is automatic and demotion
is not implemented here at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_runtime.agent_reconcile import (  # noqa: E402
    _feed_visibility_for_signal,
    _refresh_feed_visibility,
    ensure_engagement_snapshot,
)
from agent_runtime.store import InMemoryOperatorEngagementStore  # noqa: E402
from daszek_engagement_feed.build import _list_main_feed_snapshots  # noqa: E402
from feed_visibility import (  # noqa: E402
    VISIBILITY_ATTENTION_REQUIRED,
    VISIBILITY_CASE_TIMELINE_ONLY,
    VISIBILITY_HIDDEN,
    VISIBILITY_MAIN_FEED,
    classify_signal_for_feed,
    effective_visibility_mode,
    is_main_feed_member,
    mark_execution_attention,
    merge_feed_visibility,
)
from llm_contracts.engagement_snapshot_v2 import (  # noqa: E402
    ActionItem,
    EngagementSnapshotV2,
    FeedVisibility,
    HitlGate,
    OperationalStatus,
)
from signal_contract import build_canonical_signal  # noqa: E402


# ── helpers: real production objects, no hand-set visibility ───────────────────────────────


_NOISE = {"lane": "skip", "reasons": ["obvious_noise", "noise_subject_keyword:newsletter"]}
_BUSINESS = {"lane": "intake_llm", "reasons": ["business_enquiry"]}
_REFERENCE = {"lane": "reference_only", "reasons": ["reference_only_thread"]}


def _signal(preclass: dict, *, message_id: str, subject: str = "temat"):
    """A real CanonicalSignal built by the production factory."""
    return build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"message_id": message_id},
        observed_at="2026-07-27T10:00:00+00:00",
        signal_summary_pl=subject,
        payload={
            "preclassification_result": preclass,
            "intake_result_final": {"message": {"message_id": message_id, "subject": subject}},
        },
    )


class _Ctx:
    """Minimal runtime context: only `run_state` is read by `_current_trace_id`."""

    def __init__(self) -> None:
        self.run_state = {"trace_id": "trace_slice2b1"}
        self.settings = None
        self.resolved_store = None


def _vis(preclass: dict) -> FeedVisibility:
    return FeedVisibility(**classify_signal_for_feed(preclassification_result=preclass))


def _snapshot(
    *,
    engagement_id: str = "eng_1",
    case_id: str = "",
    visibility: FeedVisibility | None = None,
    hitl_required: bool = False,
    status_code: str = "enriching",
    actions: list[ActionItem] | None = None,
    version: int = 1,
) -> EngagementSnapshotV2:
    return EngagementSnapshotV2(
        engagement_id=engagement_id,
        case_id=case_id,
        version=version,
        operational_status=OperationalStatus(code=status_code, steps_remaining=3, blocking=False),
        hitl_gate=HitlGate(required=hitl_required, reason="draft_ready_for_approval" if hitl_required else ""),
        actions=actions or [],
        feed_visibility=visibility,
    )


# ── A2 / test 1-4, 12: the monotonic merge contract ────────────────────────────────────────


def test_first_noise_signal_is_hidden():
    assert _vis(_NOISE).mode == VISIBILITY_HIDDEN


def test_noise_then_business_promotes_to_main_feed():
    merged = merge_feed_visibility(_vis(_NOISE), classify_signal_for_feed(preclassification_result=_BUSINESS))
    assert merged is not None, "a later business signal must be able to lift a hidden engagement"
    assert merged["mode"] == VISIBILITY_MAIN_FEED
    assert is_main_feed_member(_snapshot(visibility=FeedVisibility(**merged))) is True


def test_reference_only_then_business_promotes_to_main_feed():
    merged = merge_feed_visibility(_vis(_REFERENCE), classify_signal_for_feed(preclassification_result=_BUSINESS))
    assert merged is not None
    assert merged["mode"] == VISIBILITY_MAIN_FEED


def test_noise_then_reference_only_promotes_only_one_step():
    merged = merge_feed_visibility(_vis(_NOISE), classify_signal_for_feed(preclassification_result=_REFERENCE))
    assert merged is not None
    assert merged["mode"] == VISIBILITY_CASE_TIMELINE_ONLY
    assert is_main_feed_member(_snapshot(visibility=FeedVisibility(**merged))) is False


def test_business_then_noise_stays_main_feed():
    assert merge_feed_visibility(_vis(_BUSINESS), classify_signal_for_feed(preclassification_result=_NOISE)) is None


def test_business_then_reference_only_stays_main_feed():
    assert merge_feed_visibility(_vis(_BUSINESS), classify_signal_for_feed(preclassification_result=_REFERENCE)) is None


def test_reference_only_then_noise_stays_case_timeline_only():
    assert merge_feed_visibility(_vis(_REFERENCE), classify_signal_for_feed(preclassification_result=_NOISE)) is None


def test_no_automatic_demotion_exists_for_any_ordered_pair():
    """Exhaustive over the three base modes: nothing this function returns can lower the rank."""
    rank = {VISIBILITY_HIDDEN: 0, VISIBILITY_CASE_TIMELINE_ONLY: 1, VISIBILITY_MAIN_FEED: 2}
    lanes = {VISIBILITY_HIDDEN: _NOISE, VISIBILITY_CASE_TIMELINE_ONLY: _REFERENCE, VISIBILITY_MAIN_FEED: _BUSINESS}
    for stored_mode, stored_lane in lanes.items():
        for incoming_mode, incoming_lane in lanes.items():
            merged = merge_feed_visibility(
                _vis(stored_lane), classify_signal_for_feed(preclassification_result=incoming_lane)
            )
            result_mode = stored_mode if merged is None else merged["mode"]
            assert rank[result_mode] >= rank[stored_mode], (
                f"{stored_mode} + {incoming_mode} demoted to {result_mode}"
            )


def test_unknown_incoming_lane_promotes_but_never_hides():
    # an unclassifiable later signal must not hide an engagement, and it may reveal one
    merged = merge_feed_visibility(_vis(_NOISE), classify_signal_for_feed(preclassification_result={"lane": "??"}))
    assert merged is not None and merged["mode"] == VISIBILITY_MAIN_FEED
    assert merge_feed_visibility(_vis(_BUSINESS), classify_signal_for_feed(preclassification_result={"lane": "??"})) is None


def test_absent_incoming_classification_changes_nothing():
    assert merge_feed_visibility(_vis(_NOISE), None) is None
    assert merge_feed_visibility(_vis(_NOISE), {}) is None


# ── test 9 / legacy ────────────────────────────────────────────────────────────────────────


def test_legacy_snapshot_is_never_backfilled_by_a_later_noise_signal():
    assert merge_feed_visibility(None, classify_signal_for_feed(preclassification_result=_NOISE)) is None
    assert is_main_feed_member(_snapshot(visibility=None)) is True


# ── test 13: bounded promotion history ─────────────────────────────────────────────────────


def test_reason_codes_record_the_promotion_and_stay_bounded():
    stored = _vis(_NOISE)
    merged = merge_feed_visibility(stored, classify_signal_for_feed(preclassification_result=_BUSINESS))
    assert f"promoted:{VISIBILITY_HIDDEN}->{VISIBILITY_MAIN_FEED}" in merged["reason_codes"]
    assert "obvious_noise" in merged["reason_codes"], "the original routing reason must survive"
    assert "business_enquiry" in merged["reason_codes"]


def test_promotion_history_is_capped_and_keeps_the_current_reason():
    noisy = FeedVisibility(mode=VISIBILITY_HIDDEN, reason_codes=[f"r{i}" for i in range(40)])
    merged = merge_feed_visibility(noisy, classify_signal_for_feed(preclassification_result=_BUSINESS))
    assert len(merged["reason_codes"]) <= 12
    assert f"promoted:{VISIBILITY_HIDDEN}->{VISIBILITY_MAIN_FEED}" in merged["reason_codes"]


# ── A3 / test 6-7: outcome_unknown ─────────────────────────────────────────────────────────


def test_outcome_unknown_forces_attention_even_with_no_executive_evidence():
    """The exact post-`outcome_unknown` snapshot shape, reproduced field by field.

    `approve_hitl_action` (agent_runtime/mcp_service.py) writes hitl_gate.required=False and
    operational_status.code="ready_for_quote"; the send then raises before touching the snapshot.
    Without the projection this state is indistinguishable from a finished case.
    """
    bare = _snapshot(
        visibility=_vis(_NOISE),
        hitl_required=False,
        status_code="ready_for_quote",
        actions=[ActionItem(id="draft_reply", enabled=False, payload_pl="tresc")],
    )
    assert is_main_feed_member(bare) is False, "precondition: nothing else would reveal this case"

    flagged = bare.model_copy(
        update={
            "feed_visibility": FeedVisibility(
                **mark_execution_attention(bare.feed_visibility, reason="hitl_send_outcome_unknown")
            )
        }
    )
    mode, reasons = effective_visibility_mode(flagged)
    assert mode == VISIBILITY_ATTENTION_REQUIRED
    assert "operator_override:execution_attention:hitl_send_outcome_unknown" in reasons
    assert is_main_feed_member(flagged) is True


def test_execution_attention_survives_a_later_promotion():
    flagged = FeedVisibility(**mark_execution_attention(_vis(_NOISE), reason="hitl_send_outcome_unknown"))
    merged = merge_feed_visibility(flagged, classify_signal_for_feed(preclassification_result=_BUSINESS))
    assert merged["execution_attention"] is True
    assert merged["execution_attention_reason"] == "hitl_send_outcome_unknown"


def test_execution_attention_on_a_legacy_row_materialises_a_neutral_classification():
    projected = mark_execution_attention(None, reason="hitl_send_outcome_unknown")
    assert projected["mode"] == VISIBILITY_MAIN_FEED, "must not invent a routing lane"
    assert projected["source_lane"] == ""
    assert projected["execution_attention"] is True


def test_removing_the_executive_override_restores_the_stored_base_mode():
    """test 7: attention_required is dynamic, never a persisted verdict."""
    stored = _vis(_NOISE)
    with_hitl = _snapshot(visibility=stored, hitl_required=True)
    assert effective_visibility_mode(with_hitl)[0] == VISIBILITY_ATTENTION_REQUIRED
    without = _snapshot(visibility=stored, hitl_required=False)
    assert effective_visibility_mode(without)[0] == VISIBILITY_HIDDEN
    assert without.feed_visibility.mode == VISIBILITY_HIDDEN, "the stored base was never overwritten"


def test_classification_never_persists_attention_required():
    for lane in (_NOISE, _BUSINESS, _REFERENCE, {"lane": "review_direct"}, {}):
        assert classify_signal_for_feed(preclassification_result=lane)["mode"] != VISIBILITY_ATTENTION_REQUIRED


# ── A4 / test 11: real production path, no hand-built visibility ───────────────────────────


def _persist_first_signal(store, sig, *, case_id: str, engagement_id: str) -> EngagementSnapshotV2:
    return ensure_engagement_snapshot(
        store,
        signal=sig,
        runtime_context=_Ctx(),
        case_id=case_id,
        engagement_id=engagement_id,
        intake_output=dict(sig.payload.get("intake_result_final") or {}),
        dry_run=False,
    )


def test_production_path_noise_then_business_then_noise_on_one_engagement():
    """CanonicalSignal -> ensure_engagement_snapshot -> persisted row -> feed read.

    Nothing here sets `feed_visibility` by hand: the classification comes from the real signal
    payload through `_feed_visibility_for_signal`, the row goes through the real store, and the
    feed is read through the real `_list_main_feed_snapshots`.
    """
    store = InMemoryOperatorEngagementStore()
    eid, cid = "eng_prod_1", "case_prod_1"

    # 1. noise arrives first
    first = _persist_first_signal(store, _signal(_NOISE, message_id="m1"), case_id=cid, engagement_id=eid)
    assert first.feed_visibility is not None, "the case-bound path must record a classification"
    assert first.feed_visibility.mode == VISIBILITY_HIDDEN
    assert store.load_snapshot(eid).feed_visibility.mode == VISIBILITY_HIDDEN
    assert _list_main_feed_snapshots(store.list_recent_snapshots, limit=50) == []

    # 2. a real enquiry arrives on the SAME engagement
    promoted = ensure_engagement_snapshot(
        store,
        signal=_signal(_BUSINESS, message_id="m2", subject="Prosze o przygotowanie oferty"),
        runtime_context=_Ctx(),
        case_id=cid,
        engagement_id=eid,
        intake_output={},
        dry_run=False,
    )
    assert promoted.feed_visibility.mode == VISIBILITY_MAIN_FEED
    persisted = store.load_snapshot(eid)
    assert persisted.feed_visibility.mode == VISIBILITY_MAIN_FEED, "the promotion must be durable"
    feed = _list_main_feed_snapshots(store.list_recent_snapshots, limit=50)
    assert [s.engagement_id for s in feed] == [eid]

    # 3. a newsletter arrives afterwards -- the card must stay
    after_noise = ensure_engagement_snapshot(
        store,
        signal=_signal(_NOISE, message_id="m3"),
        runtime_context=_Ctx(),
        case_id=cid,
        engagement_id=eid,
        intake_output={},
        dry_run=False,
    )
    assert after_noise.feed_visibility.mode == VISIBILITY_MAIN_FEED
    assert [s.engagement_id for s in _list_main_feed_snapshots(store.list_recent_snapshots, limit=50)] == [eid]


def test_production_path_business_first_is_visible_immediately():
    store = InMemoryOperatorEngagementStore()
    _persist_first_signal(
        store, _signal(_BUSINESS, message_id="b1"), case_id="case_b", engagement_id="eng_b"
    )
    assert [s.engagement_id for s in _list_main_feed_snapshots(store.list_recent_snapshots, limit=50)] == ["eng_b"]


def test_production_path_promotion_bumps_the_stored_version():
    """The promotion is a real CAS write, not an in-memory copy."""
    store = InMemoryOperatorEngagementStore()
    first = _persist_first_signal(store, _signal(_NOISE, message_id="v1"), case_id="case_v", engagement_id="eng_v")
    promoted = ensure_engagement_snapshot(
        store,
        signal=_signal(_BUSINESS, message_id="v2"),
        runtime_context=_Ctx(),
        case_id="case_v",
        engagement_id="eng_v",
        intake_output={},
        dry_run=False,
    )
    assert promoted.version == first.version + 1
    assert store.load_snapshot("eng_v").version == promoted.version


def test_dry_run_promotion_is_not_persisted():
    store = InMemoryOperatorEngagementStore()
    _persist_first_signal(store, _signal(_NOISE, message_id="d1"), case_id="case_d", engagement_id="eng_d")
    promoted = _refresh_feed_visibility(
        store, store.load_snapshot("eng_d"), signal=_signal(_BUSINESS, message_id="d2"), dry_run=True
    )
    assert promoted.feed_visibility.mode == VISIBILITY_MAIN_FEED
    assert store.load_snapshot("eng_d").feed_visibility.mode == VISIBILITY_HIDDEN


def test_a_store_failure_during_promotion_never_breaks_signal_processing():
    class _Exploding(InMemoryOperatorEngagementStore):
        def save_snapshot(self, snapshot, expected_version):  # type: ignore[override]
            raise RuntimeError("simulated CAS conflict")

    store = _Exploding()
    _persist_first_signal(store, _signal(_NOISE, message_id="x1"), case_id="case_x", engagement_id="eng_x")
    out = ensure_engagement_snapshot(
        store,
        signal=_signal(_BUSINESS, message_id="x2"),
        runtime_context=_Ctx(),
        case_id="case_x",
        engagement_id="eng_x",
        intake_output={},
        dry_run=False,
    )
    assert out is not None and out.engagement_id == "eng_x"


def test_signal_without_classification_data_yields_no_visibility_metadata():
    bare = build_canonical_signal(
        signal_kind="gmail_message_observed",
        source_kind="gmail",
        source_ref={"message_id": "bare"},
        observed_at="2026-07-27T10:00:00+00:00",
        signal_summary_pl="",
        payload={},
    )
    # absent classification must not be read as noise; the neutral main_feed decision is recorded
    assert _feed_visibility_for_signal(bare).mode == VISIBILITY_MAIN_FEED


# ── test 10: volume ────────────────────────────────────────────────────────────────────────


def test_fifty_hidden_engagements_do_not_displace_one_real_card():
    store = InMemoryOperatorEngagementStore()
    for i in range(55):
        _persist_first_signal(
            store, _signal(_NOISE, message_id=f"n{i}"), case_id=f"case_n{i}", engagement_id=f"eng_n{i}"
        )
    _persist_first_signal(
        store, _signal(_BUSINESS, message_id="real"), case_id="case_real", engagement_id="eng_real"
    )
    feed = _list_main_feed_snapshots(store.list_recent_snapshots, limit=50)
    assert [s.engagement_id for s in feed] == ["eng_real"]
