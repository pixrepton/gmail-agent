"""Fala C2: Skrzat operator copilot — context audit + optional central LLM."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from central_llm_stage import run_central_structured_stage
from config import Settings
from context_tray_set import FORBIDDEN_RAW_KEYS
from groq_client import GroqClientError
from llm_client import extract_json_candidate
from llm_contracts.skrzat_answer import SkrzatAnswerResult
from log_config import get_logger
from projection_quality_metrics import build_projection_quality_metrics
from skrzat_rag_advisory import build_rag_advisory_slice
from skrzat_runtime import ALLOWED_MODES, SCHEMA_VERSION, answer_case_question

logger = get_logger(__name__)

MAX_TRAY_ITEMS = 8
_TRAY_KEYS = (
    "essence_tray",
    "facts_tray",
    "evidence_tray",
    "gaps_tray",
    "conflicts_tray",
    "documents_tray",
    "calendar_tray",
    "history_tray",
    "operator_feedback_tray",
    "candidate_moves_tray",
    "llm_warnings_tray",
)
_SAFE_ROW_KEYS = frozenset(
    {
        "summary",
        "summary_text",
        "summary_pl",
        "content_pl",
        "fact_key",
        "value",
        "source_type",
        "source_id",
        "warning_code",
        "field_name",
        "read_only",
        "action_allowed",
    }
)

SKRZAT_COPILOT_INSTRUCTIONS = """
Odpowiedz operatorowi TOP-INSTAL po polsku w trybie read-only (tylko odczyt).

