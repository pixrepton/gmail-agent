"""Structured output for HVAC signal extraction from inbound mail."""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel


class SignalExtractionResult(BaseModel):
    hvac_intent: str = ""
    hvac_intent_raw_evidence: str = ""
    building_type: str = ""
    heated_area_m2: float | None = None
    construction_year: int | None = None
    current_heating_source: str | None = None
    budget_pln_estimated: float | None = None
    price_sensitivity: str | None = None
    raw_geographic_signal: str | None = None
    model_config = {"extra": "ignore"}


# STRUCTURED-INPUT-AND-CAPABILITY-BASELINE-CLOSEOUT-01 — hvac_intent canonical vocabulary.
# `hvac_intent` used to be completely free text (the LLM would return a descriptive
# sentence instead of a class, e.g. "inquiry about low-temperature efficiency of Panasonic
# Aquarea T-CAP"). This is a deterministic, evidence-preserving normalizer: it never calls
# the LLM again, never invents a class beyond this fixed vocabulary, and always keeps the
# original raw text (hvac_intent_raw_evidence) so nothing is silently discarded.
#
# Values are Polish, underscore-joined, and four of the six deliberately reuse the exact
# case_kind bucket names already consumed by agent_runtime.tools.handlers._HVAC_INTENT_TO_KIND
# (wycena_oferta, awaria_naprawa, przeglad_konserwacja) so canonicalized output plugs
# straight into that existing classifier with no ambiguity, and so ground-truth aliases
# (which are also Polish, e.g. "techniczne/pytanie") token-match exactly rather than relying
# on incidental stem overlap between languages.
HVAC_INTENT_CANONICAL_VALUES = frozenset(
    {
        "wycena_oferta",
        "pytanie_techniczne",
        "negocjacja_ceny",
        "odroczenie_decyzji",
        "awaria_naprawa",
        "przeglad_konserwacja",
        "nieznane",
    }
)

# Ordered (multi-word phrases before single tokens) so e.g. a technical question that
# happens to mention "cena" isn't misclassified as a price negotiation. PL/EN, casing and
# diacritics are normalized before matching. awaria_naprawa/przeglad_konserwacja are checked
# BEFORE negocjacja_ceny: a message reporting a real malfunction while also asking for a
# repair discount ("nie dziala, prosze o rabat na naprawe") must classify as a service/
# repair case, not a sales/price-negotiation one (adversarial-review finding).
_HVAC_INTENT_PHRASE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "odroczenie_decyzji",
        (
            "wroce za", "wrocimy za", "przemysle", "przemyslimy", "musze to przemyslec",
            "za jakis czas", "za miesiac", "pozniej sie odezwe", "odezwe sie",
            "damy znac pozniej", "think it over", "get back to you", "later this",
            "will be in touch", "decide later",
        ),
    ),
    (
        "awaria_naprawa",
        (
            "awaria", "nie dziala", "nie grzeje", "usterk", "reklamacj", "przeciek",
            "halasuje", "psuje sie", "wyciek",
            "not working", "broken", "malfunction", "leak", "noisy", "repair",
        ),
    ),
    (
        "przeglad_konserwacja",
        (
            "przeglad", "konserwacj", "coroczny serwis", "przeglad okresowy",
            "maintenance", "inspection", "annual service", "servicing",
        ),
    ),
    (
        "negocjacja_ceny",
        (
            "czy jest mozliwy rabat", "czy moze byc tanie", "negocjacj", "rabat",
            "znizk", "obnizyc cene", "nizsza cena", "za drogo", "cena jest za wysoka",
            "discount", "negotiat", "lower the price", "too expensive", "better price",
        ),
    ),
    (
        "pytanie_techniczne",
        (
            "czy pompa", "czy urzadzenie", "jak dziala", "jaka jest wydajnosc",
            "jaka jest sprawnosc", "przy temperaturach", "w niskich temperaturach",
            "przy niskich temperaturach", "w temperaturach", "kompatybiln", "czy nie bedzie",
            "zastanawiam sie czy", "pytanie techniczne", "efektywnosc", "efektywnosci",
            "how does it work", "efficiency", "compatib", "technical question",
            "does it work at", "how efficient",
        ),
    ),
    (
        "wycena_oferta",
        (
            "wycena", "oferta", "ofert", "zapytanie o cene", "prosze o wycene",
            "ile kosztuje", "chcialbym kupic", "wymiana na pompe", "interesuje mnie",
            "quote", "quotation", "price inquiry", "how much does it cost",
            "request for quotation", "rfq", "interested in buying", "replace with heat pump",
        ),
    ),
)


_POLISH_STROKE_L = str.maketrans({"ł": "l", "Ł": "L"})


def _normalize_hvac_text(value: str) -> str:
    # Polish "ł" is a distinct base letter, not an NFKD-decomposable combining mark (unlike
    # "ą"/"ę"/"ó" etc.) -- without this explicit mapping, "ciepła" would never match the
    # plain-ASCII "ciepla" used in the phrase-rule keywords below.
    text = str(value or "").translate(_POLISH_STROKE_L)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    return re.sub(r"\s+", " ", text).strip()


def _phrase_present(phrase: str, normalized_text: str) -> bool:
    """Plain substring match for multi-word phrases (already specific/safe by length).
    Single-token phrases are often deliberate word STEMS (e.g. "usterk" for
    usterka/usterki/usterke -- an inflection prefix, not a complete word), so a full
    `\\bphrase\\b` match would break intended stem-matching. A LEADING boundary alone is
    enough to fix the concrete collision found by adversarial review ("fault" silently
    matching inside "default") while still letting "usterk" match "usterka" etc."""
    if " " in phrase:
        return phrase in normalized_text
    return re.search(r"\b" + re.escape(phrase), normalized_text) is not None


def canonicalize_hvac_intent(raw: str) -> tuple[str, str]:
    """Map free-text hvac_intent to the fixed canonical vocabulary.

    Returns (canonical_class, raw_evidence). Never fabricates a class beyond
    HVAC_INTENT_CANONICAL_VALUES; unmatched or empty text canonicalizes to "nieznane"
    while the original text is preserved as evidence, not discarded.
    """
    raw_text = str(raw or "").strip()
    if raw_text in HVAC_INTENT_CANONICAL_VALUES:
        return raw_text, raw_text
    normalized = _normalize_hvac_text(raw_text)
    if not normalized:
        return "nieznane", raw_text
    for canonical, phrases in _HVAC_INTENT_PHRASE_RULES:
        if any(_phrase_present(phrase, normalized) for phrase in phrases):
            return canonical, raw_text
    return "nieznane", raw_text
