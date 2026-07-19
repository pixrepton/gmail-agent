from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app

# AUTH-ATTACHMENT-DOWNLOAD-01: registry_token_configured() checks these keys, in order,
# plus a GMAIL_AGENT_ENV_FILE dotenv fallback. Tests that need a genuine "no config
# anywhere" state must clear all of them, not just the primary one.
_REGISTRY_TOKEN_ENV_KEYS = (
    "NODE_B_REGISTRY_TOKEN",
    "DASZEK_NODE_B_API_TOKEN",
    "GMAIL_AGENT_INTERNAL_API_TOKEN",
    "GMAIL_AGENT_ENV_FILE",
)


def _clear_registry_token_config(monkeypatch) -> None:
    for key in _REGISTRY_TOKEN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class _Runtime:
  pass


def test_attachment_download_returns_bytes(monkeypatch):
    monkeypatch.setenv("NODE_B_REGISTRY_TOKEN", "test-registry-token")
    app = create_app(runtime_provider=lambda: _Runtime())
    client = TestClient(app)

    with patch(
        "api_app.resolve_attachment_bytes",
        return_value=(b"pdf-bytes", "application/pdf", "offer.pdf"),
    ):
        response = client.get(
            "/cases/case-1/attachments/att-1",
            headers={"Authorization": "Bearer test-registry-token"},
        )

    assert response.status_code == 200
    assert response.content == b"pdf-bytes"
    assert response.headers["content-type"].startswith("application/pdf")
    assert 'filename="offer.pdf"' in response.headers.get("content-disposition", "")


def test_attachment_download_not_found(monkeypatch):
    monkeypatch.setenv("NODE_B_REGISTRY_TOKEN", "test-registry-token")
    app = create_app(runtime_provider=lambda: _Runtime())
    client = TestClient(app)

    with patch(
        "api_app.resolve_attachment_bytes",
        side_effect=ValueError("attachment not found for case"),
    ):
        response = client.get(
            "/cases/case-1/attachments/missing",
            headers={"Authorization": "Bearer test-registry-token"},
        )

    assert response.status_code == 404
    body = response.json()
    detail = str(body.get("detail") or body.get("message") or body)
    assert "not found" in detail.lower()


def test_attachment_download_requires_bearer_when_token_configured():
    app = create_app(runtime_provider=lambda: _Runtime())
    client = TestClient(app)

    with (
        patch("api_app.verify_registry_bearer", return_value=False),
        patch("api_app.resolve_attachment_bytes") as mock_resolve,
    ):
        response = client.get("/cases/case-1/attachments/att-1")

    assert response.status_code == 401
    mock_resolve.assert_not_called()


