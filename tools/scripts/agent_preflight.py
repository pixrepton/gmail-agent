#!/usr/bin/env python3
"""Session preflight for TOP-INSTAL gmail-agent agents.

Safe by design:
- does not read .env files,
- does not print environment variable values,
- does not mutate runtime state,
- only checks local process env and bounded HTTP/SSH reachability.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_B_ROOT_TEXT = "/opt/gmail-agent/current"
NODE_B_ROOT = Path(NODE_B_ROOT_TEXT)
LAST_PROVEN_STATE = REPO_ROOT / "docs" / "runbooks" / "LAST_PROVEN_STATE.md"
OPERATIONAL_HANDOFF = REPO_ROOT / "OPERATIONAL_HANDOFF.md"
DOTENV_PRESENCE_CANDIDATES = [
    REPO_ROOT / "tools" / "gmail_audit" / ".env",
    REPO_ROOT / ".env",
    REPO_ROOT / ".env.vps",
]


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 5) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or REPO_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "status": "missing_command", "cmd": cmd[0]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout", "cmd": cmd[0]}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _git_status() -> dict[str, Any]:
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    commit = _run(["git", "rev-parse", "--short", "HEAD"], timeout=5)
    status = _run(["git", "status", "--short"], timeout=10)
    lines = status.get("stdout", "").splitlines() if status.get("ok") else []
    return {
        "branch": branch.get("stdout", "") if branch.get("ok") else "unknown",
        "commit": commit.get("stdout", "") if commit.get("ok") else "unknown",
        "status_count": len(lines),
        "status_short": lines,
        "status_ok": bool(status.get("ok")),
    }


def _node_context() -> dict[str, Any]:
    cwd = Path.cwd()
    try:
        under_node_b = cwd.resolve().is_relative_to(NODE_B_ROOT)
    except Exception:
        under_node_b = False
    node_b_root_exists = NODE_B_ROOT.exists()
    if under_node_b:
        location = "node_b_vps_runtime"
    elif node_b_root_exists:
        location = "host_with_node_b_root"
    else:
        location = "local_workspace_or_non_node_b_host"
    return {
        "location": location,
        "pwd": str(cwd),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "whoami": getpass.getuser(),
        "node_b_root_exists": node_b_root_exists,
        "node_b_root": NODE_B_ROOT_TEXT,
    }


def _ssh_alias(alias: str, timeout: int) -> dict[str, Any]:
    if shutil.which("ssh") is None:
        return {"alias": alias, "status": "ssh_missing", "reachable": False}
    cfg = _run(["ssh", "-G", alias], timeout=timeout)
    if not cfg.get("ok"):
        return {"alias": alias, "status": "not_configured", "reachable": False}
    config_lines = str(cfg.get("stdout") or "").splitlines()
    host_line = next((line for line in config_lines if line.lower().startswith("hostname ")), "")
    configured_host = host_line.split(" ", 1)[1].strip() if " " in host_line else ""
    status = "configured" if configured_host and configured_host != alias else "not_configured_or_plain_hostname"
    smoke = _run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={timeout}",
            alias,
            "cd /opt/gmail-agent/current 2>/dev/null && pwd || pwd",
        ],
        timeout=timeout + 3,
    )
    return {
        "alias": alias,
        "status": status,
        "reachable": bool(smoke.get("ok")),
        "remote_pwd": smoke.get("stdout", "").splitlines()[:1],
        "error_class": "" if smoke.get("ok") else smoke.get("status", "ssh_failed"),
    }


def _redacted_db_ref(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname or "",
        "port": str(parsed.port or ""),
        "database": parsed.path.lstrip("/")[:80],
    }


def _dotenv_key_presence(key: str) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    needle = f"{key}="
    export_needle = f"export {key}="
    for path in DOTENV_PRESENCE_CANDIDATES:
        item: dict[str, Any] = {
            "path": str(path.relative_to(REPO_ROOT)),
            "exists": path.is_file(),
            "key_present": False,
        }
        if path.is_file():
            try:
                for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith(needle) or line.startswith(export_needle):
                        item["key_present"] = True
                        break
            except OSError as exc:
                item["read_error"] = type(exc).__name__
        files.append(item)
    return {
        "enabled": True,
        "mode": "presence_only",
        "key": key,
        "files": files,
        "value_printed": False,
    }


def _mailbox_memory(timeout: int, *, allow_dotenv_presence_check: bool = False) -> dict[str, Any]:
    url = os.environ.get("MAILBOX_MEMORY_DATABASE_URL", "").strip()
    if not url:
        out: dict[str, Any] = {
            "status": "env_not_exported_to_preflight_process",
            "available": False,
            "meaning": (
                "Preflight did not receive MAILBOX_MEMORY_DATABASE_URL in os.environ. "
                "It may still be configured in tools/gmail_audit/.env or on the VPS; "
                "this local process cannot verify DB reachability from that file."
            ),
        }
        if allow_dotenv_presence_check:
            out["dotenv_presence"] = _dotenv_key_presence("MAILBOX_MEMORY_DATABASE_URL")
        else:
            out["dotenv_presence"] = {
                "enabled": False,
                "meaning": "Use --allow-dotenv-presence-check to check key presence without printing values.",
            }
        return out
    ref = _redacted_db_ref(url)
    try:
        import psycopg  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"status": "driver_missing", "available": False, "target": ref, "error": type(exc).__name__}
    try:
        with psycopg.connect(url, connect_timeout=timeout) as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        return {"status": "connect_failed", "available": False, "target": ref, "error": type(exc).__name__}
    return {"status": "ok", "available": True, "target": ref}


def _daszek(timeout: int) -> dict[str, Any]:
    base = os.environ.get("DASZEK_BASE_URL", "").strip() or "https://topinstal.com.pl"
    base = base.rstrip("/")
    url = f"{base}/wp-json/daszek/v2/desk"
    parsed = urlparse(url)
    safe_ref = {"scheme": parsed.scheme, "host": parsed.hostname or "", "path": parsed.path}
    req = Request(url, headers={"User-Agent": "gmail-agent-preflight/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - bounded operator preflight URL
            return {"status": "responded", "http_status": resp.status, "endpoint": safe_ref}
    except HTTPError as exc:
        return {"status": "responded_http_error", "http_status": exc.code, "endpoint": safe_ref}
    except URLError as exc:
        return {"status": "unreachable", "endpoint": safe_ref, "error": type(exc.reason).__name__}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unreachable", "endpoint": safe_ref, "error": type(exc).__name__}


def _first_proven_state() -> dict[str, Any]:
    if not LAST_PROVEN_STATE.is_file():
        return {"status": "missing", "path": str(LAST_PROVEN_STATE)}
    lines = LAST_PROVEN_STATE.read_text(encoding="utf-8", errors="replace").splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.startswith("## 20"):
            start = idx
            break
    if start is None:
        return {"status": "no_proof_heading", "path": str(LAST_PROVEN_STATE)}
    block: list[str] = []
    for line in lines[start : start + 80]:
        if block and line.startswith("## "):
            break
        if any(key in line for key in ("## ", "status globalny", "run_id", "proof_dir", "proof_pack", "ograniczenia")):
            block.append(line)
    return {"status": "ok", "path": str(LAST_PROVEN_STATE), "summary": block[:20]}


def _handoff_summary() -> dict[str, Any]:
    if not OPERATIONAL_HANDOFF.is_file():
        return {"status": "missing", "path": str(OPERATIONAL_HANDOFF)}
    lines = OPERATIONAL_HANDOFF.read_text(encoding="utf-8", errors="replace").splitlines()
    wanted: list[str] = []
    for heading in ("Fast status summary", "Current proof interpretation"):
        try:
            idx = next(i for i, line in enumerate(lines) if line.strip() == heading)
        except StopIteration:
            continue
        wanted.append(lines[idx])
        in_fence = False
        for line in lines[idx + 1 :]:
            if line.startswith("```"):
                wanted.append(line)
                if in_fence:
                    break
                in_fence = True
                continue
            if in_fence:
                wanted.append(line)
        if wanted:
            break
    return {"status": "ok", "path": str(OPERATIONAL_HANDOFF), "summary": wanted[:35]}


def build_report(timeout: int, *, allow_dotenv_presence_check: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "agent_preflight.v1",
        "node_context": _node_context(),
        "git": _git_status(),
        "node_b_alias": _ssh_alias("topinstal-node-b", timeout),
        "mailbox_memory_db": _mailbox_memory(timeout, allow_dotenv_presence_check=allow_dotenv_presence_check),
        "daszek_endpoint": _daszek(timeout),
        "last_proven_state": _first_proven_state(),
        "operational_handoff": _handoff_summary(),
        "safety": {
            "read_env_files": "presence_only" if allow_dotenv_presence_check else False,
            "printed_secrets": False,
            "runtime_mutation": False,
            "proof_claim": "none",
        },
    }


def print_text(report: dict[str, Any]) -> None:
    node = report["node_context"]
    git = report["git"]
    print("agent-preflight")
    print(f"location: {node['location']}")
    print(f"pwd: {node['pwd']}")
    print(f"hostname: {node['hostname']}")
    print(f"whoami: {node['whoami']}")
    print(f"node_b_root_exists: {node['node_b_root_exists']} ({node['node_b_root']})")
    print("")
    print(f"git: branch={git['branch']} commit={git['commit']} status_count={git['status_count']}")
    for line in git["status_short"][:80]:
        print(f"  {line}")
    if git["status_count"] > 80:
        print(f"  ... truncated {git['status_count'] - 80} status lines")
    print("")
    print(f"topinstal-node-b alias: {report['node_b_alias']}")
    print(f"mailbox_memory_db: {report['mailbox_memory_db']}")
    print(f"daszek_endpoint: {report['daszek_endpoint']}")
    print("")
    print("last_proven_state:")
    for line in report["last_proven_state"].get("summary", []):
        print(f"  {line}")
    print("")
    print("operational_handoff:")
    for line in report["operational_handoff"].get("summary", []):
        print(f"  {line}")
    print("")
    print(f"safety: {report['safety']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe session preflight for gmail-agent agents.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument("--timeout", type=int, default=5, help="Bounded timeout in seconds for checks.")
    parser.add_argument(
        "--allow-dotenv-presence-check",
        action="store_true",
        help="Check only whether known .env files contain required key names; never print values.",
    )
    args = parser.parse_args(argv)
    report = build_report(
        max(1, int(args.timeout)),
        allow_dotenv_presence_check=bool(args.allow_dotenv_presence_check),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
