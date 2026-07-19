"""Deterministic preclassification before intake LLM routing."""

from __future__ import annotations

import re
from typing import Any

from intake_policy import PRECLASSIFICATION_LANES
from log_config import get_logger

logger = get_logger("preclassifier")

# ── Preclassification constants ─────────────────────────────────────
PRECLASSIFY_SHORT_BODY_MAX_CHARS = 80
PRECLASSIFY_SHORT_SNIPPET_MAX_CHARS = 120

NOISE_SUBJECT_PATTERNS = (
    # Angielskie
    "newsletter",
    "unsubscribe",
    "verify your email",
    "verification code",
    "security alert",
    "login code",
    "password reset",
    "new sign-in",
    "marketing",
    # Polskie
    "biuletyn",
    "oferta handlowa",
    "reklama",
    "zaproszenie na webinar",
    "promocja",
    "wyprzedaz",
    "wypisz sie",
    "potwierdz email",
    "potwierdz adres",
    "nie przegap",
    "newsletter",
)
NOISE_SENDER_PATTERNS = (
    "noreply",
    "no-reply",
    "donotreply",
    "mailer-daemon",
    "postmaster",
    "notifications@",
)
REFERENCE_ONLY_PATTERNS = (
    "potwierdzenie",
    "confirmation",
    "payment received",
    "delivery update",
    "shipment update",
    "tracking",
    "invoice copy",
    "receipt",
)
REVIEW_DIRECT_PATTERNS = (
    "forwarded message",
    "original message",
    "fwd:",
    "fw:",
    "przekazana wiadomosc",
    "przeslana wiadomosc",
)
QUESTION_RE = re.compile(r"\?")

_KEYWORD_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def contains_noise_keyword(text: str, keyword: str) -> bool:
    """True if `keyword` appears in `text` as a standalone word/phrase.

    Plain substring containment (``keyword in text``) false-positives whenever a noise
    keyword is a prefix of a legitimate business word (e.g. "reklama" inside "reklamacja").
    Word-boundary matching keeps the same intent — cheap deterministic keyword lookup —
    without that collision, for both single words and multi-word phrases.
    """
    pattern = _KEYWORD_PATTERN_CACHE.get(keyword)
    if pattern is None:
        pattern = re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE)
        _KEYWORD_PATTERN_CACHE[keyword] = pattern
    return pattern.search(text) is not None


PRECLASSIFIER_STAGE_NAME = "preclassifier"


