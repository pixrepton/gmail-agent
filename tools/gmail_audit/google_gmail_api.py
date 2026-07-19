"""Direct Gmail API helpers for local Gmail fetch without the Groq connector."""

from __future__ import annotations

import base64
import binascii
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import getaddresses
from html import unescape
from typing import Any
from urllib.parse import quote

import requests

from config import Settings
from gmail_auth import GoogleOAuthError, get_gmail_credentials
from redaction import sanitize_text


GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
RETRYABLE_GMAIL_STATUSES = {408, 429, 500, 502, 503, 504}
METADATA_HEADERS = ("From", "To", "Cc", "Bcc", "Subject", "Date")


class GoogleGmailApiError(RuntimeError):
    """Raised when direct Gmail API access fails."""


def get_profile(settings: Settings, *, verbose: bool = False) -> dict[str, Any]:
    """Return the current Gmail profile using direct Google API access.

    Includes ``historyId`` when returned by the API (used as a cursor hint for ``list_history``).
    """
    payload = _gmail_get_json(settings, "/profile", verbose=verbose)
    email_address = str(payload.get("emailAddress") or "").strip()
    profile = dict(payload)
    if email_address:
        profile.setdefault("email", email_address)
        profile.setdefault("mailbox", email_address)
    profile["source"] = "google_api"
    return profile


