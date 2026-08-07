"""AI-OS Roadmap 3.1 -- Follow-up Guardian.

Closes the missing capability named in `SUFITY.md`'s Barrier Matrix (Journey D):

    cisza przez N dni -> temporal signal -> propozycja dzialania -> karta operatora

Before this module, silence alone never produced anything: `sla_watcher.py` only ages
ALREADY-EXISTING decisions in the queue, and `understanding_output._pending_outcome_gaps_pl`
only fires when a NEW mail signal arrives. A case with zero inbound signal for a long time had
no path to a new operator-facing proposal at all.

This module deliberately reuses three already-proven primitives instead of inventing a parallel
system:

* `stagnation_sot.evaluate_waiting_vs_stagnation` (roadmap 2.1) is the single Source of Truth for
  "is this case merely waiting or genuinely stagnating" -- this module never re-derives that
  judgement, it only supplies the two required inputs (lifecycle_state, hours_in_state).
* `feed_visibility._has_pending_operator_work` (roadmap 2.4) already promotes any snapshot whose
  `actions` list carries an `enabled=True` item to `attention_required` on X1 -- this module's
  only product-facing act is appending exactly such an `ActionItem`, never touching visibility
  logic directly.
* the read -> `apply_snapshot_delta` -> `save_snapshot(expected_version=...)` optimistic-lock
  pattern already used by `mcp_service.approve_hitl_action` for every other snapshot mutation
  outside the mail-triggered reconcile path.

Scope, honestly stated: `OperationalStatus.code` only carries 5 literal values
(`llm_contracts/engagement_snapshot_v2.py`), and `case_lifecycle.OPERATIONAL_TO_LIFECYCLE` maps
exactly 3 of them to a state with an `SLA_HOURS` budget (`new_lead`, `qualification`,
`offer_preparation`). This guardian can therefore only ever flag stagnation in those 3 states --
it cannot see `waiting_for_client` / `negotiation` / etc., because nothing in the current snapshot
contract carries a lifecycle state richer than `operational_status.code`. That is a real, named
boundary of this slice, not a silent gap.

FG-03: when a mailbox SoT store is available, cases whose mailbox `status` is in the closed set
(`closed|done|archived|resolved|cancelled`, plus `merged`) are skipped -- closed cases must not
receive stagnation proposals even if the engagement snapshot still looks stale.

FG-02: `hours_in_state` prefers durable `EngagementSnapshotV2.lifecycle_state_since`, which the
engagement store stamps only when `operational_status.code` changes (or on first insert). Row
`updated_at` remains the fallback for legacy rows that never received a since stamp -- unrelated
saves (actions, feed, HITL, guardian proposal) bump `updated_at` but must not reset the
stagnation clock when since is present.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_runtime.draft_identity import compute_draft_id
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import AgentConcurrencyError, OperatorEngagementStore
from llm_contracts.case_lifecycle import (
    OPERATIONAL_TO_LIFECYCLE,
    SLA_HOURS,
    map_case_status_to_lifecycle,
    map_operational_to_lifecycle,
)
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2
from log_config import get_logger
from stagnation_sot import STATUS_STAGNATING, evaluate_waiting_vs_stagnation

logger = get_logger(__name__)

#: the stable `ActionItem.id` this guardian owns -- used both to mint the proposal and to detect
#: an already-pending one (dedup: never propose twice for the same still-stagnating case).
FOLLOW_UP_ACTION_ID = "follow_up_guardian"

#: states this guardian can evaluate today -- see module docstring "Scope, honestly stated".
_EVALUABLE_STATUS_CODES = frozenset(OPERATIONAL_TO_LIFECYCLE)

#: mailbox case statuses that must never receive a stagnation proposal (FG-03).
#: Aligns with `business_pulse` / feed closed set; `merged` added per product sense.
_CLOSED_CASE_STATUSES = frozenset(
    {"closed", "done", "archived", "resolved", "cancelled", "merged"}
)

_PL_LABEL_BY_CODE = {
    "raw_inquiry": "Nowe zapytanie bez ruchu operatora",
    "enriching": "Sprawa w kwalifikacji bez ruchu",
    "ready_for_quote": "Oferta przygotowana, brak follow-upu do klienta",
    "pending_operator": "Sprawa czeka na operatora",
    "node_a_error": "Sprawa w błędzie przetwarzania",
}


def _parse_updated_at(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        return None


def hours_since_updated_at(updated_at: str, *, now: datetime | None = None) -> float | None:
    """Elapsed hours since `updated_at`, or `None` when unmeasurable -- never guessed."""
    parsed = _parse_updated_at(updated_at)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    delta_hours = (reference - parsed).total_seconds() / 3600.0
    return max(0.0, delta_hours)


def hours_in_lifecycle_state(
    snapshot: EngagementSnapshotV2,
    updated_at: str,
    *,
    now: datetime | None = None,
) -> float | None:
    """FG-02: prefer durable `lifecycle_state_since`; fall back to row `updated_at`."""
    since = str(getattr(snapshot, "lifecycle_state_since", "") or "").strip()
    if since:
        return hours_since_updated_at(since, now=now)
    return hours_since_updated_at(updated_at, now=now)


def is_closed_case_status(status: str | None) -> bool:
    """True when mailbox case status is terminal (closed/merged/cancelled/...)."""
    return str(status or "").strip().lower() in _CLOSED_CASE_STATUSES


def resolve_mailbox_case_status(mailbox_store: Any, case_id: str) -> str | None:
    """Thin helper: read mailbox SoT case status, or None when unavailable.

    Uses `mailbox_store.fetch_case(case_id)` when present -- same pattern as
    `similar_cases_precedent` / write executors. Never invents a status.
    """
    cid = str(case_id or "").strip()
    if not cid or mailbox_store is None:
        return None
    fetch = getattr(mailbox_store, "fetch_case", None)
    if not callable(fetch):
        return None
    try:
        row = fetch(cid) or {}
    except Exception as exc:  # noqa: BLE001 - best-effort; missing status must not block tick
        logger.warning("follow_up_guardian_case_status_failed case_id=%s exc=%s", cid, exc)
        return None
    if not isinstance(row, dict):
        return None
    raw = row.get("status")
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


def has_active_follow_up_proposal(snapshot: EngagementSnapshotV2) -> bool:
    """True when this snapshot already carries an enabled guardian proposal.

    Dedup boundary owned entirely by this module -- `snapshot.actions` has no id-uniqueness
    enforcement anywhere else in the repo (confirmed), so the guardian must not append a second
    proposal on every tick while the case remains stagnating.
    """
    for action in snapshot.actions:
        if str(getattr(action, "id", "")) == FOLLOW_UP_ACTION_ID and bool(
            getattr(action, "enabled", False)
        ):
            return True
    return False


def evaluate_follow_up_candidate(
    *,
    operational_status_code: str,
    hours_since_update: float | None,
    case_status: str = "",
) -> dict[str, Any] | None:
    """Pure decision: does this case's CURRENT state warrant a follow-up proposal right now?

    Returns the `stagnation_sot` verdict dict when `status == stagnating`, else `None`.

    FG-01 Option B (case-status-primary): when mailbox `case_status` maps to a lifecycle
    state with an SLA budget, prefer that over the collapsed operational→lifecycle map.
    """
    code = str(operational_status_code or "").strip().lower()
    status = str(case_status or "").strip().lower()

    lifecycle_state = None
    if status:
        # Prefer mailbox/pipeline status mapping when it yields a budgeted state.
        mapped = map_case_status_to_lifecycle(status, dialect="mailbox")
        if mapped in SLA_HOURS:
            lifecycle_state = mapped
    if lifecycle_state is None:
        if code not in _EVALUABLE_STATUS_CODES and not status:
            return None
        lifecycle_state = map_operational_to_lifecycle(code, case_status=status)

    if lifecycle_state not in SLA_HOURS:
        return None

    verdict = evaluate_waiting_vs_stagnation(
        lifecycle_state=lifecycle_state,
        hours_in_state=hours_since_update,
    )
    if verdict["status"] != STATUS_STAGNATING:
        return None
    return verdict


def build_follow_up_action_item(
    *,
    case_id: str,
    operational_status_code: str,
    verdict: dict[str, Any],
) -> dict[str, Any]:
    """ActionItem-shaped dict for the follow-up proposal.

    `source_signal_id=""` is deliberate and safe: `feed_visibility._has_pending_operator_work`
    only checks `enabled`, never `source_signal_id`, so a system-authored (non-mail) proposal
    surfaces on X1 exactly like a mail-triggered one.

    PF-01: run `evaluate_draft_sanity` before `enabled=True` (fail-closed). This payload is an
    operator-facing prompt (not customer email body); the gate is defense-in-depth for the
    exhaustive `enabled=True` inventory, not a claim that Guardian drafts were leaking to clients.
    """
    from agent_runtime.draft_sanity import evaluate_draft_sanity

    label = _PL_LABEL_BY_CODE.get(str(operational_status_code or "").strip().lower(), "Sprawa bez ruchu operatora")
    sla_hours = verdict.get("sla_hours")
    hours = verdict.get("hours_in_state")
    detail = ""
    if isinstance(sla_hours, (int, float)) and isinstance(hours, (int, float)):
        detail = f" ({int(hours)}h w stanie, budżet {int(sla_hours)}h)"
    payload_pl = f"{label}{detail}. Zaproponuj follow-up do klienta."
    action = {
        "id": FOLLOW_UP_ACTION_ID,
        "enabled": True,
        "payload_pl": payload_pl,
        "disabled_reason_pl": None,
        "parent_policy_decision_id": "",
        "parent_action_proposal_v2_id": "",
        "parent_decision_candidate_id": "",
        "source_signal_id": "",
        "draft_id": compute_draft_id(
            case_id=case_id, source_signal_id="", action_id=FOLLOW_UP_ACTION_ID
        ),
        "revision": 1,
        "body_hash": "",
        "case_id": case_id,
        "identity_state": "identity_incomplete",
    }
    sanity = evaluate_draft_sanity(body=payload_pl, case_kind="follow_up", intent="follow_up")
    if not sanity.get("ok"):
        reasons = ",".join(sanity.get("reason_codes") or [])
        action["enabled"] = False
        action["disabled_reason_pl"] = f"DRAFT_SANITY_FAILED: {reasons}"
    return action


def _emit_follow_up_event(*, db_url: str, case_id: str, engagement_id: str, verdict: dict[str, Any]) -> None:
    if not db_url:
        return
    try:
        from event_spine.emitter import publish_os_event

        publish_os_event(
            database_url=db_url,
            event_type="follow_up_guardian.proposed",
            source_repo="gmail-agent",
            case_id=case_id,
            engagement_id=engagement_id,
            severity="info",
            success=True,
            payload={
                "reason_codes": verdict.get("reason_codes"),
                "sla_hours": verdict.get("sla_hours"),
                "hours_in_state": verdict.get("hours_in_state"),
                "lifecycle_state": verdict.get("lifecycle_state"),
            },
        )
    except Exception as exc:  # noqa: BLE001 - best-effort audit trail, never blocks the proposal
        logger.warning("follow_up_guardian_event_emit_failed: %s", exc)


def run_follow_up_guardian_tick(
    store: OperatorEngagementStore,
    *,
    limit: int = 200,
    now: datetime | None = None,
    db_url: str = "",
    mailbox_store: Any = None,
) -> dict[str, Any]:
    """One pass: scan recent snapshots, propose a follow-up for every newly-stagnating case.

    Single-shot per candidate (no internal retry loop): a `AgentConcurrencyError` on save is
    logged and skipped -- the next tick re-evaluates the case fresh, same as `sla_watcher`'s own
    best-effort escalation.

    FG-03: when `mailbox_store` is provided, cases whose mailbox status is in the closed set
    (`closed|done|archived|resolved|cancelled|merged`) are skipped -- no stagnation proposal.
    """
    reference = now or datetime.now(timezone.utc)
    checked = 0
    proposed: list[str] = []
    skipped_not_stagnating = 0
    skipped_already_proposed = 0
    skipped_closed = 0
    conflicts = 0

    for snapshot, updated_at in store.list_recent_snapshots_with_updated_at(limit=limit):
        checked += 1
        case_id = str(snapshot.case_id or "").strip()
        if not case_id:
            continue  # staging snapshot, not a real case yet
        mailbox_status = resolve_mailbox_case_status(mailbox_store, case_id)
        if is_closed_case_status(mailbox_status):
            skipped_closed += 1
            continue
        if has_active_follow_up_proposal(snapshot):
            skipped_already_proposed += 1
            continue

        hours = hours_in_lifecycle_state(snapshot, updated_at, now=reference)
        verdict = evaluate_follow_up_candidate(
            operational_status_code=snapshot.operational_status.code,
            hours_since_update=hours,
            case_status=str(mailbox_status or ""),
        )
        if verdict is None:
            skipped_not_stagnating += 1
            continue

        action_item = build_follow_up_action_item(
            case_id=case_id,
            operational_status_code=snapshot.operational_status.code,
            verdict=verdict,
        )
        delta = {"actions": [*[a.model_dump(mode="python") for a in snapshot.actions], action_item]}
        patched = apply_snapshot_delta(snapshot, delta)
        try:
            store.save_snapshot(patched, expected_version=snapshot.version)
        except AgentConcurrencyError as exc:
            conflicts += 1
            logger.info("follow_up_guardian_conflict case_id=%s exc=%s", case_id, exc)
            continue

        proposed.append(case_id)
        _emit_follow_up_event(
            db_url=db_url, case_id=case_id, engagement_id=snapshot.engagement_id, verdict=verdict
        )

    return {
        "ok": True,
        "checked_at": reference.isoformat(),
        "checked": checked,
        "proposed_case_ids": proposed,
        "proposed_count": len(proposed),
        "skipped_not_stagnating": skipped_not_stagnating,
        "skipped_already_proposed": skipped_already_proposed,
        "skipped_closed": skipped_closed,
        "conflicts": conflicts,
    }


def follow_up_guardian_oneshot(settings: Any, *, limit: int = 200) -> dict[str, Any]:
    """CLI/tick entrypoint mirroring `sla_watcher.sla_watcher_oneshot`."""
    from agent_runtime.store import PostgresOperatorEngagementStore

    db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "")
    if not db_url:
        return {"ok": False, "error": "Database not configured."}

    store = PostgresOperatorEngagementStore(db_url)
    mailbox_store: Any = None
    try:
        from mailbox_memory_runtime import build_mailbox_memory_runtime

        runtime = build_mailbox_memory_runtime(settings)
        mailbox_store = getattr(runtime, "store", None)
    except Exception as exc:  # noqa: BLE001 - tick still useful without closed-status filter
        logger.warning("follow_up_guardian_mailbox_store_unavailable: %s", exc)
        mailbox_store = None
    return run_follow_up_guardian_tick(
        store, limit=limit, db_url=db_url, mailbox_store=mailbox_store
    )


__all__ = [
    "FOLLOW_UP_ACTION_ID",
    "build_follow_up_action_item",
    "evaluate_follow_up_candidate",
    "follow_up_guardian_oneshot",
    "has_active_follow_up_proposal",
    "hours_in_lifecycle_state",
    "hours_since_updated_at",
    "is_closed_case_status",
    "resolve_mailbox_case_status",
    "run_follow_up_guardian_tick",
]
