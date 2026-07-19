
"""Signal Source Registry (PR-Signal) — zastępuje if/elif w reconcile_signal.

Generic Hands compliant: rejestracja handlera = @register_signal_handler("source_kind").
Zero if/elif na source_kind.
"""

from __future__ import annotations

from log_config import get_logger
from typing import Any, Callable

logger = get_logger(__name__)

# Registry: source_kind → handler function
SIGNAL_HANDLERS: dict[str, Callable] = {}


def register_signal_handler(source_kind: str):
    """Dekorator rejestrujący handler dla danego source_kind.

    Użycie:
        @register_signal_handler("gmail")
        def handle_gmail(signal, runtime_context, dry_run, entity_link_dict):
            ...

    Handler signature:
        def handler(signal, *, runtime_context, dry_run, entity_link_dict) -> Any:
    """
    def decorator(func):
        SIGNAL_HANDLERS[source_kind] = func
        logger.debug("signal_handler_registered source_kind=%s handler=%s", source_kind, func.__name__)
        return func
    return decorator


def get_handler(source_kind: str) -> Callable | None:
    """Zwraca handler dla source_kind lub None."""
    return SIGNAL_HANDLERS.get(source_kind)


def registered_source_kinds() -> list[str]:
    """Zwraca listę zarejestrowanych source_kind."""
    return list(SIGNAL_HANDLERS.keys())


__all__ = [
    "SIGNAL_HANDLERS",
    "register_signal_handler",
    "get_handler",
    "registered_source_kinds",
]
