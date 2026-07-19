"""ProjectionEnvelope builder for Daszek and Skrzat read-only projection paths."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from context_tray_set import FORBIDDEN_RAW_KEYS


SCHEMA_VERSION = "projection_envelope.v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _first_summary(rows: list[dict[str, Any]], *, fallback: str = "") -> str:
    for row in rows:
        for key in ("summary", "summary_text", "content_pl", "headline_co_pl", "title"):
            text = str(row.get(key) or "").strip()
            if text:
                return text[:700]
    return fallback


def _evidence_split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    used: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for row in rows:
        source_id = str(row.get("source_id") or row.get("message_id") or row.get("chunk_id") or "").strip()
        if source_id:
            used.append(row)
        else:
            ignored.append({"reason": "missing_source_id", **row})
    return used[:24], ignored[:24]


def _task_candidates(rows: list[dict[str, Any]], decision_view: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows + _list_of_dicts(decision_view.get("action_proposals")):
        out = dict(row)
        out["read_only"] = True
        out["action_allowed"] = False
        candidates.append(out)
    return candidates[:12]


def build_projection_envelope(
    context_tray_set: dict[str, Any],
    *,
    decision_view: dict[str, Any] | None = None,
    v2_projection: dict[str, Any] | None = None,
    generated_at: str = "",
) -> dict[str, Any]:
    """Build a read-only projection envelope from ContextTraySet and legacy projections."""

    trays = context_tray_set if isinstance(context_tray_set, dict) else {}
    dv = decision_view if isinstance(decision_view, dict) else {}
    v2 = v2_projection if isinstance(v2_projection, dict) else {}
    ts = str(generated_at or trays.get("generated_at") or "").strip() or _utc_now_iso()
    case_id = str(trays.get("case_id") or "").strip()
    essence_rows = _list_of_dicts(trays.get("essence_tray"))
    summary = str(dv.get("headline_co_pl") or _first_summary(essence_rows, fallback="No case summary available.")).strip()
    evidence_blocks = _list_of_dicts(trays.get("evidence_tray")) + _list_of_dicts(dv.get("evidence_cards"))
    evidence_used, evidence_ignored = _evidence_split(evidence_blocks)
    gaps = _list_of_dicts(trays.get("gaps_tray"))
    conflicts = _list_of_dicts(trays.get("conflicts_tray"))
    warnings = _list_of_dicts(trays.get("llm_warnings_tray"))

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "generated_at": ts,
        "read_only": True,
        "action_allowed": False,
        "source_context_schema": str(trays.get("schema_version") or ""),
        "desk_cards": [{"card_type": "case_essence", "title": "Case", "summary": summary[:500], "case_id": case_id}],
        "case_detail_blocks": [
            {"block_type": "essence", "content": summary[:900], "source": "context_tray_set.essence_tray"},
            {"block_type": "facts", "items": _list_of_dicts(trays.get("facts_tray"))[:12]},
        ],
        "task_candidates": _task_candidates(_list_of_dicts(trays.get("candidate_moves_tray")), dv),
        "gap_blocks": gaps,
        "conflict_blocks": conflicts,
        "risk_blocks": conflicts[:6],
        "evidence_blocks": evidence_blocks[:32],
        "audit_blocks": [
            {
                "block_type": "projection_lineage",
                "context_schema": str(trays.get("schema_version") or ""),
                "has_decision_view": bool(dv),
                "has_v2_projection": bool(v2),
            }
        ],
        "warnings": warnings,
        "evidence_used": evidence_used,
        "evidence_ignored": evidence_ignored,
        "legacy": {
            "decision_view": _strip_forbidden(dv),
            "v2_projection": _strip_forbidden(v2),
        },
    }


__all__ = ["SCHEMA_VERSION", "build_projection_envelope"]
