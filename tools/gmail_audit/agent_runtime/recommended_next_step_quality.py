"""Recommended-next-step quality (roadmap 1.3) + IQ-01 decision-state quality.

Turns vague Brain1 NBA (ręczna ocena / escalate_internal / escalate_review)
into one concrete, operator-executable next step — without inventing facts.

IQ-01 adds a bounded Understanding→Decision classifier: every evaluated case
must land in exactly one clear decision state (execute / approve / reply /
complete-info / wait / close / consciously-do-nothing), with measurable checks
for non-vague next step, follow-up delta (FU-06/07), and gaps-vs-risks separation.

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

# IQ-01 canonical decision states (roadmap Done-when).
DECISION_STATE_EXECUTE = "execute"
DECISION_STATE_APPROVE = "approve"
DECISION_STATE_REPLY = "reply"
DECISION_STATE_COMPLETE_INFO = "complete-info"
DECISION_STATE_WAIT = "wait"
DECISION_STATE_CLOSE = "close"
DECISION_STATE_CONSCIOUSLY_DO_NOTHING = "consciously-do-nothing"

DECISION_STATES = frozenset(
    {
        DECISION_STATE_EXECUTE,
        DECISION_STATE_APPROVE,
        DECISION_STATE_REPLY,
        DECISION_STATE_COMPLETE_INFO,
        DECISION_STATE_WAIT,
        DECISION_STATE_CLOSE,
        DECISION_STATE_CONSCIOUSLY_DO_NOTHING,
    }
)

# Continuity-only / canned delta summaries are not FU-06/07 "real change".
_CONTINUITY_DELTA_MARKERS = (
    "wczesniej ustalone dane pozostaja",
    "wcześniej ustalone dane pozostają",
    "nie zostaly zmienione ani odwolane",
    "nie zostały zmienione ani odwołane",
    "nowy temat operacyjny",
    "trafil do recznej oceny",
    "trafił do ręcznej oceny",
)

_UNRESOLVED_QUESTION_MARKERS = (
    "niezalatwione pytanie",
    "niezałatwione pytanie",
    "unresolved_question",
    "wymagajace odpowiedzi",
    "wymagające odpowiedzi",
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


def _risk_summaries(risks: list[Any] | None) -> list[str]:
    out: list[str] = []
    for item in risks or []:
        if isinstance(item, dict):
            text = str(
                item.get("summary_pl")
                or item.get("summary")
                or item.get("risk_type")
                or ""
            ).strip()
        else:
            text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _has_unresolved_question_signal(
    *,
    risks: list[Any] | None = None,
    open_loops: list[Any] | None = None,
    essence_pl: str = "",
) -> bool:
    blob = " ".join(_risk_summaries(risks) + [str(x) for x in (open_loops or [])] + [essence_pl]).lower()
    return any(m in blob for m in _UNRESOLVED_QUESTION_MARKERS) or any(
        "pytan" in str(x).lower() for x in (open_loops or [])
    )


def is_meaningful_follow_up_delta(
    *,
    what_changed_pl: str = "",
    thread_delta: dict[str, Any] | None = None,
    risks: list[Any] | None = None,
    open_loops: list[Any] | None = None,
) -> bool:
    """True when FU-06/07-style movement exists (not continuity-only canned text)."""
    delta = thread_delta if isinstance(thread_delta, dict) else {}
    changed = str(what_changed_pl or delta.get("operator_visible_delta_summary") or "").strip()
    changes = delta.get("changes") if isinstance(delta.get("changes"), list) else []
    new_facts = delta.get("new_facts") if isinstance(delta.get("new_facts"), list) else []
    if changes or any(str(x).strip() for x in new_facts):
        return True
    if _has_unresolved_question_signal(risks=risks, open_loops=open_loops):
        return True
    if not changed:
        return False
    low = changed.lower()
    if any(m in low for m in _CONTINUITY_DELTA_MARKERS):
        return False
    return len(changed) >= 12


def separate_gaps_vs_risks(
    *,
    missing_critical_fields: list[Any] | None = None,
    risks: list[Any] | None = None,
    open_loops: list[Any] | None = None,
) -> dict[str, Any]:
    """Gaps = data checklist; risks = threat/uncertainty. Overlap is a quality failure."""
    gaps = [str(x).strip() for x in (missing_critical_fields or []) if str(x).strip()]
    risk_texts = _risk_summaries(risks)
    loops = [str(x).strip() for x in (open_loops or []) if str(x).strip()]
    gap_set = {g.lower() for g in gaps}
    risk_set = {r.lower() for r in risk_texts}
    overlap = sorted(gap_set & risk_set)
    # Unresolved customer questions must live in risks/open_loops, not gaps.
    questionish_gaps = [
        g
        for g in gaps
        if any(m in g.lower() for m in ("?", "pytan", "unresolved"))
    ]
    return {
        "gaps": gaps[:12],
        "risks": risk_texts[:12],
        "open_loops": loops[:12],
        "overlap": overlap[:8],
        "questionish_gaps": questionish_gaps[:8],
        "separated": not overlap and not questionish_gaps,
    }


def classify_decision_state(
    *,
    sharpened_pl: str = "",
    action_type: str = "",
    case_kind: str = "",
    missing_critical_fields: list[str] | None = None,
    risks: list[Any] | None = None,
    open_loops: list[Any] | None = None,
    lifecycle_hint: str = "",
    policy_allowed: bool | None = None,
    hitl_required: bool | None = None,
    draft_ready: bool | None = None,
) -> str:
    """Map Understanding + sharpened NBA to exactly one IQ-01 decision state."""
    text = f"{sharpened_pl} {action_type}".lower()
    kind = normalize_case_kind(case_kind=case_kind)
    missing = [str(x).strip() for x in (missing_critical_fields or []) if str(x).strip()]
    life = str(lifecycle_hint or "").strip().lower()

    if life in {"closed", "merged", "cancelled", "terminal"} or any(
        t in text for t in ("zamknij spraw", "close case", "archiwizuj")
    ):
        return DECISION_STATE_CLOSE

    if life in {"waiting_client", "waiting"} or any(
        t in text for t in ("czekaj na klienta", "waiting_client", "sla wait", "oczekuj na odpowiedź klienta")
    ):
        return DECISION_STATE_WAIT

    if any(
        t in text
        for t in (
            "świadomie nie działaj",
            "consciously do-nothing",
            "consciously do nothing",
            "brak akcji",
            "nie prowadź hvac",
            "report_gaps_and_stop",
        )
    ) or (kind in _ADMIN_KINDS and "draft" not in text and "dopytaj" not in text):
        if kind in _ADMIN_KINDS or "stop" in text or "nie działaj" in text or "brak akcji" in text:
            return DECISION_STATE_CONSCIOUSLY_DO_NOTHING
        if "report_gaps_and_stop" in text and "draft" not in text:
            return DECISION_STATE_CONSCIOUSLY_DO_NOTHING

    # Approve only when the operator already has a draft to sign off — not when a
    # prepare-draft template merely ends with "HITL".
    stripped = str(sharpened_pl or "").strip().lower()
    if draft_ready is True or stripped.startswith("zatwierdź") or stripped.startswith("zatwierdz"):
        return DECISION_STATE_APPROVE
    if "draft gotowy" in stripped and ("hitl" in stripped or "zatwierdz" in stripped):
        return DECISION_STATE_APPROVE

    if any(
        t in text
        for t in (
            "call_kalk",
            "wykonaj",
            "uruchom narzędzie",
            "execute",
            "policz wycenę",
            "policz wycene",
        )
    ) and policy_allowed is not False:
        if missing and "tylko o" in text:
            return DECISION_STATE_COMPLETE_INFO
        return DECISION_STATE_EXECUTE

    if missing and (
        "dopytaj" in text
        or "tylko o" in text
        or "uzupełnij" in text
        or "uzupelnij" in text
        or "request_operator_clarification" in text
        or "complete-info" in text
        or "uzupełnij luki" in text
        or "uzupelnij luki" in text
    ):
        return DECISION_STATE_COMPLETE_INFO

    if missing and not _has_unresolved_question_signal(risks=risks, open_loops=open_loops):
        # Data gaps without a customer question → complete-info by default.
        if "draft" not in text or "dopytaj" in text or "uzupełnij" in text or "uzupelnij" in text:
            return DECISION_STATE_COMPLETE_INFO

    if any(
        t in text
        for t in (
            "generate_draft",
            "draft",
            "odpowiedz",
            "odpowiedź",
            "reply",
            "follow-up",
            "follow_up",
        )
    ) or _has_unresolved_question_signal(risks=risks, open_loops=open_loops):
        return DECISION_STATE_REPLY

    if kind in _SALES_KINDS:
        return DECISION_STATE_REPLY if not missing else DECISION_STATE_COMPLETE_INFO
    if kind in _SERVICE_KINDS:
        return DECISION_STATE_REPLY if not missing else DECISION_STATE_COMPLETE_INFO
    if kind in _ADMIN_KINDS:
        return DECISION_STATE_CONSCIOUSLY_DO_NOTHING

    # Honest fallback: operator must still pick one concrete state — prefer reply over vague.
    return DECISION_STATE_REPLY


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
    risks: list[Any] | None = None,
    open_loops: list[Any] | None = None,
    thread_delta: dict[str, Any] | None = None,
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
    meaningful_delta = is_meaningful_follow_up_delta(
        what_changed_pl=changed,
        thread_delta=thread_delta,
        risks=risks,
        open_loops=open_loops,
    )
    has_question = _has_unresolved_question_signal(
        risks=risks, open_loops=open_loops, essence_pl=essence_pl
    )

    if not is_vague_next_step(combined):
        # Concrete NBA stays concrete — do not rewrite with thread-delta prefixes.
        if reason and reason.lower() not in title.lower():
            return f"{title}. {reason}"[:400] if title else reason[:400]
        return (title or reason)[:400]

    # Admin/finance first: never rewrite invoice/document noise into HVAC follow-up.
    if kind in _ADMIN_KINDS or "faktur" in kind or "ksieg" in kind:
        return (
            "Administracja: nie prowadź HVAC — podsumuj kwotę/stronę w "
            "request_operator_clarification i zatrzymaj (report_gaps_and_stop jeśli brak decyzji)."
        )

    # FU-06/07: follow-up with unresolved question or real delta — never leave escalate.
    if meaningful_delta or has_question:
        if has_question:
            return (
                "Follow-up: odpowiedz na pytanie klienta z wątku (generate_draft_reply), "
                "bez ponownego pytania o znane fakty; potem HITL."
            )[:400]
        if kind in _SERVICE_KINDS or "awaria" in kind:
            return (
                f"Follow-up serwis: klient zgłosił zmianę («{changed[:100] or 'delta wątku'}») — "
                "potwierdź wpływ na naprawę/termin, draft serwisowy bez metrażu/OZC, potem HITL."
            )[:400]
        if kind in _SALES_KINDS or "wycen" in kind:
            return (
                f"Follow-up oferta: uwzględnij zmianę («{changed[:100] or 'delta wątku'}») — "
                "zaktualizuj profil/wycenę gdy tool dostępny, inaczej generate_draft_reply(intent=quote), potem HITL."
            )[:400]
        return (
            f"Follow-up: uwzględnij zmianę («{changed[:100] or 'delta wątku'}») — jedna konkretna akcja "
            "(draft / clarification / stop), bez ogólnej eskalacji."
        )[:400]

    # Follow-up with detected change text (legacy path when not classified meaningful).
    if changed and not any(m in changed.lower() for m in _CONTINUITY_DELTA_MARKERS):
        if kind in _SERVICE_KINDS or "awaria" in kind:
            return (
                f"Follow-up serwis: klient zgłosił zmianę («{changed[:100]}») — "
                "potwierdź wpływ na naprawę/termin, draft serwisowy bez metrażu/OZC, potem HITL."
            )[:400]
        if kind in _SALES_KINDS or "wycen" in kind:
            return (
                f"Follow-up oferta: uwzględnij zmianę («{changed[:100]}») — "
                "zaktualizuj profil/wycenę gdy tool dostępny, inaczej generate_draft_reply(intent=quote), potem HITL."
            )[:400]
        return (
            f"Follow-up: uwzględnij zmianę («{changed[:100]}») — jedna konkretna akcja "
            "(draft / clarification / stop), bez ogólnej eskalacji."
        )[:400]

    if kind in _SERVICE_KINDS or "awaria" in kind or "serwis" in kind:
        if missing:
            return (
                "Serwis: dopytaj o objaw/urządzenie ("
                + ", ".join(missing[:3])
                + "), przygotuj draft serwisowy bez metrażu/OZC, potem HITL."
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

    if missing:
        return (
            "Operator: jedna decyzja — uzupełnij luki ("
            + ", ".join(missing[:4])
            + ") albo przygotuj draft do HITL; unikaj ogólnej eskalacji."
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


def evaluate_understanding_to_decision_quality(
    understanding: dict[str, Any] | None,
    *,
    case_id: str = "",
    case_kind: str = "",
    business_area: str = "",
    expected_decision_state: str = "",
    lifecycle_hint: str = "",
    policy_allowed: bool | None = None,
    hitl_required: bool | None = None,
    draft_ready: bool | None = None,
    require_follow_up_delta: bool = False,
) -> dict[str, Any]:
    """Score one Understanding blob for IQ-01 metrics (PASS/FAIL, no secrets)."""
    uo = dict(understanding) if isinstance(understanding, dict) else {}
    nba = uo.get("next_best_action_recommendation") if isinstance(uo.get("next_best_action_recommendation"), dict) else {}
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
    missing = [str(x) for x in (uo.get("missing_critical_fields") or []) if str(x).strip()]
    risks = list(uo.get("risks") or [])
    open_loops = list(uo.get("open_loops") or [])
    raw_title = str(nba.get("title_pl") or "")
    raw_reason = str(nba.get("reason_pl") or "")
    sharpened = sharpen_recommended_next_step(
        title_pl=raw_title,
        reason_pl=raw_reason,
        action_type=str(nba.get("action_type") or nba.get("recommended_action") or ""),
        case_kind=family,
        business_area=area,
        case_family=family,
        missing_critical_fields=missing[:6],
        essence_pl=str(oe.get("essence_pl") or uo.get("situation_summary_pl") or ""),
        what_changed_pl=str(delta.get("operator_visible_delta_summary") or ""),
        risks=risks,
        open_loops=open_loops,
        thread_delta=delta,
    )
    gaps_vs_risks = separate_gaps_vs_risks(
        missing_critical_fields=missing,
        risks=risks,
        open_loops=open_loops,
    )
    meaningful_delta = is_meaningful_follow_up_delta(
        what_changed_pl=str(delta.get("operator_visible_delta_summary") or ""),
        thread_delta=delta,
        risks=risks,
        open_loops=open_loops,
    )
    decision_state = classify_decision_state(
        sharpened_pl=sharpened,
        action_type=str(nba.get("action_type") or ""),
        case_kind=family,
        missing_critical_fields=missing,
        risks=risks,
        open_loops=open_loops,
        lifecycle_hint=lifecycle_hint,
        policy_allowed=policy_allowed,
        hitl_required=hitl_required,
        draft_ready=draft_ready,
    )
    next_step_ok = bool(sharpened) and not is_vague_next_step(sharpened)
    follow_up_ok = True
    if require_follow_up_delta or meaningful_delta:
        if decision_state == DECISION_STATE_CONSCIOUSLY_DO_NOTHING:
            # Document/admin delta may be real, but the correct state is still do-nothing HVAC.
            follow_up_ok = next_step_ok
        else:
            follow_up_ok = meaningful_delta and next_step_ok and (
                "follow-up" in sharpened.lower()
                or "odpowiedz" in sharpened.lower()
                or "odpowiedź" in sharpened.lower()
                or "draft" in sharpened.lower()
                or decision_state
                in {
                    DECISION_STATE_REPLY,
                    DECISION_STATE_APPROVE,
                    DECISION_STATE_EXECUTE,
                    DECISION_STATE_COMPLETE_INFO,
                }
            )
    expected = str(expected_decision_state or "").strip()
    state_ok = decision_state in DECISION_STATES and (not expected or decision_state == expected)
    gaps_ok = bool(gaps_vs_risks.get("separated"))
    checks = {
        "recommended_next_step_not_vague": next_step_ok,
        "decision_state_clear": state_ok,
        "gaps_vs_risks_separated": gaps_ok,
        "follow_up_delta": follow_up_ok,
    }
    failed = [name for name, ok in checks.items() if not ok]
    return {
        "case_id": str(case_id or uo.get("case_id") or "").strip() or "unknown",
        "verdict": "PASS" if not failed else "FAIL",
        "decision_state": decision_state,
        "expected_decision_state": expected or None,
        "sharpened_next_step_pl": sharpened[:400],
        "raw_next_step_was_vague": is_vague_next_step(f"{raw_title} {raw_reason}"),
        "meaningful_follow_up_delta": meaningful_delta,
        "gaps_vs_risks": {
            "separated": gaps_ok,
            "gap_count": len(gaps_vs_risks["gaps"]),
            "risk_count": len(gaps_vs_risks["risks"]),
            "overlap_count": len(gaps_vs_risks["overlap"]),
            "questionish_gap_count": len(gaps_vs_risks["questionish_gaps"]),
        },
        "checks": checks,
        "failed_checks": failed,
        "planner_action_hint": planner_action_hint(
            sharpened_pl=sharpened,
            case_kind=family,
            missing_critical_fields=missing[:6],
        ),
    }


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
    missing = [str(x) for x in (uo.get("missing_critical_fields") or [])[:6]]
    risks = list(uo.get("risks") or [])
    open_loops = list(uo.get("open_loops") or [])
    raw_title = str(nba.get("title_pl") or "")
    raw_reason = str(nba.get("reason_pl") or "")
    sharpened = sharpen_recommended_next_step(
        title_pl=raw_title,
        reason_pl=raw_reason,
        action_type=str(nba.get("action_type") or nba.get("recommended_action") or ""),
        case_kind=family,
        business_area=area,
        case_family=family,
        missing_critical_fields=missing,
        essence_pl=str(oe.get("essence_pl") or uo.get("situation_summary_pl") or ""),
        what_changed_pl=str(delta.get("operator_visible_delta_summary") or ""),
        risks=risks,
        open_loops=open_loops,
        thread_delta=delta,
    )
    decision_state = classify_decision_state(
        sharpened_pl=sharpened,
        action_type=str(nba.get("action_type") or ""),
        case_kind=family,
        missing_critical_fields=missing,
        risks=risks,
        open_loops=open_loops,
    )
    gaps_vs_risks = separate_gaps_vs_risks(
        missing_critical_fields=list(uo.get("missing_critical_fields") or []),
        risks=risks,
        open_loops=open_loops,
    )
    quality = {
        "sharpened": bool(sharpened and sharpened != raw_title),
        "was_vague": is_vague_next_step(f"{raw_title} {raw_reason}"),
        "planner_action_hint": planner_action_hint(
            sharpened_pl=sharpened or raw_title,
            case_kind=family,
            missing_critical_fields=missing,
        ),
        "decision_state": decision_state,
        "gaps_vs_risks_separated": bool(gaps_vs_risks.get("separated")),
        "meaningful_follow_up_delta": is_meaningful_follow_up_delta(
            what_changed_pl=str(delta.get("operator_visible_delta_summary") or ""),
            thread_delta=delta,
            risks=risks,
            open_loops=open_loops,
        ),
    }
    if sharpened and sharpened != raw_title:
        if not nba.get("title_pl_raw"):
            nba["title_pl_raw"] = raw_title
        nba["title_pl"] = sharpened[:400]
        quality["sharpened"] = True
    elif sharpened:
        quality["sharpened"] = False
        quality["was_vague"] = False
    nba["quality"] = quality
    uo["next_best_action_recommendation"] = nba
    return uo


__all__ = [
    "DECISION_STATES",
    "DECISION_STATE_APPROVE",
    "DECISION_STATE_CLOSE",
    "DECISION_STATE_COMPLETE_INFO",
    "DECISION_STATE_CONSCIOUSLY_DO_NOTHING",
    "DECISION_STATE_EXECUTE",
    "DECISION_STATE_REPLY",
    "DECISION_STATE_WAIT",
    "apply_nba_quality_to_understanding",
    "classify_decision_state",
    "evaluate_understanding_to_decision_quality",
    "is_meaningful_follow_up_delta",
    "is_vague_next_step",
    "normalize_case_kind",
    "planner_action_hint",
    "separate_gaps_vs_risks",
    "sharpen_recommended_next_step",
]
