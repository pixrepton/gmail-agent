#!/usr/bin/env python3
"""Local smoke for Context Projection read-only FastAPI (PR-I).

Exercises fixture runtime only — no VPS deploy, no Gate claim.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = REPO_ROOT / "tools" / "gmail_audit"
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app  # noqa: E402
from mailbox_memory_models import CaseContextPack  # noqa: E402


class _FixtureRuntime:
    def __init__(self) -> None:
        self.pack = CaseContextPack(
            case_id="smoke_case_1",
            active_facts=[{"fact_id": "f1", "fact_key": "status", "value": "open", "source_ref": "gmail:msg-smoke-1"}],
            conflicting_facts=[{"fact_key": "power", "values": ["8 kW", "10 kW"]}],
            completeness_gaps=["Missing site visit notes"],
            source_refs=[{"type": "gmail_message", "message_id": "msg-smoke-1", "source_ref": "gmail:msg-smoke-1"}],
        )

    def get_context_pack(self, *, case_id: str = "", message_id: str = "", query_text: str = "") -> CaseContextPack:
        if case_id == self.pack.case_id:
            return self.pack
        return CaseContextPack(case_id="")


def run_smoke(*, json_out: bool = False) -> int:
    try:
        from fastapi.testclient import TestClient
    except ImportError as exc:
        print(f"FAIL: fastapi/httpx required: {exc}", file=sys.stderr)
        return 2

    from unittest.mock import patch

    with patch("api_app.registry_token_configured", return_value=False):
        app = create_app(runtime_provider=lambda: _FixtureRuntime(), cohort_reader=lambda _rid: None)
        client = TestClient(app)

        health = client.get("/health")
        trays = client.get("/cases/smoke_case_1/context-trays")
        skrzat = client.post(
            "/cases/smoke_case_1/skrzat/ask",
            json={"question": "Czego brakuje w sprawie?", "mode": "ask"},
        )

    errors: list[str] = []
    if health.status_code != 200 or health.json().get("mode") not in {"read_only", "read_mostly"}:
        errors.append("health")
    if trays.status_code != 200 or trays.json().get("schema_version") != "context_tray_set.v1":
        errors.append("context-trays")
    if skrzat.status_code != 200:
        errors.append(f"skrzat-http-{skrzat.status_code}")
    else:
        body = skrzat.json()
        if body.get("read_only") is not True or body.get("action_allowed") is not False:
            errors.append("skrzat-read-only-flags")
        if not body.get("gaps") and not body.get("evidence"):
            errors.append("skrzat-empty-answer-payload")
        audit = body.get("context_audit") if isinstance(body.get("context_audit"), dict) else {}
        if audit.get("stage_name") != "skrzat_copilot":
            errors.append("skrzat-missing-context-audit")
        if not body.get("quality_metrics"):
            errors.append("skrzat-missing-quality-metrics")

    result = {
        "ok": not errors,
        "errors": errors,
        "health": health.json() if health.status_code == 200 else {"status": health.status_code},
        "trays_schema": trays.json().get("schema_version") if trays.status_code == 200 else None,
        "skrzat": skrzat.json() if skrzat.status_code == 200 else {"status": skrzat.status_code},
    }

    if json_out:
        payload = json.dumps(result, ensure_ascii=True, indent=2)
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            payload = json.dumps(result, ensure_ascii=False, indent=2)
        except (AttributeError, ValueError):
            pass
        print(payload)
    elif errors:
        print("FAIL:", ", ".join(errors))
    else:
        print("OK: context-trays + skrzat/ask read-only smoke passed (fixture runtime)")

    return 0 if not errors else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Context Projection read-only API smoke (local fixture).")
    parser.add_argument("--json", action="store_true", help="Emit JSON result")
    args = parser.parse_args()
    return run_smoke(json_out=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
