"""LLM-based extraction of business terms from documents.

Uses existing Docling + Drive Graph pipeline output.
Feeds document text to LLM with structured extraction prompt.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from business_dictionary.model import BusinessTerm
from log_config import get_logger

logger = get_logger(__name__)

EXTRACTION_SYSTEM_PROMPT = """Jestes asystentem biznesowym TOP-INSTAL.
Twoim zadaniem jest ekstrakcja terminow biznesowych z dokumentow firmowych.

Wyciagnij:
1. PRODUKTY — nazwy produktow, modeli, serii (np. "Panasonic Aquarea T-CAP", "pompa ciepla")
2. USLUGI — nazwy uslug (np. "montaz pompy ciepla", "przeglad")
3. CENNIKI — stawki, ceny, koszty (np. "koszt montazu: 5000 PLN")
4. TERMINY BRANZOWE — fachowe pojecia HVAC (np. "COP", "SCOP", "kWh", "bufor")
5. REGULY BIZNESOWE — procedury, zasady (np. "klient VIP = obsluga priorytetowa")
6. SZABLONY — szablony odpowiedzi, fragmenty korespondencji
7. KONTAKTY — nazwiska, firmy, role (np. "Pan Kowalski - klient VIP")

Zwroc JSON array: [{"name": "...", "category": "product|service|pricing|term|rule|template|contact", "definition": "...", "aliases": [...], "confidence": 0.95}]
Jesli nie ma zadnych terminow, zwroc []."""


def extract_terms_from_text(
    text: str,
    *,
    source_document: str = "",
    source_kind: str = "manual",
    llm_call: Any = None,
) -> list[BusinessTerm]:
    """Extract business terms from a document text using LLM."""
    if not text or not text.strip():
        return []

    if llm_call is None:
        _extraction_prompt = EXTRACTION_SYSTEM_PROMPT + f"\n\nDokument:\n{text[:8000]}"
        logger.warning("No llm_call provided — returning empty (dry-run mode)")
        return []

    try:
        result = llm_call(
            messages=[
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": f"Dokument:\n{text[:8000]}"},
            ],
            response_format={"type": "json_object"},
        )

        raw = result.get("content") or result.get("response") or str(result)
        if isinstance(raw, str):
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.strip("`").strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()
            terms_data = json.loads(raw)
        elif isinstance(raw, dict):
            terms_data = raw.get("terms") or raw.get("items") or [raw]
        else:
            terms_data = []

        if isinstance(terms_data, dict):
            terms_data = [terms_data]

        now = datetime.now(timezone.utc).isoformat()
        terms = []
        for item in terms_data[:50]:
            name = str(item.get("name") or "").strip()
            category = str(item.get("category") or "term").strip().lower()
            if not name:
                continue
            aliases = item.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]

            terms.append(BusinessTerm(
                term_id=f"bizterm_{uuid.uuid4().hex[:16]}",
                name=name,
                category=category,
                definition=str(item.get("definition") or "").strip(),
                source_document=source_document,
                source_kind=source_kind,
                aliases=aliases if isinstance(aliases, list) else [aliases],
                related_terms=item.get("related_terms") or [],
                confidence=float(item.get("confidence") or 0.7),
                created_at=now,
                updated_at=now,
            ))
        return terms

    except Exception as exc:
        logger.error("Business dictionary extraction failed: %s", exc)
        return []


def categorize_term(name: str, context: str = "") -> str:
    """Simple keyword-based fallback categorization when LLM is not available."""
    name_lower = name.lower()
    context_lower = context.lower()

    pricing_keywords = ["pln", "zł", "cena", "koszt", "stawka", "rabat", "promocja", "wycena"]
    product_keywords = ["pompa", "panasonic", "aquarea", "hvac", "kocioł", "piec", "grzejnik", "bufor",
                        "zasobnik", "sterownik", "falownik", "moduł", "kolektor"]
    service_keywords = ["montaż", "instalacja", "serwis", "przegląd", "naprawa",
                        "konserwacja", "projekt", "audyt"]
    term_keywords = ["cop", "scop", "kwh", "kw", "btu", "seer", "spf", "wydajność"]

    for kw in pricing_keywords:
        if kw in name_lower or kw in context_lower:
            return "pricing"
    for kw in product_keywords:
        if kw in name_lower or kw in context_lower:
            return "product"
    for kw in service_keywords:
        if kw in name_lower or kw in context_lower:
            return "service"
    for kw in term_keywords:
        if kw in name_lower:
            return "term"

    if any(kw in name_lower for kw in ["@", "tel", "telefon", "email", "prezes", "dyrektor", "kierownik"]):
        return "contact"
    if any(kw in name_lower for kw in ["zasada", "reguła", "procedura", "playbook", "sop"]):
        return "rule"

    return "product"
