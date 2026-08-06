"""Read-only ContextTraySet contract built from CaseContextPack.

This module does not mutate mailbox memory and does not call an LLM. It only
labels already-known case context for projection and Skrzat consumers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from case_context_contract import build_case_context_pack_vnext


SCHEMA_VERSION = "context_tray_set.v1"

NEXT_ACTION_PL: dict[str, str] = {
    "review_required": "Sprawdź i uzupełnij dane sprawy",
    "review": "Wymaga przeglądu",
    "wait": "Oczekiwanie — brak akcji",
    "collect_data": "Zbierz brakujące dane od klienta",
    "escalate_internal": "Eskalacja wewnętrzna",
    "close": "Zamknij sprawę",
    "send_quote": "Wyślij wycenę",
    "schedule_visit": "Umów wizytę",
}

# ActionProposal v2 / decision-view codes (aligned with decision_projection_blocks.V2_PROJECTION_LABEL_PL)
V2_ACTION_LABEL_PL: dict[str, str] = {
    "prepare_reply_draft": "Przygotuj odpowiedź (draft)",
    "request_missing_info": "Poproś o brakujące dane",
    "mark_attention_required": "Wymaga uwagi operatora",
    "ask_for_operator_adjudication": "Wymaga decyzji operatora",
    "no_action": "Brak akcji",
}


def operator_task_label_pl(
    *,
    action_type: str = "",
    title: str = "",
    summary: str = "",
    summary_pl: str = "",
    title_pl: str = "",
) -> str:
    """Prefer explicit PL fields; otherwise map known action codes to operator labels."""

    explicit_pl = str(title_pl or summary_pl or "").strip()
    if explicit_pl:
        return explicit_pl
    for key in (action_type, title, summary):
        code = str(key or "").strip()
        if not code:
            continue
        if code in NEXT_ACTION_PL:
            return NEXT_ACTION_PL[code]
        if code in V2_ACTION_LABEL_PL:
            return V2_ACTION_LABEL_PL[code]
    return str(summary or title or action_type or "").strip()


def _apply_task_label_pl(row: dict[str, Any]) -> dict[str, Any]:
    label = operator_task_label_pl(
        action_type=str(row.get("action_type") or row.get("recommended_operator_action") or ""),
        title=str(row.get("title") or ""),
        summary=str(row.get("summary") or ""),
        summary_pl=str(row.get("summary_pl") or ""),
        title_pl=str(row.get("title_pl") or ""),
    )
    if not label:
        return row
    out = dict(row)
    out["summary"] = label
    raw_title = str(out.get("title") or "").strip()
    code = str(out.get("action_type") or out.get("recommended_operator_action") or "").strip()
    if not raw_title or raw_title == code or raw_title in NEXT_ACTION_PL or raw_title in V2_ACTION_LABEL_PL:
        out["title"] = label
    return out

FORBIDDEN_RAW_KEYS = frozenset(
    {
        "body",
        "email_body",
        "snippet",
        "prompt",
        "prompt_text",
        "raw_llm",
        "raw_response",
        "raw_body",
        "message_body",
        "attachment_bytes",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        out = to_dict()
        return out if isinstance(out, dict) else {}
    return {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_strip_forbidden(v) for v in value if isinstance(v, dict)]


def _strip_forbidden(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _strip_forbidden(v) for k, v in value.items() if str(k) not in FORBIDDEN_RAW_KEYS}
    if isinstance(value, list):
        return [_strip_forbidden(v) for v in value]
    return value


def _case_context_contract(pack: Any, *, generated_at: str | None = None) -> dict[str, Any]:
    source = _as_dict(pack)
    if source.get("contract_name") == "CaseContextPack" and source.get("facts") is not None:
        return _strip_forbidden(source)
    return _strip_forbidden(build_case_context_pack_vnext(pack, generated_at=generated_at))


def _summary_from_contract(contract: dict[str, Any]) -> str:
    cs = contract.get("case_summary") if isinstance(contract.get("case_summary"), dict) else {}
    hs = contract.get("hot_state") if isinstance(contract.get("hot_state"), dict) else {}
    snap = hs.get("snapshot") if isinstance(hs.get("snapshot"), dict) else {}
    for value in (cs.get("summary_text"), snap.get("summary_text"), snap.get("summary")):
        text = str(value or "").strip()
        if text:
            return text[:800]
    return "No case summary available."


def _evidence_tray(contract: dict[str, Any]) -> list[dict[str, Any]]:
    out = _list_of_dicts(contract.get("evidence_cards"))
    if out:
        return out
    refs = _list_of_dicts(contract.get("source_refs"))
    rows: list[dict[str, Any]] = []
    for ref in refs:
        source_type = str(ref.get("source_type") or ref.get("type") or "").strip()
        source_id = str(ref.get("source_id") or ref.get("message_id") or ref.get("chunk_id") or "").strip()
        if source_type or source_id:
            rows.append({"source_type": source_type, "source_id": source_id, "evidence_role": "source_ref"})
    return rows


def _candidate_moves(contract: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _list_of_dicts(contract.get("proposed_next_actions")):
        row = _apply_task_label_pl(dict(item))
        row["read_only"] = True
        row["action_allowed"] = False
        rows.append(row)
    cs = contract.get("case_summary") if isinstance(contract.get("case_summary"), dict) else {}
    rec = str(cs.get("recommended_next_action") or "").strip()
    if rec and not rows:
        rows.append(
            {
                "action_type": rec,
                "summary": NEXT_ACTION_PL.get(rec, rec),
                "read_only": True,
                "action_allowed": False,
                "source": "case_context_pack.case_summary.recommended_next_action",
            }
        )
    return rows


def _facts_tray(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Current-facts projection for UI — exclude superseded (FACT-04)."""
    rows: list[dict[str, Any]] = []
    for item in _list_of_dicts(contract.get("facts")):
        if str(item.get("status") or "").strip() == "superseded":
            continue
        row = dict(item)
        if "fact_key" not in row:
            row["fact_key"] = str(row.get("predicate") or row.get("key") or "").strip()
        rows.append(row)
    return rows


