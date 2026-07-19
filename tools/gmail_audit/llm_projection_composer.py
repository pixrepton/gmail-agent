"""Projection composer: deterministic baseline + optional adaptive LLM enrichment."""

from __future__ import annotations
from log_config import get_logger

import json
from typing import Any

from central_llm_stage import run_central_structured_stage
from config import Settings
from groq_client import GroqClientError
from llm_client import extract_json_candidate
from llm_contracts.projection_compose import ProjectionComposeResult
from projection_composer_policy import (
    PROJECTION_COMPOSER_MODE_DETERMINISTIC,
    PROJECTION_COMPOSER_MODE_LLM,
    resolve_projection_composer_mode,
)
from projection_envelope import build_projection_envelope
from projection_validator import validate_projection_envelope
from skrzat_copilot import build_skrzat_prompt_input

logger = get_logger(__name__)

PROJECTION_COMPOSER_INSTRUCTIONS = """
Skomponuj read-only projekcje operatora TOP-INSTAL po polsku na podstawie context_trays.

Zasady (obowiazkowe):
- Uzywaj WYLACZNIE danych z context_trays. Nie wymyslaj cen, terminow montazu ani decyzji klienta.
- essence_summary_pl: krotka esencja sprawy (max ~500 znakow) dla karty Biurka.
- desk_card_title_pl: krotki tytul karty (opcjonalnie).
- operator_visibility_note_pl: kiedy operator powinien zwrocic uwage (luki, konflikty, slabe dowody).
- Nie proponuj wykonania akcji — tylko kompozycja widoku.
""".strip()

_PROJECTION_COMPOSE_SCHEMA = ProjectionComposeResult.model_json_schema()


def _parse_projection_compose(raw_text: str) -> ProjectionComposeResult:
    try:
        candidate = json.loads(extract_json_candidate(raw_text))
    except json.JSONDecodeError as exc:
        raise GroqClientError(f"Projection composer did not return valid JSON: {exc}") from exc
    if not isinstance(candidate, dict):
        raise GroqClientError("ProjectionComposeResult must be a JSON object.")
    return ProjectionComposeResult.model_validate(candidate)


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def apply_llm_enrichment_to_envelope(
    envelope: dict[str, Any],
    llm: ProjectionComposeResult,
    *,
    composer_meta: dict[str, Any],
) -> dict[str, Any]:
    """Merge bounded LLM text into a deterministic ProjectionEnvelope."""

    out = dict(envelope)
    summary = str(llm.essence_summary_pl or "").strip()
    if summary:
        desk = _list_of_dicts(out.get("desk_cards"))
        if desk:
            card = dict(desk[0])
            card["summary"] = summary[:500]
            title = str(llm.desk_card_title_pl or "").strip()
            if title:
                card["title"] = title[:120]
            desk[0] = card
            out["desk_cards"] = desk
        blocks = _list_of_dicts(out.get("case_detail_blocks"))
        if blocks:
            essence_block = dict(blocks[0])
            essence_block["content"] = summary[:900]
            essence_block["source"] = "llm_projection_composer"
            blocks[0] = essence_block
            out["case_detail_blocks"] = blocks

    warnings = _list_of_dicts(out.get("warnings"))
    for w in llm.warnings:
        if isinstance(w, str) and w.strip():
            warnings.append({"warning_code": "composer_llm", "summary": w.strip()[:500]})
    note = str(llm.operator_visibility_note_pl or "").strip()
    if note:
        warnings.append({"warning_code": "operator_visibility", "summary": note[:500]})
    if warnings:
        out["warnings"] = warnings[:16]

    audit = _list_of_dicts(out.get("audit_blocks"))
    audit.append(
        {
            "block_type": "llm_composition",
            "provider": composer_meta.get("provider"),
            "decision_reason": composer_meta.get("decision_reason"),
        }
    )
    out["audit_blocks"] = audit
    return out


