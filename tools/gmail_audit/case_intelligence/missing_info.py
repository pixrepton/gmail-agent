"""Missing information extraction for case intelligence."""
from __future__ import annotations
from typing import Any

from .constants import MISSING_INFO_CRITICAL_KEYWORDS, MISSING_INFO_IMPORTANT_KEYWORDS
from .validators import _missing_info_label_pl


# RC-IQ-R6: a missing-information item must be a concrete datum the customer/operator can
# supply. Two classes leaking from the free-text business reasoner are NOT data gaps and
# must not sit on the operator's gap checklist (they remain visible as open loops/risks):
#   - an awaited customer decision/response (the customer will decide; nothing to collect);
#   - a speculative/conditional maybe ("jeśli dotyczy", "if applicable").
# Markers are deliberately narrow to avoid dropping legitimate gaps (e.g. "jeśli istnieje",
# "ewentualnej weryfikacji" are NOT matched).
_AWAITED_DECISION_MARKERS = ("decyzja klient", "decyzją klient", "decyzje klient", "customer decision")
_SPECULATIVE_MARKERS = ("jesli dotyczy", "jeśli dotyczy", "if applicable")
# Explicitly-optional items ("(opcjonalnie)") are by definition not a gap the operator
# must collect; internal case-linking identifiers ("case id") are system state, not a
# datum the customer can supply. Both are dropped from the operator gap surface.
_OPTIONAL_MARKERS = ("(opcjonalnie)", "opcjonalnie)", "(optional)")
_INTERNAL_LINK_MARKERS = ("case id", "case_id")


def _is_collectable_gap(item: str) -> bool:
    low = str(item or "").lower()
    if any(marker in low for marker in _SPECULATIVE_MARKERS):
        return False
    if any(marker in low for marker in _AWAITED_DECISION_MARKERS):
        return False
    if any(marker in low for marker in _OPTIONAL_MARKERS):
        return False
    if any(marker in low for marker in _INTERNAL_LINK_MARKERS):
        return False
    return True


def build_missing_info(
    *,
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    reply_result: dict[str, Any],
    case_link_result: dict[str, Any],
    attachment_intelligence: dict[str, Any] | None = None,
    thread_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_items = [
        str(item).strip()
        for item in (business_result.get("missing_information") or [])
        if str(item).strip() and _is_collectable_gap(str(item))
    ]
    if str(case_link_result.get("decision") or "") in {"weak_link", "competing_links"}:
        raw_items.append("confirmed case reference")

    critical: list[str] = []
    important: list[str] = []
    helpful: list[str] = []
    for item in raw_items:
        localized_item = _missing_info_label_pl(item)
        lowered = item.lower()
        if any(keyword in lowered for keyword in MISSING_INFO_CRITICAL_KEYWORDS):
            critical.append(localized_item)
        elif any(keyword in lowered for keyword in MISSING_INFO_IMPORTANT_KEYWORDS):
            important.append(localized_item)
        else:
            helpful.append(localized_item)

    summary_parts = []
    if critical:
        summary_parts.append("Brakuje krytycznych danych: " + ", ".join(critical) + ".")
    if important:
        summary_parts.append("Warto uzupelnic: " + ", ".join(important) + ".")
    if helpful:
        summary_parts.append("Dodatkowo pomocne: " + ", ".join(helpful) + ".")
    summary_pl = " ".join(summary_parts).strip() or "Brak istotnych brakow informacji."

    customer_question_draft_pl = ""
    if bool(reply_result.get("draft_enabled")) and (reply_result.get("drafts") or []):
        customer_question_draft_pl = str((reply_result.get("drafts") or [{}])[0].get("body") or "").strip()
    elif critical or important:
        requested = critical[:2] + important[:2]
        customer_question_draft_pl = "Dzien dobry, zeby ruszyc dalej, prosimy o: " + ", ".join(requested) + "."

    operator_checklist_pl = []
    for item in critical:
        operator_checklist_pl.append(f"Ustal krytyczne dane: {item}.")
    for item in important:
        operator_checklist_pl.append(f"Sprawdz wazny brak: {item}.")
    for item in helpful:
        operator_checklist_pl.append(f"Jesli sie da, doprecyzuj: {item}.")

    _ = attachment_intelligence
    _ = thread_memory

    return {
        "summary_pl": summary_pl,
        "critical": critical,
        "important": important,
        "helpful": helpful,
        "customer_question_draft_pl": customer_question_draft_pl,
        "operator_checklist_pl": operator_checklist_pl,
    }
