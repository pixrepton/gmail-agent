"""Parse comma/semicolon/newline-separated API key pools from env values."""

from __future__ import annotations

import re

_SPLIT_RE = re.compile(r"[\s,;|]+")


def parse_api_key_pool(*values: str) -> tuple[str, ...]:
    """Merge multiple env fragments into one deduplicated key pool (order preserved)."""
    seen: set[str] = set()
    keys: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        for part in _SPLIT_RE.split(text):
            key = part.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
    return tuple(keys)
