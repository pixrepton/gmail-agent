"""Case Lifecycle Engine — formalny model stanów sprawy.

Generic Hands: stany i przejścia są w jednym pliku jako enum + dict.
Zero if/elif. Każde przejście walidowane przez ALLOWED_TRANSITIONS.
"""
from __future__ import annotations

from enum import Enum
from typing import Any
from log_config import get_logger

logger = get_logger(__name__)


class CaseLifecycleState(str, Enum):
    """Biznesowe stany sprawy TOP-INSTAL.

    Kolejność = typowy przebieg (nie wymuszamy progresji — walidacja w ALLOWED_TRANSITIONS).
    """
    NEW_LEAD             = "new_lead"
    QUALIFICATION        = "qualification"
    SITE_VISIT           = "site_visit_required"
    OFFER_PREP           = "offer_preparation"
    WAITING_CLIENT       = "waiting_for_client"
    NEGOTIATION          = "negotiation"
    READY_INSTALL        = "ready_for_installation"
    IN_PROGRESS          = "installation_in_progress"
    COMPLETED            = "completed"
    LOST                 = "lost"
    STAGNATING           = "stagnating"


# ── Dozwolone przejścia (graf) ─────────────────────────────────────────
# Klucz = stan bieżący, wartość = zbiór dozwolonych stanów docelowych.
ALLOWED_TRANSITIONS: dict[CaseLifecycleState, set[CaseLifecycleState]] = {
    CaseLifecycleState.NEW_LEAD: {
        CaseLifecycleState.QUALIFICATION,
        CaseLifecycleState.LOST,
    },
    CaseLifecycleState.QUALIFICATION: {
        CaseLifecycleState.SITE_VISIT,
        CaseLifecycleState.OFFER_PREP,
        CaseLifecycleState.LOST,
        CaseLifecycleState.STAGNATING,
    },
    CaseLifecycleState.SITE_VISIT: {
        CaseLifecycleState.OFFER_PREP,
        CaseLifecycleState.WAITING_CLIENT,
        CaseLifecycleState.LOST,
        CaseLifecycleState.STAGNATING,
    },
    CaseLifecycleState.OFFER_PREP: {
        CaseLifecycleState.WAITING_CLIENT,
        CaseLifecycleState.NEGOTIATION,
        CaseLifecycleState.LOST,
        CaseLifecycleState.STAGNATING,
    },
    CaseLifecycleState.WAITING_CLIENT: {
        CaseLifecycleState.NEGOTIATION,
        CaseLifecycleState.STAGNATING,
        CaseLifecycleState.LOST,
    },
    CaseLifecycleState.NEGOTIATION: {
        CaseLifecycleState.READY_INSTALL,
        CaseLifecycleState.STAGNATING,
        CaseLifecycleState.LOST,
    },
    CaseLifecycleState.READY_INSTALL: {
        CaseLifecycleState.IN_PROGRESS,
        CaseLifecycleState.STAGNATING,
    },
    CaseLifecycleState.IN_PROGRESS: {
        CaseLifecycleState.COMPLETED,
        CaseLifecycleState.STAGNATING,
    },
    CaseLifecycleState.COMPLETED: set(),       # terminal
    CaseLifecycleState.LOST: set(),            # terminal
    CaseLifecycleState.STAGNATING: {           # z stagnacji można wrócić
        CaseLifecycleState.QUALIFICATION,
        CaseLifecycleState.OFFER_PREP,
        CaseLifecycleState.WAITING_CLIENT,
        CaseLifecycleState.NEGOTIATION,
        CaseLifecycleState.LOST,
    },
}

# ── SLA timeout (godziny w danym stanie → propozycja STAGNATING) ────────
SLA_HOURS: dict[CaseLifecycleState, int] = {
    CaseLifecycleState.NEW_LEAD:       24,
    CaseLifecycleState.QUALIFICATION:  48,
    CaseLifecycleState.SITE_VISIT:     72,
    CaseLifecycleState.OFFER_PREP:     24,
    CaseLifecycleState.WAITING_CLIENT: 168,  # 7 dni
    CaseLifecycleState.NEGOTIATION:    72,
    CaseLifecycleState.READY_INSTALL:  48,
    CaseLifecycleState.IN_PROGRESS:    168,  # 7 dni
}

# ── Stany terminalne ────────────────────────────────────────────────────
TERMINAL_STATES = frozenset({
    CaseLifecycleState.COMPLETED,
    CaseLifecycleState.LOST,
})


def is_terminal(state: CaseLifecycleState | str) -> bool:
    """Sprawdza czy stan jest terminalny."""
    if isinstance(state, str):
        try:
            state = CaseLifecycleState(state)
        except ValueError:
            return False
    return state in TERMINAL_STATES


def is_valid_transition(
    current: CaseLifecycleState | str,
    target: CaseLifecycleState | str,
) -> bool:
    """Sprawdza czy przejście current → target jest dozwolone."""
    if isinstance(current, str):
        try:
            current = CaseLifecycleState(current)
        except ValueError:
            return False
    if isinstance(target, str):
        try:
            target = CaseLifecycleState(target)
        except ValueError:
            return False
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    return target in allowed


