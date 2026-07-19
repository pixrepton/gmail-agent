"""Build stable source snapshots and compact inference payloads for Gmail intake."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from copy import deepcopy
from typing import Any

from intake_policy import (
    BUSINESS_AREAS,
    CASE_FAMILIES,
    DECISION_ACTIONS,
    INFERENCE_MODE_COMPACT,
    INFERENCE_MODE_FULL,
    INFERENCE_MODE_MINIMAL_SAFE,
    INFERENCE_MODE_REDUCED_COMPACT,
    PRIORITIES,
    REVIEW_FLAGS,
    SNAPSHOT_VERSION,
)


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
GENERIC_CONTEXT_SUBJECTS = {
    "faktura",
    "invoice",
    "potwierdzenie",
    "payment",
    "oplata",
    "oplaty",
    "transakcja",
    "zamowienie",
    "re",
    "fw",
    "fwd",
}
REPLY_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|odp)\s*:\s*)+", flags=re.IGNORECASE)
FORWARD_BODY_PATTERNS = (
    "forwarded message",
    "przekazana wiadomosc",
    "przeslana wiadomosc",
)
REPLY_BODY_PATTERNS = (
    "napisal:",
    "w dniu",
    "wrote:",
    "original message",
)
REFERENCE_PATTERNS = {
    "invoice": re.compile(
        r"\b(?:faktura|invoice|fv|korekta)\b(?:\s+(?:vat|proforma|koryguj\w*))?(?:\s+(?:nr|no|number))?\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_\-]{2,})",
        re.IGNORECASE,
    ),
    "shipment": re.compile(
        r"\b(?:shipment|tracking|numer\s+paczki|nr\s+paczki|paczk\w*)\b(?:\s+(?:nr|no|number))?\s*[:#-]?\s*([A-Z0-9][A-Z0-9\-]{5,})",
        re.IGNORECASE,
    ),
    "order": re.compile(
        r"\b(?:zamowienie|order)\b(?:\s+(?:nr|no|number))?\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_\-]{3,})",
        re.IGNORECASE,
    ),
    "transaction": re.compile(
        r"\b(?:transakcj\w*|payment|platnosc|numer\s+platnosci)\b(?:\s+(?:nr|no|number))?\s*[:#-]?\s*([A-Z0-9][A-Z0-9\-]{5,})",
        re.IGNORECASE,
    ),
    "case": re.compile(
        r"\b(?:sprawa|case|ticket|zg\w*oszenie)\b(?:\s+(?:nr|no|number))?\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_\-]{2,})",
        re.IGNORECASE,
    ),
}
EMAIL_RE = re.compile(r"([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
ZERO_WIDTH_RE = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2060-\u206F\uFEFF]")
REPEATED_SPACE_RE = re.compile(r"[ \t]{2,}")
REPEATED_BLANKS_RE = re.compile(r"\n{3,}")
NON_ALNUM_EDGE_RE = re.compile(r"^[^A-Z0-9]+|[^A-Z0-9]+$", re.IGNORECASE)
REFERENCE_TOKEN_SPLIT_RE = re.compile(r"[\s,;]+")
REFERENCE_TOKEN_STOPWORDS = frozenset(
    {
        "witaj",
        "ciebie",
        "inpost",
        "allegro",
        "tracking",
        "shipment",
        "invoice",
        "payment",
        "order",
        "case",
        "ticket",
        "sprawa",
        "numer",
        "paczki",
        "platnosci",
        "zamowienie",
    }
)

QUOTE_BOUNDARY_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE)
    for pattern in (
        r"^-{2,}\s*forwarded message\s*-{0,}$",
        r"^-{2,}\s*original message\s*-{0,}$",
        r"^from:\s",
        r"^sent:\s",
        r"^to:\s",
        r"^subject:\s",
        r"^on .+ wrote:\s*$",
        r"^w dniu .+ napisa",
    )
)
FOOTER_BOUNDARY_MARKERS = (
    "unsubscribe",
    "update my details",
    "view in browser",
    "if you can",
    "obserwuj nasze social media",
    "social media",
    "this email was sent",
    "ta wiadomosc zostala wyslana",
    "confidentiality notice",
    "klauzula poufnosci",
    "all rights reserved",
    "wygenerowana automatycznie",
    "wiadomosc wygenerowana automatycznie",
    "polityka prywatnosci",
    "regulamin",
    "formularz kontaktowy",
    "zobacz te wiadomosc",
    "pobierz apke",
    "pokaz na mapie",
    "masz pytania",
)
NOISE_LINE_MARKERS = (
    "http://",
    "https://",
    "www.",
    "itunes.apple.com",
    "play.google.com",
)

INFERENCE_BUDGETS: dict[str, dict[str, int]] = {
    INFERENCE_MODE_FULL: {
        "subject": 240,
        "snippet": 800,
        "body": 12000,
        "context_messages": 4,
        "context_subject": 200,
        "context_snippet": 500,
        "context_body": 4000,
        "thread_summary_seed": 360,
        "attachment_names": 10,
        "candidate_count": 5,
    },
    INFERENCE_MODE_COMPACT: {
        "subject": 180,
        "snippet": 420,
        "body": 2600,
        "context_messages": 2,
        "context_subject": 170,
        "context_snippet": 240,
        "context_body": 650,
        "thread_summary_seed": 180,
        "attachment_names": 5,
        "candidate_count": 3,
    },
    INFERENCE_MODE_REDUCED_COMPACT: {
        "subject": 170,
        "snippet": 260,
        "body": 1600,
        "context_messages": 1,
        "context_subject": 150,
        "context_snippet": 160,
        "context_body": 320,
        "thread_summary_seed": 120,
        "attachment_names": 4,
        "candidate_count": 2,
    },
    INFERENCE_MODE_MINIMAL_SAFE: {
        "subject": 140,
        "snippet": 180,
        "body": 900,
        "context_messages": 1,
        "context_subject": 120,
        "context_snippet": 120,
        "context_body": 180,
        "thread_summary_seed": 90,
        "attachment_names": 3,
        "candidate_count": 1,
    },
}


def load_prompt_text(name: str) -> str:
    """Load a prompt file from the local prompts directory."""
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def render_system_prompt() -> str:
    """Render the system prompt with runtime-owned vocabulary lists."""
    template = load_prompt_text("intake_system_v1.txt")
    replacements = {
        "{{BUSINESS_AREAS}}": ", ".join(BUSINESS_AREAS),
        "{{CASE_FAMILIES}}": ", ".join(CASE_FAMILIES),
        "{{DECISION_ACTIONS}}": ", ".join(DECISION_ACTIONS),
        "{{PRIORITIES}}": ", ".join(PRIORITIES),
        "{{REVIEW_FLAGS}}": ", ".join(REVIEW_FLAGS),
    }

    rendered = template
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    return rendered


def normalize_message(raw_message: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Gmail message into a stable internal shape."""
    body = _pick_first_str(
        raw_message,
        "body",
        "plain_text_body",
        "text",
        "content",
        "message",
        default="",
    )
    snippet = _pick_first_str(raw_message, "snippet", "preview", default="")
    subject = _pick_first_str(raw_message, "subject", "display_title", default="")
    normalized_subject = normalize_subject(subject)
    thread_id = _pick_first_str(raw_message, "thread_id", "threadId", "conversation_id", default="")
    sender = _pick_first_str(raw_message, "from", "sender", default="")
    sender_email = extract_email_address(sender)

    attachments = raw_message.get("attachments")
    attachment_names: list[str] = []
    if isinstance(attachments, list):
        for item in attachments:
            if isinstance(item, dict):
                name = _pick_first_str(item, "name", "filename", default="")
                if name:
                    attachment_names.append(name)
            elif isinstance(item, str) and item.strip():
                attachment_names.append(item.strip())
    if not attachment_names:
        raw_names = raw_message.get("attachment_names")
        if isinstance(raw_names, list):
            for name in raw_names:
                text = str(name or "").strip()
                if text and text not in attachment_names:
                    attachment_names.append(text)

    has_attachments = bool(raw_message.get("has_attachment")) or bool(raw_message.get("has_attachments")) or bool(attachment_names)
    thread_position_hint = detect_thread_position(subject=subject, body=body)
    reference_tokens = extract_reference_tokens(subject=subject, body=body, snippet=snippet)

    attachment_parts = []
    raw_parts = raw_message.get("attachment_parts")
    if isinstance(raw_parts, list):
        for item in raw_parts:
            if isinstance(item, dict):
                attachment_parts.append(item)

    return {
        "message_id": _pick_first_str(raw_message, "id", "message_id", default=""),
        "thread_id": thread_id,
        "date": _pick_first_str(raw_message, "email_ts", "date", "received_at", default=""),
        "sender": sender,
        "sender_email": sender_email,
        "to": _coerce_str_list(raw_message.get("to")),
        "cc": _coerce_str_list(raw_message.get("cc")),
        "subject": subject,
        "normalized_subject": normalized_subject,
        "snippet": snippet,
        "body": body,
        "labels": _coerce_str_list(raw_message.get("labels")),
        "has_attachments": has_attachments,
        "attachment_names": attachment_names,
        "attachment_parts": attachment_parts,
        "thread_position_hint": thread_position_hint,
        "is_reply_or_forward_hint": thread_position_hint in {"reply", "forward"},
        "reference_tokens": reference_tokens,
        "raw": raw_message,
    }


