"""Internal API token verification for registry routes."""

from __future__ import annotations

import os
import hmac


def registry_token_configured() -> str:
    for key in (
        "NODE_B_REGISTRY_TOKEN",
        "DASZEK_NODE_B_API_TOKEN",
        "GMAIL_AGENT_INTERNAL_API_TOKEN",
    ):
        val = str(os.environ.get(key) or "").strip()
        if val:
            return val

    env_file = str(os.environ.get("GMAIL_AGENT_ENV_FILE") or "").strip()
    if env_file and os.path.isfile(env_file):
        try:
            from dotenv import dotenv_values

            values = dotenv_values(env_file)
            if isinstance(values, dict):
                for key in (
                    "NODE_B_REGISTRY_TOKEN",
                    "DASZEK_NODE_B_API_TOKEN",
                    "GMAIL_AGENT_INTERNAL_API_TOKEN",
                ):
                    val = str(values.get(key) or "").strip()
                    if val:
                        return val
        except Exception as exc:
            import logging; logging.getLogger("correlation_registry.auth").warning("auth: get_token_from_header failed: %s", exc)

    return ""


def verify_registry_bearer(authorization: str | None) -> bool:
    expected = registry_token_configured()
    if not expected:
        return False
    header = str(authorization or "").strip()
    if not header.lower().startswith("bearer "):
        return False
    provided = header[7:].strip()
    return hmac.compare_digest(provided, expected)
