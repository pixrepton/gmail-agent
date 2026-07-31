"""Runtime Authorization Gate — FastAPI dependency for /agent-chat and graph tool execution.

Token jest pobierany z nagłówka Authorization: Bearer.
Weryfikacja przez hmac.compare_digest — pattern zgodny z correlation_registry/auth.py.

Generic Hands compliant: zero if/elif na tool_name.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Header, HTTPException, status

# ── Poziomy dostępu ──────────────────────────────────────────────────────
# Każdy tool ma przypisany minimalny poziom. operator = pełny dostęp.
# service = dostęp tylko do odczytu (read tools).
TOOL_PERMISSION_LEVELS: dict[str, str] = {
    # Mail agent — read/search tools
    "search_gmail_thread": "service",
    "list_drive_folder": "service",
    "call_kalk_top_quote": "service",
    "generate_draft_reply": "service",
    "search_rag_knowledge": "service",
    # Read tools — operator i service
    "read_google_drive_file": "service",
    "extract_facts_from_text": "service",
    "check_cp2025_eligibility": "service",
    "report_gaps_and_stop": "service",
    "retry_hard_parse": "service",
    # Write tools — tylko operator
    "propose_mutation": "operator",
    "propose_plan": "operator",
    "request_operator_clarification": "operator",
}

# Poziomy dostępu dla operacji write (wewnątrz propose_mutation / propose_plan)
WRITE_OPERATION_PERMISSIONS: dict[str, str] = {
    "delete_document": "operator",
    "move_document": "operator",
    "merge_cases": "operator",
    "create_case": "operator",
    "update_case_status": "operator",
    "add_case_note": "operator",
    "add_case_label": "operator",
    "archive_case": "operator",
    "restore_case": "operator",
    "reassign_case": "operator",
    "link_case_to_case": "operator",
    "update_customer_info": "operator",
    "generate_draft": "operator",
    "add_deadline": "operator",
}


# ── Token helpers ────────────────────────────────────────────────────────


def _expected_token() -> str:
    for key in (
        "DASZEK_NODE_B_API_TOKEN",
        "GMAIL_AGENT_INTERNAL_API_TOKEN",
        "NODE_B_REGISTRY_TOKEN",
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
                    "DASZEK_NODE_B_API_TOKEN",
                    "GMAIL_AGENT_INTERNAL_API_TOKEN",
                    "NODE_B_REGISTRY_TOKEN",
                ):
                    val = str(values.get(key) or "").strip()
                    if val:
                        return val
        except Exception as exc:
            import logging; logging.getLogger("authz").warning("authz: get_api_key_from_header failed: %s", exc)
    return ""


def _expected_read_only_token() -> str:
    for key in (
        "NODE_B_READ_ONLY_TOKEN",
        "GMAIL_AGENT_READ_ONLY_API_TOKEN",
    ):
        val = str(os.environ.get(key) or "").strip()
        if val:
            return val
    return ""


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _write_dev_bypass_enabled() -> bool:
    profile = str(os.environ.get("GMAIL_AGENT_RUNTIME_PROFILE") or "").strip().lower()
    if profile == "canonical_production":
        return False
    return _env_truthy("NODE_B_TASK_WRITE_DEV_BYPASS")


def verify_operator_token(authorization: str | None) -> bool:
    """Weryfikacja Bearer token — operator ma 'operator' scope."""
    expected = _expected_token()
    if not expected:
        # Brak skonfigurowanego tokena = tryb deweloperski, przepuszczamy
        return True
    header = str(authorization or "").strip()
    if not header.lower().startswith("bearer "):
        return False
    provided = header[7:].strip()
    return hmac.compare_digest(provided, expected)


def verify_read_only_token(authorization: str | None) -> bool:
    expected = _expected_read_only_token()
    if not expected:
        return False
    header = str(authorization or "").strip()
    if not header.lower().startswith("bearer "):
        return False
    provided = header[7:].strip()
    return hmac.compare_digest(provided, expected)


def token_scope(authorization: str | None) -> str:
    """Zwraca scope tokena: 'operator' lub '' (brak)."""
    if verify_operator_token(authorization):
        return "operator"
    if verify_read_only_token(authorization):
        return "read_only"
    return ""


def check_tool_permission(tool_name: str, scope: str) -> bool:
    """Sprawdza czy scope ma uprawnienie do narzędzia."""
    required = TOOL_PERMISSION_LEVELS.get(tool_name, "operator")
    if required == "service" and scope in ("operator", "service"):
        return True
    if required == "operator" and scope == "operator":
        return True
    return False


def check_write_operation_permission(operation: str, scope: str) -> bool:
    """Sprawdza czy scope ma uprawnienie do operacji write."""
    required = WRITE_OPERATION_PERMISSIONS.get(operation, "operator")
    if required == "operator" and scope == "operator":
        return True
    return False


# ── FastAPI dependency ───────────────────────────────────────────────────


async def get_current_operator(
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI dependency — weryfikuje token i zwraca scope operatora.

    Returns:
        "operator" gdy token prawidłowy lub brak tokena (dev mode).
        Rzuca HTTPException 401 gdy token nieprawidłowy.

    Użycie:
        @app.post("/agent-chat")
        def agent_chat(payload: dict, operator: str = Depends(get_current_operator)):
            ...
    """
    scope = token_scope(authorization)
    if not scope:
        expected = _expected_token()
        if expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing operator token.",
            )
        # Dev mode — brak skonfigurowanego tokena
        return "operator"
    return scope