def list_history(
    settings: Settings,
    *,
    start_history_id: str,
    max_results: int = 100,
    page_token: str | None = None,
    history_types: list[str] | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Call Gmail ``history.list`` (read-only). Used for R3 discovery / spikes — not the intake worker.

    ``start_history_id`` must be a recent history id; if it is too old, the API may return HTTP 404
    (full mailbox sync required — out of scope for this helper).

    See: https://developers.google.com/gmail/api/reference/rest/v1/users.history/list
    """
    cap = max(1, min(int(max_results), 500))
    params: list[tuple[str, str]] = [
        ("startHistoryId", str(start_history_id).strip()),
        ("maxResults", str(cap)),
    ]
    if page_token:
        params.append(("pageToken", str(page_token).strip()))
    for ht in history_types or ["messageAdded"]:
        params.append(("historyTypes", ht))
    return _gmail_get_json(settings, "/history", params=params, verbose=verbose)


def search_emails(
    settings: Settings,
    *,
    query: str,
    max_results: int = 25,
    next_page_token: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Search Gmail messages and return a connector-compatible selection payload."""
    refs = _list_message_refs(
        settings,
        query=query,
        max_results=max_results,
        next_page_token=next_page_token,
        verbose=verbose,
    )
    messages = [
        _get_message_summary(settings, message_id=message_id, verbose=verbose)
        for message_id in refs["message_ids"]
    ]
    return {
        "next_page_token": refs["next_page_token"],
        "responses": messages,
        "result_size_estimate": refs["result_size_estimate"],
        "source": "google_api",
    }


def search_email_metadata(
    settings: Settings,
    *,
    query: str,
    max_results: int = 25,
    next_page_token: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Search Gmail and return message metadata only.

    This helper intentionally uses ``messages.get(format=metadata)`` and never
    fetches message bodies or attachment content.
    """
    refs = _list_message_refs(
        settings,
        query=query,
        max_results=max_results,
        next_page_token=next_page_token,
        verbose=verbose,
    )
    messages = [
        get_message_metadata(settings, message_id=message_id, verbose=verbose)
        for message_id in refs["message_ids"]
    ]
    return {
        "next_page_token": refs["next_page_token"],
        "responses": messages,
        "result_size_estimate": refs["result_size_estimate"],
        "source": "google_api",
        "format": "metadata",
    }


def get_message_metadata(settings: Settings, *, message_id: str, verbose: bool = False) -> dict[str, Any]:
    """Return one Gmail message using metadata-only fetch semantics."""
    return _get_message_summary(settings, message_id=message_id, verbose=verbose)


def get_recent_emails(
    settings: Settings,
    *,
    max_results: int = 25,
    verbose: bool = False,
) -> dict[str, Any]:
    """Return recent Gmail messages using direct Google API access."""
    refs = _list_message_refs(
        settings,
        query="",
        max_results=max_results,
        next_page_token=None,
        verbose=verbose,
    )
    messages = [
        _get_message_summary(settings, message_id=message_id, verbose=verbose)
        for message_id in refs["message_ids"]
    ]
    return {
        "next_page_token": refs["next_page_token"],
        "responses": messages,
        "result_size_estimate": refs["result_size_estimate"],
        "source": "google_api",
    }


def read_email(
    settings: Settings,
    *,
    message_id: str,
    verbose: bool = False,
) -> dict[str, Any]:
    """Read one Gmail message in full using direct Google API access."""
    payload = _gmail_get_json(
        settings,
        f"/messages/{message_id}",
        params={"format": "full"},
        verbose=verbose,
    )
    return _convert_gmail_message(payload, include_body=True)


def get_thread_messages(
    settings: Settings,
    *,
    thread_id: str,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """Return all messages from a Gmail thread using direct Google API access."""
    payload = _gmail_get_json(
        settings,
        f"/threads/{thread_id}",
        params={"format": "full"},
        verbose=verbose,
    )
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise GoogleGmailApiError("Gmail thread response did not contain a `messages` list.")
    normalized = [
        _convert_gmail_message(item, include_body=True)
        for item in messages
        if isinstance(item, dict)
    ]
    normalized.sort(
        key=lambda item: str(item.get("email_ts") or item.get("date") or ""),
        reverse=True,
    )
    return normalized


def _list_message_refs(
    settings: Settings,
    *,
    query: str,
    max_results: int,
    next_page_token: str | None,
    verbose: bool,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "maxResults": max_results,
    }
    if query.strip():
        params["q"] = query.strip()
    if next_page_token:
        params["pageToken"] = next_page_token

    payload = _gmail_get_json(
        settings,
        "/messages",
        params=params,
        verbose=verbose,
    )
    message_ids: list[str] = []
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        message_id = str(item.get("id") or "").strip()
        if message_id:
            message_ids.append(message_id)

    return {
        "message_ids": message_ids,
        "next_page_token": str(payload.get("nextPageToken") or "").strip(),
        "result_size_estimate": int(payload.get("resultSizeEstimate") or 0),
    }


def _get_message_summary(settings: Settings, *, message_id: str, verbose: bool) -> dict[str, Any]:
    params: list[tuple[str, str]] = [("format", "metadata")]
    params.extend(("metadataHeaders", header_name) for header_name in METADATA_HEADERS)
    payload = _gmail_get_json(
        settings,
        f"/messages/{message_id}",
        params=params,
        verbose=verbose,
    )
    return _convert_gmail_message(payload, include_body=False)


def send_raw_message(
    settings: Settings,
    *,
    raw_bytes: bytes,
    thread_id: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Send a MIME message via Gmail API (requires gmail.send or gmail.compose scope)."""
    body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(raw_bytes).decode("ascii")}
    if thread_id:
        body["threadId"] = str(thread_id).strip()
    return _gmail_post_json(settings, "/messages/send", json_body=body, verbose=verbose)


def _gmail_post_json(
    settings: Settings,
    path: str,
    *,
    json_body: dict[str, Any],
    verbose: bool = False,
) -> dict[str, Any]:
    """Issue a Gmail API POST request with retry and one forced-refresh retry on 401."""
    last_error: GoogleGmailApiError | None = None
    attempts = max(2 if settings.has_google_refresh_flow else 1, settings.http_max_retries)
    forced_refresh_retry_used = False

    for attempt in range(1, attempts + 1):
        try:
            credentials = get_gmail_credentials(settings, force_refresh=False)
            access_token = str(credentials.token or "").strip()
            if not access_token:
                raise GoogleOAuthError("No Gmail access token is available for Google API fetch.")
        except GoogleOAuthError as exc:
            raise GoogleGmailApiError(sanitize_text(str(exc))) from exc

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                f"{GMAIL_API_BASE_URL}{path}",
                headers=headers,
                json=json_body,
                timeout=settings.http_timeout,
            )
        except requests.Timeout as exc:
            last_error = GoogleGmailApiError(
                f"Gmail API timed out after {settings.http_timeout}s."
            )
            if attempt >= attempts:
                raise last_error from exc
            _sleep_before_retry(settings, attempt)
            continue
        except requests.RequestException as exc:
            last_error = GoogleGmailApiError(
                f"Failed to connect to Gmail API: {sanitize_text(str(exc))}"
            )
            if attempt >= attempts:
                raise last_error from exc
            _sleep_before_retry(settings, attempt)
            continue

        data = _parse_response_json(response)
        if response.status_code == 401 and settings.has_google_refresh_flow and not forced_refresh_retry_used:
            forced_refresh_retry_used = True
            try:
                get_gmail_credentials(settings, force_refresh=True)
            except GoogleOAuthError as exc:
                raise GoogleGmailApiError(sanitize_text(str(exc))) from exc
            continue

        if response.status_code >= 400:
            last_error = _build_gmail_api_error(response.status_code, data)
            if response.status_code in RETRYABLE_GMAIL_STATUSES and attempt < attempts:
                _sleep_before_retry(settings, attempt)
                continue
            raise last_error

        if verbose:
            print(
                f"[gmail-api] POST {path} ok ({response.status_code})",
                file=sys.stderr,
                flush=True,
            )
        return data

    raise last_error or GoogleGmailApiError("Unknown Gmail API failure.")