Zasady (obowiazkowe):
- Uzywaj WYLACZNIE danych z context_trays w prompt_input. Nie wymyslaj cen, terminow montazu ani decyzji klienta.
- evidence_refs, gap_refs, conflict_refs: odwolania do elementow tack (source_id, summary, fact_key itd.).
- answer_text: konkretna odpowiedz na pytanie operatora; bez zargonu wewnetrznego (trace, workflow).
- operator_caution_pl: krotki komunikat gdy dowody sa slabe lub sa luki/konflikty wymagajace uwagi.
- Nie proponuj wykonania akcji (wysylka maila, zmiana CRM) — tylko interpretacja i braki.
""".strip()

_SKRZAT_ANSWER_SCHEMA = SkrzatAnswerResult.model_json_schema()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def _strip_forbidden(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _strip_forbidden(v)
            for k, v in value.items()
            if str(k) not in FORBIDDEN_RAW_KEYS
        }
    if isinstance(value, list):
        return [_strip_forbidden(v) for v in value]
    return value


def _bounded_tray_row(row: dict[str, Any]) -> dict[str, Any]:
    cleaned = _strip_forbidden(row)
    if not isinstance(cleaned, dict):
        return {}
    out: dict[str, Any] = {}
    for key, val in cleaned.items():
        if key not in _SAFE_ROW_KEYS:
            continue
        if isinstance(val, str):
            out[key] = val[:500]
        elif val is not None:
            out[key] = val
    return out


def _bounded_tray(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_bounded_tray_row(r) for r in rows[:MAX_TRAY_ITEMS] if _bounded_tray_row(r)]


def build_skrzat_prompt_input(
    trays: dict[str, Any],
    *,
    question: str,
    mode: str,
) -> dict[str, Any]:
    """Bounded Skrzat payload from ContextTraySet — no raw mail bodies."""
    t = trays if isinstance(trays, dict) else {}
    context_trays: dict[str, Any] = {}
    for key in _TRAY_KEYS:
        context_trays[key] = _bounded_tray(_list_of_dicts(t.get(key)))
    return {
        "case_id": str(t.get("case_id") or ""),
        "question": str(question or "").strip()[:1000],
        "mode": str(mode or "ask").strip().lower(),
        "context_trays": context_trays,
    }


def assemble_skrzat_context_audit(
    settings: Settings,
    *,
    case_id: str,
    query_text: str,
) -> dict[str, Any] | None:
    """Assemble company/case context for Skrzat audit (always attempted when case_id + query)."""
    q = str(query_text or "").strip()
    cid = str(case_id or "").strip()
    if not cid or not q:
        return None
    try:
        from central_llm_stage import build_context_assembler
        from context_assembler import assembled_context_to_dict

        assembler = build_context_assembler(settings)
        assembled = assembler.assemble(q, case_id=cid)
        return assembled_context_to_dict(assembled)
    except Exception as exc:
        logger.warning("assemble_skrzat_context_audit failed", extra={"x": {
            "error": str(exc)[:200],
            "case_id": cid,
            "question_len": len(str(q or "")),
        }})
        return None


def parse_skrzat_llm_result(raw_text: str) -> SkrzatAnswerResult:
    try:
        candidate = json.loads(extract_json_candidate(raw_text))
    except json.JSONDecodeError as exc:
        raise GroqClientError(f"Skrzat copilot did not return valid JSON: {exc}") from exc
    if not isinstance(candidate, dict):
        raise GroqClientError("SkrzatAnswerResult must be a JSON object.")
    return SkrzatAnswerResult.model_validate(candidate)


def _resolve_refs_or_tray(
    refs: list[dict[str, Any]],
    tray: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if refs:
        return [_strip_forbidden(r) for r in refs[:MAX_TRAY_ITEMS] if isinstance(r, dict)]
    return tray[:MAX_TRAY_ITEMS]


def _normalize_mode(mode: str, trays: dict[str, Any]) -> tuple[str, list[Any]]:
    requested = str(mode or "ask").strip().lower()
    warnings = list(_list_of_dicts(trays.get("llm_warnings_tray")))
    if requested not in ALLOWED_MODES:
        warnings.append({"warning_code": "unsupported_mode_fallback", "summary": f"Unsupported mode: {requested}"})
        requested = "ask"
    return requested, warnings


def llm_result_to_envelope(
    llm: SkrzatAnswerResult,
    trays: dict[str, Any],
    *,
    question: str,
    mode: str,
    generated_at: str = "",
) -> dict[str, Any]:
    """Map SkrzatAnswerResult to conversation_answer_envelope.v1."""
    t = trays if isinstance(trays, dict) else {}
    resolved_mode, base_warnings = _normalize_mode(mode, t)
    warnings: list[Any] = list(base_warnings)
    for w in llm.warnings:
        if isinstance(w, str) and w.strip():
            warnings.append({"warning_code": "llm_warning", "summary": w.strip()[:500]})
        elif isinstance(w, dict):
            warnings.append(w)
    if llm.operator_caution_pl.strip():
        warnings.append({"warning_code": "operator_caution", "summary": llm.operator_caution_pl.strip()[:500]})

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": str(t.get("case_id") or ""),
        "generated_at": str(generated_at or "").strip() or _utc_now_iso(),
        "mode": resolved_mode,
        "question": str(question or "").strip()[:1000],
        "answer_text": str(llm.answer_text or "").strip()[:8000] or "Brak odpowiedzi LLM na podstawie tack.",
        "evidence": _resolve_refs_or_tray(llm.evidence_refs, _list_of_dicts(t.get("evidence_tray"))),
        "gaps": _resolve_refs_or_tray(llm.gap_refs, _list_of_dicts(t.get("gaps_tray"))),
        "conflicts": _resolve_refs_or_tray(llm.conflict_refs, _list_of_dicts(t.get("conflicts_tray"))),
        "warnings": warnings[:12],
        "candidate_moves": _list_of_dicts(t.get("candidate_moves_tray"))[:MAX_TRAY_ITEMS],
        "read_only": True,
        "action_allowed": False,
    }


def _attach_quality_metrics(envelope: dict[str, Any], trays: dict[str, Any]) -> None:
    """Read-only proof counters for bounded operator checklist (step 6)."""
    envelope["quality_metrics"] = build_projection_quality_metrics(
        None,
        skrzat_answer=envelope,
        operator_feedback=_list_of_dicts(trays.get("operator_feedback_tray")),
        generated_at=str(envelope.get("generated_at") or ""),
    )


def run_skrzat_llm_answer(
    *,
    settings: Settings,
    context_tray_set: dict[str, Any],
    question: str,
    mode: str = "ask",
    query_text: str = "",
    assembled_context: dict[str, Any] | None = None,
    case_context_pack: dict[str, Any] | None = None,
    verbose: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run Skrzat LLM stage; fallback to deterministic envelope on failure."""
    trays = context_tray_set if isinstance(context_tray_set, dict) else {}
    case_id = str(trays.get("case_id") or "").strip()
    q = str(query_text or question or "").strip()
    prompt_input = build_skrzat_prompt_input(trays, question=question, mode=mode)
    meta_base: dict[str, Any] = {
        "stage_name": "skrzat_copilot",
        "assembled_context": assembled_context,
    }

    def _deterministic_fallback(*, parse_status: str, error: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
        envelope = answer_case_question(
            trays,
            question=question,
            mode=mode,
            generated_at=str(trays.get("generated_at") or ""),
        )
        _attach_quality_metrics(envelope, trays)
        meta = {
            **meta_base,
            "parse_status": parse_status,
            "answer_mode": "deterministic_fallback",
        }
        if error:
            meta["error"] = error
        return envelope, meta

    try:
        stage_call = run_central_structured_stage(
            settings,
            stage_name="skrzat_copilot",
            task_instructions=SKRZAT_COPILOT_INSTRUCTIONS,
            prompt_input=prompt_input,
            query_text=q or question,
            json_schema=_SKRZAT_ANSWER_SCHEMA,
            schema_name="skrzat_copilot_v1",
            case_id=case_id or None,
            verbose=verbose,
            output_model=SkrzatAnswerResult,
            context_bundle={
                "case_id": case_id,
                "case_context_pack": case_context_pack,
            }
            if case_id and case_context_pack
            else ({"case_id": case_id} if case_id else None),
        )
    except GroqClientError as exc:
        return _deterministic_fallback(parse_status="fallback", error=str(exc))

    if stage_call is None:
        return _deterministic_fallback(parse_status="fallback", error="central_llm_stage_unavailable")

    if not meta_base.get("assembled_context") and stage_call.get("assembled_context"):
        meta_base["assembled_context"] = stage_call.get("assembled_context")

    parse_status = str(stage_call.get("parse_status") or "")
    if parse_status == "pydantic_validated":
        try:
            llm = parse_skrzat_llm_result(str(stage_call.get("response_text") or ""))
        except GroqClientError as exc:
            return _deterministic_fallback(parse_status="fallback", error=str(exc))
        envelope = llm_result_to_envelope(
            llm,
            trays,
            question=question,
            mode=mode,
            generated_at=str(trays.get("generated_at") or ""),
        )
        _attach_quality_metrics(envelope, trays)
        return envelope, {
            **meta_base,
            "parse_status": "pydantic_validated",
            "answer_mode": "llm",
        }

    if parse_status == "pydantic_failed":
        errors = (stage_call.get("request_meta") or {}).get("pydantic_errors")
        logger.warning("[skrzat_copilot] Pydantic ValidationError: %s", errors)
    return _deterministic_fallback(parse_status="fallback", error=parse_status or "llm_parse_failed")


def resolve_skrzat_answer(
    *,
    settings: Settings,
    context_tray_set: dict[str, Any],
    question: str,
    mode: str = "ask",
    query_text: str = "",
    case_context_pack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Main Skrzat entrypoint: always context_audit; deterministic or LLM per settings."""
    trays = context_tray_set if isinstance(context_tray_set, dict) else {}
    case_id = str(trays.get("case_id") or "").strip()
    q = str(query_text or question or "").strip()
    assembled_context = assemble_skrzat_context_audit(settings, case_id=case_id, query_text=q) if case_id and q else None

    answer_mode = str(getattr(settings, "skrzat_answer_mode", "deterministic") or "deterministic").strip().lower()
    if answer_mode == "llm":
        envelope, execution_metadata = run_skrzat_llm_answer(
            settings=settings,
            context_tray_set=trays,
            question=question,
            mode=mode,
            query_text=q,
            assembled_context=assembled_context,
            case_context_pack=case_context_pack,
        )
        if assembled_context and not execution_metadata.get("assembled_context"):
            execution_metadata["assembled_context"] = assembled_context
    else:
        envelope = answer_case_question(
            trays,
            question=question,
            mode=mode,
            generated_at=str(trays.get("generated_at") or ""),
        )
        _attach_quality_metrics(envelope, trays)
        execution_metadata = {
            "stage_name": "skrzat_copilot",
            "parse_status": "deterministic",
            "answer_mode": "deterministic",
            "assembled_context": assembled_context,
        }

    ac_final = execution_metadata.get("assembled_context") or assembled_context
    envelope["context_audit"] = {
        "answer_mode": execution_metadata.get("answer_mode") or answer_mode,
        "parse_status": execution_metadata.get("parse_status"),
        "stage_name": "skrzat_copilot",
        "assembled_context": ac_final,
    }
    if case_context_pack or ac_final:
        envelope["rag_advisory"] = build_rag_advisory_slice(
            ac_final if isinstance(ac_final, dict) else None,
            case_context_pack=case_context_pack,
        )
    return envelope


__all__ = [
    "SKRZAT_COPILOT_INSTRUCTIONS",
    "assemble_skrzat_context_audit",
    "build_skrzat_prompt_input",
    "llm_result_to_envelope",
    "parse_skrzat_llm_result",
    "resolve_skrzat_answer",
    "run_skrzat_llm_answer",
]
