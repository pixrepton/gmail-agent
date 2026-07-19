"""Deterministic projection refresh decisions for unified signal runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class ProjectionRefreshDecision:
    should_refresh: bool
    refresh_kind: str
    reason: str
    trace_note_pl: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_projection_refresh(
    signal_kind: str,
    *,
    source_kind: str,
    case_id: str = "",
    has_case_state: bool = False,
) -> ProjectionRefreshDecision:
    if not case_id and signal_kind not in {"drive_document_removed", "drive_conflict_detected"}:
        return ProjectionRefreshDecision(
            should_refresh=False,
            refresh_kind="no_projection_change",
            reason="no_case_link",
            trace_note_pl="Brak stabilnego case linku - bez odswiezenia projekcji.",
        )

    if source_kind == "gmail":
        if signal_kind == "gmail_attachment_observed":
            return ProjectionRefreshDecision(
                should_refresh=True,
                refresh_kind="decision_trace_only",
                reason="gmail_attachment_audit",
                trace_note_pl="Zaktualizowano slad dla zalacznika Gmail.",
            )
        if signal_kind == "gmail_thread_update_observed":
            return ProjectionRefreshDecision(
                should_refresh=True,
                refresh_kind="case_only",
                reason="gmail_thread_refresh",
                trace_note_pl="Odswiezono stan sprawy po zmianie watku Gmail.",
            )
        return ProjectionRefreshDecision(
            should_refresh=True,
            refresh_kind="case_and_note",
            reason="gmail_message_refresh",
            trace_note_pl="Odswiezono sprawe i notatke po wiadomosci Gmail.",
        )

    if signal_kind == "drive_conflict_detected":
        return ProjectionRefreshDecision(
            should_refresh=True,
            refresh_kind="case_and_note",
            reason="drive_conflict_visible",
            trace_note_pl="Konflikt dokumentow Drive wymaga widocznego sladu operatora.",
        )
    if signal_kind in {"drive_document_added", "drive_document_updated", "drive_extraction_completed", "drive_media_batch_observed"}:
        return ProjectionRefreshDecision(
            should_refresh=True,
            refresh_kind="case_only" if has_case_state else "decision_trace_only",
            reason="drive_document_refresh",
            trace_note_pl="Odswiezono projekcje po zmianie dokumentu Drive.",
        )
    if signal_kind == "drive_document_link_candidate":
        return ProjectionRefreshDecision(
            should_refresh=True,
            refresh_kind="decision_trace_only",
            reason="drive_link_candidate_trace",
            trace_note_pl="Dodano slad kandydata linkowania Drive.",
        )
    if signal_kind == "drive_document_removed":
        return ProjectionRefreshDecision(
            should_refresh=True,
            refresh_kind="case_only",
            reason="drive_document_removed",
            trace_note_pl="Odswiezono stan sprawy po usunieciu dokumentu Drive.",
        )
    return ProjectionRefreshDecision(
        should_refresh=False,
        refresh_kind="no_projection_change",
        reason="default_no_change",
        trace_note_pl="Brak zmian projekcji.",
    )


__all__ = [
    "ProjectionRefreshDecision",
    "decide_projection_refresh",
]
