"""Tool handlers — integrate mailbox memory, Drive, kalk-top (PR-C)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from log_config import get_logger

logger = get_logger("handlers")

from agent_runtime.cp2025 import check_cp2025_eligibility
from agent_runtime.draft_identity import compute_body_hash, compute_draft_id
from agent_runtime.kalk_top_client import (
    KalkTopClientError,
    KalkTopInvalidResponseError,
    KalkTopUnreachableError,
    build_calc_request_from_profile,
    call_calculate_offer,
)
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from evidence_authority import ensure_provenance_defaults
from mailbox_memory.active_facts import fetch_current_facts_for_case

_AREA_RE = re.compile(r"(\d{2,4})\s*m\s*[²2]", re.IGNORECASE)
_CITY_HINT_RE = re.compile(r"\b(Radlin|Rybnik|Katowice|Gliwice)\b", re.IGNORECASE)


# P1.3: deterministic Polish realization of epistemic draft context.
_CLAIM_LABEL_PL = {
    "error_code": "kodzie błędu",
    "customer_reported_error_code": "kodzie błędu",
    "device_model": "modelu urządzenia",
    "model": "modelu urządzenia",
}
_UNKNOWN_ASK_PL = {
    "exact_symptoms": "Prosimy o dokładny opis objawów.",
    "problem_start_time": "Prosimy o informację, kiedy problem się zaczął.",
    "error_code": "Prosimy o podanie kodu błędu, jeśli jest dostępny.",
    "device_model": "Prosimy o podanie modelu urządzenia.",
    "photo_or_message": "Prosimy o zdjęcie komunikatu, jeśli jest dostępne.",
}


def _compose_service_missing_info_body(
    *,
    epistemic_context: Any,
    legacy_body: str,
) -> str:
    """Deterministic epistemic-aware service draft.

    Confirmed customer-reported claims may be acknowledged; UNKNOWN fields may
    become questions; INFERRED/CONFLICTED claims are never asserted. When the
    epistemic context is empty (no durable store / no claims), the legacy
    template is preserved unchanged.
    """
    if epistemic_context is None:
        return legacy_body
    confirmed = list(getattr(epistemic_context, "confirmed_claims", None) or [])
    unknown = list(getattr(epistemic_context, "unknown_fields", None) or [])
    if not confirmed and not unknown:
        return legacy_body

    lines: list[str] = []
    for claim in confirmed[:2]:
        key = str(getattr(claim, "proposition_key", "") or "").strip().lower()
        value = str(getattr(claim, "value", "") or "").strip()
        label = _CLAIM_LABEL_PL.get(key)
        if label and value:
            lines.append("Dziękujemy za informację o " + label + " " + value + ".")
    asks: list[str] = []
    for claim in unknown:
        key = str(getattr(claim, "proposition_key", "") or "").strip().lower()
        ask = _UNKNOWN_ASK_PL.get(key)
        if ask and ask not in asks:
            asks.append(ask)
    if asks:
        lines.append(" ".join(asks[:3]))
    if not lines:
        return legacy_body
    return (
        "Dzień dobry,\n\n"
        + "\n".join(lines)
        + "\n\nPo otrzymaniu danych sprawa zostanie zweryfikowana i przekażemy ją "
        "do dalszej obsługi.\n\nZespół TOP-INSTAL"
    )


def search_gmail_thread(_plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    store = ctx.mailbox_store
    if store is None:
        return ToolResult(status="error", turn_summary_pl="Brak mailbox store.")
    case_id = ctx.snapshot.case_id
    messages = []
    fetch = getattr(store, "fetch_messages_for_case", None)
    if callable(fetch):
        messages = fetch(case_id, limit=10) or []
    subjects = [str(m.get("subject") or "")[:120] for m in messages if isinstance(m, dict)]
    summary = f"Znaleziono {len(messages)} wiadomości w sprawie."
    return ToolResult(
        status="ok",
        turn_summary_pl=summary,
        snapshot_delta={
            "operational_status": {"code": "enriching"},
            "agent_memory": {
                "reasoning_trace": [
                    {"turn": 0, "summary_pl": f"Gmail: {', '.join(subjects[:3]) or 'brak tematów'}"},
                ],
            },
        },
    )


def list_drive_folder(plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    folder_id = str(plan.arguments.get("folder_id") or "").strip()
    if not folder_id:
        try:
            from config import load_settings

            settings = load_settings(require_groq=False, require_google=False)
            folder_id = str(settings.google_drive_root_folder_id or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("Tool execution failed: list_drive_folder", exc_info=True)
            folder_id = ""
    if not folder_id:
        return ToolResult(status="error", turn_summary_pl="Brak GOOGLE_DRIVE_ROOT_FOLDER_ID.")
    try:
        from config import load_settings
        from drive_client import GoogleDriveClient

        settings = load_settings(require_groq=False, require_google=False)
        client = GoogleDriveClient(settings)
        listing = client.list_children(folder_id=folder_id, page_size=20)
        items = list(listing.get("items") or [])
        names = [str(i.get("name") or "") for i in items[:8]]
        logger.info("TOOL_COMPLETED", extra={"x": {"tool": "list_drive_folder", "items_count": len(items), "status": "ok"}})
        return ToolResult(
            status="ok",
            turn_summary_pl=f"Drive: {len(items)} elementów w folderze.",
            snapshot_delta={
                "agent_memory": {
                    "reasoning_trace": [
                        {"turn": 0, "summary_pl": "Pliki Drive: " + ", ".join(names) or "pusto"},
                    ],
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Tool execution failed: list_drive_folder listing", exc_info=True)
        return ToolResult(status="error", turn_summary_pl=f"Drive list failed: {exc}")


def apply_facts_to_snapshot_and_store(
    ctx: ToolExecutionContext,
    *,
    profile_delta: dict[str, Any],
    heated: int | None,
    city: str | None,
) -> None:
    _persist_facts(ctx, heated=heated, city=city)
    _ = profile_delta


def generate_draft_reply(plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    from agent_runtime.draft_sanity import evaluate_draft_sanity
    from agent_runtime.failure_taxonomy import attach_attribution, attribution
    from agent_runtime.known_fact_guard import is_service_case_kind

    profile = ctx.snapshot.hvac_profile
    intent = str(plan.arguments.get("intent") or "quote").strip()
    area = profile.heated_area_m2 or "?"
    city = profile.location.city or "Państwa lokalizacji"
    # P1.3: epistemic context projected from durable facts + Understanding
    # missing fields. Deterministic; legacy template preserved when absent.
    from agent_runtime.epistemic_projection import build_draft_claim_context_from_store

    case_id = str(ctx.snapshot.case_id or "")
    missing_fields: list[str] = []
    case_understanding = getattr(ctx.snapshot, "case_understanding", None)
    if case_understanding is not None:
        missing_fields = list(getattr(case_understanding, "missing_critical_fields", None) or [])
    epistemic_context = build_draft_claim_context_from_store(
        getattr(ctx, "mailbox_store", None),
        case_id,
        missing_fields,
    )
    if intent == "missing_info":
        if is_service_case_kind(str(ctx.snapshot.case_kind or "")):
            body = (
                "Dzień dobry,\n\n"
                "dziękujemy za zgłoszenie. Żeby bezpiecznie zweryfikować kolejny krok, "
                "prosimy o przesłanie modelu urządzenia, opisu objawu lub kodu błędu "
                "oraz zdjęcia komunikatu, jeśli jest dostępne.\n\n"
                "Po otrzymaniu danych sprawa zostanie zweryfikowana i przekażemy ją "
                "do dalszej obsługi.\n\n"
                "Zespół TOP-INSTAL"
            )
        else:
            body = (
                f"Dzień dobry,\n\nprosimy o uzupełnienie danych technicznych (metraż, OZC) "
                f"dla instalacji w {city}.\n\nZespół TOP-INSTAL"
            )
    else:
        body = (
            f"Dzień dobry,\n\ndziękujemy za zapytanie dotyczące pompy ciepła "
            f"dla budynku ok. {area} m2 w {city}. Przygotowaliśmy wstępną kalkulację — "
            f"prosimy o chwilę na weryfikację przez operatora.\n\nZespół TOP-INSTAL"
        )
    if (
        intent == "missing_info"
        and is_service_case_kind(str(ctx.snapshot.case_kind or ""))
    ):
        body = _compose_service_missing_info_body(
            epistemic_context=epistemic_context,
            legacy_body=body,
        )
    body_hash = compute_body_hash(body)
    if not body_hash:
        # Fail-closed: an empty/whitespace-only body is not a valid operator-facing
        # draft. Never let it into final_actions as if it were a real artifact.
        return ToolResult(status="error", turn_summary_pl="generate_draft_reply produced an empty body.")

    envelope = getattr(ctx.snapshot, "policy_action_envelope", None)
    policy_allows = None
    if envelope is not None and getattr(envelope, "freshness", "") == "current":
        policy_allows = bool(getattr(envelope, "allowed_by_policy", True))

    sanity = evaluate_draft_sanity(
        body=body,
        case_kind=str(ctx.snapshot.case_kind or ""),
        intent=intent,
        snapshot=ctx.snapshot,
        policy_allows_draft=policy_allows,
        epistemic_context=epistemic_context,
    )
    if not sanity.get("ok"):
        return attach_attribution(
            ToolResult(
                status="error",
                turn_summary_pl=(
                    "Draft sanity gate zablokował treść skierowaną do klienta: "
                    + ", ".join(sanity.get("reason_codes") or [])
                ),
                snapshot_delta={
                    "hitl_gate": {
                        "required": True,
                        "reason": "draft_sanity_failed:"
                        + ",".join(sanity.get("reason_codes") or [])[:120],
                    },
                    "operational_status": {"code": "pending_operator", "blocking": True},
                    "actions": [
                        {
                            "id": "draft_reply",
                            "enabled": False,
                            "payload_pl": body,
                            "disabled_reason_pl": "DRAFT_SANITY_FAILED: "
                            + ",".join(sanity.get("reason_codes") or []),
                            "identity_state": "identity_incomplete",
                        }
                    ],
                },
            ),
            attribution(
                failure_class="DRAFT_SANITY_FAILED",
                owner="quality",
                stage="draft_sanity_gate",
                retryable=True,
                safe_next_step="request_operator_clarification",
                detail=",".join(sanity.get("reason_codes") or []),
            ),
        )

    action_id = "draft_reply"
    case_id = str(ctx.snapshot.case_id or "")
    source_signal_id = str(ctx.snapshot.signal_id or "")
    from agent_runtime.draft_lineage_provenance import build_draft_lineage_provenance

    provenance = build_draft_lineage_provenance(
        draft_origin="brain2_fallback",
        origin_correlation_id=source_signal_id,
        origin_producer="generate_draft_reply",
    )
    return ToolResult(
        status="ok",
        turn_summary_pl="Draft odpowiedzi przygotowany (bez wysyłki).",
        snapshot_delta={
            "actions": [
                {
                    "id": action_id,
                    "enabled": True,
                    "payload_pl": body,
                    "disabled_reason_pl": None,
                    # Identity always minted at creation. Parent lineage refs stay
                    # empty here; annotate_action_parent_refs fills them only when a
                    # fresh, id-matching policy_action_envelope is correlated.
                    "draft_id": compute_draft_id(
                        case_id=case_id, source_signal_id=source_signal_id, action_id=action_id
                    ),
                    "revision": 1,
                    "body_hash": body_hash,
                    "case_id": case_id,
                    "source_signal_id": source_signal_id,
                    "identity_state": "identity_incomplete",
                    "parent_policy_decision_id": "",
                    "parent_action_proposal_v2_id": "",
                    "parent_decision_candidate_id": "",
                }
            ],
            "hitl_gate": {"required": True, "reason": "draft_ready_for_approval"},
            "operational_status": {"code": "pending_operator"},
            "draft_lineage_provenance": provenance,
        },
    )


def read_google_drive_file(plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    file_id = str(plan.arguments.get("file_id") or "").strip()
    if not file_id:
        return ToolResult(status="error", turn_summary_pl="file_id jest wymagane.")
    name = str(plan.arguments.get("file_name") or "")
    try:
        from agent_runtime.drive_file_reader import download_and_parse_drive_file

        parsed = download_and_parse_drive_file(
            file_id,
            file_name=name,
            settings=ctx.settings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Tool execution failed: read_google_drive_file", exc_info=True)
        return ToolResult(status="error", turn_summary_pl=f"Drive read/parse failed: {exc}")
    text = str(parsed.get("extracted_text") or "").strip()
    parser_name = str(parsed.get("parser_name") or "")
    title = str(parsed.get("file_name") or name or file_id)
    summary = f"Drive {title}: {len(text)} znaków ({parser_name or 'parser'})."
    delta: dict[str, Any] = {
        "agent_memory": {
            "reasoning_trace": [
                {
                    "turn": 0,
                    "summary_pl": (text[:500] + "…") if len(text) > 500 else (text or summary),
                },
            ],
        },
    }
    hvac: dict[str, Any] = {}
    area_match = _AREA_RE.search(text)
    if area_match:
        hvac["heated_area_m2"] = int(area_match.group(1))
    city_match = _CITY_HINT_RE.search(text)
    if city_match:
        hvac["location"] = {"city": city_match.group(1)}
    if hvac:
        delta["hvac_profile"] = hvac
        _persist_facts(ctx, heated=hvac.get("heated_area_m2"), city=(hvac.get("location") or {}).get("city"))
    if not text.strip():
        delta["gaps"] = [
            {
                "field": "drive_document_text",
                "severity": "warning",
                "ask_pl": f"Nie udało się wyciągnąć tekstu z pliku {title}.",
            }
        ]
    logger.info("TOOL_COMPLETED", extra={"x": {"tool": "read_google_drive_file", "status": "ok", "text_length": len(text)}})
    return ToolResult(status="ok", turn_summary_pl=summary, snapshot_delta=delta)


# ── Klasyfikacja typu sprawy (case_kind) ──────────────────────────────────────
# Szeroki podział z już policzonego intake (business_area / case_family — intake_policy.py),
# doprecyzowany przez hvac_intent z ekstrakcji LLM. Mail nie-HVAC (księgowość, szkolenie)
# klasyfikuje business_area, bo HVAC-owy run_signal_extraction nic dla niego nie zwróci.
_BUSINESS_AREA_TO_KIND = {
    "finance": "ksiegowosc",
    "procurement": "zakupy_materialow",
    "supplier_commercial": "zakupy_materialow",
    "logistics": "zakupy_materialow",
    "marketing_growth": "szkolenie",
    "internal_coordination": "inne",
    "general_admin": "inne",
    "compliance_legal": "inne",
    "security": "inne",
}
_HVAC_INTENT_TO_KIND = {
    "service": "awaria_naprawa", "serwis": "awaria_naprawa", "awaria": "awaria_naprawa",
    "repair": "awaria_naprawa", "naprawa": "awaria_naprawa", "usterka": "awaria_naprawa",
    "maintenance": "przeglad_konserwacja", "przeglad": "przeglad_konserwacja",
    "konserwacja": "przeglad_konserwacja", "inspection": "przeglad_konserwacja",
    "quote": "wycena_oferta", "offer": "wycena_oferta", "oferta": "wycena_oferta",
    "wycena": "wycena_oferta", "purchase": "wycena_oferta", "zakup": "wycena_oferta",
    "inquiry": "zapytanie_klienta", "zapytanie": "zapytanie_klienta", "lead": "zapytanie_klienta",
    # STRUCTURED-INPUT-AND-CAPABILITY-BASELINE-CLOSEOUT-01 — canonical hvac_intent values
    # (llm_contracts.signal_extraction.HVAC_INTENT_CANONICAL_VALUES) map directly onto
    # case_kind buckets. "nieznane" (no detected intent) is deliberately NOT mapped here so
    # the existing raw-text keyword heuristics below remain the fallback, unchanged.
    # Every OTHER canonical value must appear here: the raw-text heuristics have no rule for
    # deferral language, so an unmapped detected intent falls all the way through to
    # "niezaklasyfikowane" -- discarding a signal the extractor correctly identified.
    "wycena_oferta": "wycena_oferta",
    "awaria_naprawa": "awaria_naprawa",
    "przeglad_konserwacja": "przeglad_konserwacja",
    "pytanie_techniczne": "zapytanie_klienta",
    "negocjacja_ceny": "wycena_oferta",
    # a customer deferring a decision is a sales-pipeline state (the offer is out, the
    # decision is pending), same bucket as price negotiation
    "odroczenie_decyzji": "wycena_oferta",
}


def _classify_case_kind(*, business_area: str, case_family: str, hvac_intent: str, text: str) -> str:
    ba = (business_area or "").strip().lower()
    cf = (case_family or "").strip().lower()
    intent = (hvac_intent or "").strip().lower()
    low = (text or "").lower()
    # 1) Administracja/wewnętrzne — decyduje business_area
    if ba in _BUSINESS_AREA_TO_KIND:
        if ba == "marketing_growth" and not any(k in low for k in ("szkolenie", "webinar", "kurs", "warsztat")):
            return "inne"
        return _BUSINESS_AREA_TO_KIND[ba]  # finance -> ksiegowosc; kierunek faktury ustali _invoice_direction_refine
    # 2) Sprawy klienckie — właściwy jest hvac_intent
    if intent in _HVAC_INTENT_TO_KIND:
        return _HVAC_INTENT_TO_KIND[intent]
    # 3) Heurystyki słowne (intent pusty, mail kliencki)
    if any(k in low for k in ("awaria", "nie grzeje", "usterka", "nie dziala")):
        return "awaria_naprawa"
    if any(k in low for k in ("przeglad", "konserwacj", "coroczn")):
        return "przeglad_konserwacja"
    if any(k in low for k in ("faktura", "proform")):
        return "faktura_sprzedaz"
    if cf == "lead_opportunity" or ba == "sales":
        return "zapytanie_klienta"
    if any(k in low for k in ("wycena", "oferta", "ofertę", "oferte")):
        return "wycena_oferta"
    if any(k in low for k in ("zapytanie", "proszę o", "prosze o", "interesuje")):
        return "zapytanie_klienta"
    if any(k in low for k in ("klimatyzac", "pompa ciep", "ogrzewan")):
        return "zapytanie_klienta"
    return "niezaklasyfikowane"


def _digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _nip_eq(a: Any, b: Any) -> bool:
    da, db = _digits(a), _digits(b)
    return bool(da) and da == db


def _own_nip() -> str:
    import os

    return _digits(os.getenv("TOPINSTAL_OWN_NIP") or "")


def _fetch_invoice_fields(store: Any, case_id: str) -> dict[str, str]:
    """seller_nip / buyer_nip z faktów dokumentowych (promowanych przez document_field_extractor)."""
    out: dict[str, str] = {}
    try:
        # 4.2b: superseded NIP must not decide invoice direction.
        for row in fetch_current_facts_for_case(store, case_id):
            if not isinstance(row, dict):
                continue
            key = str(row.get("fact_key") or "")
            if key in ("seller_nip", "buyer_nip") and key not in out:
                out[key] = str(row.get("raw_value") or row.get("normalized_value") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_invoice_fields_failed case=%s: %s", case_id, exc)
        return {}
    return out


def _invoice_direction_refine(ctx: ToolExecutionContext, kind: str) -> str:
    """Kierunek faktury z DANYCH dokumentu (Sprzedawca/Nabywca NIP vs nasz NIP) — nie ze słów."""
    if kind not in {"ksiegowosc", "faktura_sprzedaz", "faktura_zakup"}:
        return kind
    store = ctx.mailbox_store
    if store is None:
        return kind
    fields = _fetch_invoice_fields(store, ctx.snapshot.case_id)
    if not fields:
        return kind  # brak sparsowanej faktury -> ksiegowosc (operator koryguje)
    own = _own_nip()
    if own and _nip_eq(fields.get("seller_nip"), own):
        return "faktura_sprzedaz"
    if own and _nip_eq(fields.get("buyer_nip"), own):
        return "faktura_zakup"
    return kind


# The agent turn is supervised at 45s (graph.AGENT_TURN_TIMEOUT_SECONDS), so an LLM tool
# inside it must terminate well before that. This is the tool's budget contract; it is
# enforced as a deadline shared with provider retry/fallback rather than as a wrapper thread.
AGENT_EXTRACTION_BUDGET_SEC = 30


def _run_llm_extraction(ctx: ToolExecutionContext, text: str) -> dict[str, Any] | None:
    """Realna ekstrakcja HVAC przez central LLM (reużycie istniejącego run_signal_extraction).

    Previously wrapped in ``ThreadPoolExecutor(...).result(timeout=30)`` around a chain whose
    single HTTP attempt was already allowed 60s -- the outer kill could not fire later than
    the first attempt's own timeout, so retry and provider fallback were unreachable here for
    the same structural reason as intake reasoning, and the abandoned request kept running.
    The 30s contract is preserved, but as a budget the whole chain can see.
    """
    from llm_deadline import stage_deadline

    try:
        from signal_extractor import run_signal_extraction
        from intake_payload import coerce_source_snapshot

        sp = ctx.signal_payload or {}
        snap = coerce_source_snapshot({"source_message": {
            "subject": sp.get("subject", ""),
            "body": sp.get("body_text") or text,
            "snippet": sp.get("snippet", ""),
            "from": sp.get("customer_email", ""),
            "message_id": sp.get("message_id", ""),
        }})
        settings = ctx.settings
        if settings is None:
            from config import load_settings

            settings = load_settings(require_groq=False, require_google=False)

        with stage_deadline("agent_extract_facts_from_text", AGENT_EXTRACTION_BUDGET_SEC) as deadline:
            # run_signal_extraction never raises for provider problems: it converts them into
            # an {"parse_status": "extraction_failed"} marker, so budget exhaustion arrives
            # here as a marker too and is handled by the status check below.
            res = run_signal_extraction(
                settings=settings,
                snapshot=snap,
                context_bundle={"case_id": ctx.snapshot.case_id},
            )
            budget_exhausted = deadline.expired()
            budget_telemetry = deadline.telemetry()

        status = str(res.get("parse_status") or "")
        if status.startswith("extraction_failed") or status == "empty_result":
            if budget_exhausted:
                logger.warning("LLM_EXTRACTION_TIMEOUT", extra={"x": {
                    "tool": "extract_facts_from_text",
                    "terminal_failure_reason": "stage_deadline_exhausted",
                    "parse_status": status,
                    **budget_telemetry,
                }})
            return None
        return res
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_llm_extraction_failed: %s", exc)
        return None


def _apply_extraction(ctx: ToolExecutionContext, ex: dict[str, Any]) -> ToolResult:
    from llm_contracts.engagement_snapshot_v2 import SALES_CASE_KINDS

    sp = ctx.signal_payload or {}
    kind = _classify_case_kind(
        business_area=str(sp.get("business_area") or ""),
        case_family=str(sp.get("case_family") or ""),
        hvac_intent=str(ex.get("hvac_intent") or ""),
        text=_collect_source_text(ctx),
    )
    kind = _invoice_direction_refine(ctx, kind)
    hvac: dict[str, Any] = {}
    area = ex.get("heated_area_m2")
    if isinstance(area, (int, float)) and area > 0:
        hvac["heated_area_m2"] = int(area)
    geo = str(ex.get("raw_geographic_signal") or "").strip()
    if geo:
        hvac["location"] = {"city": geo}
    btype = str(ex.get("building_type") or "").strip()
    if btype:
        hvac["building_type"] = btype
    delta: dict[str, Any] = {"operational_status": {"code": "enriching"}, "case_kind": kind}
    if hvac:
        delta["hvac_profile"] = hvac
        _persist_facts(ctx, heated=hvac.get("heated_area_m2"), city=(hvac.get("location") or {}).get("city"))
    # Metraż blokujący TYLKO dla leadów ofertowych — serwis/przegląd/administracja nigdy nie pyta o m².
    if kind in SALES_CASE_KINDS and "heated_area_m2" not in hvac:
        delta["gaps"] = [{
            "field": "heated_area_m2",
            "severity": "blocking",
            "ask_pl": "Podaj metraż ogrzewanego budynku (m²).",
        }]
    facts_str = ", ".join(f"{k}={v}" for k, v in hvac.items()) or "brak twardych danych HVAC"
    return ToolResult(
        status="ok",
        turn_summary_pl=f"Typ sprawy: {kind}. {facts_str}.",
        snapshot_delta=delta,
    )


def _llm_extraction_failed_result(reason: str | None = None) -> ToolResult:
    detail = str(reason or "wyczerpano łańcuch providerów LLM").strip()
    return ToolResult(
        status="error",
        turn_summary_pl=f"Ekstrakcja LLM nie powiodła się ({detail}).",
        snapshot_delta={
            "gaps": [{
                "field": "llm_extraction",
                "severity": "blocking",
                "ask_pl": "Ponów przetwarzanie sprawy — wszystkie providery LLM zwróciły błąd.",
            }],
        },
    )


def extract_facts_from_text(_plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    # Follow-up na już sklasyfikowanej sprawie — nie ponawiaj ekstrakcji leadowej.
    has_case_id = bool(ctx.snapshot.case_id and ctx.snapshot.case_id not in ("", "(nowy lead)"))
    has_case_id = has_case_id or bool(ctx.signal_payload.get("case_id", "").strip())
    case_kind = str(getattr(ctx.snapshot, "case_kind", "") or "")
    still_unclassified = case_kind in {"", "niezaklasyfikowane"}
    if has_case_id and not still_unclassified:
        cid = ctx.snapshot.case_id or ctx.signal_payload.get("case_id", "") or ""
        return ToolResult(
            status="error",
            turn_summary_pl=f"Sprawa {cid} istnieje. Nie wywoluj extract_facts_from_text. Uzyj search_rag_knowledge, propose_mutation albo przygotuj draft dla operatora.",
        )
    text = _collect_source_text(ctx)
    if not text.strip():
        return ToolResult(
            status="ok",
            turn_summary_pl="Brak treści źródłowej do analizy.",
            snapshot_delta={"gaps": [{
                "field": "source_text",
                "severity": "warning",
                "ask_pl": "Brak treści maila do analizy — otwórz pełny wątek w Gmailu.",
            }]},
        )
    extraction = _run_llm_extraction(ctx, text)
    if extraction is None:
        return _llm_extraction_failed_result()
    status = str(extraction.get("parse_status") or "")
    if status.startswith("extraction_failed") or status in {"empty_result", "pydantic_failed"}:
        return _llm_extraction_failed_result(str(extraction.get("error_reason") or status))
    return _apply_extraction(ctx, extraction)


_RAG_LIMIT_MAILBOX = 50
_RAG_LIMIT_DRIVE = 50


def search_rag_knowledge(plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    raw_query = plan.arguments.get("query")
    query = str(raw_query or "").strip()
    if not query:
        logger.warning("search_rag_knowledge: missing/empty query, backend not called")
        return ToolResult(
            status="error",
            turn_summary_pl="search_rag_knowledge wymaga niepustego argumentu query.",
        )
    store = ctx.mailbox_store
    fetch = getattr(store, "fetch_semantic_chunk_candidates_for_case", None) if store is not None else None
    if not callable(fetch):
        logger.warning("search_rag_knowledge: backend unavailable (no mailbox_store)")
        return ToolResult(
            status="error",
            turn_summary_pl="Backend RAG niedostępny (brak mailbox store).",
        )
    case_id = ctx.snapshot.case_id
    if not str(case_id or "").strip():
        logger.warning("search_rag_knowledge: missing case_id, backend not called")
        return ToolResult(
            status="error",
            turn_summary_pl="Backend RAG niedostępny (brak Case).",
        )

    try:
        from config import load_settings
        from embedding_runtime import build_embedding_runtime
        from mailbox_memory_store import _vector_literal

        settings = load_settings(require_groq=False, require_google=False)
        embedding_runtime = build_embedding_runtime(settings)
    except Exception:
        logger.warning("search_rag_knowledge: embedding runtime unavailable")
        return ToolResult(
            status="error",
            turn_summary_pl="Backend RAG niedostępny (brak embedding runtime).",
        )
    if embedding_runtime is None:
        logger.warning("search_rag_knowledge: embedding runtime not configured")
        return ToolResult(
            status="error",
            turn_summary_pl="Backend RAG niedostępny (embedding runtime nieskonfigurowany).",
        )

    try:
        vectors = embedding_runtime.embed_texts([query])
    except Exception:
        logger.warning("search_rag_knowledge: embedding generation failed")
        return ToolResult(
            status="error",
            turn_summary_pl="Backend RAG: błąd generowania embeddingu zapytania.",
        )
    query_vector = vectors[0] if vectors else None
    if (
        not isinstance(query_vector, list)
        or not query_vector
        or not all(isinstance(v, (int, float)) for v in query_vector)
    ):
        logger.warning("search_rag_knowledge: empty or invalid embedding vector")
        return ToolResult(
            status="error",
            turn_summary_pl="Backend RAG: pusty lub niepoprawny wektor zapytania.",
        )
    expected_dim = int(getattr(settings, "openai_compat_embedding_dimensions", 0) or 0)
    if expected_dim > 0 and len(query_vector) != expected_dim:
        logger.warning(
            "search_rag_knowledge: embedding dimension mismatch (%d != %d)",
            len(query_vector),
            expected_dim,
        )
        return ToolResult(
            status="error",
            turn_summary_pl="Backend RAG: niezgodny wymiar wektora zapytania.",
        )

    query_vector_literal = _vector_literal(query_vector)
    if not query_vector_literal:
        logger.warning("search_rag_knowledge: could not serialize query vector")
        return ToolResult(
            status="error",
            turn_summary_pl="Backend RAG: nie udało się zserializować wektora zapytania.",
        )

    try:
        rows = fetch(
            case_id,
            query_vector_literal,
            limit_mailbox=_RAG_LIMIT_MAILBOX,
            limit_drive=_RAG_LIMIT_DRIVE,
        ) or []
    except Exception as exc:
        logger.warning("search_rag_knowledge: backend error, degrading: %s", exc)
        return ToolResult(
            status="error",
            turn_summary_pl="Backend RAG zwrócił błąd wykonania.",
        )
    hit_rows = [r for r in rows if isinstance(r, dict)]
    hits = [str(r.get("chunk_text") or "")[:160] for r in hit_rows]
    top_provenance = (
        ensure_provenance_defaults(
            hit_rows[0].get("metadata") or {},
            default_origin="RAG",
        )
        if hit_rows
        else {}
    )
    evidence_ids: list[str] = []
    for index, row in enumerate(hit_rows[:3]):
        evidence_id = ""
        for key in ("chunk_id", "document_id", "source_ref", "source"):
            evidence_id = str(row.get(key) or "").strip()
            if evidence_id:
                break
        evidence_ids.append((evidence_id or f"hit-{index + 1}")[:80])
    section = "mitsubishi_sizing" if "mitsubishi" in query.lower() else "hvac_rules"
    top_hit = hits[0] if hits else "Brak chunkow RAG w bazie."
    evidence_summary = ",".join(evidence_ids) if evidence_ids else "none"
    reasoning_summary = (
        f"RAG query={query[:120]}; hits={len(hits)}; "
        f"evidence={evidence_summary}; "
        f"provenance={top_provenance.get('source_origin') or 'RAG'}"
        f"(instruction={top_provenance.get('instruction_authority') or 'NONE'}); "
        f"top={top_hit}"
    )
    return ToolResult(
        status="ok",
        turn_summary_pl=f"RAG: {len(hits)} fragmentów dla zapytania.",
        snapshot_delta={
            "agent_memory": {
                "constitution_sections_used": [section],
                "reasoning_trace": [
                    {"turn": 0, "summary_pl": reasoning_summary},
                ],
            },
        },
    )


def check_cp2025_eligibility_tool(_plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    eligible, summary = check_cp2025_eligibility(ctx.snapshot.hvac_profile)
    return ToolResult(
        status="ok",
        turn_summary_pl=summary,
        snapshot_delta={
            "hvac_profile": {"cp2025_eligible": eligible},
            "agent_memory": {"constitution_sections_used": ["cp2025_rules"]},
        },
    )


def call_kalk_top_quote(_plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    from agent_runtime.draft_sanity import evaluate_draft_sanity
    from agent_runtime.failure_taxonomy import (
        attach_attribution,
        attribution,
        classify_tool_handler_error,
    )

    # P1.4A fail-closed secondary defence: a direct or unexpected invocation must
    # not reach the endpoint when the case is not eligible / not technically
    # ready. Zero HTTP calls for ineligible cases is a hard contract.
    from agent_runtime.kalk_eligibility import decision_from_snapshot

    signal_payload = getattr(ctx, "signal_payload", None)
    decision_context = (
        signal_payload.get("decision_comparison_inputs")
        if isinstance(signal_payload, dict)
        else None
    )
    case_id = str(getattr(ctx.snapshot, "case_id", "") or "").strip()
    if not case_id:
        return attach_attribution(
            ToolResult(
                status="error",
                turn_summary_pl="kalk-top wymaga tożsamości sprawy (case_id)",
            ),
            attribution(
                failure_class="KALK_TOP_NOT_ELIGIBLE",
                owner="policy",
                stage="tool_eligibility",
                retryable=False,
                safe_next_step="proceed_without_calculation",
                detail="case_identity_missing",
            ),
        )

    decision = decision_from_snapshot(ctx.snapshot, decision_context=decision_context)
    if not decision.offered:
        reason = ";".join(decision.reasons) or "not eligible"
        return attach_attribution(
            ToolResult(
                status="error",
                turn_summary_pl=f"kalk-top nie jest dozwolony dla tej sprawy: {reason}",
            ),
            attribution(
                failure_class="KALK_TOP_NOT_ELIGIBLE",
                owner="policy",
                stage="tool_eligibility",
                retryable=False,
                safe_next_step="proceed_without_calculation",
                detail=reason,
            ),
        )

    payload = build_calc_request_from_profile(ctx.snapshot.model_dump(mode="python"))
    try:
        offer = call_calculate_offer(payload, settings=ctx.settings)
    except KalkTopUnreachableError as exc:
        result = ToolResult(
            status="node_a_error",
            turn_summary_pl=f"kalk-top niedostępny: {exc}",
            snapshot_delta={"operational_status": {"code": "node_a_error"}},
        )
        return attach_attribution(
            result,
            classify_tool_handler_error(
                tool_name="call_kalk_top_quote",
                summary=str(exc),
                status="node_a_error",
            ),
        )
    except KalkTopInvalidResponseError as exc:
        return attach_attribution(
            ToolResult(status="error", turn_summary_pl=str(exc)),
            attribution(
                failure_class="DOWNSTREAM_RESULT_INVALID",
                owner="infra",
                stage="tool_execution",
                retryable=False,
                safe_next_step="escalate_downstream_contract",
                detail="call_kalk_top_quote: downstream response invalid",
            ),
        )
    except KalkTopClientError as exc:
        result = ToolResult(status="error", turn_summary_pl=str(exc))
        return attach_attribution(
            result,
            classify_tool_handler_error(
                tool_name="call_kalk_top_quote",
                summary=str(exc),
                status="error",
            ),
        )
    totals = ""
    pricing = offer.get("pricing") if isinstance(offer.get("pricing"), dict) else {}
    totals_obj = pricing.get("totals") if isinstance(pricing.get("totals"), dict) else {}
    if totals_obj:
        totals = json.dumps(totals_obj, ensure_ascii=False)[:400]
    body = f"Oferta (skrót): {totals or 'zobacz kalk-top'}"

    envelope = getattr(ctx.snapshot, "policy_action_envelope", None)
    policy_allows = None
    if envelope is not None and getattr(envelope, "freshness", "") == "current":
        policy_allows = bool(getattr(envelope, "allowed_by_policy", True))

    sanity = evaluate_draft_sanity(
        body=body,
        case_kind=str(ctx.snapshot.case_kind or ""),
        intent="quote",
        snapshot=ctx.snapshot,
        policy_allows_draft=policy_allows,
    )
    if not sanity.get("ok"):
        reasons = ",".join(sanity.get("reason_codes") or [])
        return attach_attribution(
            ToolResult(
                status="error",
                turn_summary_pl=(
                    "Draft sanity gate zablokował treść oferty skierowaną do klienta: "
                    + reasons
                ),
                snapshot_delta={
                    "operational_status": {"code": "pending_operator", "blocking": True},
                    "hitl_gate": {
                        "required": True,
                        "reason": "draft_sanity_failed:" + reasons[:120],
                    },
                    "actions": [
                        {
                            "id": "draft_reply",
                            "enabled": False,
                            "payload_pl": body,
                            "disabled_reason_pl": f"DRAFT_SANITY_FAILED: {reasons}",
                            "identity_state": "identity_incomplete",
                        }
                    ],
                },
            ),
            attribution(
                failure_class="DRAFT_SANITY_FAILED",
                owner="quality",
                stage="draft_sanity_gate",
                retryable=True,
                safe_next_step="request_operator_clarification",
                detail=reasons,
            ),
        )

    return ToolResult(
        status="ok",
        turn_summary_pl="Wycena z kalk-top pobrana.",
        snapshot_delta={
            "operational_status": {"code": "ready_for_quote"},
            "actions": [
                {
                    "id": "draft_reply",
                    "enabled": True,
                    "payload_pl": body,
                    "disabled_reason_pl": None,
                }
            ],
        },
    )


def request_operator_clarification(plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    from agent_runtime.failure_taxonomy import attach_attribution, attribution
    from agent_runtime.known_fact_guard import guard_known_fact_reask

    ask = str(plan.arguments.get("ask_pl") or "Operatorze, proszę o decyzję w tej sprawie.").strip()
    blocked = guard_known_fact_reask(
        tool_name="request_operator_clarification",
        arguments={"ask_pl": ask},
        snapshot=ctx.snapshot,
    )
    if blocked is not None:
        return attach_attribution(
            ToolResult(
                status="error",
                turn_summary_pl=(
                    "Zablokowano pytanie o znany fakt: "
                    + ", ".join(blocked.get("fact_keys") or [])
                ),
                next_tool_hint="generate_draft_reply",
                snapshot_delta={
                    "agent_memory": {
                        "reasoning_trace": [
                            {
                                "turn": 0,
                                "summary_pl": "known_fact_reask_blocked:"
                                + ",".join(blocked.get("fact_keys") or []),
                            }
                        ]
                    }
                },
            ),
            attribution(
                failure_class="PLANNER_KNOWN_FACT_REASK",
                owner="planner",
                stage="tool_handler",
                retryable=True,
                safe_next_step="choose_non_reask_action",
                detail=",".join(blocked.get("fact_keys") or []),
            ),
        )
    return ToolResult(
        status="ok",
        turn_summary_pl=ask,
        snapshot_delta={
            "operational_status": {"code": "pending_operator"},
            "hitl_gate": {"required": True, "reason": "operator_clarification"},
            "gaps": [
                {
                    "field": "operator_decision",
                    "severity": "blocking",
                    "ask_pl": ask,
                }
            ],
        },
    )


def report_gaps_and_stop(_plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    gaps = [g.model_dump(mode="python") for g in ctx.snapshot.gaps]
    if not gaps:
        gaps = [
            {
                "field": "thermal_demand_kw",
                "severity": "blocking",
                "ask_pl": "Podaj szacowaną stratę ciepła (OZC) budynku.",
            }
        ]
    blocking = any(str(g.get("severity") or "") == "blocking" for g in gaps)
    return ToolResult(
        status="ok",
        turn_summary_pl="Zatrzymano — pending_operator.",
        snapshot_delta={
            "operational_status": {"code": "pending_operator", "blocking": blocking},
            "hitl_gate": {"required": True, "reason": "agent_stopped"},
            "gaps": gaps,
        },
    )


def _proposal_result(
    *,
    proposal_type: str,
    payload: dict[str, Any],
    summary_pl: str,
    ctx: ToolExecutionContext,
) -> ToolResult:
    from agent_runtime.materialize import append_materialize_proposal

    updated_snapshot = append_materialize_proposal(
        ctx.snapshot,
        proposal_type=proposal_type,
        payload=dict(payload or {}),
    )
    return ToolResult(
        status="ok",
        turn_summary_pl=summary_pl,
        snapshot_delta={
            "operational_status": {"code": "pending_operator", "blocking": True},
            "hitl_gate": {"required": True, "reason": f"materialize_proposal:{proposal_type}"},
            "agent_memory": {
                "materialize_proposals": [
                    p.model_dump(mode="python")
                    for p in updated_snapshot.agent_memory.materialize_proposals
                ],
            },
        },
    )


def propose_plan(plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    title = str(plan.arguments.get("plan_title_pl") or "").strip()
    steps = plan.arguments.get("steps") or []
    reasoning = str(plan.arguments.get("reasoning_pl") or "").strip()
    if not title or not steps:
        return ToolResult(status="error", turn_summary_pl="plan_title_pl i steps są wymagane.")
    return _proposal_result(
        proposal_type="composite_plan",
        payload={
            "plan_title_pl": title,
            "steps": steps,
            "reasoning_pl": reasoning,
        },
        summary_pl=f"Propozycja planu: {title}.",
        ctx=ctx,
    )


def retry_hard_parse(plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    file_id = str(plan.arguments.get("file_id") or ctx.signal_payload.get("file_id") or "").strip()
    if not file_id:
        return ToolResult(status="error", turn_summary_pl="file_id jest wymagane.")
    name = str(plan.arguments.get("file_name") or ctx.signal_payload.get("file_name") or "")
    try:
        from agent_runtime.drive_file_reader import download_and_parse_drive_file

        parsed = download_and_parse_drive_file(
            file_id,
            file_name=name,
            settings=ctx.settings,
            force_hard_lane=True,
        )
        text = str(parsed.get("extracted_text") or "").strip()
        if not text:
            return ToolResult(
                status="ok",
                turn_summary_pl="Hard PDF lane — nadal brak tekstu.",
                snapshot_delta={
                    "gaps": [
                        {
                            "field": "document_text",
                            "severity": "blocking",
                            "ask_pl": "Skan wymaga ręcznej weryfikacji operatora.",
                        }
                    ],
                },
            )
        return ToolResult(
            status="ok",
            turn_summary_pl=f"Hard PDF: {len(text)} znaków.",
            snapshot_delta={
                "agent_memory": {
                    "reasoning_trace": [
                        {"turn": 0, "summary_pl": f"Hard parse: {text[:200]}..."},
                    ],
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Tool execution failed: retry_hard_parse", exc_info=True)
        return ToolResult(status="error", turn_summary_pl=f"retry_hard_parse failed: {exc}")
_PROPOSAL_ARG_SCHEMAS: dict[str, dict[str, Any]] = {
    # create_case: wymaga minimum customer_email lub name
    "create_case": {
        "required_fields": [],  # Wszystkie opcjonalne — case_id może być auto
        "optional_fields": {"customer_email", "customer_name", "case_family", "email", "name", "family"},
    },
    # generate_draft: wymaga case_id
    "generate_draft": {
        "required_one_of": [{"case_id", "target"}],
        "optional_fields": {"to", "recipient", "subject", "body", "message"},
    },
    # add_deadline: wymaga daty
    "add_deadline": {
        "required_one_of": [{"deadline", "date"}],
        "optional_fields": {"case_id", "description", "note"},
    },
    # update_case_status: wymaga nowego statusu
    "update_case_status": {
        "optional_fields": {"status", "lifecycle_state"},
    },
    # update_case_lifecycle: wymaga nowego lifecycle
    "update_case_lifecycle": {
        "optional_fields": {"lifecycle_state", "status"},
    },
    # delete_document / move_document: wymagają file_id
    "delete_document": {
        "required_one_of": [{"file_id", "target"}],
    },
    "move_document": {
        "required_one_of": [{"file_id", "target"}],
        "optional_fields": {"destination", "folder_id"},
    },
    # merge_cases: wymaga source i target
    "merge_cases": {
        "required_fields": ["source_case_id", "target_case_id"],
    },
    # add_case_note / add_case_label: wymagają treści
    "add_case_note": {
        "required_one_of": [{"note", "content", "text"}],
        "optional_fields": {"case_id"},
    },
    "add_case_label": {
        "required_one_of": [{"label", "tag", "name"}],
        "optional_fields": {"case_id"},
    },
    # archive_case / restore_case / reassign_case: wymagają case_id
    "archive_case": {
        "optional_fields": {"case_id"},
    },
    "restore_case": {
        "optional_fields": {"case_id"},
    },
    "reassign_case": {
        "optional_fields": {"case_id", "assignee", "technician", "user"},
    },
    # link_case_to_case: wymaga dwóch case_id
    "link_case_to_case": {
        "required_one_of": [{"source_case_id", "case_id"}],
        "optional_fields": {"target_case_id"},
    },
    # update_customer_info: wymaga danych klienta
    "update_customer_info": {
        "optional_fields": {"customer_email", "customer_name", "phone", "address"},
    },
}


def _validate_proposal_args(operation: str, target: str, payload: dict) -> str | None:
    """Waliduje argumenty propozycji przed zapisem.

    Enterprise: sprawdza typy danych, puste stringi, znaki niedozwolone.
    Zwraca string z błędem lub None jeśli OK.
    """
    schema = _PROPOSAL_ARG_SCHEMAS.get(operation)
    if schema is None:
        return None  # Brak schematu = przepuść (bezpieczny fallback)

    # Zbierz wszystkie dostępne wartości (z payload + target jako fallback)
    available = set(payload.keys())
    if target:
        available.add("target")

    # Sprawdź wymagane pola (muszą być w payload i niepuste)
    required = schema.get("required_fields", [])
    for field in required:
        val = payload.get(field) or payload.get(field.lower())
        if val is None:
            return f"Brak wymaganego pola '{field}' dla operacji {operation}."
        if not isinstance(val, str) or not val.strip():
            return f"Pole '{field}' jest puste lub ma nieprawidłowy typ dla operacji {operation}."
        if len(val.strip()) > 1000:
            return f"Pole '{field}' przekracza 1000 znaków dla operacji {operation}."

    # Sprawdź required_one_of (przynajmniej jedno z grupy musi być niepuste)
    required_groups = schema.get("required_one_of", [])
    for group in required_groups:
        found = False
        for field in group:
            val = payload.get(field) or payload.get(field.lower())
            if val is not None and isinstance(val, str) and val.strip():
                found = True
                break
            # Sprawdź też w target (dla aliasów)
            if field == "target" and target and target.strip():
                found = True
                break
        if not found:
            alternatives = " lub ".join(f"'{f}'" for f in sorted(group))
            return f"Brak wymaganego pola ({alternatives}) dla operacji {operation}."

    # Walidacja typów dla znanych pól
    type_checks = {
        "customer_email": str,
        "customer_name": str,
        "case_id": str,
        "engagement_id": str,
        "file_id": str,
        "source_case_id": str,
        "target_case_id": str,
        "case_family": str,
        "deadline": str,
        "date": str,
        "scheduled_date": str,
    }
    for field, expected_type in type_checks.items():
        val = payload.get(field)
        if val is not None and not isinstance(val, expected_type):
            return f"Pole '{field}' ma nieprawidłowy typ (oczekiwano {expected_type.__name__}, otrzymano {type(val).__name__})."

    # Ostrzeżenie o nieznanych polach (tylko log, nie blokada)
    optional = schema.get("optional_fields", set())
    all_known = set(required) | optional
    for g in required_groups:
        all_known.update(g)
    unknown = available - all_known - {"target"}
    if unknown:
        logger.info("proposal_unknown_fields op=%s fields=%s", operation, sorted(unknown))

    return None


def propose_mutation(plan: ToolCallPlan, ctx: ToolExecutionContext) -> ToolResult:
    """Generyczny prymityw mutacji — typ operacji jako parametr.
    Zawsze przez HITL. Wykonanie nastąpi w _execute_composite_step po approve.
    known_operations generowane dynamicznie z WRITE_EXECUTORS (source of truth).
    """
    from agent_runtime.tools.write_executors import WRITE_EXECUTORS

    operation = str(plan.arguments.get("operation") or "").strip()
    target = str(plan.arguments.get("target") or "").strip()
    payload = dict(plan.arguments.get("payload") or {})
    reasoning = str(plan.arguments.get("reasoning_pl") or "").strip()

    if not operation or not target:
        return ToolResult(status="error", turn_summary_pl="operation i target są wymagane.")

    # Walidacja operation — dynamicznie z WRITE_EXECUTORS (source of truth)
    known_operations = frozenset(WRITE_EXECUTORS.keys())
    if operation not in known_operations:
        return ToolResult(status="error", turn_summary_pl=f"Nieznana operacja: {operation}")

    # Walidacja argumentów specyficznych dla operacji (PR-5C: action_proposal validation)
    validation_error = _validate_proposal_args(operation, target, payload)
    if validation_error:
        logger.warning("propose_mutation_validation_failed op=%s target=%s error=%s", operation, target, validation_error)
        return ToolResult(status="error", turn_summary_pl=validation_error)

    # Coherence validation (I2) — sprawdź czy mutacja jest spójna z lifecycle i faktami
    try:
        from case_coherence import CaseCoherenceValidator
        validator = CaseCoherenceValidator()
        coherence_result = validator.validate_mutation_coherence(
            snapshot=ctx.snapshot,
            mutation_payload={
                "operation": operation,
                "target": target,
                "facts": payload,
            },
        )
        if coherence_result.blocks:
            blocks_str = "; ".join(coherence_result.blocks)
            return ToolResult(
                status="error",
                turn_summary_pl=f"Propozycja zablokowana przez coherence validator: {blocks_str}",
            )
        # Dodaj warnings do payload jeśli istnieją
        _add_coherence_warnings(payload, coherence_result)
        if coherence_result.warnings:
            pass  # warnings zostaną dodane do payloadu
    except ImportError:
        logger.debug("case_coherence module not available — coherence validation skipped")
    except Exception as exc:
        logger.warning("Coherence validator error (non-blocking): %s", exc)

    step_args = dict(payload)
    if target:
        step_args.setdefault("case_id", target)
        step_args.setdefault("target", target)

    return _proposal_result(
        proposal_type="composite_plan",
        payload={
            "plan_title_pl": f"{operation}: {target}",
            "steps": [{
                "step_name_pl": operation,
                "operation": operation,
                "target": target,
                "args": step_args,
                "reasoning_pl": reasoning,
            }],
            "reasoning_pl": reasoning,
        },
        summary_pl=f"Propozycja: {operation} na {target}.",
        ctx=ctx,
    )


def _add_coherence_warnings(payload: dict, result: Any) -> None:
    """Dodaje ostrzeżenia coherence do payloadu mutacji."""
    if result.warnings:
        payload.setdefault("_coherence_warnings", [])
        if isinstance(result.warnings, list):
            payload["_coherence_warnings"].extend(result.warnings)


HANDLERS: dict[str, Callable[[ToolCallPlan, ToolExecutionContext], ToolResult]] = {
    # Mail agent — legacy read tools
    "search_gmail_thread": search_gmail_thread,
    "list_drive_folder": list_drive_folder,
    "generate_draft_reply": generate_draft_reply,
    "propose_mutation": propose_mutation,
    "propose_plan": propose_plan,
    # Domain-specific (wymagają specyficznej logiki — nie da się uogólnić)
    "read_google_drive_file": read_google_drive_file,
    "extract_facts_from_text": extract_facts_from_text,
    "check_cp2025_eligibility": check_cp2025_eligibility_tool,
    "call_kalk_top_quote": call_kalk_top_quote,
    "request_operator_clarification": request_operator_clarification,
    "report_gaps_and_stop": report_gaps_and_stop,
    "retry_hard_parse": retry_hard_parse,
    # Wyszukiwanie RAG (I4: GENERAL_GATEWAY_SYSTEM_NOTE referencjonuje to narzedzie)
    "search_rag_knowledge": search_rag_knowledge,
    # ETAP 3: Business Pulse (9 narzedzi biznesowych agenta)
    "get_pipeline_summary": lambda p, c: _bp_dispatch(c, "get_pipeline_summary"),
    "get_client_health": lambda p, c: _bp_dispatch(c, "get_client_health"),
    "get_daily_delta": lambda p, c: _bp_dispatch(c, "get_daily_delta"),
    "get_win_rate": lambda p, c: _bp_dispatch(c, "get_win_rate"),
    "get_top_clients": lambda p, c: _bp_dispatch(c, "get_top_clients"),
    "get_revenue_forecast": lambda p, c: _bp_dispatch(c, "get_revenue_forecast"),
    "get_system_health_snapshot": lambda p, c: _bp_dispatch(c, "get_system_health_snapshot"),
    "get_business_signals": lambda p, c: _bp_dispatch(c, "get_business_signals"),
    "get_agent_activity_summary": lambda p, c: _bp_dispatch(c, "get_agent_activity_summary"),
}


def _collect_source_text(ctx: ToolExecutionContext) -> str:
    parts: list[str] = []
    for key in ("subject", "snippet", "body_text", "signal_summary_pl"):
        value = str(ctx.signal_payload.get(key) or "").strip()
        if value:
            parts.append(value)
    store = ctx.mailbox_store
    if store is not None:
        fetch = getattr(store, "fetch_messages_for_case", None)
        if callable(fetch):
            for row in fetch(ctx.snapshot.case_id, limit=3) or []:
                if isinstance(row, dict):
                    parts.append(str(row.get("subject") or ""))
                    parts.append(str(row.get("body_text") or row.get("snippet") or ""))
    return "\n".join(parts)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _agent_fact_row(
    *,
    case_id: str,
    message_id: str,
    fact_key: str,
    normalized_value: str,
    entity_scope: str = "building",
) -> dict[str, Any]:
    from mailbox_memory.facts import stable_id

    fact_id = stable_id("fact", case_id, fact_key, message_id, normalized_value)
    return {
        "fact_id": fact_id,
        "case_id": case_id,
        "message_id": message_id,
        "document_id": "",
        "entity_scope": entity_scope,
        "fact_key": fact_key,
        "normalized_value": normalized_value,
        "raw_value": normalized_value,
        "confidence": 0.8,
        "observed_at": _utc_now_iso(),
        "source_type": "agent_extraction",
        "source_ref": f"agent:{message_id}",
        "status": "active",
        "metadata": {"tool": "extract_facts_from_text"},
    }


def _persist_facts(ctx: ToolExecutionContext, *, heated: int | None, city: str | None) -> None:
    store = ctx.mailbox_store
    if store is None:
        return
    append = getattr(store, "append_facts_with_supersession", None)
    if not callable(append):
        append = getattr(store, "append_fact_rows", None)
    if not callable(append):
        return
    case_id = ctx.snapshot.case_id
    message_id = str(ctx.signal_payload.get("message_id") or ctx.snapshot.trace_id or "agent")
    rows: list[dict[str, Any]] = []
    if heated is not None:
        rows.append(
            _agent_fact_row(
                case_id=case_id,
                message_id=message_id,
                fact_key="heated_area_m2",
                normalized_value=str(heated),
            )
        )
    if city:
        rows.append(
            _agent_fact_row(
                case_id=case_id,
                message_id=message_id,
                fact_key="city",
                normalized_value=city,
            )
        )
    if rows:
        # Idempotency: hash rows to avoid duplicate writes from read-only tools
        import hashlib
        idem_key = hashlib.sha256(
            json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        if hasattr(_persist_facts, "_seen_keys") and idem_key in _persist_facts._seen_keys:
            logger.info("FACTS_IDEMPOTENT_SKIP", extra={"x": {"case_id": case_id, "idem_key": idem_key}})
            return
        try:
            result = append(rows)
            if isinstance(result, dict) and result.get("unchanged") and not result.get("inserted"):
                logger.info(
                    "FACTS_IDEMPOTENT_SKIP",
                    extra={"x": {"case_id": case_id, "idem_key": idem_key, "supersession": result}},
                )
            if not hasattr(_persist_facts, "_seen_keys"):
                _persist_facts._seen_keys = set()
            _persist_facts._seen_keys.add(idem_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("persist_facts_failed case=%s: %s", case_id, exc)
            return


# ── ETAP 3: Business Pulse handlers (9 narzędzi) ─────────────────────────

def _bp_dispatch(ctx: ToolExecutionContext, bp_func_name: str) -> ToolResult:
    """Wraps a business_pulse function call in the ToolResult protocol."""
    from agent_runtime.business_pulse import (
        get_pipeline_summary,
        get_client_health,
        get_daily_delta,
        get_win_rate,
        get_top_clients,
        get_revenue_forecast,
        get_system_health_snapshot,
        get_business_signals,
        get_agent_activity_summary,
    )

    store = ctx.mailbox_store
    settings = getattr(ctx, "settings", None)

    func_map = {
        "get_pipeline_summary": get_pipeline_summary,
        "get_client_health": get_client_health,
        "get_daily_delta": get_daily_delta,
        "get_win_rate": get_win_rate,
        "get_top_clients": get_top_clients,
        "get_revenue_forecast": get_revenue_forecast,
        "get_system_health_snapshot": get_system_health_snapshot,
        "get_business_signals": get_business_signals,
        "get_agent_activity_summary": get_agent_activity_summary,
    }

    func = func_map.get(bp_func_name)
    if func is None:
        return ToolResult(
            status="error",
            turn_summary_pl=f"Nieznane narzedzie Business Pulse: {bp_func_name}",
        )

    try:
        data = func(store, settings)
        summary = ", ".join(str(k) for k in data.keys() if k != "ok")
        return ToolResult(
            status="ok",
            turn_summary_pl=f"[Business Pulse] {bp_func_name}: {summary}",
            snapshot_delta={
                "operational_status": {"code": "enriching"},
                "agent_memory": {
                    "reasoning_trace": [{"turn": 0, "summary_pl": str(data)}],
                },
                "business_pulse": data,
            },
        )
    except Exception as exc:
        return ToolResult(
            status="error",
            turn_summary_pl=f"Business Pulse {bp_func_name} blad: {exc}",
        )
