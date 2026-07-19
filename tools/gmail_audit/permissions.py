"""Minimal Daszek AI-native V1 permission model.

This is intentionally not enterprise RBAC. It only models the two owners and
the agent service account needed by the supervised execution loop.
"""

from __future__ import annotations

from typing import Any


OWNER_ROLE = "owner"
AGENT_SERVICE_ROLE = "agent_service"
UNKNOWN_ROLE = "unknown"

OWNER_ACTORS = ("konrad", "darek")
AGENT_SERVICE_ACTORS = ("agent_service", "daszek")


class PermissionDenied(RuntimeError):
    """Raised when a caller is outside the minimal V1 permission envelope."""


def actor_role(actor_id: str) -> str:
    actor = str(actor_id or "").strip().lower()
    if actor in OWNER_ACTORS:
        return OWNER_ROLE
    if actor in AGENT_SERVICE_ACTORS:
        return AGENT_SERVICE_ROLE
    return UNKNOWN_ROLE


def require_owner(actor_id: str, *, allow_test_bypass: bool = False) -> None:
    actor = str(actor_id or "").strip()
    if allow_test_bypass and actor == "test_owner":
        return
    if actor_role(actor) != OWNER_ROLE:
        raise PermissionDenied(f"owner role required for actor={actor or '<missing>'}")


def permission_snapshot(actor_id: str) -> dict[str, Any]:
    role = actor_role(actor_id)
    return {
        "actor_id": str(actor_id or "").strip(),
        "role": role,
        "can_view_all_cases": role == OWNER_ROLE,
        "can_approve_actions": role == OWNER_ROLE,
        "can_reject_actions": role == OWNER_ROLE,
        "can_execute_approved_actions": role == OWNER_ROLE,
        "can_write_eval_artifacts": role in {OWNER_ROLE, AGENT_SERVICE_ROLE},
        "full_rbac": False,
    }


__all__ = [
    "AGENT_SERVICE_ACTORS",
    "AGENT_SERVICE_ROLE",
    "OWNER_ACTORS",
    "OWNER_ROLE",
    "PermissionDenied",
    "actor_role",
    "permission_snapshot",
    "require_owner",
]
