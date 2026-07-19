"""D3 ingress — Gmail mutation only via signal-worker / signal-run (signal-active)."""

from __future__ import annotations

from typing import Any

from config import ConfigError, Settings

_LEGACY_MUTATING_COMMANDS = frozenset({"message", "period", "batch", "shadow-run"})


def enforce_legacy_cli_ingress_allowed(
    settings: Settings,
    *,
    command: str,
    force_legacy_ingress: bool = False,
) -> None:
    """Raise when CLI would mutate mailbox outside signal-active ingress."""

    _ = (settings, force_legacy_ingress)
    cmd = str(command or "").strip().lower()
    if cmd not in _LEGACY_MUTATING_COMMANDS:
        return
    raise ConfigError(
        f"Command {cmd!r} is disabled. gmail-agent uses signal-active only. "
        "Use: gmail_intake.py signal-worker | signal-run --oneshot"
    )


def ingress_owner_warnings(settings: Settings) -> list[str]:
    """Ingress warnings (D3 resolved — single signal_worker owns Gmail)."""
    return []


__all__ = ["enforce_legacy_cli_ingress_allowed", "ingress_owner_warnings"]
