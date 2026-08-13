"""Thread Memory: first-class conversational continuity for case intelligence."""

from __future__ import annotations

from typing import Any


def build_thread_memory(
    snapshot: dict[str, Any],
    *,
    intake_result: dict[str, Any] | None = None,
    case_link_result: dict[str, Any] | None = None,
    business_result: dict[str, Any] | None = None,
    existing_thread_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build or update canonical thread memory from snapshot and stage outputs."""
    source_message = snapshot.get("source_message") or {}
    context_messages = snapshot.get("context_messages") or []
    thread_id = str(source_message.get("thread_id") or "").strip()
    thread_context = snapshot.get("thread_context") or {}
    existing = existing_thread_memory or {}

    canonical_summary = _build_canonical_summary(
        source_message=source_message,
        context_messages=context_messages,
        thread_context=thread_context,
        business_result=business_result or {},
        existing_summary=str(existing.get("canonical_thread_summary") or ""),
    )
    unresolved = _detect_unresolved_questions(
        source_message=source_message,
        context_messages=context_messages,
        existing_unresolved=list(existing.get("unresolved_questions") or []),
    )
    commitments = _detect_commitments(
        source_message=source_message,
        context_messages=context_messages,
        existing_commitments=list(existing.get("commitments_made") or []),
    )
    last_decision = _extract_last_decision(
        business_result=business_result or {},
        existing_decision=str(existing.get("last_decision") or ""),
    )
    key_facts = _merge_key_facts(
        source_message=source_message,
        intake_result=intake_result or {},
        existing_facts=list(existing.get("key_facts_so_far") or []),
    )
    participant_actions = _extract_participant_actions(
        source_message=source_message,
        context_messages=context_messages,
        existing=existing,
    )

    return {
        "thread_id": thread_id,
        "case_id": str((case_link_result or {}).get("selected_case_key") or ""),
        "canonical_thread_summary": canonical_summary,
        "unresolved_questions": unresolved,
        "commitments_made": commitments,
        "last_decision": last_decision,
        "key_facts_so_far": key_facts[:20],
        "open_tasks_from_thread": _extract_open_tasks(business_result or {}),
        "latest_attachment_versions": list(source_message.get("attachment_names") or []),
        "last_participant_action": participant_actions.get("last_participant", ""),
        "last_operator_action": participant_actions.get("last_operator", ""),
        "last_customer_action": participant_actions.get("last_customer", ""),
        "message_count": 1 + len(context_messages),
        "has_unanswered_question": bool(unresolved),
        "has_open_commitment": bool(commitments),
        "thread_state": _classify_thread_state(unresolved, commitments, participant_actions),
        "updated_at": str(source_message.get("date") or ""),
    }


def _build_canonical_summary(
    *,
    source_message: dict[str, Any],
    context_messages: list[dict[str, Any]],
    thread_context: dict[str, Any],
    business_result: dict[str, Any],
    existing_summary: str,
) -> str:
    business_interpretation = str(business_result.get("business_interpretation") or "").strip()
    if business_interpretation:
        if existing_summary:
            if business_interpretation in existing_summary:
                return existing_summary
            if existing_summary in business_interpretation:
                return business_interpretation
            return f"{existing_summary} Aktualizacja: {business_interpretation}"
        return business_interpretation
    subject = str(source_message.get("subject") or "").strip()
    if existing_summary:
        return f"{existing_summary} Nowa wiadomość: {subject}." if subject else existing_summary
    return subject or "Brak podsumowania wątku."


def _detect_unresolved_questions(
    *,
    source_message: dict[str, Any],
    context_messages: list[dict[str, Any]],
    existing_unresolved: list[str],
) -> list[str]:
    questions: list[str] = list(existing_unresolved)

    body = str(source_message.get("body") or "").strip()
    if "?" in body:
        sentences = [s.strip() for s in body.split("?") if s.strip()]
        for sentence in sentences[:3]:
            short = sentence[-120:].strip() + "?"
            if short not in questions:
                questions.append(short)

    return questions[:10]


def _detect_commitments(
    *,
    source_message: dict[str, Any],
    context_messages: list[dict[str, Any]],
    existing_commitments: list[str],
) -> list[str]:
    commitments: list[str] = list(existing_commitments)
    commitment_markers = (
        "potwierdz", "obiecuj", "do konca", "do piątku", "do poniedziałku",
        "wyślę", "wracam", "dam znac", "oddzwonię", "przygotuj",
        "confirm", "promise", "will send", "I'll", "we will",
    )
    for msg in [source_message, *context_messages]:
        body = str(msg.get("body") or "").lower()
        for marker in commitment_markers:
            if marker in body:
                sender = str(msg.get("sender") or msg.get("from") or "").strip()
                short_body = body[:200].strip()
                commitment = f"{sender}: {short_body}"
                if commitment not in commitments:
                    commitments.append(commitment)
                break
    return commitments[:10]


def _extract_last_decision(*, business_result: dict[str, Any], existing_decision: str) -> str:
    action = str(business_result.get("recommended_next_action") or "").strip()
    reason = str(business_result.get("recommended_action_reason") or "").strip()
    if action and reason:
        return f"{action}: {reason}"
    if action:
        return action
    return existing_decision


def _merge_key_facts(
    *,
    source_message: dict[str, Any],
    intake_result: dict[str, Any],
    existing_facts: list[str],
) -> list[str]:
    facts: list[str] = list(existing_facts)
    extracted = (intake_result.get("extracted_data") or {})
    for key in ("deadlines", "amounts", "dates"):
        items = extracted.get(key) or []
        for item in items:
            if isinstance(item, dict):
                text = str(item.get("date") or item.get("value") or item.get("kind") or "").strip()
                if text and text not in facts:
                    facts.append(text)
    references = extracted.get("references") or {}
    for ref_key in ("shipment_numbers", "invoice_numbers", "order_numbers"):
        for ref in references.get(ref_key) or []:
            text = str(ref).strip()
            if text and text not in facts:
                facts.append(text)
    return facts


def _extract_open_tasks(business_result: dict[str, Any]) -> list[str]:
    action = str(business_result.get("recommended_next_action") or "").strip()
    if action and action not in {"wait", "ignore"}:
        return [action]
    return []


def _extract_participant_actions(
    *,
    source_message: dict[str, Any],
    context_messages: list[dict[str, Any]],
    existing: dict[str, Any],
) -> dict[str, str]:
    sender = str(source_message.get("sender") or source_message.get("from") or "").strip().lower()
    subject = str(source_message.get("subject") or "").strip()
    date = str(source_message.get("date") or "").strip()
    action_text = f"{subject} ({date})"

    result = {
        "last_participant": action_text,
        "last_operator": str(existing.get("last_operator_action") or ""),
        "last_customer": str(existing.get("last_customer_action") or ""),
    }
    if "topinstal" in sender or "biuro" in sender or "ops@" in sender:
        result["last_operator"] = action_text
    else:
        result["last_customer"] = action_text
    return result


def _classify_thread_state(
    unresolved: list[str],
    commitments: list[str],
    participant_actions: dict[str, str],
) -> str:
    if unresolved and not participant_actions.get("last_customer"):
        return "waiting_for_customer"
    if commitments and not participant_actions.get("last_operator"):
        return "waiting_for_operator"
    if unresolved:
        return "open_questions"
    if commitments:
        return "commitments_pending"
    return "active"


__all__ = [
    "build_thread_memory",
]
