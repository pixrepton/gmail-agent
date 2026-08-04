"""Operator feed membership contract (SLICE-2B).

Core principle, stated once:

    the existence of a snapshot is NOT membership of the operator's main feed.

Before this module, `list_recent_snapshots` had no `WHERE` clause at all, so every
`operator_engagement_snapshots` row was a feed-membership candidate — including the staging
snapshot a confirmed-noise mail creates (routing proof: `NOISE_CAN_REACH_FEED_CONFIRMED`).

Design constraints honoured here:

* one pure deterministic function, no new table, no new orchestrator, no scheduler;
* the *routing classification* (lane / triage / reason codes) is stored in the existing snapshot
  envelope and RE-EVALUATED on every later external signal for the same engagement, under a
  monotonic promotion rule (`merge_feed_visibility`) -- a first noise mail must never permanently
  hide a business message that arrives on the same engagement later (SLICE-2B1);
* the *effective mode* is recomputed from that classification plus the snapshot's CURRENT
  executive fields, so a message that later acquires a real pending HITL becomes visible without
  anyone rewriting its stored classification;
* the safety override reads real executive state (`hitl_gate`, `actions`, `operational_status`),
  never a text keyword;
* staging status and TTL are NOT used as a visibility proxy — they answer a different question.

Node B owns this contract. Daszek consumes the decision; it must not re-derive it.
"""
from __future__ import annotations

from typing import Any

# Ordered from least to most operator attention.
VISIBILITY_HIDDEN = "hidden"
VISIBILITY_CASE_TIMELINE_ONLY = "case_timeline_only"
VISIBILITY_MAIN_FEED = "main_feed"
VISIBILITY_ATTENTION_REQUIRED = "attention_required"

FEED_VISIBILITY_MODES = (
    VISIBILITY_HIDDEN,
    VISIBILITY_CASE_TIMELINE_ONLY,
    VISIBILITY_MAIN_FEED,
    VISIBILITY_ATTENTION_REQUIRED,
)

#: modes that place a card in the operator's MAIN feed
MAIN_FEED_MODES = frozenset({VISIBILITY_MAIN_FEED, VISIBILITY_ATTENTION_REQUIRED})

#: Monotonic order of the STORED base classification. `attention_required` is deliberately absent:
#: it is a dynamic executive override recomputed at read time, never a persisted routing verdict.
#: An unrecognised stored mode ranks as `main_feed`, so it can never be demoted by accident.
_BASE_MODE_RANK = {
    VISIBILITY_HIDDEN: 0,
    VISIBILITY_CASE_TIMELINE_ONLY: 1,
    VISIBILITY_MAIN_FEED: 2,
}

#: hard cap on the promotion audit trail carried inside the snapshot
_MAX_REASON_CODES = 12

#: lanes whose signals must not create a standalone main-feed card on their own
_NON_MEMBER_LANES = {"skip", "reference_only"}

#: triage classes that mirror the above (both are recorded; either is sufficient evidence)
_NON_MEMBER_TRIAGE = {"ignore", "reference_only"}

#: `OperationalStatus.code` values that mean an operator decision is genuinely outstanding.
#: Restricted to the real Literal vocabulary of the model -- `raw_inquiry`, `enriching`,
#: `ready_for_quote`, `pending_operator`, `node_a_error`.
#:
#: NOTE on `outcome_unknown`: it is a HITL *send* state living in MailboxMemory keyed by
#: `decision_key` (`agent_hitl_bridge._read_hitl_state`), and it never appears on the snapshot --
#: `OperationalStatus.code` has no such literal. SLICE-2B1 therefore projects it explicitly via
#: `mark_execution_attention` at the moment the send resolves, instead of relying on the
#: (unguaranteed) coincidence that such a case still carries an enabled action proposal.
_ATTENTION_STATUS_CODES = frozenset({"pending_operator", "node_a_error"})