def run_projection_llm_compose(
    *,
    settings: Settings,
    context_tray_set: dict[str, Any],
    decision: dict[str, Any],
) -> tuple[ProjectionComposeResult | None, dict[str, Any]]:
    """Call central LLM for projection enrichment; return (result, execution_meta)."""

    trays = context_tray_set if isinstance(context_tray_set, dict) else {}
    case_id = str(trays.get("case_id") or "").strip()
    prompt_input = build_skrzat_prompt_input(
        trays,
        question="Skomponuj esencje i widocznosc operatora dla tej sprawy (read-only).",
        mode="compose",
    )
    meta: dict[str, Any] = {"stage_name": "projection_composer", "decision": decision}
    try:
        stage = run_central_structured_stage(
            settings,
            stage_name="projection_composer",
            task_instructions=PROJECTION_COMPOSER_INSTRUCTIONS,
            prompt_input=prompt_input,
            query_text=f"projection compose case {case_id}".strip(),
            json_schema=_PROJECTION_COMPOSE_SCHEMA,
            schema_name="projection_compose_v1",
            case_id=case_id or None,
            output_model=ProjectionComposeResult,
        )
    except Exception as exc:
        logger.warning("[projection_composer] LLM stage failed: %s", exc)
        meta["parse_status"] = "llm_error"
        meta["error"] = str(exc)
        return None, meta

    if not isinstance(stage, dict):
        meta["parse_status"] = "llm_empty"
        return None, meta

    meta["parse_status"] = str(stage.get("parse_status") or "unknown")
    meta["model_name"] = stage.get("model_name")
    meta["latency_ms"] = stage.get("latency_ms")
    response_json = stage.get("response_json")
    if isinstance(response_json, dict):
        try:
            return ProjectionComposeResult.model_validate(response_json), meta
        except Exception as exc:
            get_logger("llm_projection_composer").warning(
                "projection_compose: model_validate failed: %s", exc
            )
    raw = str(stage.get("response_text") or "").strip()
    if raw:
        try:
            return _parse_projection_compose(raw), meta
        except GroqClientError as exc:
            meta["parse_status"] = "parse_failed"
            meta["error"] = str(exc)
    return None, meta


def compose_projection_from_trays(
    context_tray_set: dict[str, Any],
    *,
    decision_view: dict[str, Any] | None = None,
    v2_projection: dict[str, Any] | None = None,
    generated_at: str = "",
    settings: Settings | None = None,
    stage_outputs: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Build ProjectionEnvelope from trays with adaptive LLM vs deterministic composer.

    Returns (envelope, composer_decision).
    """

    trays = context_tray_set if isinstance(context_tray_set, dict) else {}
    decision = resolve_projection_composer_mode(trays, stage_outputs=stage_outputs, settings=settings)
    mode = str(decision.get("mode") or PROJECTION_COMPOSER_MODE_DETERMINISTIC)

    envelope = build_projection_envelope(
        trays,
        decision_view=decision_view,
        v2_projection=v2_projection,
        generated_at=generated_at,
    )
    provider = PROJECTION_COMPOSER_MODE_DETERMINISTIC
    live_llm = False
    llm_meta: dict[str, Any] = {}

    if mode == PROJECTION_COMPOSER_MODE_LLM and settings is not None:
        llm_result, llm_meta = run_projection_llm_compose(
            settings=settings,
            context_tray_set=trays,
            decision=decision,
        )
        if llm_result is not None:
            composer_meta = {
                "provider": "llm",
                "decision_reason": decision.get("decision_reason"),
            }
            envelope = apply_llm_enrichment_to_envelope(envelope, llm_result, composer_meta=composer_meta)
            provider = "llm"
            live_llm = True
        else:
            decision = {
                **decision,
                "mode": PROJECTION_COMPOSER_MODE_DETERMINISTIC,
                "decision_reason": f"{decision.get('decision_reason')}_llm_fallback",
                "live_llm": False,
            }

    validation = validate_projection_envelope(envelope, context_tray_set=trays)
    envelope["composer"] = {
        "schema_version": "llm_projection_composer.v1",
        "provider": provider,
        "live_llm": live_llm,
        "read_only": True,
        "policy_mode": decision.get("policy_mode"),
        "decision_reason": decision.get("decision_reason"),
    }
    envelope["projection_validation"] = validation
    if llm_meta:
        envelope["composer"]["llm_execution"] = llm_meta
    return envelope, decision


__all__ = [
    "apply_llm_enrichment_to_envelope",
    "compose_projection_from_trays",
    "run_projection_llm_compose",
]
