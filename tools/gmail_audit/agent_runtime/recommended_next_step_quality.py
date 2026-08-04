"""Sharpen vague Brain1 recommended_next_step into an operator-executable action.

Does not invent facts. Rewrites only known vague patterns (manual review /
escalate_internal without concrete ask) into a single precise next step.
"""

from __future__ import annotations

import re

_VAGUE = re.compile(
    r"(r[eę]czna\s+ocena|manual\s+review|escalate_internal|wymaga\s+decyzji|"
    r"przeka[zż]\s+operatorowi|human\s+review|ocena\s+operatora)",
    re.IGNORECASE,
)


def sharpen_recommended_next_step(
    *,
    title_pl: str,
    reason_pl: str = "",
    action_type: str = "",
    case_kind: str = "",
    missing_critical_fields: list[str] | None = None,
    essence_pl: str = "",
) -> str:
    title = str(title_pl or "").strip()
    reason = str(reason_pl or "").strip()
    action = str(action_type or "").strip()
    kind = str(case_kind or "").strip().lower()
    missing = [str(x).strip() for x in (missing_critical_fields or []) if str(x).strip()]
    base = title or reason or action
    if not base:
        # Honest absence — do not invent a next step when Brain1 left NBA empty.
        return ""

    combined = f"{title} {reason} {action}"
    if not _VAGUE.search(combined):
        # Already concrete enough — keep title, append reason if distinct.
        if reason and reason.lower() not in title.lower():
            return f"{title}. {reason}"[:400] if title else reason[:400]
        return (title or reason)[:400]

    # Vague → rewrite by case class + gaps.
    if kind in {"awaria_naprawa", "przeglad_konserwacja"} or "awaria" in kind or "serwis" in kind:
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

    if kind in {"wycena_oferta", "zapytanie_klienta"} or "wycen" in kind or "ofert" in kind:
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


__all__ = ["sharpen_recommended_next_step"]
