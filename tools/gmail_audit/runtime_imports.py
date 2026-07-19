"""Lazy runtime imports for Gmail-specific auth and fetch helpers.

This seam keeps lightweight runtime/test imports away from the full Gmail
OAuth and Google client stack until a live mailbox operation actually needs it.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import import_module
from types import ModuleType
from typing import Any


DEFAULT_GMAIL_SOURCE = "google_api"
VALID_GMAIL_SOURCES = ("google_api", "groq_connector")


@lru_cache(maxsize=1)
def _gmail_fetch_module() -> ModuleType:
    return import_module("gmail_fetch")


@lru_cache(maxsize=1)
def _gmail_auth_module() -> ModuleType:
    return import_module("gmail_auth")


def build_period_query(*args: Any, **kwargs: Any) -> str:
    return _gmail_fetch_module().build_period_query(*args, **kwargs)


def get_profile(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _gmail_fetch_module().get_profile(*args, **kwargs)


def get_message_metadata(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _gmail_fetch_module().get_message_metadata(*args, **kwargs)


def get_thread_messages(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return _gmail_fetch_module().get_thread_messages(*args, **kwargs)


def normalize_gmail_source(*args: Any, **kwargs: Any) -> str:
    return _gmail_fetch_module().normalize_gmail_source(*args, **kwargs)


def read_email(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _gmail_fetch_module().read_email(*args, **kwargs)


def search_emails(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _gmail_fetch_module().search_emails(*args, **kwargs)


def search_email_metadata(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _gmail_fetch_module().search_email_metadata(*args, **kwargs)


def build_google_auth_check(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _gmail_auth_module().build_google_auth_check(*args, **kwargs)


def run_google_direct_auth_check(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _gmail_auth_module().run_google_direct_auth_check(*args, **kwargs)


__all__ = [
    "DEFAULT_GMAIL_SOURCE",
    "VALID_GMAIL_SOURCES",
    "build_google_auth_check",
    "build_period_query",
    "get_message_metadata",
    "get_profile",
    "get_thread_messages",
    "normalize_gmail_source",
    "read_email",
    "run_google_direct_auth_check",
    "search_email_metadata",
    "search_emails",
]