def _context_quality(contract: dict[str, Any]) -> dict[str, Any]:
    quality = _strip_forbidden(contract.get("context_quality") or {})
    if isinstance(quality, dict) and "readiness_status" not in quality:
        quality = dict(quality)
        quality["readiness_status"] = str(quality.get("action_readiness") or "review_only")
    return quality if isinstance(quality, dict) else {"readiness_status": "review_only"}


def _llm_warnings(contract: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, warning in enumerate(contract.get("warnings") or []):
        text = str(warning or "").strip()
        if text:
            rows.append({"warning_code": f"context_warning_{idx + 1}", "summary": text})
    for limitation in contract.get("limitations") or []:
        text = str(limitation or "").strip()
        if not text:
            continue
        rows.append({"warning_code": text, "summary": text})
    if not any("llm" in str(row.get("warning_code", "")).lower() for row in rows):
        rows.append({"warning_code": "llm_output_not_operational_truth", "summary": "LLM output is not operational truth."})
    return rows


def _history_tray(contract: dict[str, Any], hot_state: dict[str, Any]) -> list[dict[str, Any]]:
    """messages_summary bywa dict {message_ids, recent_events} albo listą — wyciągnij eventy.

    Wcześniej `_list_of_dicts(dict)` zwracało [], przez co history_tray był pusty mimo
    istniejących eventów w packu (bug audytu WYMIAR 2.1).
    """
    msgs = contract.get("messages_summary")
    events = _list_of_dicts(msgs.get("recent_events")) if isinstance(msgs, dict) else _list_of_dicts(msgs)
    if not events:
        events = _list_of_dicts(hot_state.get("recent_events"))
    precedents = _list_of_dicts(contract.get("precedent_evidence_refs"))
    if precedents:
        events = list(events) + precedents
    return events


def build_context_tray_set(case_context_pack: Any, *, generated_at: str = "") -> dict[str, Any]:
    """Return a projection-safe ContextTraySet from an existing CaseContextPack."""

    ts = str(generated_at or "").strip() or _utc_now_iso()
    contract = _case_context_contract(case_context_pack, generated_at=ts)
    case_id = str(contract.get("case_id") or "").strip()
    source_contract = {
        "contract_name": str(contract.get("contract_name") or "CaseContextPack"),
        "schema_version": str(contract.get("schema_version") or ""),
        "contract_version": str(contract.get("contract_version") or contract.get("version") or ""),
        "pack_build": str(contract.get("pack_build") or ""),
    }
    cs = contract.get("case_summary") if isinstance(contract.get("case_summary"), dict) else {}
    hot_state = contract.get("hot_state") if isinstance(contract.get("hot_state"), dict) else {}

    essence = {
        "summary": _summary_from_contract(contract),
        "status": str(cs.get("status") or ""),
        "recommended_next_action": str(cs.get("recommended_next_action") or ""),
        "latest_signal_at": str((contract.get("runtime_state") or {}).get("latest_signal_at") or ""),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "generated_at": ts,
        "read_only": True,
        "truth_source": "node_b_mailbox_memory",
        "source_contract": source_contract,
        "context_quality": _context_quality(contract),
        "essence_tray": [essence],
        "facts_tray": _facts_tray(contract),
        "evidence_tray": _evidence_tray(contract),
        "gaps_tray": _list_of_dicts(contract.get("completeness_gaps")),
        "conflicts_tray": _list_of_dicts(contract.get("conflicting_facts")),
        "documents_tray": _list_of_dicts(contract.get("drive_documents_summary")),
        "calendar_tray": _list_of_dicts(contract.get("calendar_context")),
        "history_tray": _history_tray(contract, hot_state),
        "operator_feedback_tray": _list_of_dicts(contract.get("operator_history")),
        "candidate_moves_tray": _candidate_moves(contract),
        "llm_warnings_tray": _llm_warnings(contract),
    }


__all__ = [
    "SCHEMA_VERSION",
    "NEXT_ACTION_PL",
    "V2_ACTION_LABEL_PL",
    "build_context_tray_set",
    "operator_task_label_pl",
]
