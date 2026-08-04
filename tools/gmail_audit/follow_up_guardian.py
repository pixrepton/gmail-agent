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

`hours_in_state` has no dedicated field anywhere in this repo (confirmed: `api_app`'s
`lifecycle_state_since` / `lifecycle_state_updated_at` reader has no writer). This module uses the
snapshot row's own `updated_at` as the proxy: `operational_status.code` only changes when the
snapshot is rewritten, so elapsed time since the last write IS elapsed time in the current state,
for every actor that writes this table today.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_runtime.draft_identity import compute_draft_id
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import AgentConcurrencyError, OperatorEngagementStore
from llm_contracts.case_lifecycle import OPERATIONAL_TO_LIFECYCLE, map_operational_to_lifecycle
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2
from log_config import get_logger
from stagnation_sot import STATUS_STAGNATING, evaluate_waiting_vs_stagnation

logger = get_logger(__name__)

#: the stable `ActionItem.id` this guardian owns -- used both to mint the proposal and to detect
#: an already-pending one (dedup: never propose twice for the same still-stagnating case).
FOLLOW_UP_ACTION_ID = "follow_up_guardian"

#: states this guardian can evaluate today -- see module docstring "Scope, honestly stated".
_EVALUABLE_STATUS_CODES = frozenset(OPERATIONAL_TO_LIFECYCLE)

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
) -> dict[str, Any] | None:
    """Pure decision: does this case's CURRENT state warrant a follow-up proposal right now?

    Returns the `stagnation_sot` verdict dict when `status == stagnating`, else `None`. Delegates
    the actual waiting-vs-stagnation judgement entirely to `stagnation_sot` (roadmap 2.1) -- this
    function only maps `operational_status.code` to a lifecycle state, per this module's stated
    scope boundary.
    """
    code = str(operational_status_code or "").strip().lower()
    if code not in _EVALUABLE_STATUS_CODES:
        return None
    lifecycle_state = map_operational_to_lifecycle(code)
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
    """
    label = _PL_LABEL_BY_CODE.get(str(operational_status_code or "").strip().lower(), "Sprawa bez ruchu operatora")
    sla_hours = verdict.get("sla_hours")
    hours = verdict.get("hours_in_state")
    detail = ""
    if isinstance(sla_hours, (int, float)) and isinstance(hours, (int, float)):
        detail = f" ({int(hours)}h w stanie, budżet {int(sla_hours)}h)"
    payload_pl = f"{label}{detail}. Zaproponuj follow-up do klienta."
    return {
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
) -> dict[str, Any]:
    """One pass: scan recent snapshots, propose a follow-up for every newly-stagnating case.

    Single-shot per candidate (no internal retry loop): a `AgentConcurrencyError` on save is
    logged and skipped -- the next tick re-evaluates the case fresh, same as `sla_watcher`'s own
    best-effort escalation.
    """
    reference = now or datetime.now(timezone.utc)
    checked = 0
    proposed: list[str] = []
    skipped_not_stagnating = 0
    skipped_already_proposed = 0
    conflicts = 0

    for snapshot, updated_at in store.list_recent_snapshots_with_updated_at(limit=limit):
        checked += 1
        case_id = str(snapshot.case_id or "").strip()
        if not case_id:
            continue  # staging snapshot, not a real case yet
        if has_active_follow_up_proposal(snapshot):
            skipped_already_proposed += 1
            continue

        hours = hours_since_updated_at(updated_at, now=reference)
        verdict = evaluate_follow_up_candidate(
            operational_status_code=snapshot.operational_status.code,
            hours_since_update=hours,
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
        "conflicts": conflicts,
    }


def follow_up_guardian_oneshot(settings: Any, *, limit: int = 200) -> dict[str, Any]:
    """CLI/tick entrypoint mirroring `sla_watcher.sla_watcher_oneshot`."""
    from agent_runtime.store import PostgresOperatorEngagementStore

    db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "")
    if not db_url:
        return {"ok": False, "error": "Database not configured."}

    store = PostgresOperatorEngagementStore(db_url)
    return run_follow_up_guardian_tick(store, limit=limit, db_url=db_url)


__all__ = [
    "FOLLOW_UP_ACTION_ID",
    "build_follow_up_action_item",
    "evaluate_follow_up_candidate",
    "follow_up_guardian_oneshot",
    "has_active_follow_up_proposal",
    "hours_since_updated_at",
    "run_follow_up_guardian_tick",
]
