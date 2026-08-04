"""Recommended-next-step quality (roadmap 1.3).

Turns vague Brain1 NBA (ręczna ocena / escalate_internal / escalate_review)
into one concrete, operator-executable next step — without inventing facts.

Applied at Understanding source (`build_understanding_output`) and again at
planner projection for defense in depth.
"""

from __future__ import annotations

import re
from typing import Any

_VAGUE = re.compile(
    r"(r[eę]czna\s+ocena|manual\s+review|escalate_internal|escalate_review|"
    r"wymaga\s+decyzji|wymaga\s+oceny|przeka[zż]\s+operatorowi|human\s+review|"
    r"ocena\s+operatora|ocena\s+r[eę]czna|do\s+weryfikacji\s+operatora|"
    r"\bunknown\b|\bignore\b|\bwait\b)",
    re.IGNORECASE,
)

_SALES_KINDS = frozenset(
    {"wycena_oferta", "zapytanie_klienta", "lead_opportunity", "sales", "oferta"}
)
_SERVICE_KINDS = frozenset(
    {
        "awaria_naprawa",
        "przeglad_konserwacja",
        "reklamacja",
        "serwis",
        "service",
        "complaint",
    }
)
_ADMIN_KINDS = frozenset(
    {
        "faktura_sprzedaz",
        "faktura_zakup",
        "ksiegowosc",
        "zakupy_materialow",
        "szkolenie",
        "accounting",
        "invoice",
    }
)


def is_vague_next_step(text: str) -> bool:
    return bool(_VAGUE.search(str(text or "")))


def normalize_case_kind(*, case_kind: str = "", business_area: str = "", case_family: str = "") -> str:
    raw = " ".join(
        str(x or "").strip().lower() for x in (case_kind, business_area, case_family) if x
    )
    if any(k in raw for k in ("awaria", "serwis", "reklam", "napraw", "przeglad", "service")):
        return "awaria_naprawa"
    if any(k in raw for k in ("wycen", "ofert", "lead", "sales", "zapytanie")):
        return "wycena_oferta"
    if any(k in raw for k in ("faktur", "ksieg", "invoice", "account", "zakup", "szkolen")):
        return "ksiegowosc"
    return str(case_kind or case_family or business_area or "").strip().lower()


def planner_action_hint(
    *,
    sharpened_pl: str,
    case_kind: str = "",
    missing_critical_fields: list[str] | None = None,
) -> str:
    """Map sharpened next step to a preferred tool class (not a hard tool binding)."""
    text = str(sharpened_pl or "").lower()
    kind = normalize_case_kind(case_kind=case_kind)
    missing = [str(x).strip() for x in (missing_critical_fields or []) if str(x).strip()]
    if "report_gaps" in text or (missing and "tylko o" in text):
        if "generate_draft" in text or "draft" in text:
            return "generate_draft_reply"
        return "request_operator_clarification"
    if "call_kalk" in text or "wycen" in text:
        return "call_kalk_top_quote_or_draft"
    if "serwis" in text or kind in _SERVICE_KINDS:
        if "draft" in text:
            return "generate_draft_reply"
        return "request_operator_clarification"
    if "generate_draft" in text or "draft" in text:
        return "generate_draft_reply"
    if "clarification" in text or "dopytaj" in text or "decyz" in text:
        return "request_operator_clarification"
    if kind in _SALES_KINDS:
        return "generate_draft_reply"
    return "request_operator_clarification"


