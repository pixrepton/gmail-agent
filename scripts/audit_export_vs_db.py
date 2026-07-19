#!/usr/bin/env python3
"""Audit synthetic bootstrap classification vs runtime classify_message."""

from __future__ import annotations

import json
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TOOL_DIR = WORKSPACE_ROOT / "tools" / "gmail_audit"
FIXTURE = TOOL_DIR / "fixtures" / "synthetic_bootstrap_cases.json"

if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_routing import route_gmail_message  # noqa: E402
from mail_classification import classify_message  # noqa: E402


def run_audit() -> dict:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mismatches: list[dict] = []
    for row in rows:
        cls = classify_message(
            subject=row["subject"],
            snippet=row.get("body", "")[:240],
            sender=row["sender"],
            labels=row.get("labels", []),
            body=row.get("body", ""),
            has_attachment=bool(row.get("has_attachment")),
            direction="inbound",
        )
        routing = route_gmail_message(
            subject=row["subject"],
            snippet=row.get("body", "")[:240],
            sender=row["sender"],
            labels=row.get("labels", []),
            body=row.get("body", ""),
            has_attachment=bool(row.get("has_attachment")),
        )
        got = {
            "case_type": cls.get("case_type"),
            "case_family": routing.case_family,
            "requires_action": routing.requires_action,
            "upsert_allowed": routing.upsert_allowed,
        }
        expected = {
            "case_type": row["expected_case_type"],
            "case_family": row["expected_case_family"],
            "requires_action": row["expected_requires_action"],
            "upsert_allowed": row["expected_upsert"],
        }
        if got != expected:
            mismatches.append({"id": row["id"], "expected": expected, "got": got})
    total = len(rows)
    ok = total - len(mismatches)
    return {"total": total, "ok": ok, "mismatch": len(mismatches), "match_rate": ok / total if total else 0, "mismatches": mismatches[:20]}


if __name__ == "__main__":
    print(json.dumps(run_audit(), ensure_ascii=False))
