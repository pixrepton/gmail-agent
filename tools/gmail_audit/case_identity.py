"""Canonical case-id derivation shared by runtime and case intelligence."""

from __future__ import annotations

import hashlib
from typing import Any


def stable_case_id(prefix: str, *parts: str) -> str:
    seed = "::".join(str(part or "").strip() for part in parts if str(part or "").strip())
    if not seed:
        seed = prefix
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def canonicalize_case_anchor(*, anchor: str, thread_id: str = "", message_id: str = "") -> str:
    normalized = str(anchor or "").strip()
    if not normalized:
        return ""
    lowered = normalized.lower()
    if thread_id and normalized == thread_id:
        return f"thread:{thread_id}"
    if message_id and normalized == message_id and not thread_id:
        return f"message:{message_id}"
    if lowered.startswith("thread:"):
        alias = normalized.split(":", 1)[1].strip()
        if alias and thread_id and alias == thread_id:
            return f"thread:{thread_id}"
    if lowered.startswith("message:"):
        alias = normalized.split(":", 1)[1].strip()
        if alias and message_id and alias == message_id:
            return f"message:{message_id}"
    return normalized


def derive_canonical_case_id(
    *,
    case_family: str,
    selected_case_key: str = "",
    projected_case_key: str = "",
    reference_tokens: dict[str, Any] | None = None,
    thread_id: str = "",
    message_id: str = "",
) -> str:
    normalized_family = str(case_family or "unknown").strip() or "unknown"
    for candidate in (selected_case_key, projected_case_key):
        anchor = canonicalize_case_anchor(anchor=candidate, thread_id=thread_id, message_id=message_id)
        if anchor:
            return stable_case_id("case", normalized_family, anchor)
    if isinstance(reference_tokens, dict):
        for value in reference_tokens.values():
            if isinstance(value, list):
                for item in value:
                    anchor = canonicalize_case_anchor(anchor=str(item or ""), thread_id=thread_id, message_id=message_id)
                    if anchor:
                        return stable_case_id("case", normalized_family, anchor)
            else:
                anchor = canonicalize_case_anchor(anchor=str(value or ""), thread_id=thread_id, message_id=message_id)
                if anchor:
                    return stable_case_id("case", normalized_family, anchor)
    anchor = canonicalize_case_anchor(anchor=thread_id.strip(), thread_id=thread_id, message_id=message_id)
    if not anchor:
        anchor = canonicalize_case_anchor(anchor=message_id.strip(), thread_id=thread_id, message_id=message_id)
    return stable_case_id("case", normalized_family, anchor)


__all__ = [
    "canonicalize_case_anchor",
    "derive_canonical_case_id",
    "stable_case_id",
]
