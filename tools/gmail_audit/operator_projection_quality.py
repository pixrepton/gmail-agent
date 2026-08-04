"""Operator-facing projection helpers for Understanding quality and readiness facets."""

from __future__ import annotations

from typing import Any


def _provenance_from_intelligence(intelligence_result: dict[str, Any] | None) -> dict[str, Any] | None:
    intel = intelligence_result if isinstance(intelligence_result, dict) else {}
    meta = intel.get("execution_metadata")
    if not isinstance(meta, dict):
        return None
    provenance = meta.get("case_understanding_provenance")
    return provenance if isinstance(provenance, dict) and provenance else None


def _operator_label_pl(*, availability: str, source_mode: str, validation_state: str) -> str:
    if availability == "not_required" or source_mode == "skipped_for_lane":
        return "Rozumowanie niewymagane dla tej ścieżki"
    if availability == "unavailable":
        return "Rozumienie sprawy niedostępne"
    if availability != "available":
        return ""
    if source_mode == "fallback":
        return "Rozumienie zastępcze (fallback)"
    if validation_state == "corrected":
        return "Rozumienie dostępne po korekcie"
    if source_mode in {"model_result", "normalized_model_result"}:
        return "Pełne rozumienie sprawy"
    return "Rozumienie sprawy dostępne"


def _operator_detail_pl(*, source_mode: str, validation_state: str, reason_codes: list[str]) -> str:
    parts: list[str] = []
    if source_mode:
        parts.append(f"tryb: {source_mode}")
    if validation_state:
        parts.append(f"walidacja: {validation_state}")
    if reason_codes:
        parts.append(f"powody: {', '.join(str(c) for c in reason_codes[:3])}")
    return "; ".join(parts)[:400]


def build_understanding_quality_projection(
    intelligence_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Project SLICE-3A provenance for Daszek/X1 without inventing membership semantics."""
    provenance = _provenance_from_intelligence(intelligence_result)
    if not provenance:
        return None
    availability = str(provenance.get("availability") or "").strip()
    if not availability:
        return None
    source_mode = str(provenance.get("source_mode") or "").strip()
    validation_state = str(provenance.get("validation_state") or "").strip()
    reason_codes = [str(code)[:120] for code in (provenance.get("reason_codes") or [])[:6]]
    return {
        "schema_version": str(provenance.get("schema_version") or "v1"),
        "availability": availability,
        "source_mode": source_mode,
        "validation_state": validation_state,
        "reason_codes": reason_codes,
        "observed_at": str(provenance.get("observed_at") or "").strip(),
        "operator_label_pl": _operator_label_pl(
            availability=availability,
            source_mode=source_mode,
            validation_state=validation_state,
        ),
        "operator_detail_pl": _operator_detail_pl(
            source_mode=source_mode,
            validation_state=validation_state,
            reason_codes=reason_codes,
        ),
    }


def _readiness_operator_label_pl(context_readiness: str, *, blocked_by_data: bool) -> str:
    if blocked_by_data or context_readiness == "not_ready":
        return "Zablokowane brakami danych"
    if context_readiness == "decision_ready":
        return "Gotowa do decyzji"
    return "Wymaga przeglądu operatora"


def build_readiness_facets_projection(
    intelligence_result: dict[str, Any] | None,
    *,
    projection_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derived readiness for v2 dash patches — mirrors projection_envelope semantics."""
    intel = intelligence_result if isinstance(intelligence_result, dict) else {}
    state = projection_state if isinstance(projection_state, dict) else {}
    cg = intel.get("case_guidance") if isinstance(intel.get("case_guidance"), dict) else {}
    case_understanding = intel.get("case_understanding") if isinstance(intel.get("case_understanding"), dict) else {}
    missing_info = intel.get("missing_info") if isinstance(intel.get("missing_info"), dict) else {}
    critical = list(missing_info.get("critical") or [])
    important = list(missing_info.get("important") or [])
    conflicts = list(state.get("conflicting_facts") or [])
    completeness_gaps = list(state.get("completeness_gaps") or []) if "completeness_gaps" in state else []

    business_readiness = str(cg.get("business_readiness") or "").strip()
    review_required = bool(case_understanding.get("review_required", False))
    blocked = bool(critical) or review_required

    if business_readiness in {"ready_for_offer", "ready_for_decision", "ready"}:
        context_readiness = "decision_ready"
    elif blocked:
        context_readiness = "not_ready"
    else:
        context_readiness = "review_only"

    gap_count = len(critical) + len(important) + len(completeness_gaps)
    conflict_count = len(conflicts)
    blocked_by_data = blocked or context_readiness == "not_ready"

    return {
        "context_readiness": context_readiness,
        "ready_for_decision": context_readiness == "decision_ready",
        "ready_for_operator_review": context_readiness == "review_only",
        "blocked_by_data": blocked_by_data,
        "policy_status": "",
        "gap_count": gap_count,
        "conflict_count": conflict_count,
        "operator_label_pl": _readiness_operator_label_pl(
            context_readiness,
            blocked_by_data=blocked_by_data,
        ),
    }


__all__ = [
    "build_readiness_facets_projection",
    "build_understanding_quality_projection",
]
