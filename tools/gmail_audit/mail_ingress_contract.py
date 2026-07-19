"""HTTP contract for outbound mail-ingress smoke requests.

This module documents and implements the minimal client shape gmail-agent operators use
when validating the mail-ingress seam (POST JSON + agent key header). Production ingress
implementation lives outside this repository; tests exercise this contract against a local stub.

See docs/archive/runbooks/CROSS_REPO_LIVE_SMOKE_D1.md for Gate B evidence expectations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

import requests

# Typical WordPress ingress stacks use this header name; server may accept successors.
AGENT_KEY_HEADER = "X-Top-Instal-Agent-Key"


@dataclass(frozen=True)
class MailIngressResponse:
    """Normalized HTTP result for assertions and logging (avoid storing full bodies in proofs)."""

    status_code: int
    content_type: str | None
    body_preview: str


def post_mail_ingress_json(
    url: str,
    *,
    agent_key: str,
    payload: Mapping[str, Any],
    timeout_seconds: float = 30.0,
    idempotency_key: str | None = None,
    extra_headers: Mapping[str, str] | None = None,
) -> MailIngressResponse:
    """POST JSON to mail-ingress URL with required agent key header.

    ``url`` must be the full endpoint URL (as in ``MAIL_INGRESS_URL`` operator env).
    """
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        AGENT_KEY_HEADER: agent_key,
    }
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    if extra_headers:
        headers.update(dict(extra_headers))

    resp = requests.post(
        url,
        headers=headers,
        data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        timeout=timeout_seconds,
    )
    ct = resp.headers.get("Content-Type")
    text = resp.text or ""
    preview = text if len(text) <= 512 else text[:512] + "…"
    return MailIngressResponse(status_code=resp.status_code, content_type=ct, body_preview=preview)
