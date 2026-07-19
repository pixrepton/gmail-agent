"""Canonical bridge between lifecycle intent, persistence command, and trace type."""

from __future__ import annotations


CANONICAL_LIFECYCLE_INTENTS = (
    "create",
    "update",
    "escalate_presence",
    "deescalate_presence",
    "move_to_case_only",
    "resolve",
    "withdraw",
    "suppress",
    "noop",
)

CANONICAL_DESK_NOTE_COMMANDS = (
    "create",
    "update",
    "escalate_presence",
    "deescalate_presence",
    "resolve",
    "merge",
    "withdraw",
    "suppress",
)


def normalize_lifecycle_intent(lifecycle_intent: str, target_zone: str | None = None) -> str:
    intent = str(lifecycle_intent or "").strip()
    if is_case_only_transition(intent, target_zone):
        return "move_to_case_only"
    if intent in CANONICAL_LIFECYCLE_INTENTS:
        return intent
    return "noop"


def is_case_only_transition(lifecycle_intent: str, target_zone: str | None = None) -> bool:
    return str(target_zone or "").strip() == "case_only" or str(lifecycle_intent or "").strip() == "move_to_case_only"


def command_from_lifecycle_intent(lifecycle_intent: str, target_zone: str | None = None) -> str:
    intent = normalize_lifecycle_intent(lifecycle_intent, target_zone)
    if intent == "move_to_case_only":
        return "deescalate_presence"
    if intent in CANONICAL_DESK_NOTE_COMMANDS:
        return intent
    return ""


def decision_type_from_command(command: str, lifecycle_intent: str | None = None, target_zone: str | None = None) -> str:
    normalized_command = str(command or "").strip()
    if is_case_only_transition(str(lifecycle_intent or "").strip(), target_zone):
        return "move_to_case_only"
    if normalized_command == "suppress":
        return "suppress_note"
    if normalized_command in CANONICAL_DESK_NOTE_COMMANDS:
        return f"{normalized_command}_note"
    raise ValueError(f"Unsupported desk-note command for decision trace: {normalized_command or '<missing>'}")


__all__ = [
    "CANONICAL_DESK_NOTE_COMMANDS",
    "CANONICAL_LIFECYCLE_INTENTS",
    "command_from_lifecycle_intent",
    "decision_type_from_command",
    "is_case_only_transition",
    "normalize_lifecycle_intent",
]
