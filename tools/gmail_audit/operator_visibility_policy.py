"""Single policy for operator-visible surfaces (desk / cases / tasks)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cieplo_orchestrator_hook import CIEPLO_DESK_INFO_BRIEF_PL

NON_BUSINESS_DESK_BUSINESS_AREAS = frozenset({"marketing", "promotional", "spam"})
NON_BUSINESS_DESK_PRECLASSIFICATION_LANES = frozenset({"no_action", "promotional"})
DESK_SUPPRESSION_REASON_NON_BUSINESS = "non_business_noise"


@dataclass(frozen=True, slots=True)
class DeskCardSpec:
    kind: str
    title: str
    summary: str
    reason: str
    priority: str


def _ensure_list(val: Any) -> list[Any]:
    return val if isinstance(val, list) else []


def should_suppress_desk_and_tasks(
    *,
    business_result: dict[str, Any] | None = None,
    preclassification_result: dict[str, Any] | None = None,
) -> bool:
    """True when desk cards and feed tasks should be hidden (case may remain in Sprawy)."""

    pre = preclassification_result if isinstance(preclassification_result, dict) else {}
    lane = str(pre.get("lane") or "").strip().lower()
    if lane in NON_BUSINESS_DESK_PRECLASSIFICATION_LANES:
        return True

    biz = business_result if isinstance(business_result, dict) else {}
    rec = str(biz.get("recommended_next_action") or "").strip().lower()
    area = str(biz.get("business_area") or "").strip().lower()
    return rec == "wait" and area in NON_BUSINESS_DESK_BUSINESS_AREAS


def suppress_projection_envelope_surfaces(envelope: dict[str, Any]) -> dict[str, Any]:
    """Strip desk/task surfaces from a projection envelope (cases unchanged downstream)."""

    out = dict(envelope)
    out["desk_cards"] = []
    out["task_candidates"] = []
    out["desk_tasks_suppressed"] = True
    out["desk_suppression_reason"] = DESK_SUPPRESSION_REASON_NON_BUSINESS
    return out


def desk_tasks_suppressed_on_routes(routes: dict[str, Any] | None) -> bool:
    routes = routes if isinstance(routes, dict) else {}
    policy = routes.get("desk_surface_policy")
    if isinstance(policy, dict) and bool(policy.get("suppressed")):
        return True
    envelope = routes.get("projection_envelope")
    if isinstance(envelope, dict) and bool(envelope.get("desk_tasks_suppressed")):
        return True
    return False


def is_cieplo_informational_feed_case(feed_case: dict[str, Any]) -> bool:
    """Cieplo orchestrator succeeded — informational Biurko card without operator action."""

    sk = str(feed_case.get("source_kind") or "").strip()
    orch = str(feed_case.get("orchestrator_status") or "").strip().lower()
    ra = feed_case.get("requires_action")
    return sk == "cieplo_orchestrated" and orch == "ok" and ra is False


def should_show_on_desk(feed_case: dict[str, Any]) -> bool:
    """True when feed_case warrants a desk card (CEL visibility policy)."""

    if is_cieplo_informational_feed_case(feed_case):
        return True

    if bool(feed_case.get("desk_tasks_suppressed")):
        return False

    badges = feed_case.get("badges") if isinstance(feed_case.get("badges"), dict) else {}
    blocking_c = bool(badges.get("blocking_conflict") or feed_case.get("has_blocking_conflicts"))
    blocking_g = bool(badges.get("blocking_gap") or feed_case.get("has_blocking_gaps"))
    needs_review = bool(badges.get("needs_operator_review"))
    status = str(feed_case.get("status") or "").lower()
    status_review = status in {"review", "waiting"}
    return bool(blocking_c or blocking_g or needs_review or status_review)


def desk_card_spec_for_case(feed_case: dict[str, Any]) -> DeskCardSpec | None:
    """Build desk card fields when should_show_on_desk; else None."""

    if is_cieplo_informational_feed_case(feed_case):
        case_title = str(feed_case.get("title") or feed_case.get("case_id") or "Lead Cieplo.app")[:200]
        brief = str(feed_case.get("operator_brief_pl") or CIEPLO_DESK_INFO_BRIEF_PL).strip()[:800]
        return DeskCardSpec(
            kind="cieplo_info",
            title=case_title,
            summary=brief,
            reason=brief,
            priority="normal",
        )

    if not should_show_on_desk(feed_case):
        return None

    badges = feed_case.get("badges") if isinstance(feed_case.get("badges"), dict) else {}
    blocking_c = bool(badges.get("blocking_conflict") or feed_case.get("has_blocking_conflicts"))
    blocking_g = bool(badges.get("blocking_gap") or feed_case.get("has_blocking_gaps"))
    status = str(feed_case.get("status") or "").lower()
    status_review = status in {"review", "waiting"}

    pri = str(feed_case.get("priority") or "normal")
    case_title = str(feed_case.get("title") or feed_case.get("case_id") or "")[:200]
    essence = str(feed_case.get("operator_essence_pl") or feed_case.get("summary") or "")[:800]
    kind = "attention"
    reason = "Sprawa wymaga Twojej uwagi na biurku."
    summary = essence
    title = case_title
    card_pri = "high" if pri in {"high", "urgent", "critical"} else "normal"

    if blocking_c:
        kind = "conflict"
        reason = "Wykryto sprzeczność w faktach sprawy."
        for c in _ensure_list(feed_case.get("conflicting_facts")):
            if isinstance(c, dict) and c.get("exclude_from_operator_projection_top"):
                continue
            summary = str(c.get("projection_summary") or c.get("summary") or essence)[:400]
            break
        title = f"Sprzeczność — {case_title}"
        card_pri = "high"
    elif blocking_g:
        kind = "gap"
        reason = "Brakuje kluczowych danych w sprawie."
        for g in _ensure_list(feed_case.get("completeness_gaps")):
            if isinstance(g, dict):
                summary = str(g.get("projection_summary") or g.get("summary") or essence)[:400]
                break
        title = f"Brak danych — {case_title}"
        card_pri = "high" if pri in {"high", "urgent"} else "normal"
    elif status_review:
        kind = "review_needed"
        reason = f"Status sprawy: {status}"
        title = f"Do przeglądu — {case_title}"
        card_pri = "high"

    return DeskCardSpec(
        kind=kind,
        title=title[:300],
        summary=(summary or essence)[:800],
        reason=reason[:800],
        priority=card_pri,
    )


def apply_desk_composition_visibility(feed_case: dict[str, Any], desk_composition: dict[str, Any] | None) -> None:
    """Align feed_case badges with case_intelligence desk_composition (single visibility policy)."""

    if not isinstance(feed_case, dict):
        return
    desk = desk_composition if isinstance(desk_composition, dict) else {}
    if bool(desk.get("desk_tasks_suppressed")):
        feed_case["desk_tasks_suppressed"] = True
        reason = str(desk.get("desk_suppression_reason") or DESK_SUPPRESSION_REASON_NON_BUSINESS).strip()
        if reason:
            feed_case["desk_suppression_reason"] = reason
        badges = feed_case.get("badges")
        if not isinstance(badges, dict):
            badges = {}
            feed_case["badges"] = badges
        badges["needs_operator_review"] = False
        feed_case["surface_zone"] = str(desk.get("surface_zone") or "case_only")
        return
    zone = surface_zone_from_desk_composition(desk_composition)
    badges = feed_case.get("badges")
    if not isinstance(badges, dict):
        badges = {}
        feed_case["badges"] = badges
    if zone == "silent":
        badges["needs_operator_review"] = False
        badges["blocking_conflict"] = False
        badges["blocking_gap"] = False
        return
    if zone == "desk":
        badges["needs_operator_review"] = True
    elif zone == "day":
        badges["needs_operator_review"] = True
    feed_case["surface_zone"] = zone


def surface_zone_from_desk_composition(desk_composition: dict[str, Any] | None) -> str:
    """Map case_intelligence desk_composition to surface_zone string."""

    desk = desk_composition if isinstance(desk_composition, dict) else {}
    zone = str(desk.get("surface_zone") or "").strip().lower()
    if zone in {"desk", "day", "case_only", "silent"}:
        return zone
    if not bool(desk.get("should_surface")):
        return "silent"
    return "desk"


__all__ = [
    "DESK_SUPPRESSION_REASON_NON_BUSINESS",
    "DeskCardSpec",
    "NON_BUSINESS_DESK_BUSINESS_AREAS",
    "NON_BUSINESS_DESK_PRECLASSIFICATION_LANES",
    "apply_desk_composition_visibility",
    "desk_card_spec_for_case",
    "desk_tasks_suppressed_on_routes",
    "is_cieplo_informational_feed_case",
    "should_show_on_desk",
    "should_suppress_desk_and_tasks",
    "suppress_projection_envelope_surfaces",
    "surface_zone_from_desk_composition",
]