def build_source_snapshot(
    *,
    mailbox: str,
    source_message: dict[str, Any],
    context_messages: list[dict[str, Any]] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Build a stable full-fidelity source snapshot for audit and reruns."""
    normalized_source = normalize_message(source_message)
    normalized_context = [normalize_message(item) for item in (context_messages or [])]
    normalized_context = sorted(
        normalized_context,
        key=lambda item: _context_match_score(normalized_source, item),
        reverse=True,
    )
    thread_context = assess_thread_context_quality(normalized_source, normalized_context)
    case_link_candidates = build_case_link_candidates(normalized_source, normalized_context)
    routing_hints = assess_routing_hints(
        mailbox=mailbox,
        source_message=normalized_source,
        context_messages=normalized_context,
    )

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "mailbox": mailbox,
        "observed_at": observed_at or datetime.now().astimezone().isoformat(),
        "source_message": normalized_source,
        "context_messages": normalized_context,
        "normalized_subject": normalized_source["normalized_subject"],
        "thread_context_quality": thread_context["quality"],
        "thread_context": thread_context,
        "case_link_candidates": case_link_candidates,
        "routing_hints": routing_hints,
    }


def coerce_source_snapshot(snapshot_like: dict[str, Any], *, mailbox_fallback: str = "unknown") -> dict[str, Any]:
    """Upgrade an older snapshot or rebuild one from raw message-like data."""
    if "source_message" in snapshot_like:
        source_message = snapshot_like["source_message"]
        context_messages = snapshot_like.get("context_messages") or []
        mailbox = str(snapshot_like.get("mailbox") or mailbox_fallback)
        observed_at = str(snapshot_like.get("observed_at") or "") or None
        return build_source_snapshot(
            mailbox=mailbox,
            source_message=source_message if isinstance(source_message, dict) else {},
            context_messages=[item for item in context_messages if isinstance(item, dict)],
            observed_at=observed_at,
        )

    return build_source_snapshot(
        mailbox=mailbox_fallback,
        source_message=snapshot_like,
        context_messages=[],
    )


def build_decision_input(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible alias for the default compact inference payload."""
    return build_inference_payload(snapshot, mode=INFERENCE_MODE_COMPACT)["payload"]


def _inference_enrichment_for_mode(snapshot: dict[str, Any], *, mode: str) -> dict[str, Any] | None:
    """Attach bounded pre-LLM attachment/thread envelopes; shrink further for reduced/minimal modes."""
    raw = snapshot.get("inference_enrichment")
    if not isinstance(raw, dict):
        return None
    block = {
        "version": raw.get("version", "1"),
        "attachment_evidence_envelope": deepcopy(raw.get("attachment_evidence_envelope") or {}),
        "prior_thread_context_envelope": deepcopy(raw.get("prior_thread_context_envelope") or {}),
    }
    att = block["attachment_evidence_envelope"]
    pt = block["prior_thread_context_envelope"]
    attachments = list(att.get("attachments") or []) if isinstance(att, dict) else []
    if mode == INFERENCE_MODE_REDUCED_COMPACT:
        att["attachments"] = attachments[:2]
        for item in att["attachments"]:
            if isinstance(item, dict):
                item["attachment_key_facts"] = list(item.get("attachment_key_facts") or [])[:2]
        pt["open_questions"] = list(pt.get("open_questions") or [])[:4]
        pt["key_facts_so_far"] = list(pt.get("key_facts_so_far") or [])[:4]
    elif mode == INFERENCE_MODE_MINIMAL_SAFE:
        att["attachments"] = attachments[:1]
        for item in att["attachments"]:
            if isinstance(item, dict):
                item["attachment_key_facts"] = []
        pt["open_questions"] = list(pt.get("open_questions") or [])[:2]
        pt["key_facts_so_far"] = list(pt.get("key_facts_so_far") or [])[:2]
        pt["commitments_made"] = list(pt.get("commitments_made") or [])[:2]
    return block


def build_inference_payload(snapshot: dict[str, Any], *, mode: str = INFERENCE_MODE_COMPACT) -> dict[str, Any]:
    """Build a compact model-facing payload separate from the full source snapshot."""
    budgets = _inference_budgets(mode)
    source_message = snapshot["source_message"]
    context_messages = snapshot.get("context_messages") or []
    thread_context = snapshot.get("thread_context") or {
        "quality": snapshot.get("thread_context_quality", "weak"),
        "reasons": [],
    }
    routing_hints = snapshot.get("routing_hints") or {"self_forward": False, "reasons": []}

    source_view, source_metrics = _build_message_view(
        source_message,
        body_budget=budgets["body"],
        subject_budget=budgets["subject"],
        snippet_budget=budgets["snippet"],
        attachment_budget=budgets["attachment_names"],
    )

    context_views: list[dict[str, Any]] = []
    context_notes: list[str] = []
    for item in context_messages[: budgets["context_messages"]]:
        context_view, context_metric = _build_message_view(
            item,
            body_budget=budgets["context_body"],
            subject_budget=budgets["context_subject"],
            snippet_budget=budgets["context_snippet"],
            attachment_budget=0,
        )
        context_view["match_hints"] = _summarize_context_match(source_message, item)
        context_views.append(context_view)
        context_notes.extend(context_metric["notes"])

    payload = {
        "snapshot_version": snapshot.get("snapshot_version", SNAPSHOT_VERSION),
        "mailbox": snapshot["mailbox"],
        "observed_at": snapshot["observed_at"],
        "inference_mode": mode,
        "source_message": source_view,
        "context_messages": context_views,
        "normalized_subject": _compact_inline_text(snapshot.get("normalized_subject", ""), budgets["subject"]),
        "thread_context": {
            "quality": snapshot.get("thread_context_quality") or thread_context.get("quality") or "weak",
            "reasons": [str(item).strip() for item in thread_context.get("reasons", []) if str(item).strip()][:4],
            "thread_summary_seed": _build_thread_summary_seed(
                source_view=source_view,
                context_views=context_views,
                snapshot=snapshot,
                budget=budgets["thread_summary_seed"],
            ),
        },
        "case_link_candidates": _compact_case_link_candidates(
            snapshot.get("case_link_candidates") or [],
            limit=budgets["candidate_count"],
        ),
        "routing_hints": {
            "self_forward": bool(routing_hints.get("self_forward")),
            "reasons": [str(item).strip() for item in routing_hints.get("reasons", []) if str(item).strip()][:3],
        },
    }
    enrichment = _inference_enrichment_for_mode(snapshot, mode=mode)
    extra_notes: list[str] = []
    if enrichment:
        payload["inference_enrichment"] = enrichment
        extra_notes.append("inference_enrichment_attached")
    metrics = {
        "mode": mode,
        "payload_chars": len(json.dumps(payload, ensure_ascii=False)),
        "source_body_chars": len(source_view["body"]),
        "context_messages": len(context_views),
        "notes": sorted(set([*source_metrics["notes"], *context_notes, *extra_notes])),
        "section_budgets": {
            "source_body": budgets["body"],
            "context_messages": budgets["context_messages"],
            "context_body": budgets["context_body"],
            "thread_summary_seed": budgets["thread_summary_seed"],
        },
    }
    return {"mode": mode, "payload": payload, "metrics": metrics}


def build_inference_payload_variants(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ordered payload variants for normal, reduced, and minimal-safe inference."""
    return [
        build_inference_payload(snapshot, mode=INFERENCE_MODE_COMPACT),
        build_inference_payload(snapshot, mode=INFERENCE_MODE_REDUCED_COMPACT),
        build_inference_payload(snapshot, mode=INFERENCE_MODE_MINIMAL_SAFE),
    ]


def build_intake_reasoning_payload(
    snapshot: dict[str, Any],
    context_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the default model-facing payload for intake reasoning."""
    base_payload = build_inference_payload(snapshot, mode=INFERENCE_MODE_COMPACT)["payload"]
    evidence = _build_evidence_sections(snapshot=snapshot, intake_result=None, case_link_result=None)
    return {
        **base_payload,
        "context_bundle": sanitize_prompt_input(context_bundle or {}),
        "hard_evidence": evidence["hard_evidence"],
        "soft_evidence": evidence["soft_evidence"],
        "uncertain_links": evidence["uncertain_links"],
    }


def build_business_reasoning_payload(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    business_context_bundle: dict[str, Any],
) -> dict[str, Any]:
    """Build the business-reasoner payload with separated evidence confidence layers."""
    evidence = _build_evidence_sections(snapshot=snapshot, intake_result=intake_result, case_link_result=case_link_result)
    return {
        "message_summary": {
            "sender": str(snapshot.get("source_message", {}).get("sender") or ""),
            "subject": str(snapshot.get("source_message", {}).get("subject") or ""),
            "snippet": str(snapshot.get("source_message", {}).get("snippet") or ""),
            "thread_quality": str(snapshot.get("thread_context_quality") or "weak"),
        },
        "intake_result": sanitize_prompt_input(intake_result),
        "case_link_result": sanitize_prompt_input(case_link_result),
        "business_context": sanitize_prompt_input(business_context_bundle),
        "hard_evidence": evidence["hard_evidence"],
        "soft_evidence": evidence["soft_evidence"],
        "uncertain_links": evidence["uncertain_links"],
    }


def build_reply_draft_payload(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    business_result: dict[str, Any],
    business_context_bundle: dict[str, Any],
    context_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the reply-drafter payload with business guidance and uncertainty markers."""
    evidence = _build_evidence_sections(snapshot=snapshot, intake_result=intake_result, case_link_result=None)
    return {
        "customer_message": {
            "sender": str(snapshot.get("source_message", {}).get("sender") or ""),
            "subject": str(snapshot.get("source_message", {}).get("subject") or ""),
            "body_excerpt": str(snapshot.get("source_message", {}).get("body") or "")[:900],
        },
        "intake_result": sanitize_prompt_input(intake_result),
        "business_reasoning_result": sanitize_prompt_input(business_result),
        "business_context": sanitize_prompt_input(business_context_bundle),
        "context_bundle": sanitize_prompt_input(context_bundle or {}),
        "hard_evidence": evidence["hard_evidence"],
        "soft_evidence": evidence["soft_evidence"],
        "uncertain_links": evidence["uncertain_links"],
    }


def render_task_prompt(
    snapshot: dict[str, Any],
    *,
    inference_payload: dict[str, Any] | None = None,
) -> str:
    """Render the task prompt for a single intake decision."""
    template = load_prompt_text("intake_task_v1.txt")
    package = inference_payload or build_inference_payload(snapshot, mode=INFERENCE_MODE_COMPACT)
    payload = json.dumps(package["payload"], indent=2, ensure_ascii=False)
    metrics = package.get("metrics") or {}
    notes = metrics.get("notes") or []
    note_text = ", ".join(notes) if notes else "none"
    return (
        template.replace("{{MAILBOX}}", snapshot["mailbox"])
        .replace("{{OBSERVED_AT}}", snapshot["observed_at"])
        .replace("{{INFERENCE_MODE}}", package.get("mode", INFERENCE_MODE_COMPACT))
        .replace("{{INFERENCE_NOTES}}", note_text)
        .replace("{{SOURCE_SNAPSHOT_JSON}}", payload)
    )


def sanitize_prompt_input(value: Any) -> Any:
    """Keep prompt inputs compact, JSON-serializable, and predictable."""
    if isinstance(value, dict):
        return {str(key): sanitize_prompt_input(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_prompt_input(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_prompt_input(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def normalize_subject(subject: str) -> str:
    """Normalize a subject line for conservative thread heuristics."""
    value = subject.strip()
    value = REPLY_PREFIX_RE.sub("", value)
    value = re.sub(r"\[[^\]]{0,24}\]\s*", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()


def detect_thread_position(*, subject: str, body: str) -> str:
    """Infer a coarse thread position hint from subject and body."""
    subject_value = subject.strip().lower()
    body_value = body.strip().lower()

    if re.match(r"^\s*(fw|fwd)\s*:", subject_value):
        return "forward"
    if re.match(r"^\s*(re|aw|odp)\s*:", subject_value):
        return "reply"

    if any(pattern in body_value for pattern in FORWARD_BODY_PATTERNS):
        return "forward"
    if any(pattern in body_value for pattern in REPLY_BODY_PATTERNS):
        return "reply"
    if subject_value:
        return "new_thread"
    return "unknown"


def build_related_context_query(source_message: dict[str, Any], *, max_days: int = 365) -> str | None:
    """Build a conservative Gmail query for context messages."""
    source_refs = _flatten_reference_tokens(source_message.get("reference_tokens") or {})
    if source_refs:
        token = _select_reference_case_key(source_refs).replace('"', "")
        if len(token) >= 4:
            return f'"{token}" newer_than:{max_days}d -in:spam -in:trash'

    normalized = normalize_subject(_pick_first_str(source_message, "subject", "display_title", default=""))
    if not normalized or len(normalized) < 8 or normalized in GENERIC_CONTEXT_SUBJECTS:
        return None

    escaped = normalized.replace('"', "")
    return f'subject:"{escaped}" newer_than:{max_days}d -in:spam -in:trash'


def assess_thread_context_quality(
    source_message: dict[str, Any],
    context_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assess whether the available context is strong, partial, or weak."""
    reasons: list[str] = []
    if not context_messages:
        reasons.append("no_additional_context_messages")
        return {"quality": "weak", "reasons": reasons}

    same_thread_count = 0
    same_subject_count = 0
    reference_overlap_count = 0
    source_thread_id = str(source_message.get("thread_id") or "").strip()
    source_subject = str(source_message.get("normalized_subject") or "").strip()
    subject_usable = bool(source_subject and source_subject not in GENERIC_CONTEXT_SUBJECTS and len(source_subject) >= 8)
    source_refs = _flatten_reference_tokens(source_message.get("reference_tokens") or {})

    for item in context_messages:
        if source_thread_id and source_thread_id == str(item.get("thread_id") or "").strip():
            same_thread_count += 1
        if subject_usable and source_subject == str(item.get("normalized_subject") or "").strip():
            same_subject_count += 1

        item_refs = _flatten_reference_tokens(item.get("reference_tokens") or {})
        if source_refs and item_refs and source_refs.intersection(item_refs):
            reference_overlap_count += 1

    if same_thread_count > 0:
        reasons.append("exact_thread_id_match")
    if same_subject_count > 0:
        reasons.append("same_normalized_subject")
    if reference_overlap_count > 0:
        reasons.append("shared_business_reference")
    if source_message.get("is_reply_or_forward_hint"):
        reasons.append("source_message_looks_like_reply_or_forward")

    if same_thread_count > 0 or reference_overlap_count > 0:
        return {
            "quality": "strong",
            "reasons": reasons,
            "same_thread_count": same_thread_count,
            "same_subject_count": same_subject_count,
            "reference_overlap_count": reference_overlap_count,
        }

    if same_subject_count > 0:
        return {
            "quality": "partial",
            "reasons": reasons,
            "same_thread_count": same_thread_count,
            "same_subject_count": same_subject_count,
            "reference_overlap_count": reference_overlap_count,
        }

    reasons.append("context_not_strong_enough_for_case_link")
    return {
        "quality": "weak",
        "reasons": reasons,
        "same_thread_count": same_thread_count,
        "same_subject_count": same_subject_count,
        "reference_overlap_count": reference_overlap_count,
    }


def build_case_link_candidates(
    source_message: dict[str, Any],
    context_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build lightweight case-link candidates from the available context."""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_thread_id = str(source_message.get("thread_id") or "").strip()
    normalized_subject = str(source_message.get("normalized_subject") or "").strip()
    source_refs = _flatten_reference_tokens(source_message.get("reference_tokens") or {})

    for item in context_messages:
        evidence: list[str] = []
        match_confidence = 0.0
        case_type = "message_context"
        candidate_key = ""
        context_thread_id = str(item.get("thread_id") or "").strip()
        context_subject = str(item.get("normalized_subject") or "").strip()
        context_refs = _flatten_reference_tokens(item.get("reference_tokens") or {})

        if source_thread_id and context_thread_id and source_thread_id == context_thread_id:
            candidate_key = f"thread:{context_thread_id}"
            case_type = "thread_context"
            match_confidence = 0.93
            evidence.append("same_thread_id")
        elif source_refs and context_refs and source_refs.intersection(context_refs):
            shared = source_refs.intersection(context_refs)
            candidate_key = f"reference:{_select_reference_case_key(shared)}"
            case_type = "reference_context"
            match_confidence = 0.84
            evidence.append("shared_reference_token")
        elif (
            normalized_subject
            and normalized_subject not in GENERIC_CONTEXT_SUBJECTS
            and len(normalized_subject) >= 8
            and context_subject
            and normalized_subject == context_subject
        ):
            candidate_key = f"subject:{normalized_subject}"
            case_type = "subject_context"
            match_confidence = 0.68
            evidence.append("same_normalized_subject")

        if not candidate_key or candidate_key in seen:
            continue

        candidates.append(
            {
                "case_key": candidate_key,
                "case_type": case_type,
                "match_confidence": round(match_confidence, 2),
                "evidence": evidence,
                "context_message_id": item.get("message_id") or "",
            }
        )
        seen.add(candidate_key)

    candidates.sort(key=lambda item: float(item["match_confidence"]), reverse=True)
    return candidates[:5]


def assess_routing_hints(
    *,
    mailbox: str,
    source_message: dict[str, Any],
    context_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return conservative routing hints derived from the source snapshot."""
    mailbox_email = extract_email_address(mailbox) or mailbox.strip().lower()
    sender_email = str(source_message.get("sender_email") or "").strip().lower()
    recipients = {
        extract_email_address(item) or str(item).strip().lower()
        for item in [*source_message.get("to", []), *source_message.get("cc", [])]
        if str(item).strip()
    }

    reasons: list[str] = []
    self_forward = False
    if source_message.get("thread_position_hint") == "forward":
        if mailbox_email and sender_email and sender_email == mailbox_email:
            self_forward = True
            reasons.append("forward_from_mailbox_owner")
        elif sender_email and sender_email in recipients:
            self_forward = True
            reasons.append("sender_is_also_recipient")

    if context_messages and any(source_message.get("thread_id") == item.get("thread_id") for item in context_messages):
        reasons.append("has_same_thread_context")

    # D3 resolution: detect cieplo.app/ cieplowlasciwie.pl emails to route business_lane
    business_lane: str | None = None
    if sender_email and any(
        domain in sender_email for domain in ("cieplo.app", "cieplowlasciwie.pl")
    ):
        business_lane = "cieplo"
        reasons.append(f"cieplo_detected:{sender_email}")

    return {
        "self_forward": self_forward,
        "reasons": reasons,
        "business_lane": business_lane,
    }


def extract_reference_tokens(*, subject: str, body: str, snippet: str) -> dict[str, list[str]]:
    """Extract coarse business reference tokens from message text."""
    text = "\n".join(part for part in [subject, snippet, body] if part)
    results: dict[str, list[str]] = {}

    for name, pattern in REFERENCE_PATTERNS.items():
        found = []
        for raw_match in pattern.findall(text):
            candidate = _normalize_reference_token(raw_match)
            if not _looks_structured_reference_token(candidate):
                continue
            found.append(candidate)
        if found:
            results[name] = sorted({item for item in found})

    return results


def extract_email_address(value: str) -> str:
    """Extract a lowercase email address from a display string when possible."""
    if not value:
        return ""
    match = EMAIL_RE.search(value)
    if match:
        return match.group(1).strip().lower()
    candidate = value.strip().lower()
    return candidate if "@" in candidate else ""


def _build_message_view(
    message: dict[str, Any],
    *,
    body_budget: int,
    subject_budget: int,
    snippet_budget: int,
    attachment_budget: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cleaned_subject = _compact_inline_text(message.get("subject", ""), subject_budget)
    original_snippet = _prepare_text(message.get("snippet", ""))
    cleaned_snippet = _compact_inline_text(original_snippet, snippet_budget)
    cleaned_body, body_notes = _compact_body_text(message.get("body", ""), body_budget)
    view = {
        "message_id": str(message.get("message_id") or "").strip(),
        "thread_id": str(message.get("thread_id") or "").strip(),
        "date": str(message.get("date") or "").strip(),
        "sender": str(message.get("sender") or "").strip(),
        "sender_email": str(message.get("sender_email") or "").strip(),
        "to": [str(item).strip() for item in message.get("to", []) if str(item).strip()],
        "cc": [str(item).strip() for item in message.get("cc", []) if str(item).strip()],
        "subject": cleaned_subject,
        "normalized_subject": _compact_inline_text(message.get("normalized_subject", ""), subject_budget),
        "snippet": cleaned_snippet,
        "body": cleaned_body,
        "labels": [str(item).strip() for item in message.get("labels", []) if str(item).strip()][:4],
        "has_attachments": bool(message.get("has_attachments")),
        "attachment_names": [
            _compact_inline_text(item, 80)
            for item in message.get("attachment_names", [])[:attachment_budget]
            if str(item).strip()
        ],
        "thread_position_hint": str(message.get("thread_position_hint") or "unknown").strip() or "unknown",
        "is_reply_or_forward_hint": bool(message.get("is_reply_or_forward_hint")),
        "reference_tokens": message.get("reference_tokens") or {},
    }
    notes = list(body_notes)
    if len(cleaned_subject) < len(_prepare_text(message.get("subject", ""))):
        notes.append("subject_truncated")
    if len(cleaned_snippet) < len(original_snippet):
        notes.append("snippet_truncated")
    return view, {"notes": sorted(set(notes))}


def _build_thread_summary_seed(
    *,
    source_view: dict[str, Any],
    context_views: list[dict[str, Any]],
    snapshot: dict[str, Any],
    budget: int,
) -> str:
    bits: list[str] = []
    subject = str(source_view.get("subject") or "").strip()
    sender = str(source_view.get("sender") or "").strip()
    if subject:
        bits.append(subject)
    if sender:
        bits.append(f"from {sender}")
    top_refs = _flatten_reference_tokens(source_view.get("reference_tokens") or {})
    if top_refs:
        bits.append("refs: " + ", ".join(sorted(top_refs)[:3]))
    if context_views:
        subjects = [str(item.get("subject") or "").strip() for item in context_views if str(item.get("subject") or "").strip()]
        if subjects:
            bits.append("ctx: " + " | ".join(subjects[:2]))
    bits.append(f"context={snapshot.get('thread_context_quality') or 'weak'}")
    return _compact_inline_text("; ".join(bits), budget)


def _compact_case_link_candidates(candidates: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for candidate in candidates[:limit]:
        if not isinstance(candidate, dict):
            continue
        compact.append(
            {
                "case_key": str(candidate.get("case_key") or "").strip(),
                "case_type": str(candidate.get("case_type") or "").strip(),
                "match_confidence": candidate.get("match_confidence", 0),
                "evidence": [str(item).strip() for item in candidate.get("evidence", []) if str(item).strip()][:3],
            }
        )
    return compact


def _summarize_context_match(source_message: dict[str, Any], context_message: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    if source_message.get("thread_id") and source_message.get("thread_id") == context_message.get("thread_id"):
        hints.append("same_thread_id")
    if source_message.get("normalized_subject") and source_message.get("normalized_subject") == context_message.get("normalized_subject"):
        hints.append("same_subject")
    source_refs = _flatten_reference_tokens(source_message.get("reference_tokens") or {})
    context_refs = _flatten_reference_tokens(context_message.get("reference_tokens") or {})
    if source_refs and context_refs and source_refs.intersection(context_refs):
        hints.append("shared_reference")
    return hints[:3]


def _context_match_score(source_message: dict[str, Any], context_message: dict[str, Any]) -> tuple[int, int, str]:
    score = 0
    if source_message.get("thread_id") and source_message.get("thread_id") == context_message.get("thread_id"):
        score += 100
    if source_message.get("normalized_subject") and source_message.get("normalized_subject") == context_message.get("normalized_subject"):
        score += 20

    source_refs = _flatten_reference_tokens(source_message.get("reference_tokens") or {})
    context_refs = _flatten_reference_tokens(context_message.get("reference_tokens") or {})
    score += 30 * len(source_refs.intersection(context_refs))

    return (score, _date_sort_key(context_message.get("date")), str(context_message.get("message_id") or ""))


def _date_sort_key(value: Any) -> int:
    if not isinstance(value, str) or not value.strip():
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _flatten_reference_tokens(reference_tokens: dict[str, list[str]]) -> set[str]:
    flattened: set[str] = set()
    for values in reference_tokens.values():
        if isinstance(values, list):
            for item in values:
                candidate = _normalize_reference_token(item)
                if _looks_structured_reference_token(candidate):
                    flattened.add(candidate)
    return flattened


def _normalize_reference_token(value: Any) -> str:
    token = _prepare_text(value)
    token = REFERENCE_TOKEN_SPLIT_RE.split(token, maxsplit=1)[0]
    token = NON_ALNUM_EDGE_RE.sub("", token)
    token = token.strip("._")
    token = token.replace(" ", "")
    return token.upper()


def _looks_structured_reference_token(token: str) -> bool:
    if not token or len(token) < 4:
        return False
    lowered = token.lower()
    if lowered in REFERENCE_TOKEN_STOPWORDS:
        return False
    digit_count = sum(character.isdigit() for character in token)
    has_separator = any(character in "/-_" for character in token)
    alpha_count = sum(character.isalpha() for character in token)
    if digit_count > 0:
        return len(token) >= 5
    if has_separator and alpha_count > 0:
        return len(token) >= 5
    return False


def _select_reference_case_key(reference_tokens: set[str]) -> str:
    ordered = sorted((token for token in reference_tokens if token), key=lambda item: (-len(item), item))
    return ordered[0] if ordered else ""


def _build_evidence_sections(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any] | None,
    case_link_result: dict[str, Any] | None,
) -> dict[str, Any]:
    source_message = snapshot.get("source_message") or {}
    case_link_candidates = snapshot.get("case_link_candidates") or []
    normalized_subject = str(snapshot.get("normalized_subject") or "")
    reference_tokens = source_message.get("reference_tokens") or {}
    if not isinstance(reference_tokens, dict):
        reference_tokens = {}

    hard_evidence = {
        "sender": str(source_message.get("sender") or ""),
        "subject": str(source_message.get("subject") or ""),
        "thread_id": str(source_message.get("thread_id") or ""),
        "explicit_case_reference": list(reference_tokens.get("case") or []),
        "reference_tokens": reference_tokens,
    }
    soft_evidence = {
        "normalized_subject": normalized_subject,
        "thread_context_quality": str(snapshot.get("thread_context_quality") or "weak"),
        "routing_hints": snapshot.get("routing_hints") or {},
        "intake_decision": (intake_result or {}).get("decision") or {},
        "selected_case_key": str((case_link_result or {}).get("selected_case_key") or ""),
    }
    uncertain_links = [
        {
            "kind": str(candidate.get("case_type") or candidate.get("source") or "candidate"),
            "value": str(candidate.get("case_key") or ""),
            "confidence": round(float(candidate.get("match_confidence") or candidate.get("score") or 0.0), 4),
        }
        for candidate in case_link_candidates[:5]
        if isinstance(candidate, dict) and str(candidate.get("case_key") or "").strip()
    ]
    return {
        "hard_evidence": hard_evidence,
        "soft_evidence": soft_evidence,
        "uncertain_links": uncertain_links,
    }


def _pick_first_str(payload: dict[str, Any], *keys: str, default: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[;,]", value)
        return [item.strip() for item in parts if item.strip()]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                result.append(item.strip())
            elif isinstance(item, dict):
                text = _pick_first_str(item, "email", "address", "value", default="")
                if text:
                    result.append(text)
        return result
    return []


def _prepare_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = ZERO_WIDTH_RE.sub("", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = REPEATED_SPACE_RE.sub(" ", text)
    return text.strip()


def _compact_body_text(value: Any, max_chars: int) -> tuple[str, list[str]]:
    text = _prepare_text(value)
    if not text:
        return "", []

    lines = text.splitlines()
    notes: list[str] = []
    cleaned_lines: list[str] = []
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        lower = line.lower()
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        if _looks_like_quote_boundary(lower):
            notes.append("quoted_history_removed")
            break
        if lower.startswith(">"):
            notes.append("quoted_history_removed")
            continue
        if _looks_like_noise_line(lower):
            notes.append("html_noise_removed")
            continue
        if _looks_like_footer_boundary(lower) and index >= 2:
            notes.append("footer_noise_removed")
            break
        cleaned_lines.append(line)

    compact = "\n".join(cleaned_lines).strip()
    compact = REPEATED_BLANKS_RE.sub("\n\n", compact)
    if len(compact) > max_chars:
        compact = _smart_truncate(compact, max_chars)
        notes.append("body_truncated")
    return compact, sorted(set(notes))


def _looks_like_quote_boundary(lower_line: str) -> bool:
    if not lower_line:
        return False
    return any(pattern.search(lower_line) for pattern in QUOTE_BOUNDARY_PATTERNS)


def _looks_like_footer_boundary(lower_line: str) -> bool:
    return any(marker in lower_line for marker in FOOTER_BOUNDARY_MARKERS)


def _looks_like_noise_line(lower_line: str) -> bool:
    if not lower_line:
        return False
    if any(marker in lower_line for marker in NOISE_LINE_MARKERS) and len(lower_line) > 80:
        return True
    if lower_line.count("͏") > 3 or lower_line.count("�") > 3:
        return True
    return False


def _smart_truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 20:
        return text[:max_chars]
    clipped = text[: max_chars - 14].rstrip()
    for separator in ("\n\n", "\n", ". ", "; ", ", "):
        cut = clipped.rfind(separator)
        if cut >= max_chars // 3:
            clipped = clipped[:cut].rstrip()
            break
    return f"{clipped} [truncated]"


def _compact_inline_text(value: Any, max_chars: int) -> str:
    text = _prepare_text(value)
    if len(text) <= max_chars:
        return text
    return _smart_truncate(text, max_chars)


def _inference_budgets(mode: str) -> dict[str, int]:
    return INFERENCE_BUDGETS.get(mode, INFERENCE_BUDGETS[INFERENCE_MODE_COMPACT])
