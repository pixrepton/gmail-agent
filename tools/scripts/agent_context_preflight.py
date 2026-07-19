#!/usr/bin/env python3
"""Repo-local agent context preflight.

This deterministic guardrail checks the current documentation canon. It does
not read secrets, call network services, or prove runtime state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

CANONICAL_CONTEXT = [
    "AGENTS.md",
    "README.md",
    "docs/README.md",
    "docs/core/PROJECT_README.md",
    "docs/runbooks/LAST_PROVEN_STATE.md",
]

REQUIRED_CONTROL_FILES = [
    ".cursor/rules/00-topinstal-core-router.mdc",
    ".cursor/hooks/stop-followup.js",
    "tools/scripts/agent_harness_audit.py",
]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")


def _line_count(rel: str) -> int:
    return len(_read(rel).splitlines())


def main() -> int:
    checks: list[dict[str, object]] = []
    failures: list[str] = []

    for rel in [*CANONICAL_CONTEXT, *REQUIRED_CONTROL_FILES]:
        exists = (REPO_ROOT / rel).is_file()
        checks.append({"path": rel, "exists": exists})
        if not exists:
            failures.append(f"missing required agent context file: {rel}")

    if not failures:
        agents = _read("AGENTS.md")
        router = _read(".cursor/rules/00-topinstal-core-router.mdc")
        stop_hook = _read(".cursor/hooks/stop-followup.js")
        harness = _read("tools/scripts/agent_harness_audit.py")

        for rel in ("docs/core/PROJECT_README.md", "docs/runbooks/LAST_PROVEN_STATE.md"):
            if rel not in agents:
                failures.append(f"AGENTS.md does not route through {rel}")

        if "gmail-agent" not in agents:
            failures.append("AGENTS.md missing gmail-agent routing")
        if "memory-bank" in agents or "memory-bank" in router or "memory-bank" in stop_hook:
            failures.append("active agent context still references memory-bank")
        if "check_canonical_context" not in harness:
            failures.append("agent_harness_audit.py does not enforce canonical context")

    result = {
        "ok": not failures,
        "repo_root": str(REPO_ROOT),
        "canonical_context": [
            {
                "path": rel,
                "exists": (REPO_ROOT / rel).is_file(),
                "lines": _line_count(rel) if (REPO_ROOT / rel).is_file() else 0,
            }
            for rel in CANONICAL_CONTEXT
        ],
        "control_files": checks,
        "failures": failures,
        "not_proof": [
            "This is local agent-context preflight only.",
            "It does not prove Node A, Node B, VPS, Gmail, Drive, Daszek, Postgres, or Gate status.",
        ],
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
