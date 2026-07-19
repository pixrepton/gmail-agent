"""Attachment download resolver tests."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from attachment_download import resolve_attachment_bytes


def test_resolve_from_blob_path(tmp_path):
    blob = tmp_path / "ab" / "abc123"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"pdf-bytes")

    store = MagicMock()
    store._fetch_one.side_effect = [
        {
            "attachment_id": "att-1",
            "case_id": "case-1",
            "message_id": "msg-1",
            "file_name": "f.pdf",
            "mime_type": "application/pdf",
            "gmail_attachment_id": "",
            "blob_path": str(blob),
        },
        None,
    ]
    runtime = MagicMock(store=store)

    data, mime, name = resolve_attachment_bytes(runtime, case_id="case-1", attachment_ref="att-1")
    assert data == b"pdf-bytes"
    assert mime == "application/pdf"
    assert name == "f.pdf"


def test_resolve_metadata_only_raises(tmp_path):
    store = MagicMock()
    store._fetch_one.side_effect = [
        {
            "attachment_id": "att-meta",
            "case_id": "case-1",
            "message_id": "msg-1",
            "file_name": "f.pdf",
            "mime_type": "application/pdf",
            "gmail_attachment_id": "",
            "blob_path": "",
        },
        None,
    ]
    runtime = MagicMock(store=store)

    import pytest

    with pytest.raises(ValueError, match="metadata only"):
        resolve_attachment_bytes(runtime, case_id="case-1", attachment_ref="att-meta")


def test_resolve_missing_blob_file_raises(tmp_path):
    store = MagicMock()
    store._fetch_one.side_effect = [
        {
            "attachment_id": "att-1",
            "case_id": "case-1",
            "message_id": "msg-1",
            "file_name": "f.pdf",
            "mime_type": "application/pdf",
            "gmail_attachment_id": "",
            "blob_path": str(tmp_path / "missing.bin"),
        },
        None,
    ]
    runtime = MagicMock(store=store)

    import pytest

    with pytest.raises(ValueError, match="metadata only"):
        resolve_attachment_bytes(runtime, case_id="case-1", attachment_ref="att-1")
