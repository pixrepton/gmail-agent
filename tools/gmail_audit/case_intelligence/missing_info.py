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
_INVALID_FACT_STATUSES = {"superseded", "rejected", "stale", "invalidated", "disputed"}

_FACT_GAP_MARKERS: dict[str, tuple[str, ...]] = {
    "heated_area_m2": ("metraz", "metraż", "powierzchnia", "area"),
    "city": ("miasto", "lokalizacja", "localization", "location"),
    "raw_geographic_signal": ("miasto", "lokalizacja", "localization", "location"),
    "scope": ("zakres", "scope"),
    "dhw_required": ("cwu", "ciepla woda", "ciepła woda", "zasobnik"),
    "current_heating_source": ("obecne zrodlo", "obecne źródło"),
}

_TRUSTED_LINK_GAP_MARKERS = (
    "do ktorej konkretnej wyceny",
    "do której konkretnej wyceny",
    "ktorej oferty",
    "której oferty",
    "ktorego przypadku",
    "którego przypadku",
    "brak powiazania",
    "brak powiązania",
    "powiazanie z istniejącym",
    "powiązanie z istniejącym",
    "odniesienie do wczesniejszej wyceny",
    "odniesienie do wcześniejszej wyceny",
    "pelny watek",
    "pełny wątek",
    "historia sprawy",
    "confirmed case reference",
)


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


def _active_fact_values(case_context_pack: dict[str, Any] | None) -> dict[str, Any]:
    pack = case_context_pack if isinstance(case_context_pack, dict) else {}
    conflicts = pack.get("conflicting_facts") if isinstance(pack.get("conflicting_facts"), list) else []
    conflicted_keys = {
        str(item.get("fact_key") or item.get("key") or "").strip()
        for item in conflicts
        if isinstance(item, dict)
    }
    grouped: dict[str, list[Any]] = {}
    for fact in pack.get("active_facts") or []:
        if not isinstance(fact, dict):
            continue
        status = str(fact.get("status") or "active").strip().lower()
        if status in _INVALID_FACT_STATUSES:
            continue
        key = str(fact.get("fact_key") or fact.get("key") or "").strip()
        if not key or key in conflicted_keys:
            continue
        value = fact.get("value")
        if value is None:
            value = fact.get("normalized_value")
        if value in (None, "", [], {}):
            continue
        grouped.setdefault(key, []).append(value)
    out: dict[str, Any] = {}
    for key, values in grouped.items():
        distinct = {str(v) for v in values}
        if len(distinct) == 1:
            out[key] = values[-1]
    return out


def _is_redundant_known_fact_gap(item: str, *, known_facts: dict[str, Any], trusted_case_link: bool) -> bool:
    low = str(item or "").lower()
    if trusted_case_link and any(marker in low for marker in _TRUSTED_LINK_GAP_MARKERS):
        return True
    for fact_key, markers in _FACT_GAP_MARKERS.items():
        if fact_key in known_facts and any(marker in low for marker in markers):
            return True
    if trusted_case_link and known_facts.get("offer_sent") is True:
        if (
            "numer oferty" in low
            or "identyfikator oferty" in low
            or "identyfikator lub numer" in low
            or "wczesniejszej oferty" in low
            or "wcześniejszej oferty" in low
            or "oryginalnej oferty" in low
            or "offer id" in low
        ):
            return True
    return False


def build_missing_info(
    *,
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    reply_result: dict[str, Any],
    case_link_result: dict[str, Any],
    attachment_intelligence: dict[str, Any] | None = None,
    thread_memory: dict[str, Any] | None = None,
    case_context_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    known_facts = _active_fact_values(case_context_pack)
    trusted_case_link = str(case_link_result.get("decision") or "") == "linked"
    raw_items = []
    for item in business_result.get("missing_information") or []:
        text = str(item).strip()
        if not text or not _is_collectable_gap(text):
            continue
        if _is_redundant_known_fact_gap(text, known_facts=known_facts, trusted_case_link=trusted_case_link):
            continue
        raw_items.append(text)
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