def classify_signal_for_feed(
    *,
    preclassification_result: dict[str, Any] | None,
    triage_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide the ROUTING part of feed membership, once, at snapshot creation.

    Pure. Returns the stored classification; it deliberately does not look at executive state,
    which changes over time and is applied later by `effective_visibility_mode`.

    An unknown or absent lane is NEVER treated as noise — it falls through to `main_feed`, so a
    classification gap can only ever over-show, never silently hide a real case.
    """
    pre = preclassification_result if isinstance(preclassification_result, dict) else {}
    tri = triage_result if isinstance(triage_result, dict) else {}

    lane = str(pre.get("lane") or "").strip().lower()
    triage_class = str(tri.get("triage_class") or "").strip().lower()
    reason_codes = [str(r)[:80] for r in (pre.get("reasons") or tri.get("reason_codes") or []) if str(r).strip()][:8]

    if lane == "skip" or triage_class == "ignore":
        base_mode = VISIBILITY_HIDDEN
    elif lane == "reference_only" or triage_class == "reference_only":
        # attached to a case it belongs on that case's timeline; orphaned it is simply not a card
        base_mode = VISIBILITY_CASE_TIMELINE_ONLY
    else:
        # review_direct / needs_operator_review / intake_llm / unknown / absent
        base_mode = VISIBILITY_MAIN_FEED

    return {
        "mode": base_mode,
        "reason_codes": reason_codes,
        "source_lane": lane,
        "source_triage_class": triage_class,
        "operator_override": False,
    }


def _base_rank(mode: Any) -> int:
    return _BASE_MODE_RANK.get(str(mode or "").strip().lower(), _BASE_MODE_RANK[VISIBILITY_MAIN_FEED])


def merge_feed_visibility(
    stored: Any,
    incoming: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """SLICE-2B1: fold a NEW signal's routing classification into an EXISTING snapshot's stored one.

    Returns the merged classification to persist, or ``None`` when nothing must change.

    The rule is **monotonic promotion**, never "last signal wins":

        hidden  <  case_timeline_only  <  main_feed

    Rationale, in one line each:

    * a later business message must be able to lift a case out of `hidden` -- an engagement is
      long-lived, and the first mail on it does not get to decide its visibility forever;
    * a later newsletter on an already-visible case must NOT be able to hide it again, which is
      exactly what "last signal wins" would do, and is the more dangerous of the two errors;
    * demotion (closing, archiving, lifecycle) is a different contract with a different trigger
      and is deliberately not implemented here -- nothing in this function can lower a mode.

    Legacy rows (`stored is None`) are left untouched: they already read as `main_feed` via the
    explicit fallback in `effective_visibility_mode`, and writing a classification onto them would
    be a silent backfill that could only ever reduce what the operator sees.

    `execution_attention` is carried across a promotion unchanged -- it is execution state, not a
    routing verdict, and only its own owner clears it.
    """
    if stored is None:
        return None
    if not isinstance(incoming, dict) or not incoming:
        return None

    stored_mode = str(getattr(stored, "mode", "") or VISIBILITY_MAIN_FEED)
    incoming_mode = str(incoming.get("mode") or VISIBILITY_MAIN_FEED)
    if _base_rank(incoming_mode) <= _base_rank(stored_mode):
        return None

    stored_reasons = [str(r) for r in (getattr(stored, "reason_codes", None) or [])]
    incoming_reasons = [str(r) for r in (incoming.get("reason_codes") or [])]
    reasons = [*stored_reasons, f"promoted:{stored_mode}->{incoming_mode}", *incoming_reasons]

    return {
        "mode": incoming_mode,
        # newest-last would drop the promotion history first; keep the tail so the reason for the
        # CURRENT mode always survives the cap
        "reason_codes": reasons[-_MAX_REASON_CODES:],
        "source_lane": str(incoming.get("source_lane") or ""),
        "source_triage_class": str(incoming.get("source_triage_class") or ""),
        "operator_override": bool(getattr(stored, "operator_override", False)),
        "execution_attention": bool(getattr(stored, "execution_attention", False)),
        "execution_attention_reason": str(getattr(stored, "execution_attention_reason", "") or ""),
    }


def mark_execution_attention(stored: Any, *, reason: str) -> dict[str, Any]:
    """Project an unresolved EXECUTION state that the snapshot itself cannot express.

    `outcome_unknown` is the motivating case: it is a HITL *send* state kept in MailboxMemory
    under `decision_key`, and `OperationalStatus.code` has no literal for it. After a send
    resolves to `outcome_unknown` the snapshot is left with `hitl_gate.required=False` and
    `operational_status.code="ready_for_quote"` (both written by `approve_hitl_action`), so the
    executive-state override in `_has_pending_operator_work` cannot see it.

    This flag is visibility-only. It does not touch the exactly-once send state machine, the
    `decision_key`, retry policy, or `operational_status` -- nothing here can cause a re-send.
    """
    base: dict[str, Any]
    if stored is None:
        # legacy row: it already reads as main_feed, but the attention state is real and must be
        # recorded, so materialise the neutral classification rather than inventing a routing lane
        base = {
            "mode": VISIBILITY_MAIN_FEED,
            "reason_codes": ["legacy_snapshot_without_visibility_metadata"],
            "source_lane": "",
            "source_triage_class": "",
            "operator_override": False,
        }
    else:
        base = {
            "mode": str(getattr(stored, "mode", "") or VISIBILITY_MAIN_FEED),
            "reason_codes": [str(r) for r in (getattr(stored, "reason_codes", None) or [])],
            "source_lane": str(getattr(stored, "source_lane", "") or ""),
            "source_triage_class": str(getattr(stored, "source_triage_class", "") or ""),
            "operator_override": bool(getattr(stored, "operator_override", False)),
        }
    base["execution_attention"] = True
    base["execution_attention_reason"] = str(reason or "")[:80]
    base["reason_codes"] = base["reason_codes"][-_MAX_REASON_CODES:]
    return base


def apply_operator_visibility_override(stored: Any, *, mode: str, reason: str = "") -> dict[str, Any]:
    """Roadmap 2.4: the operator reclassifies where a signal belongs, explicitly and auditably.

    Only the three STORED base modes are accepted. `attention_required` is rejected on purpose: it
    is not a routing verdict but a dynamic read-time override recomputed from real executive state,
    so persisting it would let a stale flag speak for state that has since changed.

    What this function does NOT do, deliberately:

    * it does not disable the executive safety override — a case the operator hid still surfaces as
      `attention_required` while real operator work is outstanding (`_has_pending_operator_work`).
      Hiding is a preference; an unresolved HITL gate is a fact;
    * it does not stop later monotonic promotion (`merge_feed_visibility`). An operator hiding a
      case today must not permanently silence a business message that arrives on it tomorrow.

    Both properties are asserted in `test_aios_2_4_x1_exceptions_only.py`.
    """
    target = str(mode or "").strip().lower()
    if target not in _BASE_MODE_RANK:
        raise ValueError(
            f"operator override mode must be one of {sorted(_BASE_MODE_RANK)}; got {mode!r}"
        )
    base = mark_execution_attention(stored, reason="")
    base["execution_attention"] = bool(getattr(stored, "execution_attention", False))
    base["execution_attention_reason"] = str(getattr(stored, "execution_attention_reason", "") or "")
    previous = str(getattr(stored, "mode", "") or VISIBILITY_MAIN_FEED) if stored is not None else VISIBILITY_MAIN_FEED
    trail = f"operator_reclassified:{previous}->{target}"
    if str(reason or "").strip():
        trail = f"{trail}:{str(reason).strip()[:40]}"
    base["mode"] = target
    base["operator_override"] = True
    base["reason_codes"] = [*base["reason_codes"], trail][-_MAX_REASON_CODES:]
    return base


def clear_operator_visibility_override(stored: Any) -> dict[str, Any] | None:
    """Clear an operator reclassification and restore routing-derived base mode."""
    if stored is None:
        return None
    if not bool(getattr(stored, "operator_override", False)):
        return None

    lane = str(getattr(stored, "source_lane", "") or "").strip().lower()
    triage = str(getattr(stored, "source_triage_class", "") or "").strip().lower()
    reasons = [
        str(r)
        for r in (getattr(stored, "reason_codes", None) or [])
        if str(r).strip() and not str(r).startswith("operator_reclassified:")
    ]

    if lane == "skip" or triage == "ignore":
        base_mode = VISIBILITY_HIDDEN
    elif lane == "reference_only" or triage == "reference_only":
        base_mode = VISIBILITY_CASE_TIMELINE_ONLY
    else:
        base_mode = VISIBILITY_MAIN_FEED

    return {
        "mode": base_mode,
        "reason_codes": reasons[-_MAX_REASON_CODES:],
        "source_lane": lane,
        "source_triage_class": triage,
        "operator_override": False,
        "execution_attention": bool(getattr(stored, "execution_attention", False)),
        "execution_attention_reason": str(getattr(stored, "execution_attention_reason", "") or ""),
    }


def clear_execution_attention(stored: Any) -> dict[str, Any]:
    """Clear visibility-only execution attention after confirmed communication_sent."""
    base = mark_execution_attention(stored, reason="")
    base["execution_attention"] = False
    base["execution_attention_reason"] = ""
    return base


def _has_pending_operator_work(snapshot: Any) -> tuple[bool, str]:
    """Real executive evidence that an operator decision is outstanding. Never keyword-based."""
    stored = getattr(snapshot, "feed_visibility", None)
    if stored is not None and bool(getattr(stored, "execution_attention", False)):
        why = str(getattr(stored, "execution_attention_reason", "") or "unresolved_execution")
        return True, f"execution_attention:{why}"

    hitl = getattr(snapshot, "hitl_gate", None)
    if hitl is not None and bool(getattr(hitl, "required", False)):
        return True, "pending_hitl_gate"

    status = getattr(snapshot, "operational_status", None)
    code = str(getattr(status, "code", "") or "").strip().lower()
    if code in _ATTENTION_STATUS_CODES:
        return True, f"operational_status:{code}"

    for action in getattr(snapshot, "actions", None) or []:
        if bool(getattr(action, "enabled", False)):
            return True, "enabled_action_proposal"

    return False, ""


def effective_visibility_mode(snapshot: Any) -> tuple[str, list[str]]:
    """Effective feed mode for one snapshot: stored classification + CURRENT executive state.

    Backward compatibility: a snapshot with no stored classification (every row written before
    this slice) is treated as `main_feed`. Legacy data is never mass-hidden — an explicit,
    reason-coded fallback, not a silent default.
    """
    stored = getattr(snapshot, "feed_visibility", None)
    if stored is None:
        base_mode = VISIBILITY_MAIN_FEED
        reasons = ["legacy_snapshot_without_visibility_metadata"]
    else:
        base_mode = str(getattr(stored, "mode", "") or VISIBILITY_MAIN_FEED)
        reasons = list(getattr(stored, "reason_codes", None) or [])
        if base_mode not in FEED_VISIBILITY_MODES:
            base_mode = VISIBILITY_MAIN_FEED
            reasons = [*reasons, "unknown_visibility_mode_defaulted_to_main_feed"]

    pending, why = _has_pending_operator_work(snapshot)
    if pending:
        # Safety override: real outstanding operator work always wins over a routing decision,
        # including for a snapshot that was classified as noise.
        return VISIBILITY_ATTENTION_REQUIRED, [*reasons, f"operator_override:{why}"]

    return base_mode, reasons


def is_main_feed_member(snapshot: Any) -> bool:
    """True when this snapshot belongs in the operator's MAIN feed."""
    mode, _reasons = effective_visibility_mode(snapshot)
    return mode in MAIN_FEED_MODES


def is_case_timeline_only(snapshot: Any) -> bool:
    """True when this snapshot belongs on a case timeline but NOT in the main feed.

    Roadmap 2.4: `case_timeline_only` existed as a classification with no reader, which made it
    indistinguishable from `hidden` in practice. This is the predicate the feed build uses to
    surface those signals in their own bucket, separately from the operator's desk.
    """
    mode, _reasons = effective_visibility_mode(snapshot)
    return mode == VISIBILITY_CASE_TIMELINE_ONLY


__all__ = [
    "FEED_VISIBILITY_MODES",
    "MAIN_FEED_MODES",
    "VISIBILITY_ATTENTION_REQUIRED",
    "VISIBILITY_CASE_TIMELINE_ONLY",
    "VISIBILITY_HIDDEN",
    "VISIBILITY_MAIN_FEED",
    "apply_operator_visibility_override",
    "clear_operator_visibility_override",
    "classify_signal_for_feed",
    "effective_visibility_mode",
    "is_case_timeline_only",
    "is_main_feed_member",
    "clear_execution_attention",
    "mark_execution_attention",
    "merge_feed_visibility",
]