def test_attachment_download_accepts_bearer_when_token_configured():
    app = create_app(runtime_provider=lambda: _Runtime())
    client = TestClient(app)

    with (
        patch("api_app.verify_registry_bearer", return_value=True),
        patch(
            "api_app.resolve_attachment_bytes",
            return_value=(b"x", "application/octet-stream", "x.bin"),
        ),
    ):
        response = client.get(
            "/cases/case-1/attachments/att-1",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200
    assert response.content == b"x"


# --- AUTH-ATTACHMENT-DOWNLOAD-01 ------------------------------------------
#
# Root cause: case_attachment_download() gated its auth check behind
# `if registry_token_configured(): ...`, so when no registry token is
# configured anywhere, the check was skipped entirely and the route served
# real attachment bytes to any caller. Fix: call verify_registry_bearer()
# unconditionally, same as /internal/registry/links and
# /internal/email/personalize-offer. verify_registry_bearer() itself already
# returns False (deny) when unconfigured, so this alone closes the gap.


def test_attachment_download_denies_when_no_registry_token_configured_no_header(monkeypatch):
    _clear_registry_token_config(monkeypatch)
    app = create_app(runtime_provider=lambda: _Runtime())
    client = TestClient(app)

    with patch("api_app.resolve_attachment_bytes") as mock_resolve:
        response = client.get("/cases/case-1/attachments/att-1")

    assert response.status_code == 401
    mock_resolve.assert_not_called()
    body = response.json()
    assert "error" in body


def test_attachment_download_denies_when_no_registry_token_configured_garbage_header(monkeypatch):
    _clear_registry_token_config(monkeypatch)
    app = create_app(runtime_provider=lambda: _Runtime())
    client = TestClient(app)

    with patch("api_app.resolve_attachment_bytes") as mock_resolve:
        response = client.get(
            "/cases/case-1/attachments/att-1",
            headers={"Authorization": "Bearer anything-at-all"},
        )

    assert response.status_code == 401
    mock_resolve.assert_not_called()


def test_attachment_download_denies_empty_bearer_when_token_configured(monkeypatch):
    monkeypatch.setenv("NODE_B_REGISTRY_TOKEN", "test-registry-token")
    app = create_app(runtime_provider=lambda: _Runtime())
    client = TestClient(app)

    with patch("api_app.resolve_attachment_bytes") as mock_resolve:
        response = client.get(
            "/cases/case-1/attachments/att-1",
            headers={"Authorization": ""},
        )

    assert response.status_code == 401
    mock_resolve.assert_not_called()


def test_attachment_download_denies_wrong_bearer_when_token_configured(monkeypatch):
    monkeypatch.setenv("NODE_B_REGISTRY_TOKEN", "test-registry-token")
    app = create_app(runtime_provider=lambda: _Runtime())
    client = TestClient(app)

    with patch("api_app.resolve_attachment_bytes") as mock_resolve:
        response = client.get(
            "/cases/case-1/attachments/att-1",
            headers={"Authorization": "Bearer wrong-value"},
        )

    assert response.status_code == 401
    mock_resolve.assert_not_called()


class _CaseScopedAttachmentStore:
    """Minimal case_id+ref scoped store — mirrors the real parameterized SQL
    shape in attachment_download._lookup_attachment_row (case_id AND ref both
    required to match), for route-level case-scope regression coverage."""

    def __init__(self, blob_path: str) -> None:
        self._rows = {
            ("case-A", "att-A"): {
                "attachment_id": "att-A",
                "case_id": "case-A",
                "message_id": "msg-A",
                "file_name": "offer-case-A.pdf",
                "mime_type": "application/pdf",
                "gmail_attachment_id": "",
                "blob_path": blob_path,
            },
        }

    def _fetch_one(self, sql: str, params: dict):
        if "mailbox_memory_attachments" in sql:
            return self._rows.get((params.get("case_id"), params.get("ref")))
        return None


class _RuntimeWithCaseScopedStore:
    def __init__(self, blob_path: str) -> None:
        self.store = _CaseScopedAttachmentStore(blob_path)


def test_attachment_download_case_scope_regression(tmp_path, monkeypatch):
    """Case scoping must be preserved by the AUTH-ATTACHMENT-DOWNLOAD-01 fix:
    only Case A's own attachment is downloadable; cross-case, missing-case,
    and missing-attachment requests all return the same generic 404."""
    monkeypatch.setenv("NODE_B_REGISTRY_TOKEN", "test-registry-token")
    blob = tmp_path / "marker.txt"
    blob.write_bytes(b"CASE_SCOPE_REGRESSION_MARKER")
    headers = {"Authorization": "Bearer test-registry-token"}

    app = create_app(runtime_provider=lambda: _RuntimeWithCaseScopedStore(str(blob)))
    client = TestClient(app)

    ok = client.get("/cases/case-A/attachments/att-A", headers=headers)
    assert ok.status_code == 200
    assert ok.content == b"CASE_SCOPE_REGRESSION_MARKER"

    cross_case = client.get("/cases/case-B/attachments/att-A", headers=headers)
    assert cross_case.status_code == 404

    missing_case = client.get("/cases/case-NOPE/attachments/att-A", headers=headers)
    assert missing_case.status_code == 404

    missing_attachment = client.get("/cases/case-A/attachments/att-NOPE", headers=headers)
    assert missing_attachment.status_code == 404
