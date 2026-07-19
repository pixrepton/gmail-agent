"""Shared redaction helpers for Gmail Intake diagnostics and artifacts."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any


SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "groq_api_key",
        "apikey",
        "access_token",
        "google_access_token",
        "refresh_token",
        "google_refresh_token",
        "token",
        "secret",
        "client_secret",
        "google_client_secret",
        "password",
        "cookie",
        "set-cookie",
        "csrf_token",
        "x-csrf-token",
        "session",
        "session_id",
        "sessionid",
    }
)

REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", flags=re.IGNORECASE), "Bearer <redacted>"),
    (re.compile(r"\bya29\.[A-Za-z0-9._\-]+\b"), "<redacted-google-access-token>"),
    (re.compile(r"\bgsk_[A-Za-z0-9]+\b"), "<redacted-groq-api-key>"),
    (
        re.compile(
            r'("?(?:authorization|api_key|groq_api_key|apikey|access_token|google_access_token|refresh_token|google_refresh_token|token|secret|client_secret|google_client_secret|password|cookie|set-cookie|csrf_token|x-csrf-token|session|session_id|sessionid)"?\s*[:=]\s*")([^"]+)(")',
            flags=re.IGNORECASE,
        ),
        r"\1<redacted>\3",
    ),
    (
        re.compile(
            r"((?:authorization|api_key|groq_api_key|apikey|access_token|google_access_token|refresh_token|google_refresh_token|token|secret|client_secret|google_client_secret|password|cookie|set-cookie|csrf_token|x-csrf-token|session|session_id|sessionid)\s*[:=]\s*)([^\s,;]+)",
            flags=re.IGNORECASE,
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(r"([?&](?:access_token|google_access_token|refresh_token|google_refresh_token|token|api_key|groq_api_key|apikey|password|secret|client_secret|google_client_secret)=)[^&\s]+", flags=re.IGNORECASE),
        r"\1<redacted>",
    ),
)


def sanitize_text(text: str) -> str:
    """Redact auth-like values from arbitrary text."""
    sanitized = text
    for pattern, replacement in REDACTION_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def sanitize_for_storage(value: Any) -> Any:
    """Recursively redact auth-like values before writing them to disk."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in SENSITIVE_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = sanitize_for_storage(item)
        return redacted

    if isinstance(value, list):
        return [sanitize_for_storage(item) for item in value]

    if isinstance(value, tuple):
        return [sanitize_for_storage(item) for item in value]

    if isinstance(value, str):
        return sanitize_text(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, date):
        return value.isoformat()

    return value


def sanitize_error_message(message: str) -> str:
    """Return a trimmed and redacted error string suitable for operator-facing logs."""
    return sanitize_text(message.strip())


def mask_secret(secret: str, *, keep_start: int = 4, keep_end: int = 3) -> str:
    """Return a short masked representation without exposing the full secret."""
    value = secret.strip()
    if not value:
        return "<missing>"
    if len(value) <= keep_start + keep_end:
        return "<masked>"
    return f"{value[:keep_start]}...{value[-keep_end:]}"
