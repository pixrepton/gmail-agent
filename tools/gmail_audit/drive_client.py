"""Read-only Google Drive API client for bounded Drive ingress."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from config import Settings
from log_config import get_logger
from redaction import sanitize_text


GOOGLE_DRIVE_API_BASE_URL = "https://www.googleapis.com/drive/v3"
GOOGLE_DRIVE_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.presentation": "application/pdf",
    "application/vnd.google-apps.drawing": "application/pdf",
}
GOOGLE_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

log = get_logger(__name__)


class GoogleDriveClientError(RuntimeError):
    """Raised when Drive API access or bounded downloads fail."""


@dataclass(slots=True)
class DownloadedDriveContent:
    data: bytes
    mime_type: str
    source_ref: str
    source_kind: str


class GoogleDriveClient:
    """Small read-only Drive client with service-account or shared Google OAuth auth."""

    def __init__(self, settings: Settings, *, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        # Enterprise connection pooling: configure adapter pool size from env
        pool_size = int(os.getenv("DRIVE_CLIENT_POOL_SIZE", "10"))
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._service_account_credentials = None

    def list_children(
        self,
        *,
        folder_id: str,
        page_token: str = "",
        page_size: int | None = None,
    ) -> dict[str, Any]:
        if not folder_id.strip():
            raise GoogleDriveClientError("Google Drive root folder id is required for bounded ingest.")
        params = self._drive_params()
        params.update(
            {
                "q": f"'{folder_id}' in parents and trashed = false",
                "pageSize": str(page_size or self.settings.google_drive_batch_page_size),
                "fields": "nextPageToken,files(id,name,mimeType,parents,size,modifiedTime,webViewLink,iconLink)",
                "orderBy": "folder,name",
            }
        )
        if page_token.strip():
            params["pageToken"] = page_token.strip()
        payload = self._request_json("GET", "/files", params=params)
        return {
            "items": list(payload.get("files") or []),
            "next_page_token": str(payload.get("nextPageToken") or "").strip(),
        }

    def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        params = self._drive_params()
        params["fields"] = "id,name,mimeType,parents,size,modifiedTime,webViewLink,iconLink"
        return self._request_json("GET", f"/files/{quote(file_id)}", params=params)

    def get_start_page_token(self) -> str:
        params = self._drive_params()
        params["fields"] = "startPageToken"
        payload = self._request_json("GET", "/changes/startPageToken", params=params)
        token = str(payload.get("startPageToken") or "").strip()
        if not token:
            raise GoogleDriveClientError("Drive changes API did not return startPageToken.")
        return token

    def list_changes(
        self,
        *,
        page_token: str,
        page_size: int | None = None,
        include_removed: bool = True,
    ) -> dict[str, Any]:
        if not page_token.strip():
            raise GoogleDriveClientError("Drive changes page token is required.")
        params = self._drive_params()
        params.update(
            {
                "pageToken": page_token.strip(),
                "pageSize": str(page_size or self.settings.google_drive_batch_page_size),
                "fields": (
                    "nextPageToken,newStartPageToken,"
                    "changes(fileId,removed,time,file(id,name,mimeType,parents,size,modifiedTime,webViewLink,iconLink))"
                ),
                "includeRemoved": "true" if include_removed else "false",
            }
        )
        payload = self._request_json("GET", "/changes", params=params)
        return {
            "changes": list(payload.get("changes") or []),
            "next_page_token": str(payload.get("nextPageToken") or "").strip(),
            "new_start_page_token": str(payload.get("newStartPageToken") or "").strip(),
        }

    def download_content(self, metadata: dict[str, Any], *, max_bytes: int) -> DownloadedDriveContent:
        file_id = str(metadata.get("id") or "").strip()
        mime_type = str(metadata.get("mimeType") or "").strip()
        source_ref = self.build_source_ref(metadata)
        if not file_id:
            raise GoogleDriveClientError("Drive metadata is missing file id.")
        size_bytes = _coerce_int(metadata.get("size"))
        if size_bytes and size_bytes > max_bytes and mime_type not in GOOGLE_DRIVE_EXPORTS:
            raise GoogleDriveClientError(f"Drive file exceeds max download bytes: {size_bytes} > {max_bytes}")
        if mime_type in GOOGLE_DRIVE_EXPORTS:
            export_mime = GOOGLE_DRIVE_EXPORTS[mime_type]
            data = self._request_bytes(
                "GET",
                f"/files/{quote(file_id)}/export",
                params={"mimeType": export_mime},
                max_bytes=max_bytes,
            )
            return DownloadedDriveContent(
                data=data,
                mime_type=export_mime,
                source_ref=source_ref,
                source_kind="google_workspace_export",
            )
        data = self._request_bytes(
            "GET",
            f"/files/{quote(file_id)}",
            params={"alt": "media"},
            max_bytes=max_bytes,
        )
        return DownloadedDriveContent(
            data=data,
            mime_type=mime_type,
            source_ref=source_ref,
            source_kind="binary_download",
        )

    def build_source_ref(self, metadata: dict[str, Any]) -> str:
        web_view = str(metadata.get("webViewLink") or "").strip()
        if web_view:
            return web_view
        file_id = str(metadata.get("id") or "").strip()
        return f"https://drive.google.com/file/d/{file_id}" if file_id else ""

    def describe_item(self, metadata: dict[str, Any], *, folder_path: str = "") -> dict[str, Any]:
        return {
            "drive_item_id": str(metadata.get("id") or "").strip(),
            "title": str(metadata.get("name") or "").strip(),
            "mime_type": str(metadata.get("mimeType") or "").strip(),
            "parent_drive_item_id": str((metadata.get("parents") or [""])[0] or "").strip(),
            "folder_path": folder_path,
            "source_ref": self.build_source_ref(metadata),
            "is_folder": str(metadata.get("mimeType") or "") == GOOGLE_DRIVE_FOLDER_MIME,
            "size_bytes": _coerce_int(metadata.get("size")),
            "modified_time": str(metadata.get("modifiedTime") or "").strip(),
            "metadata": dict(metadata),
        }

    def _drive_params(self) -> dict[str, str]:
        params = {
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        shared_drive_id = str(self.settings.google_drive_shared_drive_id or "").strip()
        if shared_drive_id:
            params["driveId"] = shared_drive_id
            params["corpora"] = "drive"
        return params

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._request(method, path, params=params, stream=False)
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise GoogleDriveClientError("Drive API returned non-JSON payload.") from exc
        if not isinstance(payload, dict):
            raise GoogleDriveClientError("Drive API returned an unexpected payload shape.")
        return payload

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        max_bytes: int,
    ) -> bytes:
        response = self._request(method, path, params=params, stream=True)
        buffer = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise GoogleDriveClientError(f"Drive download exceeded max bytes ({max_bytes}).")
        return bytes(buffer)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        stream: bool,
    ) -> requests.Response:
        access_token = self._resolve_access_token()
        # Enterprise timeout: explicit connect (10s) / read (remaining) split
        raw_timeout = int(self.settings.http_timeout or 60)
        timeout = (10, max(raw_timeout - 10, 5))
        response = self.session.request(
            method.upper(),
            f"{GOOGLE_DRIVE_API_BASE_URL}{path}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            params=params,
            timeout=timeout,
            stream=stream,
        )
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                detail = str((payload.get("error") or {}).get("message") or payload)
            except ValueError:
                detail = response.text
            raise GoogleDriveClientError(
                f"Drive API request failed ({response.status_code}): {sanitize_text(detail)}"
            )
        return response

    def _resolve_access_token(self) -> str:
        credentials_path = self.settings.google_drive_credentials_path
        if credentials_path is not None:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account

            credentials = self._service_account_credentials
            if credentials is None:
                credentials = service_account.Credentials.from_service_account_file(
                    str(Path(credentials_path).resolve()),
                    scopes=[GOOGLE_DRIVE_READONLY_SCOPE],
                )
                self._service_account_credentials = credentials
            if not credentials.valid or credentials.expired or not credentials.token:
                credentials.refresh(Request())
            token = str(credentials.token or "").strip()
            if not token:
                raise GoogleDriveClientError("Service-account credentials did not yield a Drive access token.")
            return token
        try:
            from gmail_auth import GoogleOAuthError, resolve_google_access_token

            return resolve_google_access_token(self.settings, force_refresh=True)
        except GoogleOAuthError as exc:
            raise GoogleDriveClientError(
                "Drive auth is unavailable. Provide GOOGLE_DRIVE_CREDENTIALS_PATH or a Google OAuth token with Drive readonly scope. "
                + sanitize_text(str(exc))
            ) from exc


def build_google_drive_check(
    settings: Settings,
    *,
    check_access: bool,
    root_folder_id: str = "",
) -> dict[str, Any]:
    """Return a doctor/preflight-friendly Drive readiness check payload."""
    resolved_root = root_folder_id.strip() or str(settings.google_drive_root_folder_id or "").strip()
    using_service_account = settings.google_drive_credentials_path is not None
    using_shared_oauth = not using_service_account and (
        settings.has_google_refresh_flow or settings.has_google_access_token
    )

    report: dict[str, Any] = {
        "enabled": bool(settings.google_drive_enabled),
        "ingest_enabled": bool(settings.google_drive_ingest_enabled),
        "graph_enabled": bool(settings.google_drive_graph_enabled),
        "auth_mode": (
            "service_account"
            if using_service_account
            else "shared_google_oauth"
            if using_shared_oauth
            else "missing"
        ),
        "shared_drive_id_present": bool(str(settings.google_drive_shared_drive_id or "").strip()),
        "root_folder_id_present": bool(resolved_root),
        "root_folder_id": resolved_root or None,
        "batch_page_size": int(settings.google_drive_batch_page_size),
        "max_download_bytes": int(settings.google_drive_max_download_bytes),
        "database_url_configured": bool(str(settings.mailbox_memory_database_url or "").strip()),
        "env_file": str(settings.env_path.resolve()) if settings.env_path else "environment_only",
        "config_sources": {
            key: value
            for key, value in settings.config_sources.items()
            if key in {
                "_loaded_env_file",
                "GOOGLE_OAUTH_SCOPES",
                "GOOGLE_DRIVE_ENABLED",
                "GOOGLE_DRIVE_CREDENTIALS_PATH",
                "GOOGLE_DRIVE_SHARED_DRIVE_ID",
                "GOOGLE_DRIVE_ROOT_FOLDER_ID",
                "GOOGLE_DRIVE_BATCH_PAGE_SIZE",
                "GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES",
                "GOOGLE_DRIVE_INGEST_ENABLED",
                "GOOGLE_DRIVE_GRAPH_ENABLED",
                "MAILBOX_MEMORY_DATABASE_URL",
                "DATABASE_URL",
            }
        },
    }
    warnings: list[str] = []
    if using_service_account:
        warnings.append(
            "Shared Google OAuth refresh-token flow is canonical for Drive in this repo; service-account auth is compatibility-only."
        )
    if "DATABASE_URL" == settings.config_sources.get("MAILBOX_MEMORY_DATABASE_URL"):
        warnings.append(
            "MAILBOX_MEMORY_DATABASE_URL currently resolves from legacy DATABASE_URL. Prefer MAILBOX_MEMORY_DATABASE_URL in tools/gmail_audit/.env."
        )
    if settings.config_warnings:
        warnings.extend(settings.config_warnings)
    if warnings:
        report["warnings"] = warnings

    if not settings.google_drive_enabled or not settings.google_drive_ingest_enabled:
        report["status"] = "failed" if check_access else "disabled"
        report["reason"] = (
            "Drive ingest is disabled. Set GOOGLE_DRIVE_ENABLED=1 and GOOGLE_DRIVE_INGEST_ENABLED=1."
        )
        return report

    if not check_access:
        report["status"] = "skipped"
        report["reason"] = "Drive access check not requested. Use --check-drive for bounded auth/root verification."
        return report

    if not str(settings.mailbox_memory_database_url or "").strip():
        report["status"] = "failed"
        report["error"] = "MAILBOX_MEMORY_DATABASE_URL is required for Drive shared-memory ingest."
        return report
    if not resolved_root:
        report["status"] = "failed"
        report["error"] = "GOOGLE_DRIVE_ROOT_FOLDER_ID or --root-folder-id is required for bounded Drive checks."
        return report
    if not using_service_account and GOOGLE_DRIVE_READONLY_SCOPE not in settings.google_oauth_scopes:
        report["status"] = "failed"
        report["error"] = f"GOOGLE_OAUTH_SCOPES must include {GOOGLE_DRIVE_READONLY_SCOPE} for shared Drive OAuth."
        report["manual_prerequisite"] = (
            "Refresh token was issued without Drive scope. Add "
            f"{GOOGLE_DRIVE_READONLY_SCOPE} to GOOGLE_OAUTH_SCOPES, then run the OAuth consent flow again "
            "so Google issues a new refresh token; update GOOGLE_REFRESH_TOKEN in tools/gmail_audit/.env. "
            "Shared OAuth (not service account) remains the canonical Drive auth path."
        )
        return report
    if not using_service_account and not using_shared_oauth:
        report["status"] = "failed"
        report["error"] = (
            "Drive auth is missing. Configure shared Google OAuth refresh-token flow with drive.readonly scope "
            "or provide GOOGLE_DRIVE_CREDENTIALS_PATH."
        )
        return report

    try:
        client = GoogleDriveClient(settings)
        payload = client.list_children(folder_id=resolved_root, page_size=1)
    except GoogleDriveClientError as exc:
        report["status"] = "failed"
        report["error"] = sanitize_text(str(exc))
        return report

    report["status"] = "ok"
    report["sample_item_count"] = len(payload.get("items") or [])
    report["next_page_token_present"] = bool(str(payload.get("next_page_token") or "").strip())
    return report


def _coerce_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "DownloadedDriveContent",
    "GOOGLE_DRIVE_EXPORTS",
    "GOOGLE_DRIVE_FOLDER_MIME",
    "GOOGLE_DRIVE_READONLY_SCOPE",
    "GoogleDriveClient",
    "GoogleDriveClientError",
    "build_google_drive_check",
]
