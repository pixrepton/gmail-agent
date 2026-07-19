"""Fala C1: personalized outbound offer email via ContextAssembler + central LLM."""

from __future__ import annotations
from log_config import get_logger

import html
import json
import re
from typing import Any

from central_llm_stage import resolve_case_id, run_central_structured_stage
from config import Settings
from groq_client import GroqClientError
from llm_client import extract_json_candidate
from llm_contracts.email_result import EmailPersonalizationResult

logger = get_logger(__name__)

EMAIL_PERSONALIZATION_INSTRUCTIONS = """
Napisz spersonalizowany mail do klienta z ofertą pompy ciepła TOP-INSTAL / Panasonic Aquarea.

Zasady (obowiązkowe):
- Używaj WYŁĄCZNIE kwot i parametrów technicznych z payloadu offer_summary.
- NIE wymyślaj cen, rabatów, mocy, modelu ani terminów montażu spoza danych.
- Ton: profesjonalny, konkretny, po polsku; bez żargonu wewnętrznego (trace, workflow, reason codes).
- W treści wspomnij o załączonym PDF z pełną ofertą.
- subject: krótki, po polsku, bez prefiksu [NOWY LEAD].
- body: plain text, akapity oddzielone pustą linią; bez HTML.
""".strip()

_EMAIL_PERSONALIZATION_SCHEMA = EmailPersonalizationResult.model_json_schema()
_PRICE_CONTEXT_RE = re.compile(r"\b(\d[\d\s]{1,12})\s*(?:PLN|zł|zl)\b", re.I)


def summarize_offer_for_llm(offer: dict[str, Any]) -> dict[str, Any]:
    """Bounded offer facts for LLM — no full OfferDTO dump."""
    eng = offer.get("engineering") if isinstance(offer.get("engineering"), dict) else {}
    sel = eng.get("selection") if isinstance(eng.get("selection"), dict) else {}
    buf = eng.get("buffer") if isinstance(eng.get("buffer"), dict) else {}
    pricing = offer.get("pricing") if isinstance(offer.get("pricing"), dict) else {}
    totals = pricing.get("totals") if isinstance(pricing.get("totals"), dict) else {}
    return {
        "pump_model": sel.get("pumpModel"),
        "pump_label": sel.get("pumpLabel") or sel.get("displayName") or sel.get("modelName"),
        "capacity_kw": sel.get("capacity_kW") or sel.get("capacityKw"),
        "buffer_liters": buf.get("liters") or buf.get("capacity_liters"),
        "gross_pln": totals.get("gross"),
        "net_pln": totals.get("net"),
        "vat_pln": totals.get("vat"),
    }


def allowed_price_tokens(offer_summary: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in ("gross_pln", "net_pln", "vat_pln"):
        raw = offer_summary.get(key)
        if raw is None or raw == "":
            continue
        try:
            num = float(str(raw).replace(" ", "").replace(",", "."))
            tokens.add(str(int(num)) if num == int(num) else f"{num:.0f}")
            tokens.add(f"{num:,.0f}".replace(",", " "))
            tokens.add(f"{int(num):,}".replace(",", " "))
        except (TypeError, ValueError):
            tokens.add(str(raw).strip())
    return {t for t in tokens if t}


def verify_no_hallucinated_prices(body: str, allowed: set[str]) -> bool:
    if not allowed:
        return True
    allowed_ints: set[int] = set()
    for token in allowed:
        digits = re.sub(r"\D", "", token)
        if len(digits) >= 4:
            try:
                allowed_ints.add(int(digits))
            except ValueError:
                continue
    for match in _PRICE_CONTEXT_RE.findall(body or ""):
        digits = re.sub(r"\D", "", match)
        if len(digits) < 4:
            continue
        try:
            val = int(digits)
        except ValueError:
            continue
        if val not in allowed_ints:
            return False
    return True


def plain_text_to_html(body: str) -> str:
    paras = [p.strip() for p in (body or "").split("\n\n") if p.strip()]
    if not paras:
        return ""
    return "".join(f"<p>{html.escape(p)}</p>" for p in paras)


def run_email_personalization(
    *,
    settings: Settings,
    offer: dict[str, Any],
    cieplo_url: str,
    contact_email: str,
    client_email: str = "",
    case_id: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Return EmailPersonalizationResult dict + execution_metadata."""
    offer_summary = summarize_offer_for_llm(offer)
    prompt_input = {
        "cieplo_url": cieplo_url,
        "contact_email": contact_email,
        "client_email": client_email,
        "offer_summary": offer_summary,
    }
    query_text = (
        f"Oferta pompy ciepła {offer_summary.get('pump_label') or offer_summary.get('pump_model') or ''} "
        f"{offer_summary.get('capacity_kw') or ''} kW"
    ).strip()
    context_bundle: dict[str, Any] = {}
    if case_id:
        context_bundle["case_id"] = case_id
    try:
        stage_call = run_central_structured_stage(
            settings,
            stage_name="email_personalization",
            task_instructions=EMAIL_PERSONALIZATION_INSTRUCTIONS,
            prompt_input=prompt_input,
            query_text=query_text,
            json_schema=_EMAIL_PERSONALIZATION_SCHEMA,
            schema_name="email_personalization_v1",
            case_id=resolve_case_id(context_bundle=context_bundle) or case_id,
            verbose=verbose,
            output_model=EmailPersonalizationResult,
            context_bundle=context_bundle or None,
        )
        if stage_call is None:
            return fallback_email_personalization(reason="central_llm_stage_unavailable")
        if str(stage_call.get("parse_status") or "") == "pydantic_failed":
            errors = (stage_call.get("request_meta") or {}).get("pydantic_errors")
            logger.warning("[email_personalization] Pydantic ValidationError: %s", errors)
        parsed = parse_email_personalization(stage_call["response_text"])
        if not verify_no_hallucinated_prices(parsed.get("body") or "", allowed_price_tokens(offer_summary)):
            return fallback_email_personalization(reason="price_hallucination_guard")
        parsed["execution_metadata"] = stage_call
        return parsed
    except GroqClientError as exc:
        return fallback_email_personalization(reason=str(exc))


def parse_email_personalization(raw_text: str) -> dict[str, Any]:
    try:
        candidate = json.loads(extract_json_candidate(raw_text))
    except json.JSONDecodeError as exc:
        raise GroqClientError(f"Email personalization did not return valid JSON: {exc}") from exc
    if not isinstance(candidate, dict):
        raise GroqClientError("EmailPersonalizationResult must be a JSON object.")
    model = EmailPersonalizationResult.model_validate(candidate)
    return model.model_dump()


def fallback_email_personalization(*, reason: str) -> dict[str, Any]:
    result = EmailPersonalizationResult(subject="", body="", tone_used="fallback").model_dump()
    result["execution_metadata"] = {
        "stage_name": "email_personalization",
        "fallback_used": True,
        "parse_status": "fallback",
        "error": reason,
    }
    return result


__all__ = [
    "run_email_personalization",
    "summarize_offer_for_llm",
    "allowed_price_tokens",
    "verify_no_hallucinated_prices",
    "plain_text_to_html",
    "fallback_email_personalization",
]
