
"""Lifecycle SLA Monitor — background job do wykrywania stagnacji.

Generic Hands: używany przez reconcile worker lub nightly cron.
Emituje os_event + proponuje przejście do STAGNATING.
"""
from __future__ import annotations

from log_config import get_logger
from datetime import datetime, timezone
from typing import Any

from llm_contracts.case_lifecycle import CaseLifecycleState, SLA_HOURS

from case_family_boundary import is_operational_feed_case_row

logger = get_logger(__name__)


def _find_cases_in_state_over_hours(
    mailbox_store: Any,
    state: CaseLifecycleState,
    hours: int,
) -> list[str]:
    """Znajduje sprawy w danym stanie, które przekroczyły SLA.

    Args:
        mailbox_store: store z metodą fetch_cases_in_state_since
        state: stan do sprawdzenia
        hours: próg SLA w godzinach

    Returns:
        lista case_id
    """
    if mailbox_store is None:
        return []

    finder = getattr(mailbox_store, "fetch_case_ids_in_state_since", None)
    if finder is None:
        finder = getattr(mailbox_store, "fetch_cases_by_state", None)

    if finder is None:
        logger.warning("lifecycle_monitor: mailbox_store has no fetch_case_ids_in_state_since or fetch_cases_by_state")
        return []

    try:
        cases = finder(state=state.value)
    except Exception as exc:
        logger.error("lifecycle_monitor: fetch failed for state %s: %s", state.value, exc)
        return []

    now = datetime.now(timezone.utc)
    cutoff_hours = max(hours, 1)
    overdue: list[str] = []

    for case in cases:
        if isinstance(case, dict):
            if not is_operational_feed_case_row(case):
                continue
            case_id = case.get("case_id") or case.get("id") or ""
            updated_at = case.get("updated_at") or case.get("last_activity_at") or case.get("created_at")
        else:
            case_id = str(case)
            updated_at = None

        if not case_id:
            continue

        if updated_at is not None:
            if isinstance(updated_at, str):
                try:
                    updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    updated_at = None
            if isinstance(updated_at, datetime):
                elapsed_hours = (now - updated_at).total_seconds() / 3600
                if elapsed_hours < cutoff_hours:
                    continue  # jeszcze w SLA

        overdue.append(case_id)

    return overdue


def _emit_sla_violation_event(
    case_id: str,
    state: CaseLifecycleState,
    hours: int,
    *,
    os_event_emitter: Any = None,
) -> None:
    """Emituje os_event o naruszeniu SLA."""
    try:
        if os_event_emitter is not None and callable(os_event_emitter):
            os_event_emitter(
                event_type="lifecycle.sla_violation",
                payload={
                    "case_id": case_id,
                    "lifecycle_state": state.value,
                    "sla_hours": hours,
                    "trigger": "lifecycle_monitor",
                },
            )
        logger.info(
            "lifecycle_monitor: SLA violation case=%s state=%s hours=%d",
            case_id, state.value, hours,
        )
    except Exception as exc:
        logger.error("lifecycle_monitor: emit error for %s: %s", case_id, exc)


def check_sla_violations(
    mailbox_store: Any,
    *,
    dry_run: bool = False,
    os_event_emitter: Any = None,
) -> list[dict]:
    """Sprawdza sprawy przekraczające SLA w danym stanie.

    Emituje os_event + proponuje przejście do STAGNATING.
    Uruchamiany przez reconcile worker lub nightly cron.

    Args:
        mailbox_store: przechowalnik spraw MailboxMemory
        dry_run: jeśli True — tylko raport, bez emisji
        os_event_emitter: callback do emitowania os_event (np. publish_os_event)

    Returns:
        lista słowników z naruszeniami:
        [{"case_id": str, "state": str, "hours_exceeded": int, "violation_detected_at": str}]
    """
    violations: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for state, hours in SLA_HOURS.items():
        try:
            cases = _find_cases_in_state_over_hours(mailbox_store, state, hours)
        except Exception as exc:
            logger.error("lifecycle_monitor: error checking state %s: %s", state.value, exc)
            continue

        for case_id in cases:
            violation = {
                "case_id": case_id,
                "state": state.value,
                "hours_exceeded": hours,
                "violation_detected_at": now_iso,
            }
            violations.append(violation)

            if not dry_run:
                _emit_sla_violation_event(case_id, state, hours, os_event_emitter=os_event_emitter)
                logger.info(
                    "lifecycle_monitor: violation detected case=%s state=%s sla=%dh",
                    case_id, state.value, hours,
                )

    return violations


__all__ = [
    "check_sla_violations",
    "_find_cases_in_state_over_hours",  # testable
    "_emit_sla_violation_event",
]