def require_mutation_token(authorization: str | None) -> str:
    """Fail-closed write auth for mutation routes.

    Unlike get_current_operator(), missing token does not open writes by default.
    Local bypass requires an explicit env flag and is disabled in canonical_production.
    """
    expected = _expected_token()
    header = str(authorization or "").strip()
    if expected:
        if header.lower().startswith("bearer "):
            provided = header[7:].strip()
            if hmac.compare_digest(provided, expected):
                return "operator"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing mutation token.",
        )
    if _write_dev_bypass_enabled():
        return "operator"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing mutation token.",
    )


# ── Verified mutation principal (AUTH-02 / AUTH-03) ─────────────────────


@dataclass(frozen=True)
class MutationPrincipal:
    """Verified identity for a mutation request.

    operator_id is derived solely from the verified credential (never from
    request body), so a client cannot stamp decisions/journals/events with an
    arbitrary identity. Today the runtime has a single shared mutation
    credential, so operator_id mirrors scope ("operator"); if per-operator
    credentials are introduced later, this is the one place to resolve them.
    """

    operator_id: str
    scope: str


def require_mutation_principal(
    authorization: str | None = Header(default=None),
) -> MutationPrincipal:
    """Canonical default-deny auth gate for mutation routes.

    Thin wrapper around require_mutation_token() (AUTH-01) that also exposes
    the verified principal, so handlers stamp identity from the credential
    instead of trusting a client-supplied operator_id field.
    """
    scope = require_mutation_token(authorization)
    return MutationPrincipal(operator_id=scope, scope=scope)


# ── Graph-level guard ────────────────────────────────────────────────────


def guard_tool_authz(
    tool_name: str,
    *,
    scope: str,
    operation: str | None = None,
) -> str | None:
    """Sprawdza uprawnienia dla narzędzia na poziomie graph engine.

    Args:
        tool_name: Nazwa narzędzia (np. 'propose_mutation', 'search_rag_knowledge')
        scope: Scope operatora ('operator' lub '')
        operation: Opcjonalna operacja write (np. 'delete_document')

    Returns:
        None gdy OK, string z komunikatem błędu gdy brak uprawnień.
    """
    if not check_tool_permission(tool_name, scope):
        return f"Brak uprawnień do narzędzia {tool_name} (wymagany poziom: {TOOL_PERMISSION_LEVELS.get(tool_name, 'operator')})."
    if operation and not check_write_operation_permission(operation, scope):
        return f"Brak uprawnień do operacji {operation} (wymagany poziom operator)."
    return None


__all__ = [
    "require_mutation_token",
    "require_mutation_principal",
    "MutationPrincipal",
    "get_current_operator",
    "guard_tool_authz",
    "verify_operator_token",
    "verify_read_only_token",
    "check_tool_permission",
    "TOOL_PERMISSION_LEVELS",
]