def preclassify_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a cheap deterministic lane before the main intake reasoning call."""
    message = snapshot.get("source_message") or {}
    subject = _message_text(message.get("subject"))
    sender = _message_text(message.get("sender"), message.get("sender_email"))

    noise_detail = _obvious_noise_reason(snapshot)
    if noise_detail is not None:
        reasons = ["obvious_noise", noise_detail]
        result = {"lane": "skip", "reasons": reasons, "confidence": 0.97, "stage_name": PRECLASSIFIER_STAGE_NAME}
        logger.info("PRECLASSIFY_DECISION", extra={"x": {
            "decision": "skip", "subject": subject[:100], "sender": sender,
            "reason": "obvious_noise", "reason_detail": noise_detail,
        }})
        return result

    reference_detail = _reference_only_reason(snapshot)
    if reference_detail is not None:
        reasons = ["reference_only_signal", reference_detail]
        result = {"lane": "reference_only", "reasons": reasons, "confidence": 0.82, "stage_name": PRECLASSIFIER_STAGE_NAME}
        logger.info("PRECLASSIFY_DECISION", extra={"x": {
            "decision": "reference_only", "subject": subject[:100], "sender": sender,
            "reason": "reference_only_signal", "reason_detail": reference_detail,
        }})
        return result

    if is_obvious_review_direct(snapshot):
        result = {
            "lane": "review_direct",
            "reasons": ["low_signal_forward_or_attachment_only"],
            "confidence": 0.84,
            "stage_name": PRECLASSIFIER_STAGE_NAME,
        }
        logger.info("PRECLASSIFY_DECISION", extra={"x": {
            "decision": "review_direct", "subject": subject[:100], "sender": sender,
            "reason": "low_signal_forward_or_attachment_only",
        }})
        return result

    result = {
        "lane": "intake_llm",
        "reasons": ["default_intake_lane"],
        "confidence": 0.75,
        "stage_name": PRECLASSIFIER_STAGE_NAME,
    }
    logger.info("PRECLASSIFY_DECISION", extra={"x": {
        "decision": "intake_llm", "subject": subject[:100], "sender": sender,
        "reason": "default_intake_lane",
    }})
    return result


def _obvious_noise_reason(snapshot: dict[str, Any]) -> str | None:
    """Return which specific rule matched, or None. Detail carries only fixed-vocabulary
    tokens from NOISE_SENDER_PATTERNS/NOISE_SUBJECT_PATTERNS — never raw message content.
    """
    message = snapshot.get("source_message") or {}
    sender = _message_text(message.get("sender"), message.get("sender_email"))
    subject = _message_text(message.get("subject"))
    body = _message_text(message.get("body"), message.get("snippet"))

    for token in NOISE_SENDER_PATTERNS:
        if token in sender:
            return f"noise_sender_pattern:{token}"
    for token in NOISE_SUBJECT_PATTERNS:
        if contains_noise_keyword(subject, token):
            return f"noise_subject_keyword:{token}"
    if re.search(r'\bunsubscribe\b', body, re.IGNORECASE) and re.search(r'\bnewsletter\b', body, re.IGNORECASE):
        return "noise_body_unsubscribe_newsletter"
    if "two-factor" in body or "one-time code" in body:
        return "noise_body_otp_signal"
    return None


def is_obvious_noise(snapshot: dict[str, Any]) -> bool:
    """Return True for clear system/noise traffic that should not consume LLM budget."""
    return _obvious_noise_reason(snapshot) is not None


def is_obvious_review_direct(snapshot: dict[str, Any]) -> bool:
    """Return True for low-signal messages that should go straight to review."""
    message = snapshot.get("source_message") or {}
    thread_context = snapshot.get("thread_context") or {}
    body = _message_text(message.get("body"))
    snippet = _message_text(message.get("snippet"))
    subject = _message_text(message.get("subject"))
    attachment_names = message.get("attachment_names") or []
    routing_hints = snapshot.get("routing_hints") or {}

    very_short_body = len(body) < PRECLASSIFY_SHORT_BODY_MAX_CHARS and len(snippet) < PRECLASSIFY_SHORT_SNIPPET_MAX_CHARS
    attachment_only = bool(attachment_names) and very_short_body
    forwarded_chaos = any(token in body or token in subject for token in REVIEW_DIRECT_PATTERNS) and len(body) < 320
    weak_thread = str(thread_context.get("quality") or "weak") == "weak"
    self_forward = bool(routing_hints.get("self_forward"))

    return attachment_only or forwarded_chaos or (self_forward and weak_thread and very_short_body)


def _reference_only_reason(snapshot: dict[str, Any]) -> str | None:
    """Return which specific REFERENCE_ONLY_PATTERNS token matched, or None."""
    message = snapshot.get("source_message") or {}
    subject = _message_text(message.get("subject"))
    body = _message_text(message.get("body"), message.get("snippet"))
    has_question = bool(QUESTION_RE.search(body)) or bool(QUESTION_RE.search(subject))
    has_request_language = any(token in body for token in ("prosze", "please", "can you", "czy mozecie", "need", "potrzeb"))

    if has_question or has_request_language:
        return None

    for token in REFERENCE_ONLY_PATTERNS:
        if token in subject or token in body:
            return f"reference_only_pattern:{token}"
    return None


def is_reference_only(snapshot: dict[str, Any]) -> bool:
    """Return True for informational mail that should stay visible without action creation."""
    return _reference_only_reason(snapshot) is not None


def _message_text(*parts: Any) -> str:
    text = " ".join(str(part or "").strip().lower() for part in parts if str(part or "").strip())
    return " ".join(text.split())


__all__ = [
    "PRECLASSIFICATION_LANES",
    "PRECLASSIFIER_STAGE_NAME",
    "contains_noise_keyword",
    "is_obvious_noise",
    "is_obvious_review_direct",
    "is_reference_only",
    "preclassify_snapshot",
]
