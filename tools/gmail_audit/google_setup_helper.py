"""Google OAuth / Drive setup helpers for local operator onboarding."""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlencode

from config import Settings


def build_authorization_url(
    settings: Settings,
    *,
    redirect_uri: str,
    state: str | None = None,
) -> tuple[str, str]:
    """Build Google OAuth authorization URL and return (url, state)."""
    oauth_state = str(state or secrets.token_urlsafe(16))
    scopes = " ".join(str(s) for s in (settings.google_oauth_scopes or ()) if str(s).strip())
    params = {
        "client_id": str(settings.google_client_id or "").strip(),
        "redirect_uri": str(redirect_uri or "").strip(),
        "response_type": "code",
        "scope": scopes,
        "access_type": "offline",
        "prompt": "consent",
        "state": oauth_state,
    }
    query = urlencode(params)
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}", oauth_state


def parse_drive_folder_id(raw: str) -> str:
    """Accept raw Drive folder id or folders/ URL."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if "/folders/" in text:
        return text.rstrip("/").split("/folders/")[-1].split("?")[0].strip()
    return text


def parse_oauth_callback_request_path(
    request_path: str,
    *,
    redirect_uri: str,
) -> dict[str, Any]:
    """Parse OAuth callback path/query; detect redirect path mismatch."""
    redirect = urlparse(str(redirect_uri or "").strip())
    req = urlparse(str(request_path or "").strip())
    expected_path = redirect.path or "/"
    actual_path = req.path or "/"
    out: dict[str, Any] = {}
    if expected_path != actual_path:
        out["path_mismatch"] = "1"
        return out
    params = parse_qs(req.query)
    if params.get("error"):
        out["error"] = str(params["error"][0])
    if params.get("code"):
        out["code"] = str(params["code"][0])
    if params.get("state"):
        out["state"] = str(params["state"][0])
    return out


def upsert_env_value(env_path: Path, key: str, value: str) -> None:
    """Replace or append KEY=value in .env; dedupe duplicate keys."""
    path = Path(env_path)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    prefix = f"{key}="
    kept = [line for line in lines if not line.startswith(prefix)]
    kept.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def run_local_oauth_listen(
    settings: Settings,
    *,
    redirect_uri: str,
    state: str,
    auth_url_file: Path,
    write_env: bool = True,
    open_browser_flag: bool = True,
    timeout_sec: float = 120.0,
) -> dict[str, Any]:
    """Start local callback listener; write auth URL file before blocking."""
    auth_url, oauth_state = build_authorization_url(
        settings, redirect_uri=redirect_uri, state=state
    )
    payload = {
        "status": "waiting_for_browser",
        "auth_url": auth_url,
        "state": oauth_state,
        "redirect_uri": redirect_uri,
    }
    Path(auth_url_file).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    parsed_redirect = urlparse(redirect_uri)
    host = parsed_redirect.hostname or "127.0.0.1"
    port = parsed_redirect.port or 8765
    callback_path = parsed_redirect.path or "/callback"
    result: dict[str, Any] = {"status": "failed", "reason": "timeout"}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            nonlocal result
            parsed = parse_oauth_callback_request_path(self.path, redirect_uri=redirect_uri)
            if parsed.get("path_mismatch"):
                self.send_response(404)
                self.end_headers()
                return
            if parsed.get("error"):
                result = {"status": "failed", "error": parsed["error"]}
            elif parsed.get("code"):
                result = {
                    "status": "ok",
                    "code": parsed["code"],
                    "state": parsed.get("state") or "",
                }
                if write_env and parsed.get("code"):
                    upsert_env_value(settings.env_path, "GOOGLE_AUTH_CODE", str(parsed["code"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    server = HTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    if open_browser_flag:
        try:
            import webbrowser

            webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001
            pass
    deadline = time.monotonic() + float(timeout_sec)
    while time.monotonic() < deadline and result.get("status") == "failed":
        time.sleep(0.05)
    server.server_close()
    return result


__all__ = [
    "build_authorization_url",
    "parse_drive_folder_id",
    "parse_oauth_callback_request_path",
    "run_local_oauth_listen",
    "upsert_env_value",
]
