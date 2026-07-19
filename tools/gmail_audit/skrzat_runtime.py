"""Read-only Skrzat case assistant runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "conversation_answer_envelope.v1"
ALLOWED_MODES = frozenset({"ask", "investigate", "case_copilot"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def _summary(rows: list[dict[str, Any]], *keys: str) -> str:
    for row in rows:
        for key in keys or ("summary", "summary_text", "content", "value"):
            text = str(row.get(key) or "").strip()
            if text:
                return text[:500]
    return ""


def answer_case_question(
    context_tray_set: dict[str, Any],
    *,
    question: str,
    mode: str = "ask",
    generated_at: str = "",
) -> dict[str, Any]:
    """Return a deterministic read-only Skrzat answer envelope from ContextTraySet."""

    trays = context_tray_set if isinstance(context_tray_set, dict) else {}
    requested_mode = str(mode or "ask").strip().lower()
    warnings = list(_list_of_dicts(trays.get("llm_warnings_tray")))
    if requested_mode not in ALLOWED_MODES:
        warnings.append({"warning_code": "unsupported_mode_fallback", "summary": f"Unsupported mode: {requested_mode}"})
        requested_mode = "ask"

    essence = _summary(_list_of_dicts(trays.get("essence_tray")), "summary", "summary_text")
    gap = _summary(_list_of_dicts(trays.get("gaps_tray")), "summary", "content_pl")
    conflict = _summary(_list_of_dicts(trays.get("conflicts_tray")), "summary", "summary_pl", "field_name")
    evidence = _list_of_dicts(trays.get("evidence_tray"))[:8]
    parts = [f"Sprawa: {essence or 'brak skrotu sprawy'}."]
    if gap:
        parts.append(f"Braki: {gap}.")
    if conflict:
        parts.append(f"Konflikty: {conflict}.")
    if evidence:
        parts.append("Odpowiedz oparta na dostepnych dowodach z tacki evidence.")
    parts.append("Tryb read-only: Skrzat niczego nie wykonuje i nie zmienia prawdy operacyjnej.")

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": str(trays.get("case_id") or ""),
        "generated_at": str(generated_at or "").strip() or _utc_now_iso(),
        "mode": requested_mode,
        "question": str(question or "").strip()[:1000],
        "answer_text": " ".join(parts),
        "evidence": evidence,
        "gaps": _list_of_dicts(trays.get("gaps_tray"))[:8],
        "conflicts": _list_of_dicts(trays.get("conflicts_tray"))[:8],
        "warnings": warnings[:12],
        "candidate_moves": _list_of_dicts(trays.get("candidate_moves_tray"))[:8],
        "read_only": True,
        "action_allowed": False,
    }


__all__ = ["ALLOWED_MODES", "SCHEMA_VERSION", "answer_case_question"]
