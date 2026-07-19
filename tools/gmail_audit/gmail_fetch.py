"""Read-only Gmail fetch helpers with selectable fetch source."""

from __future__ import annotations

import json
from typing import Any

from config import Settings
from google_gmail_api import (
    GoogleGmailApiError,
    get_message_metadata as get_message_metadata_google_api,
    get_profile as get_profile_google_api,
    get_recent_emails as get_recent_emails_google_api,
    get_thread_messages as get_thread_messages_google_api,
    read_email as read_email_google_api,
    search_email_metadata as search_email_metadata_google_api,
    search_emails as search_emails_google_api,
)
from groq_client import (
    GroqClientError,
    extract_json_candidate,
    extract_mcp_output,
    format_connector_tool_error,
    request_audit,
)


GMAIL_SOURCE_GOOGLE_API = "google_api"
GMAIL_SOURCE_GROQ_CONNECTOR = "groq_connector"
DEFAULT_GMAIL_SOURCE = GMAIL_SOURCE_GOOGLE_API
VALID_GMAIL_SOURCES = (GMAIL_SOURCE_GOOGLE_API, GMAIL_SOURCE_GROQ_CONNECTOR)


def get_profile(
    settings: Settings,
    *,
    model: str | None = None,
    verbose: bool = False,
    gmail_source: str = DEFAULT_GMAIL_SOURCE,
) -> dict[str, Any]:
    """Fetch mailbox profile metadata from the selected Gmail source."""
    source = normalize_gmail_source(gmail_source)
    if source == GMAIL_SOURCE_GOOGLE_API:
        try:
            return get_profile_google_api(settings, verbose=verbose)
        except GoogleGmailApiError as exc:
            raise GroqClientError(str(exc)) from exc

    prompt = (
        "Use the Gmail connector tool `get_profile` exactly once.\n"
        "Return ONLY the raw tool result as valid JSON.\n"
        "No markdown. No commentary."
    )
    return _request_object_tool(settings, prompt, tool_name="get_profile", model=model, verbose=verbose)


def search_emails(
    settings: Settings,
    *,
    query: str,
    max_results: int = 25,
    next_page_token: str | None = None,
    model: str | None = None,
    verbose: bool = False,
    gmail_source: str = DEFAULT_GMAIL_SOURCE,
) -> dict[str, Any]:
    """Search mailbox messages using the selected Gmail source."""
    source = normalize_gmail_source(gmail_source)
    if source == GMAIL_SOURCE_GOOGLE_API:
        try:
            return search_emails_google_api(
                settings,
                query=query,
                max_results=max_results,
                next_page_token=next_page_token,
                verbose=verbose,
            )
        except GoogleGmailApiError as exc:
            raise GroqClientError(str(exc)) from exc

    token_text = next_page_token or ""
    prompt = (
        "Use the Gmail connector tool `search_emails` exactly once.\n"
        f"Query: {query}\n"
        f"max_results: {max_results}\n"
        f'next_page_token: "{token_text}"\n'
        "Return ONLY the raw tool result as valid JSON.\n"
        "No markdown. No commentary."
    )
    payload = _request_object_tool(settings, prompt, tool_name="search_emails", model=model, verbose=verbose)
    responses = payload.get("responses")
    if not isinstance(responses, list):
        raise GroqClientError("search_emails did not return a `responses` list.")
    return payload


def search_email_metadata(
    settings: Settings,
    *,
    query: str,
    max_results: int = 25,
    next_page_token: str | None = None,
    model: str | None = None,
    verbose: bool = False,
    gmail_source: str = DEFAULT_GMAIL_SOURCE,
) -> dict[str, Any]:
    """Search mailbox messages using metadata-only Google API semantics."""
    source = normalize_gmail_source(gmail_source)
    if source != GMAIL_SOURCE_GOOGLE_API:
        raise GroqClientError("gmail historical metadata scan requires gmail_source=google_api.")
    _ = model
    try:
        return search_email_metadata_google_api(
            settings,
            query=query,
            max_results=max_results,
            next_page_token=next_page_token,
            verbose=verbose,
        )
    except GoogleGmailApiError as exc:
        raise GroqClientError(str(exc)) from exc


def get_message_metadata(
    settings: Settings,
    *,
    message_id: str,
    model: str | None = None,
    verbose: bool = False,
    gmail_source: str = DEFAULT_GMAIL_SOURCE,
) -> dict[str, Any]:
    """Fetch one message with metadata-only Google API semantics."""
    source = normalize_gmail_source(gmail_source)
    if source != GMAIL_SOURCE_GOOGLE_API:
        raise GroqClientError("gmail historical metadata scan requires gmail_source=google_api.")
    _ = model
    try:
        return get_message_metadata_google_api(settings, message_id=message_id, verbose=verbose)
    except GoogleGmailApiError as exc:
        raise GroqClientError(str(exc)) from exc


def search_emails_paginated(
    settings: Settings,
    *,
    query: str,
    limit: int,
    page_size: int = 25,
    model: str | None = None,
    verbose: bool = False,
    gmail_source: str = DEFAULT_GMAIL_SOURCE,
) -> list[dict[str, Any]]:
    """Collect up to `limit` search results using repeated pagination."""
    selected: list[dict[str, Any]] = []
    next_page_token = ""

    while len(selected) < limit:
        remaining = limit - len(selected)
        payload = search_emails(
            settings,
            query=query,
            max_results=min(page_size, remaining),
            next_page_token=next_page_token or None,
            model=model,
            verbose=verbose,
            gmail_source=gmail_source,
        )
        responses = payload["responses"]
        page_items = [item for item in responses if isinstance(item, dict)]
        selected.extend(page_items)

        next_page_token = str(payload.get("next_page_token") or "").strip()
        if not next_page_token or not page_items:
            break

    return selected[:limit]