def validate_transition(
    current: CaseLifecycleState | str,
    target: CaseLifecycleState | str,
) -> dict[str, Any]:
    """Waliduje przejście i zwraca szczegółowy wynik.

    Returns:
        dict z polami:
          - allowed: bool
          - current: str
          - target: str
          - allowed_targets: list[str] (jeśli niedozwolone)
          - reason: str (jeśli niedozwolone)
    """
    if isinstance(current, str):
        try:
            current = CaseLifecycleState(current)
        except ValueError:
            return {"allowed": False, "current": str(current), "target": str(target), "reason": f"Nieznany stan bieżący: {current}"}
    if isinstance(target, str):
        try:
            target = CaseLifecycleState(target)
        except ValueError:
            return {"allowed": False, "current": current.value, "target": str(target), "reason": f"Nieznany stan docelowy: {target}"}

    allowed_set = ALLOWED_TRANSITIONS.get(current, set())
    if target in allowed_set:
        return {"allowed": True, "current": current.value, "target": target.value, "reason": ""}

    return {
        "allowed": False,
        "current": current.value,
        "target": target.value,
        "allowed_targets": sorted(s.value for s in allowed_set),
        "reason": f"Niedozwolone przejście: {current.value} → {target.value}. "
                  f"Dozwolone: {', '.join(sorted(s.value for s in allowed_set)) or 'brak (stan terminalny)'}.",
    }


def list_allowed_transitions(state: CaseLifecycleState | str) -> list[str]:
    """Zwraca listę dozwolonych stanów docelowych dla danego stanu."""
    if isinstance(state, str):
        try:
            state = CaseLifecycleState(state)
        except ValueError:
            return []
    return sorted(s.value for s in ALLOWED_TRANSITIONS.get(state, set()))


# ── Mapowanie operational_status ↔ lifecycle ────────────────────────────
# EngagementSnapshotV2.operational_status.code → CaseLifecycleState
OPERATIONAL_TO_LIFECYCLE: dict[str, CaseLifecycleState] = {
    "raw_inquiry":       CaseLifecycleState.NEW_LEAD,
    "enriching":         CaseLifecycleState.QUALIFICATION,
    "ready_for_quote":   CaseLifecycleState.OFFER_PREP,
    "pending_operator":  CaseLifecycleState.QUALIFICATION,  # zależy od kontekstu
    "node_a_error":      CaseLifecycleState.QUALIFICATION,
}

# MailboxMemory case_status → CaseLifecycleState (przybliżone)
CASE_STATUS_TO_LIFECYCLE: dict[str, CaseLifecycleState] = {
    "open":     CaseLifecycleState.QUALIFICATION,
    "merged":   CaseLifecycleState.QUALIFICATION,
    "archived": CaseLifecycleState.COMPLETED,
    "resolved": CaseLifecycleState.COMPLETED,
    "closed":   CaseLifecycleState.COMPLETED,
}


def map_operational_to_lifecycle(
    operational_code: str,
    *,
    case_status: str = "",
    default: CaseLifecycleState = CaseLifecycleState.NEW_LEAD,
) -> CaseLifecycleState:
    """Mapuje operational_status.code na CaseLifecycleState.

    Args:
        operational_code: kod z EngagementSnapshotV2.operational_status.code
        case_status: opcjonalny status z MailboxMemory (fallback)
        default: domyślny stan gdy brak mapowania

    Returns:
        CaseLifecycleState
    """
    code = str(operational_code or "").strip().lower()
    result = OPERATIONAL_TO_LIFECYCLE.get(code)
    if result is not None:
        return result

    # Fallback: case_status
    status = str(case_status or "").strip().lower()
    result = CASE_STATUS_TO_LIFECYCLE.get(status)
    if result is not None:
        return result

    return default


def map_lifecycle_to_operational(state: CaseLifecycleState | str) -> str:
    """Odwrotne mapowanie — lifecycle → operational_status.code."""
    if isinstance(state, str):
        try:
            state = CaseLifecycleState(state)
        except ValueError:
            return "raw_inquiry"
    # Odwrócona mapa
    reverse: dict[CaseLifecycleState, str] = {
        CaseLifecycleState.NEW_LEAD:       "raw_inquiry",
        CaseLifecycleState.QUALIFICATION:  "enriching",
        CaseLifecycleState.OFFER_PREP:     "ready_for_quote",
        CaseLifecycleState.WAITING_CLIENT: "pending_operator",
    }
    return reverse.get(state, "enriching")


__all__ = [
    "CaseLifecycleState",
    "ALLOWED_TRANSITIONS",
    "SLA_HOURS",
    "TERMINAL_STATES",
    "OPERATIONAL_TO_LIFECYCLE",
    "is_terminal",
    "is_valid_transition",
    "validate_transition",
    "list_allowed_transitions",
    "map_operational_to_lifecycle",
    "map_lifecycle_to_operational",
]
