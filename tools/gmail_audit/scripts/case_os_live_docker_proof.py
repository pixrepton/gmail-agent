#!/usr/bin/env python3
"""Live Docker Gate B: Case OS product on local stack.

Requires:
  - gmail-agent-nodeb-api :8766
  - Daszek :8090 (optional for feed readback)
  - MAILBOX_MEMORY_DATABASE_URL in tools/gmail_audit/.env

Stdout on success: CASE_OS_LIVE_DOCKER_PROOF_OK
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import load_settings
from mailbox_memory_runtime import build_mailbox_memory_runtime


def _node_b_base() -> str:
    return (os.getenv("NODE_B_REGISTRY_BASE_URL") or "http://127.0.0.1:8766").rstrip("/")


def _token(settings: Any) -> str:
    for key in ("daszek_node_b_service_token", "daszek_bridge_token"):
        val = str(getattr(settings, key, "") or "").strip()
        if val:
            return val
    raise RuntimeError("Missing DASZEK_NODE_B_SERVICE_TOKEN or DASZEK_BRIDGE_TOKEN")


def _pick_case(settings: Any) -> str:
    runtime = build_mailbox_memory_runtime(settings)
    if runtime is None:
        raise RuntimeError("Mailbox memory runtime unavailable")
    runtime.bootstrap()
    store = runtime.store
    rows = store.fetch_cases(limit=30) if hasattr(store, "fetch_cases") else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("case_id") or "").strip()
        if cid and not cid.startswith("case_local_hitl"):
            pack = runtime.get_context_pack(case_id=cid, query_text="")
            resolved = str(getattr(pack, "case_id", "") or (pack.get("case_id") if isinstance(pack, dict) else "") or "").strip()
            if resolved:
                return resolved
    raise RuntimeError("No mailbox case with context pack for live Skrzat proof")


def _verify_node_b_health(token: str) -> None:
    r = requests.get(f"{_node_b_base()}/health", headers={"Authorization": f"Bearer {token}"}, timeout=20)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or data.get("contract_surface") != "case_context_pack_vnext":
        raise RuntimeError(f"Node B health/contract failed: HTTP {r.status_code} {data}")
    print("CASE_OS_LIVE_NODE_B_OK")


def _verify_skrzat_live(token: str, case_id: str) -> None:
    r = requests.post(
        f"{_node_b_base()}/cases/{case_id}/skrzat/ask",
        json={"question": "Czego brakuje w tej sprawie?", "mode": "ask"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=60,
    )
    body = r.json() if r.content else {}
    if r.status_code >= 400:
        raise RuntimeError(f"Skrzat live failed HTTP {r.status_code}: {body}")
    lineage = body.get("context_pack_lineage") if isinstance(body.get("context_pack_lineage"), dict) else {}
    rag = body.get("rag_advisory") if isinstance(body.get("rag_advisory"), dict) else {}
    if lineage.get("case_id") != case_id:
        raise RuntimeError(f"Missing context_pack_lineage.case_id for {case_id}")
    if rag.get("schema_version") != "case_os.rag_advisory_slice.v1":
        raise RuntimeError(f"Missing rag_advisory slice: {rag}")
    if rag.get("boundary") != "D1_advisory_only":
        raise RuntimeError("rag_advisory boundary must be D1_advisory_only")
    print("CASE_OS_LIVE_SKRZAT_OK")


def _verify_daszek_html() -> None:
    base = str(os.getenv("DASZEK_BASE_URL") or "http://127.0.0.1:8090").rstrip("/")
    r = requests.get(f"{base}/daszek/", timeout=30)
    html = r.text or ""
    for marker in ("Biurko Case OS", "Sprawy Case OS", "System Case OS"):
        if marker not in html:
            raise RuntimeError(f"Daszek HTML missing nav marker: {marker}")
    print("CASE_OS_LIVE_DASZEK_HTML_OK")


def main() -> int:
    try:
        settings = load_settings(require_groq=False, require_google=False)
        token = _token(settings)
        _verify_node_b_health(token)
        case_id = _pick_case(settings)
        _verify_skrzat_live(token, case_id)
        _verify_daszek_html()
        print("CASE_OS_LIVE_DOCKER_PROOF_OK")
        return 0
    except Exception as exc:
        print(f"CASE_OS_LIVE_DOCKER_PROOF_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
