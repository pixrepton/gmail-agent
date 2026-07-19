"""service_request_intake_v1 — bounded checklist for service mail (no diagnosis, no auto-send)."""

from __future__ import annotations

from typing import Any

PLAYBOOK_ID = "service_request_intake_v1"
PLAYBOOK_VERSION = "1"


REQUIRED_SERVICE_FIELDS = (
    "adres",
    "telefon",
    "model_urzadzenia",
    "opis_objawu",
    "od_kiedy_problem",
    "kod_bledu",
    "zdjecie_lub_nagranie",
    "urzadzenie_dziala",
    "pilne_zagrozenie",
    "preferowany_termin_kontaktu",
)


def run_service_request_intake_v1(
    *,
    topic_result: dict[str, Any],
    missing_info: dict[str, Any],
    conflicting_facts: list[dict[str, Any]],
    case_link_decision: str = "",
    calendar_event_count: int = 0,
) -> dict[str, Any]:
    matched = str(topic_result.get("topic_id") or "") == "service_request"
    match_confidence = float(topic_result.get("confidence") or 0.0) if matched else 0.0

    gaps: list[str] = []
    if matched:
        crit = list((missing_info or {}).get("critical") or [])
        imp = list((missing_info or {}).get("important") or [])
        gaps = [str(x).lower() for x in crit + imp if str(x).strip()]
        for field in REQUIRED_SERVICE_FIELDS:
            if not any(field in g for g in gaps):
                gaps.append(f"brak_{field}")

    blocking = [g for g in gaps if g.startswith("brak_")][:12]
    completed_steps = ["detect_topic", "scan_missing_info"] if matched else []
    if matched and case_link_decision.lower() in {"linked", "weak_link"}:
        completed_steps.append("verify_case_link")
    if matched and calendar_event_count > 0:
        completed_steps.append("check_calendar_memory")

    required_steps = [
        "verify_case_link",
        "collect_service_fields",
        "check_conflicts",
        "prepare_draft_request_info",
        "operator_review",
    ]

    allowed_types = ["request_missing_info", "mark_attention_required", "prepare_reply_draft"]
    forbidden = ["send_email", "auto_send", "live_calendar_write", "hvac_offer", "diagnose_technically"]

    link_ok = case_link_decision.lower() in {"linked", "weak_link"}
    conflict_n = len([x for x in conflicting_facts if isinstance(x, (dict, str))])

    return {
        "playbook_id": PLAYBOOK_ID,
        "playbook_version": PLAYBOOK_VERSION,
        "matched": matched,
        "match_confidence": round(match_confidence, 4),
        "case_link_decision": case_link_decision,
        "case_link_ok": bool(link_ok),
        "calendar_event_count": int(max(0, calendar_event_count)),
        "conflict_signal_count": conflict_n,
        "required_steps": required_steps,
        "completed_steps": completed_steps,
        "blocking_gaps": blocking[:10],
        "allowed_action_types": allowed_types if matched else [],
        "forbidden_action_types": list(forbidden),
        "handoff_reason": "missing_information" if blocking else "",
        "operator_instruction": (
            "Uzupełnij braki danych serwisowych; nie diagnozuj technicznie ani nie obiecuj terminu bez weryfikacji. "
            "Nie wysyłaj automatycznie odpowiedzi e-mail. "
            f"Powiązanie sprawy: {'OK' if link_ok else 'do weryfikacji'}. "
            f"Konflikty sygnałów: {conflict_n}. "
            f"Kalendarz (pamięć): {calendar_event_count} wpisów."
        ),
    }