def _gmail_get_json(
    settings: Settings,
    path: str,
    *,
    params: dict[str, Any] | list[tuple[str, str]] | None = None,
    verbose: bool,
) -> dict[str, Any]:
    """Issue a Gmail API GET request with retry and one forced-refresh retry on 401."""
    last_error: GoogleGmailApiError | None = None
    attempts = max(2 if settings.has_google_refresh_flow else 1, settings.http_max_retries)
    forced_refresh_retry_used = False

    for attempt in range(1, attempts + 1):
        try:
            credentials = get_gmail_credentials(settings, force_refresh=False)
            access_token = str(credentials.token or "").strip()
            if not access_token:
                raise GoogleOAuthError("No Gmail access token is available for Google API fetch.")
        except GoogleOAuthError as exc:
            raise GoogleGmailApiError(sanitize_text(str(exc))) from exc

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        try:
            response = requests.get(
                f"{GMAIL_API_BASE_URL}{path}",
                headers=headers,
                params=params,
                timeout=settings.http_timeout,
            )
        except requests.Timeout as exc:
            last_error = GoogleGmailApiError(
                f"Gmail API timed out after {settings.http_timeout}s."
            )
            if attempt >= attempts:
                raise last_error from exc
            _sleep_before_retry(settings, attempt)
            continue
        except requests.RequestException as exc:
            last_error = GoogleGmailApiError(
                f"Failed to connect to Gmail API: {sanitize_text(str(exc))}"
            )
            if attempt >= attempts:
                raise last_error from exc
            _sleep_before_retry(settings, attempt)
            continue

        data = _parse_response_json(response)
        if response.status_code == 401 and settings.has_google_refresh_flow and not forced_refresh_retry_used:
            forced_refresh_retry_used = True
            try:
                get_gmail_credentials(settings, force_refresh=True)
            except GoogleOAuthError as exc:
                raise GoogleGmailApiError(sanitize_text(str(exc))) from exc
            continue

        if response.status_code >= 400:
            last_error = _build_gmail_api_error(response.status_code, data)
            if response.status_code in RETRYABLE_GMAIL_STATUSES and attempt < attempts:
                _sleep_before_retry(settings, attempt)
                continue
            raise last_error

        if verbose:
            print(
                f"[gmail-api] GET {path} ok ({response.status_code})",
                file=sys.stderr,
                flush=True,
            )
        return data

    raise last_error or GoogleGmailApiError("Unknown Gmail API failure.")


