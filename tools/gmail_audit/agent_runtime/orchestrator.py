
"""TUM orchestrator — fast_link vs deep_understand vs defer (RFC E2)."""

from __future__ import annotations

from log_config import get_logger
import os
from dataclasses import dataclass
from typing import Any, Callable, Literal

from signal_contract import CanonicalSignal
from _protocols import CaseSnapshotStore

logger = get_logger(__name__)

OrchestratorRoute = Literal["fast_link", "deep_understand", "defer", "legacy_fallback"]

FAST_LINK_CONFIDENCE = float(os.getenv("ORCHESTRATOR_FAST_LINK_CONFIDENCE", "0.92"))


@dataclass(frozen=True)
class OrchestratorDecision:
    route: OrchestratorRoute
    reason: str
    link_confidence: float = 0.0


@dataclass(frozen=True)
class MergeSuggestion:
    suggestion_type: str
    candidate_case_id: str
    customer_identifier: str
    confidence: float
    reason_pl: str
    review_required: bool


def route_signal(
    signal: CanonicalSignal | dict[str, Any],
    *,
    entity_link: dict[str, Any] | None = None,
    triage: dict[str, Any] | None = None,
    case_id: str = "",
    link_confidence: float = 0.0,
    linkage_status: str = "",
) -> OrchestratorDecision:
    """Deterministic router before agent vs legacy reconcile."""
    entity_link = dict(entity_link or {})
    triage = dict(triage or {})
    cid = str(case_id or entity_link.get("case_id") or "").strip()
    conf = float(link_confidence or entity_link.get("link_confidence") or 0.0)
    status = str(linkage_status or entity_link.get("linkage_status") or "").strip().lower()

    if str(triage.get("routing_decision") or "").strip().lower() == "defer":
        return OrchestratorDecision(route="defer", reason="triage_defer")

    if cid:
        if conf >= FAST_LINK_CONFIDENCE or status in {"deterministic", "verified"}:
            return OrchestratorDecision(route="fast_link", reason="linked_high_confidence", link_confidence=conf)
        return OrchestratorDecision(route="fast_link", reason="existing_case_id", link_confidence=conf)

    if conf >= FAST_LINK_CONFIDENCE and status in {"deterministic", "verified"}:
        return OrchestratorDecision(route="fast_link", reason="deterministic_link", link_confidence=conf)

    source_kind = str(getattr(signal, "source_kind", None) or signal.get("source_kind") if isinstance(signal, dict) else "")
    if source_kind == "drive":
        return OrchestratorDecision(route="deep_understand", reason="drive_orphan_staging")

    if source_kind in {"gmail", "calendar"} and not cid:
        return OrchestratorDecision(route="deep_understand", reason=f"{source_kind}_orphan_staging")

    if str(entity_link.get("decision") or "").strip().upper() == "VERIFIED":
        return OrchestratorDecision(route="fast_link", reason="entity_verified", link_confidence=conf)

    return OrchestratorDecision(route="deep_understand", reason="uncertain_or_orphan")


def suggest_merge_if_duplicate(
    signal: CanonicalSignal | dict[str, Any],
    *,
    customer_identifier: str = "",
    lookup_active_cases: Callable[[str], list[dict[str, Any]]] | None = None,
    store: CaseSnapshotStore | None = None,
) -> MergeSuggestion | None:
    """PR-Merge (Merge.3): Auto-merge trigger.

    Jeśli sygnał dotyczy klienta, który ma już aktywną sprawę,
    zwraca sugestię merge z HITL (review_required=True).

    Args:
        signal: CanonicalSignal lub dict z signal.payload lub signal.artifacts
        customer_identifier: email lub inny identyfikator klienta
        lookup_active_cases: callback zwracający listę aktywnych spraw dla klienta
        store: alternatywnie — CorrelationRegistryStore lub MailboxMemoryStore

    Returns:
        dict z sugestią merge lub None:
        {
            "suggestion_type": "merge",
            "candidate_case_id": "...",
            "customer_identifier": "...",
            "confidence": 0.85,
            "reason_pl": "Klient ma już aktywną sprawę: ...",
            "review_required": True,
        }
    """
    if not customer_identifier:
        # Spróbuj wyciągnąć z sygnału
        payload = signal.payload if hasattr(signal, "payload") else (signal.get("payload") if isinstance(signal, dict) else {})
        artifacts = signal.artifacts if hasattr(signal, "artifacts") else (signal.get("artifacts") if isinstance(signal, dict) else {})
        entity_link = (artifacts or {}).get("entity_link") or (payload or {}).get("_entity_link") or {}
        customer_identifier = str(
            entity_link.get("primary_email")
            or entity_link.get("customer_email")
            or payload.get("customer_email")
            or ""
        ).strip()

    if not customer_identifier:
        return None

    existing_cases: list[dict[str, Any]] = []
    if lookup_active_cases is not None:
        existing_cases = lookup_active_cases(customer_identifier)
    elif store is not None:
        # Próba użycia CorrelationRegistryStore
        lookup = getattr(store, "find_cases_by_customer", None)
        if callable(lookup):
            existing_cases = lookup(customer_identifier) or []

    # Filtruj tylko aktywne sprawy
    active = [
        c for c in existing_cases
        if str(c.get("status") or "").strip().lower() not in ("merged", "archived", "resolved", "closed")
    ]

    if not active:
        return None

    # Weź pierwszą aktywną sprawę jako kandydata
    candidate = active[0]
    candidate_id = str(candidate.get("case_id") or candidate.get("engagement_id") or "").strip()
    if not candidate_id:
        return None

    logger.info(
        "auto_merge_suggestion customer=%s existing_case=%s source_kind=%s",
        customer_identifier,
        candidate_id,
        getattr(signal, "source_kind", None) or (isinstance(signal, dict) and signal.get("source_kind")),
    )

    return MergeSuggestion(
        suggestion_type="merge",
        candidate_case_id=candidate_id,
        customer_identifier=customer_identifier,
        confidence=0.85,
        reason_pl=f"Klient ma już aktywną sprawę ({candidate_id}). "
        f"Proponujemy scalenie nowego sygnału z istniejącą sprawą.",
        review_required=True,
    )


__all__ = ["OrchestratorDecision", "MergeSuggestion", "OrchestratorRoute", "route_signal", "suggest_merge_if_duplicate", "FAST_LINK_CONFIDENCE"]
