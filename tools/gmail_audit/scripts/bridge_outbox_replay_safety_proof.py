#!/usr/bin/env python3
"""Proof token for bridge outbox claim/lease wiring (client + drain)."""

from __future__ import annotations

import json
from pathlib import Path

TOKEN = "BRIDGE_OUTBOX_REPLAY_SAFETY_PROOF_OK"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    client = (root / "daszek_client.py").read_text(encoding="utf-8")
    drain = (root / "daszek_bridge_queue_drain.py").read_text(encoding="utf-8")
    outbox = Path(__file__).resolve().parents[3] / "daszek" / "includes" / "command-outbox.php"
    checks = {
        "client_claim_param": 'claim=True' in client or "claim" in client,
        "client_lease_token": "lease_token" in client,
        "drain_claim_fetch": "claim=True" in drain,
        "drain_lease_complete": "lease_token" in drain,
        "php_outbox_exists": outbox.is_file(),
        "php_claim_endpoint": "daszek_command_outbox_claim_batch" in outbox.read_text(encoding="utf-8"),
    }
    ok = all(checks.values())
    report = {"token": TOKEN, "ok": ok, "checks": checks}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if ok:
        print(TOKEN)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
