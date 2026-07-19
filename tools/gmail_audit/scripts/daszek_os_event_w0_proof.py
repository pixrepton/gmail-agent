#!/usr/bin/env python3
"""Bounded W0 proof: HITL approve → gmail.hitl.approved on Node B + Daszek proxy.

Requires local Docker stack (Daszek :8090, Node B :8765) and mailbox Postgres.

Stdout on success: DASZEK_OS_EVENT_W0_PROOF_OK
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent

if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import load_settings  # noqa: E402
from daszek_local_133_proof import (  # noqa: E402
    PROOF_CASE_ID,
    PROOF_ENGAGEMENT_ID,
    _base_url,
    _fetch_os_events_daszek,
    _fetch_os_events_node_b,
    _hitl_approve_via_daszek,
    _node_b_token,
    _seed_hitl_engagement,
    _session_login,
    _verify_w0_os_event_projection,
)


def main() -> int:
    settings = load_settings(require_groq=False, require_google=False)
    base = _base_url(settings)
    login = str(settings.daszek_login or "konrad").strip()
    password = str(settings.daszek_password or "")
    if not password:
        print("ERROR: DASZEK_PASSWORD required", file=sys.stderr)
        return 2

    nb_token = _node_b_token(settings)
    report: dict = {"proof": "daszek_os_event_w0", "ok": False}

    try:
        report["seed_hitl"] = _seed_hitl_engagement(settings)
        sess = _session_login(base, login, password)
        report["hitl_approve"] = _hitl_approve_via_daszek(sess, base)
        report["os_events_node_b"] = _fetch_os_events_node_b(nb_token, PROOF_ENGAGEMENT_ID)
        report["os_events_daszek"] = _fetch_os_events_daszek(sess, base, PROOF_ENGAGEMENT_ID)
        report["verify"] = _verify_w0_os_event_projection(
            approve_payload=report["hitl_approve"],
            node_b_payload=report["os_events_node_b"],
            daszek_payload=report["os_events_daszek"],
        )
        if not report["verify"].get("ok"):
            raise RuntimeError(f"W0 verification failed: {report['verify']}")
        report["ok"] = True
        print(
            json.dumps(
                {
                    "ok": True,
                    "engagement_id": PROOF_ENGAGEMENT_ID,
                    "case_id": PROOF_CASE_ID,
                    "os_event_id": report["hitl_approve"].get("os_event_id"),
                },
                indent=2,
            )
        )
        print("DASZEK_OS_EVENT_W0_PROOF_OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
        print(f"DASZEK_OS_EVENT_W0_PROOF_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
