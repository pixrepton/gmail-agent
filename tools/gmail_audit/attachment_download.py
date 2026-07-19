"""Bounded read-only attachment bytes for internal API (no feed payload bytes)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Tuple

from config import Settings, load_settings
from google_gmail_api import fetch_gmail_attachment_bytes


def resolve_attachment_bytes(
    runtime: Any,
    *,
    case_id: str,
    attachment_ref: str,
    settings: Settings | None = None,
) -> Tuple[bytes, str, str]:
    """
    Return (raw_bytes, mime_type, file_name) for case-bound attachment/document ref.
    Raises ValueError when not found or unavailable.
    """
    case_id = str(case_id or "").strip()
    attachment_ref = str(attachment_ref or "").strip()
    if not case_id or not attachment_ref:
        raise ValueError("case_id and attachment_ref are required")

    store = getattr(runtime, "store", None)
    if store is None:
        raise ValueError("mailbox store unavailable")

    row = _lookup_attachment_row(store, case_id=case_id, ref=attachment_ref)
    if row is None:
        row = _lookup_document_row(store, case_id=case_id, ref=attachment_ref)
    if row is None:
        raise ValueError("attachment not found for case")

    file_name = str(row.get("file_name") or "attachment.bin")
    mime_type = str(row.get("mime_type") or "application/octet-stream")
    blob_path = str(row.get("blob_path") or "").strip()
    if blob_path:
        path = Path(blob_path)
        if path.is_file():
            return path.read_bytes(), mime_type, file_name

    message_id = str(row.get("message_id") or "").strip()
    gmail_attachment_id = str(row.get("gmail_attachment_id") or row.get("storage_ref") or "").strip()
    if message_id and gmail_attachment_id:
        cfg = settings or load_settings(require_groq=False, require_google=True)
        data = fetch_gmail_attachment_bytes(
            cfg,
            message_id=message_id,
            attachment_id=gmail_attachment_id,
        )
        if data:
            return data, mime_type, file_name

    raise ValueError("attachment bytes not available (metadata only)")


def _lookup_attachment_row(store: Any, *, case_id: str, ref: str) -> dict[str, Any] | None:
    fetch_one = getattr(store, "_fetch_one", None)
    if not callable(fetch_one):
        return None
    return fetch_one(
        """
        SELECT attachment_id, case_id, message_id, file_name, mime_type,
               gmail_attachment_id, blob_path
        FROM mailbox_memory_attachments
        WHERE case_id = %(case_id)s
          AND (attachment_id = %(ref)s OR gmail_attachment_id = %(ref)s)
        LIMIT 1
        """,
        {"case_id": case_id, "ref": ref},
    )


def _lookup_document_row(store: Any, *, case_id: str, ref: str) -> dict[str, Any] | None:
    fetch_one = getattr(store, "_fetch_one", None)
    if not callable(fetch_one):
        return None
    return fetch_one(
        """
        SELECT document_id, case_id, message_id, attachment_id, file_name, mime_type, blob_path
        FROM mailbox_memory_documents
        WHERE case_id = %(case_id)s
          AND (document_id = %(ref)s OR attachment_id = %(ref)s)
        LIMIT 1
        """,
        {"case_id": case_id, "ref": ref},
    )