def get_recent_emails(
    settings: Settings,
    *,
    max_results: int = 25,
    model: str | None = None,
    verbose: bool = False,
    gmail_source: str = DEFAULT_GMAIL_SOURCE,
) -> dict[str, Any]:
    """Fetch recent mailbox messages using the selected Gmail source."""
    source = normalize_gmail_source(gmail_source)
    if source == GMAIL_SOURCE_GOOGLE_API:
        try:
            return get_recent_emails_google_api(
                settings,
                max_results=max_results,
                verbose=verbose,
            )
        except GoogleGmailApiError as exc:
            raise GroqClientError(str(exc)) from exc

    prompt = (
        "Use the Gmail connector tool `get_recent_emails` exactly once.\n"
        f"max_results: {max_results}\n"
        "Return ONLY the raw tool result as valid JSON.\n"
        "No markdown. No commentary."
    )
    payload = _request_object_tool(
        settings,
        prompt,
        tool_name="get_recent_emails",
        model=model,
        verbose=verbose,
    )
    responses = payload.get("responses")
    if not isinstance(responses, list):
        raise GroqClientError("get_recent_emails did not return a `responses` list.")
    return payload


def read_email(
    settings: Settings,
    *,
    message_id: str,
    model: str | None = None,
    verbose: bool = False,
    gmail_source: str = DEFAULT_GMAIL_SOURCE,
) -> dict[str, Any]:
    """Read a single message in full using the selected Gmail source."""
    source = normalize_gmail_source(gmail_source)
    if source == GMAIL_SOURCE_GOOGLE_API:
        try:
            return read_email_google_api(
                settings,
                message_id=message_id,
                verbose=verbose,
            )
        except GoogleGmailApiError as exc:
            raise GroqClientError(str(exc)) from exc

    prompt = (
        "Use the Gmail connector tool `read_email` exactly once.\n"
        f"Read the Gmail message with this exact message id: {message_id}\n"
        "Return ONLY the raw tool result as valid JSON.\n"
        "No markdown. No commentary."
    )
    payload = _request_json_tool(settings, prompt, tool_name="read_email", model=model, verbose=verbose)

    if isinstance(payload, dict):
        if isinstance(payload.get("response"), dict):
            return payload["response"]
        if isinstance(payload.get("responses"), list) and payload["responses"]:
            first = payload["responses"][0]
            if isinstance(first, dict):
                return first
        return payload

    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return first

    raise GroqClientError("read_email returned an unexpected payload shape.")


def get_thread_messages(
    settings: Settings,
    *,
    thread_id: str,
    model: str | None = None,
    verbose: bool = False,
    gmail_source: str = DEFAULT_GMAIL_SOURCE,
) -> list[dict[str, Any]]:
    """Return thread messages when the selected source supports direct thread fetch."""
    source = normalize_gmail_source(gmail_source)
    if source == GMAIL_SOURCE_GOOGLE_API:
        try:
            return get_thread_messages_google_api(
                settings,
                thread_id=thread_id,
                verbose=verbose,
            )
        except GoogleGmailApiError as exc:
            raise GroqClientError(str(exc)) from exc
    return []


def build_period_query(base_query: str, *, days: int | None) -> str:
    """Build a bounded Gmail query for period-based runs."""
    query = base_query.strip()
    if days is None:
        return query
    suffix = f" newer_than:{days}d"
    if suffix.strip() in query:
        return query
    return f"{query}{suffix}".strip()


def normalize_gmail_source(raw_value: str | None) -> str:
    """Normalize and validate the selected Gmail fetch source."""
    source = str(raw_value or DEFAULT_GMAIL_SOURCE).strip().lower()
    if source not in VALID_GMAIL_SOURCES:
        allowed = ", ".join(VALID_GMAIL_SOURCES)
        raise GroqClientError(f"Unsupported gmail source `{source}`. Expected one of: {allowed}.")
    return source


def _request_object_tool(
    settings: Settings,
    prompt: str,
    *,
    tool_name: str,
    model: str | None,
    verbose: bool,
) -> dict[str, Any]:
    payload = _request_json_tool(settings, prompt, tool_name=tool_name, model=model, verbose=verbose)
    if not isinstance(payload, dict):
        raise GroqClientError(f"{tool_name} returned JSON, but not an object.")
    return payload


def _request_json_tool(
    settings: Settings,
    prompt: str,
    *,
    tool_name: str,
    model: str | None,
    verbose: bool,
) -> dict[str, Any] | list[Any]:
    result = request_audit(settings, prompt, model=model, verbose=verbose)
    raw_tool_output = extract_mcp_output(result.response_json, tool_name=tool_name)
    stripped = raw_tool_output.strip()
    if stripped.lower().startswith("error:"):
        raise GroqClientError(format_connector_tool_error(stripped, tool_name=tool_name))
    candidate = extract_json_candidate(raw_tool_output)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise GroqClientError(f"{tool_name} did not return valid JSON: {exc}") from exc

    if not isinstance(payload, (dict, list)):
        raise GroqClientError(f"{tool_name} returned an unsupported JSON root.")
    return payload