def _parse_response_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        body_preview = sanitize_text(response.text[:500].strip())
        raise GoogleGmailApiError(
            "Gmail API returned invalid JSON. "
            f"HTTP {response.status_code}. Body: {body_preview!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise GoogleGmailApiError("Gmail API returned an unexpected JSON root.")
    return payload


def _build_gmail_api_error(status_code: int, payload: dict[str, Any]) -> GoogleGmailApiError:
    error_node = payload.get("error")
    message = ""
    status = ""

    if isinstance(error_node, dict):
        message = sanitize_text(str(error_node.get("message") or "").strip())
        status = sanitize_text(str(error_node.get("status") or "").strip())

    details = " | ".join(part for part in (message, status) if part)
    suffix = f" Details: {details}" if details else ""

    if status_code == 400:
        return GoogleGmailApiError("Gmail API rejected the request (HTTP 400)." + suffix)
    if status_code == 401:
        return GoogleGmailApiError(
            "Gmail API rejected the active access token (HTTP 401)."
            + suffix
        )
    if status_code == 403:
        return GoogleGmailApiError(
            "Gmail API denied access (HTTP 403). Check gmail.readonly scope and mailbox permissions."
            + suffix
        )
    if status_code == 404:
        return GoogleGmailApiError("Requested Gmail resource was not found (HTTP 404)." + suffix)
    if status_code == 429:
        return GoogleGmailApiError("Gmail API rate limit reached (HTTP 429)." + suffix)
    if status_code >= 500:
        return GoogleGmailApiError(f"Gmail API server error (HTTP {status_code})." + suffix)
    return GoogleGmailApiError(f"Gmail API request failed (HTTP {status_code})." + suffix)


def _convert_gmail_message(payload: dict[str, Any], *, include_body: bool) -> dict[str, Any]:
    message_id = str(payload.get("id") or "").strip()
    thread_id = str(payload.get("threadId") or "").strip()
    history_id = str(payload.get("historyId") or "").strip()
    internal_date_raw = str(payload.get("internalDate") or "").strip()
    snippet = str(payload.get("snippet") or "").strip()
    label_ids = [
        str(item).strip()
        for item in payload.get("labelIds") or []
        if str(item).strip()
    ]

    message_payload = payload.get("payload")
    headers = _extract_headers(message_payload if isinstance(message_payload, dict) else {})
    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    to_items = _split_addresses(headers.get("to", ""))
    cc_items = _split_addresses(headers.get("cc", ""))
    bcc_items = _split_addresses(headers.get("bcc", ""))
    attachment_names = _collect_attachment_names(message_payload if isinstance(message_payload, dict) else {})
    attachment_parts = _collect_attachment_parts(message_payload if isinstance(message_payload, dict) else {})
    body = ""
    if include_body:
        body = _extract_best_body(message_payload if isinstance(message_payload, dict) else {})

    received_at = _coerce_internal_date(payload.get("internalDate"))
    return {
        "id": message_id,
        "message_id": message_id,
        "history_id": history_id,
        "thread_id": thread_id,
        "threadId": thread_id,
        "email_ts": received_at,
        "date": received_at,
        "internal_date": received_at,
        "internal_date_ms": internal_date_raw,
        "from": sender,
        "sender": sender,
        "to": to_items,
        "cc": cc_items,
        "bcc": bcc_items,
        "subject": subject,
        "display_title": subject,
        "snippet": snippet,
        "body": body,
        "labels": label_ids,
        "has_attachment": bool(attachment_names),
        "attachments": attachment_parts if attachment_parts else attachment_names,
        "attachment_names": attachment_names,
        "attachment_parts": attachment_parts,
        "display_url": f"https://mail.google.com/mail/#all/{message_id}" if message_id else "",
        "source": "google_api",
    }


def _extract_headers(payload: dict[str, Any]) -> dict[str, str]:
    header_map: dict[str, str] = {}
    for header in payload.get("headers") or []:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name") or "").strip().lower()
        value = str(header.get("value") or "").strip()
        if name and value:
            header_map[name] = value
    return header_map


def _split_addresses(raw_value: str) -> list[str]:
    if not raw_value.strip():
        return []
    parsed = getaddresses([raw_value])
    normalized = [address.strip() for _, address in parsed if address.strip()]
    return normalized or [part.strip() for part in raw_value.split(",") if part.strip()]


def _collect_attachment_names(payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for part in _walk_parts(payload):
        filename = str(part.get("filename") or "").strip()
        if filename:
            names.append(filename)
    return names


def _collect_attachment_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect Gmail attachment metadata including attachmentId for later byte fetch."""
    items: list[dict[str, Any]] = []
    for part in _walk_parts(payload):
        filename = str(part.get("filename") or "").strip()
        if not filename:
            continue
        body_node = part.get("body") if isinstance(part.get("body"), dict) else {}
        att_id = str(body_node.get("attachmentId") or "").strip()
        if not att_id:
            continue
        try:
            size_b = int(body_node.get("size") or 0)
        except (TypeError, ValueError):
            size_b = 0
        mime_type = str(part.get("mimeType") or "").strip()
        items.append(
            {
                "filename": filename,
                "name": filename,
                "mime_type": mime_type,
                "mimeType": mime_type,
                "attachment_id": att_id,
                "size_bytes": size_b,
            }
        )
    return items


def _gmail_attachment_resource_path(message_id: str, attachment_id: str) -> str:
    """Build the path for ``users.messages.attachments.get``.

    Gmail ids can include reserved URL characters (e.g. ``+``, ``/``). Path segments
    must be percent-encoded so ``requests`` does not mis-parse the URL.
    """
    mid = quote(str(message_id or "").strip(), safe="")
    aid = quote(str(attachment_id or "").strip(), safe="")
    return f"/messages/{mid}/attachments/{aid}"


def fetch_gmail_attachment_bytes(
    settings: Settings,
    *,
    message_id: str,
    attachment_id: str,
    verbose: bool = False,
) -> bytes:
    """Download raw bytes for a Gmail attachment (users.messages.attachments.get)."""
    message_id = str(message_id or "").strip()
    attachment_id = str(attachment_id or "").strip()
    if not message_id or not attachment_id:
        raise GoogleGmailApiError("fetch_gmail_attachment_bytes requires message_id and attachment_id.")
    path = _gmail_attachment_resource_path(message_id, attachment_id)
    payload = _gmail_get_json(
        settings,
        path,
        params=None,
        verbose=verbose,
    )
    data = str(payload.get("data") or "").strip()
    size_hint = 0
    try:
        size_hint = int(payload.get("size") or 0)
    except (TypeError, ValueError):
        size_hint = 0
    if not data:
        if size_hint > 0:
            raise GoogleGmailApiError(
                "Gmail attachments.get returned empty `data` while reporting non-zero size "
                f"({size_hint} bytes). message_id={message_id!r} attachment_id={attachment_id!r}"
            )
        return b""
    try:
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding)
    except (ValueError, binascii.Error) as exc:
        raise GoogleGmailApiError("Failed to decode Gmail attachment body.") from exc


def _extract_best_body(payload: dict[str, Any]) -> str:
    plain_chunks: list[str] = []
    html_chunks: list[str] = []

    for part in _walk_parts(payload):
        mime_type = str(part.get("mimeType") or "").strip().lower()
        filename = str(part.get("filename") or "").strip()
        body_node = part.get("body")
        if not isinstance(body_node, dict) or filename:
            continue
        data = str(body_node.get("data") or "").strip()
        if not data:
            continue
        decoded = _decode_body_data(data)
        if not decoded:
            continue
        if mime_type == "text/plain":
            plain_chunks.append(decoded)
        elif mime_type == "text/html":
            html_chunks.append(decoded)

    if plain_chunks:
        return "\n\n".join(chunk.strip() for chunk in plain_chunks if chunk.strip()).strip()
    if html_chunks:
        cleaned = [_strip_html(chunk) for chunk in html_chunks if chunk.strip()]
        return "\n\n".join(chunk for chunk in cleaned if chunk).strip()
    return ""


def _walk_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = [payload]

    while stack:
        current = stack.pop()
        parts.append(current)
        nested_parts = current.get("parts")
        if isinstance(nested_parts, list):
            for item in reversed(nested_parts):
                if isinstance(item, dict):
                    stack.append(item)

    return parts


def _decode_body_data(data: str) -> str:
    try:
        padding = "=" * (-len(data) % 4)
        raw_bytes = base64.urlsafe_b64decode(data + padding)
    except (ValueError, TypeError):
        return ""
    return raw_bytes.decode("utf-8", errors="replace").strip()


def _strip_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\\s*>", "\n\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _coerce_internal_date(value: Any) -> str:
    try:
        timestamp_ms = int(value)
    except (TypeError, ValueError):
        return ""
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone()
    return dt.replace(microsecond=0).isoformat()


def _sleep_before_retry(settings: Settings, attempt: int) -> None:
    delay = round(settings.http_retry_base_delay * (2 ** max(0, attempt - 1)), 1)
    time.sleep(delay)
