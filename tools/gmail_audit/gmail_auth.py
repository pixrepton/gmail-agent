"""Canonical Gmail OAuth runtime auth owner for `.env`-backed config."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from config import DEFAULT_GOOGLE_OAUTH_SCOPES, Settings, load_settings, normalize_google_access_token
from intake_policy import CHECK_STATUS_FAILED, CHECK_STATUS_OK
from redaction import mask_secret, sanitize_text


TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 60
EXPECTED_GMAIL_SCOPE = DEFAULT_GOOGLE_OAUTH_SCOPES[0]
GOOGLE_GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


class GoogleOAuthError(RuntimeError):
    """Raised when Gmail OAuth config or refresh flow fails."""


def load_google_oauth_config(settings: Settings | None = None) -> dict[str, Any]:
    """Load Gmail OAuth config from the canonical `.env`/process config."""
    resolved_settings = _coerce_settings(settings)
    missing = resolved_settings.google_refresh_missing_fields
    if missing:
        raise GoogleOAuthError(
            "Google OAuth refresh flow is incomplete. Missing: "
            + ", ".join(missing)
            + "."
        )

    scopes = list(resolved_settings.google_oauth_scopes or DEFAULT_GOOGLE_OAUTH_SCOPES)
    if EXPECTED_GMAIL_SCOPE not in scopes:
        raise GoogleOAuthError(
            f"GOOGLE_OAUTH_SCOPES must include {EXPECTED_GMAIL_SCOPE}."
        )

    return {
        "client_id": resolved_settings.google_client_id,
        "client_secret": resolved_settings.google_client_secret,
        "refresh_token": resolved_settings.google_refresh_token,
        "access_token": resolved_settings.google_access_token or None,
        "scopes": scopes,
        "token_uri": resolved_settings.google_token_endpoint,
    }


def build_google_credentials(
    config: dict[str, Any],
    *,
    access_token: str | None = None,
    expiry: datetime | None = None,
) -> Credentials:
    """Build Google credentials from normalized OAuth config."""
    scopes = [str(scope).strip() for scope in config.get("scopes") or DEFAULT_GOOGLE_OAUTH_SCOPES if str(scope).strip()]
    token = access_token if access_token is not None else config.get("access_token")
    credentials = Credentials(
        token=str(token).strip() or None,
        refresh_token=str(config.get("refresh_token") or "").strip() or None,
        token_uri=str(config.get("token_uri") or "").strip() or None,
        client_id=str(config.get("client_id") or "").strip() or None,
        client_secret=str(config.get("client_secret") or "").strip() or None,
        scopes=scopes,
    )
    if expiry is not None:
        credentials.expiry = expiry
    return credentials


def refresh_google_access_token(
    credentials: Credentials,
    *,
    settings: Settings | None = None,
) -> tuple[Credentials, str, dict[str, Any]]:
    """Refresh Google credentials and return safe metadata about the refresh."""
    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        raise _build_refresh_error(exc) from exc
    except Exception as exc:  # pragma: no cover - safety net for unexpected transport wrappers
        raise GoogleOAuthError(
            "Google OAuth refresh failed. "
            + sanitize_text(str(exc))
        ) from exc

    access_token, _ = normalize_google_access_token(str(credentials.token or ""))
    if not access_token:
        raise GoogleOAuthError("Google OAuth refresh returned no access token.")

    credentials.token = access_token

    resolved_settings = settings or None
    if resolved_settings is not None:
        _store_runtime_token_metadata(resolved_settings, credentials, token_source="refresh_token")

    metadata = {
        "refresh_used": True,
        "token_present": True,
        "scopes": list(credentials.scopes or []),
        "expired": _credential_expired(credentials),
        "auth_source": "env",
    }
    if isinstance(getattr(credentials, "expiry", None), datetime):
        metadata["expiry"] = _normalize_expiry_datetime(credentials.expiry).isoformat()
    return credentials, access_token, metadata


def get_gmail_credentials(
    settings: Settings | None = None,
    *,
    force_refresh: bool = False,
) -> Credentials:
    """Return ready-to-use Gmail credentials with refresh-token flow when configured."""
    resolved_settings = _coerce_settings(settings)
    if resolved_settings.has_google_refresh_flow:
        config = load_google_oauth_config(resolved_settings)
        if not force_refresh and _cached_token_is_usable(resolved_settings):
            resolved_settings.google_active_token_source = "refresh_token_cache"
            expiry = _runtime_expiry_datetime(resolved_settings)
            return build_google_credentials(
                config,
                access_token=resolved_settings.google_runtime_access_token,
                expiry=expiry,
            )

        credentials = build_google_credentials(config)
        refreshed_credentials, _, _ = refresh_google_access_token(
            credentials,
            settings=resolved_settings,
        )
        return refreshed_credentials

    if resolved_settings.google_access_token:
        credentials = Credentials(
            token=resolved_settings.google_access_token,
            scopes=list(resolved_settings.google_oauth_scopes),
        )
        resolved_settings.google_active_token_source = "static_access_token"
        resolved_settings.google_runtime_access_token = ""
        resolved_settings.google_runtime_access_token_expires_at = 0.0
        resolved_settings.google_runtime_token_type = ""
        return credentials

    if resolved_settings.google_refresh_missing_fields:
        raise GoogleOAuthError(
            "Google auth is incomplete. Provide GOOGLE_ACCESS_TOKEN or complete the refresh-token flow with: "
            + ", ".join(resolved_settings.google_refresh_missing_fields)
            + "."
        )

    raise GoogleOAuthError(
        "Missing Google auth. Provide GOOGLE_ACCESS_TOKEN or configure GOOGLE_CLIENT_ID, "
        "GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN."
    )


def resolve_google_access_token(
    settings: Settings | None = None,
    *,
    force_refresh: bool = False,
) -> str:
    """Return the current Gmail access token, refreshing it in memory when needed."""
    credentials = get_gmail_credentials(settings, force_refresh=force_refresh)
    access_token, _ = normalize_google_access_token(str(credentials.token or ""))
    if not access_token:
        raise GoogleOAuthError("No Gmail access token is available after auth resolution.")
    return access_token


def get_gmail_auth_metadata(settings: Settings | None = None) -> dict[str, Any]:
    """Return redaction-safe Gmail auth metadata for doctor/preflight/debug output."""
    resolved_settings = _coerce_settings(settings)
    return {
        "auth_source": "env",
        "env_file": str(resolved_settings.env_path.resolve()) if resolved_settings.env_path else "environment_only",
        "refresh_configured": resolved_settings.has_google_refresh_flow,
        "refresh_missing_fields": resolved_settings.google_refresh_missing_fields,
        "access_token_present": bool(resolved_settings.google_access_token),
        "scopes": list(resolved_settings.google_oauth_scopes),
        "token_uri": resolved_settings.google_token_endpoint,
        "active_token_source": resolved_settings.google_active_token_source or summarize_google_token_source_detail(resolved_settings),
    }


def summarize_google_token_source(settings: Settings) -> str:
    """Return the operator-facing token source category."""
    active_source = settings.google_active_token_source
    if active_source in {"refresh_token", "refresh_token_cache"}:
        return "refresh_token"
    if active_source == "static_access_token":
        return "static_access_token"
    if settings.has_google_refresh_flow:
        return "refresh_token"
    if settings.has_google_access_token:
        return "static_access_token"
    return "missing"


def summarize_google_token_source_detail(settings: Settings) -> str:
    """Return the precise auth source currently active in runtime memory."""
    if settings.google_active_token_source:
        return settings.google_active_token_source
    if settings.has_google_refresh_flow:
        return "refresh_token_pending"
    if settings.has_google_access_token:
        return "static_access_token_pending"
    return "missing"


def build_google_auth_report(settings: Settings) -> dict[str, Any]:
    """Return a redaction-safe description of the configured Gmail auth mode."""
    warnings: list[str] = []
    if settings.has_google_refresh_flow and settings.google_access_token:
        warnings.append(
            "Refresh-token flow is configured and will be preferred over static GOOGLE_ACCESS_TOKEN."
        )
    elif settings.google_access_token and not settings.has_google_refresh_flow:
        warnings.append(
            "Using manual GOOGLE_ACCESS_TOKEN mode. Runtime will stop when the token expires."
        )
    if settings.google_refresh_partially_configured:
        warnings.append(
            "Refresh-token flow is incomplete and will stay disabled until all missing fields are set."
        )
    warnings.extend(settings.config_warnings)

    report: dict[str, Any] = {
        "active_auth_mode": settings.google_auth_mode,
        "mode": settings.google_auth_mode,
        "auth_source": "env",
        "token_source": summarize_google_token_source(settings),
        "token_source_detail": summarize_google_token_source_detail(settings),
        "expects_gmail_readonly_scope": True,
        "expected_gmail_scope": EXPECTED_GMAIL_SCOPE,
        "requested_scopes": list(settings.google_oauth_scopes),
        "access_token_present": bool(settings.google_access_token),
        "refresh_flow_configured": settings.has_google_refresh_flow,
        "client_id_present": bool(settings.google_client_id),
        "client_secret_present": bool(settings.google_client_secret),
        "refresh_token_present": bool(settings.google_refresh_token),
        "token_endpoint": settings.google_token_endpoint,
        "env_file": str(settings.env_path.resolve()) if settings.env_path else "environment_only",
        "config_sources": {
            key: value
            for key, value in settings.config_sources.items()
            if key.startswith("GOOGLE_") or key == "_loaded_env_file"
        },
    }
    if settings.google_refresh_missing_fields:
        report["missing_refresh_fields"] = settings.google_refresh_missing_fields
    if warnings:
        report["warnings"] = warnings
    if settings.google_active_token_source:
        report["active_token_source"] = settings.google_active_token_source
    if settings.google_runtime_access_token:
        report["cached_access_token"] = mask_secret(settings.google_runtime_access_token)
    return report


def build_google_auth_check(settings: Settings, *, require_google: bool) -> dict[str, Any]:
    """Return a doctor/preflight-friendly Gmail auth check payload."""
    report = build_google_auth_report(settings)
    has_usable_auth = settings.has_google_refresh_flow or settings.has_google_access_token
    if EXPECTED_GMAIL_SCOPE not in settings.google_oauth_scopes:
        report["status"] = CHECK_STATUS_FAILED
        report["error"] = f"GOOGLE_OAUTH_SCOPES must include {EXPECTED_GMAIL_SCOPE}."
        return report

    if require_google and not has_usable_auth:
        report["status"] = CHECK_STATUS_FAILED
        report["error"] = (
            "No usable Google auth configuration. Provide GOOGLE_ACCESS_TOKEN or configure "
            "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN."
        )
        return report

    report["status"] = CHECK_STATUS_OK
    if not require_google and not has_usable_auth:
        report.setdefault("warnings", []).append(
            "Google auth is not configured. Gmail-backed commands will fail until GOOGLE_ACCESS_TOKEN or refresh-token flow is provided."
        )
    if require_google and settings.google_refresh_partially_configured and not settings.has_google_access_token:
        report["status"] = CHECK_STATUS_FAILED
        report["error"] = (
            "Refresh-token flow is incomplete and no fallback GOOGLE_ACCESS_TOKEN is configured."
        )
    elif settings.google_refresh_partially_configured:
        report.setdefault("warnings", []).append(
            "Refresh-token flow is incomplete. Runtime will fall back to GOOGLE_ACCESS_TOKEN until the missing fields are set."
        )
    return report


def run_google_direct_auth_check(settings: Settings) -> dict[str, Any]:
    """Validate the active Google token directly against Google APIs."""
    report = build_google_auth_report(settings)
    try:
        access_token = resolve_google_access_token(
            settings,
            force_refresh=settings.has_google_refresh_flow,
        )
    except GoogleOAuthError as exc:
        report["status"] = CHECK_STATUS_FAILED
        report["error"] = sanitize_text(str(exc))
        return report

    report = build_google_auth_report(settings)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    profile_status, profile_payload = _request_google_json(
        GOOGLE_GMAIL_PROFILE_URL,
        settings=settings,
        headers=headers,
    )
    tokeninfo_status, tokeninfo_payload = _request_google_json(
        GOOGLE_TOKENINFO_URL,
        settings=settings,
        params={"access_token": access_token},
    )

    report["google_profile_status"] = profile_status
    report["tokeninfo_status"] = tokeninfo_status

    mailbox = ""
    if isinstance(profile_payload, dict):
        mailbox = sanitize_text(str(profile_payload.get("emailAddress") or "").strip())
    if mailbox:
        report["mailbox"] = mailbox

    reported_scopes: list[str] = []
    if isinstance(tokeninfo_payload, dict):
        scope_text = str(tokeninfo_payload.get("scope") or "").strip()
        reported_scopes = [scope for scope in scope_text.split() if scope]
    report["reported_scopes"] = reported_scopes
    report["expected_scope_present"] = EXPECTED_GMAIL_SCOPE in reported_scopes

    errors: list[str] = []
    if profile_status >= 400:
        errors.append(_describe_google_http_error("profile", profile_status, profile_payload))
    if tokeninfo_status >= 400:
        errors.append(_describe_google_http_error("tokeninfo", tokeninfo_status, tokeninfo_payload))
    if tokeninfo_status < 400 and EXPECTED_GMAIL_SCOPE not in reported_scopes:
        errors.append(
            "Google tokeninfo succeeded, but gmail.readonly is missing from the reported scopes."
        )

    if errors:
        report["status"] = CHECK_STATUS_FAILED
        report["error"] = " | ".join(errors)
        return report

    report["status"] = CHECK_STATUS_OK
    return report


def _coerce_settings(settings: Settings | None) -> Settings:
    if settings is not None:
        return settings
    return load_settings(require_groq=False, require_google=False)


def _cached_token_is_usable(settings: Settings) -> bool:
    if not settings.google_runtime_access_token:
        return False
    if settings.google_runtime_access_token_expires_at <= 0:
        return False
    return settings.google_runtime_access_token_expires_at > (
        time.time() + TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS
    )


def _runtime_expiry_datetime(settings: Settings) -> datetime | None:
    if settings.google_runtime_access_token_expires_at <= 0:
        return None
    return datetime.fromtimestamp(
        settings.google_runtime_access_token_expires_at,
        tz=timezone.utc,
    )


def _store_runtime_token_metadata(
    settings: Settings,
    credentials: Credentials,
    *,
    token_source: str,
) -> None:
    access_token, _ = normalize_google_access_token(str(credentials.token or ""))
    settings.google_runtime_access_token = access_token
    expiry = getattr(credentials, "expiry", None)
    if isinstance(expiry, datetime):
        settings.google_runtime_access_token_expires_at = _normalize_expiry_datetime(expiry).timestamp()
    else:
        settings.google_runtime_access_token_expires_at = 0.0
    settings.google_runtime_token_type = ""
    settings.google_active_token_source = token_source


def _credential_expired(credentials: Credentials) -> bool:
    expiry = getattr(credentials, "expiry", None)
    if not isinstance(expiry, datetime):
        return False
    normalized_expiry = _normalize_expiry_datetime(expiry)
    return normalized_expiry <= datetime.now(timezone.utc)


def _normalize_expiry_datetime(expiry: datetime) -> datetime:
    if expiry.tzinfo is None:
        return expiry.replace(tzinfo=timezone.utc)
    return expiry.astimezone(timezone.utc)


def _build_refresh_error(exc: RefreshError) -> GoogleOAuthError:
    message = sanitize_text(str(exc))
    lowered = message.lower()
    if "invalid_scope" in lowered:
        return GoogleOAuthError(
            "Google refresh token was issued for a different scope set (`invalid_scope`). "
            "Re-run the OAuth consent flow with the scopes from GOOGLE_OAUTH_SCOPES and replace "
            "GOOGLE_REFRESH_TOKEN. If Drive ingest is enabled, make sure "
            "`https://www.googleapis.com/auth/drive.readonly` is included before re-consenting."
        )
    if "invalid_grant" in lowered and "invalid_rapt" in lowered:
        return GoogleOAuthError(
            "Google refresh token requires user reauthentication (`invalid_grant` / `invalid_rapt`). "
            "Re-run the OAuth consent flow to obtain a fresh refresh token."
        )
    if "invalid_grant" in lowered:
        return GoogleOAuthError(
            "Google refresh token was rejected (`invalid_grant`). Check GOOGLE_REFRESH_TOKEN and whether it was revoked."
        )
    if "invalid_client" in lowered:
        return GoogleOAuthError(
            "Google OAuth client credentials were rejected (`invalid_client`). Check GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
        )
    return GoogleOAuthError(
        "Google OAuth refresh failed. " + message
    )


def _request_google_json(
    url: str,
    *,
    settings: Settings,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | str]:
    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=settings.http_timeout,
        )
    except requests.Timeout:
        return 0, f"timeout after {settings.http_timeout}s"
    except requests.RequestException as exc:
        return 0, sanitize_text(str(exc))

    try:
        payload = response.json()
    except ValueError:
        payload = sanitize_text(response.text[:300].strip())
    return response.status_code, payload


def _describe_google_http_error(
    label: str,
    status_code: int,
    payload: dict[str, Any] | str,
) -> str:
    detail = ""
    if isinstance(payload, dict):
        error_node = payload.get("error")
        if isinstance(error_node, dict):
            message = sanitize_text(str(error_node.get("message") or "").strip())
            status = sanitize_text(str(error_node.get("status") or "").strip())
            detail = " | ".join(part for part in (message, status) if part)
        else:
            detail = sanitize_text(str(payload or "").strip())
    else:
        detail = sanitize_text(payload)

    suffix = f" Details: {detail}" if detail else ""
    if status_code == 0:
        return f"Direct Google {label} check failed before an HTTP response.{suffix}"
    return f"Direct Google {label} check returned HTTP {status_code}.{suffix}"