def sharpen_recommended_next_step(
    *,
    title_pl: str,
    reason_pl: str = "",
    action_type: str = "",
    case_kind: str = "",
    business_area: str = "",
    case_family: str = "",
    missing_critical_fields: list[str] | None = None,
    essence_pl: str = "",
    what_changed_pl: str = "",
) -> str:
    title = str(title_pl or "").strip()
    reason = str(reason_pl or "").strip()
    action = str(action_type or "").strip()
    kind = normalize_case_kind(
        case_kind=case_kind, business_area=business_area, case_family=case_family
    )
    missing = [str(x).strip() for x in (missing_critical_fields or []) if str(x).strip()]
    changed = str(what_changed_pl or "").strip()
    base = title or reason or action
    if not base:
        # Honest absence — do not invent a next step when Brain1 left NBA empty.
        return ""

    combined = f"{title} {reason} {action}"
    if not is_vague_next_step(combined):
        # Concrete NBA stays concrete — do not rewrite with thread-delta prefixes.
        if reason and reason.lower() not in title.lower():
            return f"{title}. {reason}"[:400] if title else reason[:400]
        return (title or reason)[:400]

    # Follow-up with detected change: never ignore the delta.
    if changed:
        if kind in _SERVICE_KINDS or "awaria" in kind:
            return (
                f"Follow-up serwis: klient zgłosił zmianę («{changed[:100]}») — "
                "potwierdź wpływ na naprawę/termin, draft serwisowy bez metrażu/OZC, HITL."
            )[:400]
        if kind in _SALES_KINDS or "wycen" in kind:
            return (
                f"Follow-up oferta: uwzględnij zmianę («{changed[:100]}») — "
                "zaktualizuj profil/wycenę gdy tool dostępny, inaczej generate_draft_reply(intent=quote), HITL."
            )[:400]
        return (
            f"Follow-up: uwzględnij zmianę («{changed[:100]}») — jedna konkretna akcja "
            "(draft / clarification / stop), bez ogólnego escalate_internal."
        )[:400]

    if kind in _SERVICE_KINDS or "awaria" in kind or "serwis" in kind:
        if missing:
            return (
                "Serwis: dopytaj o objaw/urządzenie ("
                + ", ".join(missing[:3])
                + "), przygotuj draft serwisowy bez metrażu/OZC, HITL approve."
            )
        return (
            "Serwis: potwierdź urządzenie i objaw z wątku, przygotuj draft serwisowy "
            "lub request_operator_clarification z jedną konkretną decyzją (termin/część)."
        )

    if kind in _SALES_KINDS or "wycen" in kind or "ofert" in kind:
        if missing:
            return (
                "Oferta: użyj znanych faktów z profilu; dopytaj TYLKO o: "
                + ", ".join(missing[:3])
                + "; potem call_kalk_top_quote (jeśli dostępne) i generate_draft_reply(intent=quote)."
            )
        return (
            "Oferta: nie pytaj ponownie o znany metraż/miasto; policz wycenę gdy narzędzie dostępne, "
            "inaczej generate_draft_reply(intent=quote) i HITL."
        )

    if kind in _ADMIN_KINDS or "faktur" in kind or "ksieg" in kind:
        return (
            "Administracja: nie prowadź HVAC — podsumuj kwotę/stronę w "
            "request_operator_clarification i zatrzymaj (report_gaps_and_stop jeśli brak decyzji)."
        )

    if missing:
        return (
            "Operator: jedna decyzja — uzupełnij luki ("
            + ", ".join(missing[:4])
            + ") albo zatwierdź draft; unikaj ogólnego escalate_internal."
        )

    essence = str(essence_pl or "").strip()
    if essence:
        return (
            f"Operator: na podstawie «{essence[:120]}» podejmij jedną konkretną akcję "
            f"(draft / clarification / stop) zamiast ogólnej ręcznej oceny."
        )[:400]

    return (
        "Operator: zamień ogólną ręczną ocenę na jedną akcję — "
        "generate_draft_reply, request_operator_clarification (z konkretnym ask_pl) "
        "albo report_gaps_and_stop."
    )[:400]


def apply_nba_quality_to_understanding(
    understanding: dict[str, Any] | None,
    *,
    case_kind: str = "",
    business_area: str = "",
) -> dict[str, Any]:
    """Sharpen NBA title in-place on UnderstandingOutput; preserve raw title."""
    uo = dict(understanding) if isinstance(understanding, dict) else {}
    nba = uo.get("next_best_action_recommendation")
    if not isinstance(nba, dict) or not nba:
        return uo
    nba = dict(nba)
    oe = uo.get("operator_explanation") if isinstance(uo.get("operator_explanation"), dict) else {}
    delta = uo.get("thread_delta") if isinstance(uo.get("thread_delta"), dict) else {}
    cu = uo.get("case_understanding") if isinstance(uo.get("case_understanding"), dict) else {}
    ss = uo.get("situation_summary") if isinstance(uo.get("situation_summary"), dict) else {}
    family = str(
        case_kind
        or cu.get("case_family")
        or ss.get("case_family")
        or uo.get("case_family")
        or ""
    )
    area = str(business_area or ss.get("business_area") or cu.get("business_area") or "")
    raw_title = str(nba.get("title_pl") or "")
    raw_reason = str(nba.get("reason_pl") or "")
    sharpened = sharpen_recommended_next_step(
        title_pl=raw_title,
        reason_pl=raw_reason,
        action_type=str(nba.get("action_type") or nba.get("recommended_action") or ""),
        case_kind=family,
        business_area=area,
        case_family=family,
        missing_critical_fields=[str(x) for x in (uo.get("missing_critical_fields") or [])[:6]],
        essence_pl=str(oe.get("essence_pl") or uo.get("situation_summary_pl") or ""),
        what_changed_pl=str(delta.get("operator_visible_delta_summary") or ""),
    )
    if sharpened and sharpened != raw_title:
        if not nba.get("title_pl_raw"):
            nba["title_pl_raw"] = raw_title
        nba["title_pl"] = sharpened[:400]
        nba["quality"] = {
            "sharpened": True,
            "was_vague": is_vague_next_step(f"{raw_title} {raw_reason}"),
            "planner_action_hint": planner_action_hint(
                sharpened_pl=sharpened,
                case_kind=family,
                missing_critical_fields=[str(x) for x in (uo.get("missing_critical_fields") or [])[:6]],
            ),
        }
    elif sharpened:
        nba["quality"] = {
            "sharpened": False,
            "was_vague": False,
            "planner_action_hint": planner_action_hint(
                sharpened_pl=sharpened,
                case_kind=family,
                missing_critical_fields=[str(x) for x in (uo.get("missing_critical_fields") or [])[:6]],
            ),
        }
    uo["next_best_action_recommendation"] = nba
    return uo


__all__ = [
    "apply_nba_quality_to_understanding",
    "is_vague_next_step",
    "normalize_case_kind",
    "planner_action_hint",
    "sharpen_recommended_next_step",
]
