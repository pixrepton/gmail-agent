#!/usr/bin/env python3
"""Proof: build_operator_engagement_store guard against silent InMemory fallback.

Audits all callers and runs guard unit tests.

Stdout on success: ENGAGEMENT_STORE_FALLBACK_GUARD_PROOF_OK
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_reconcile import build_operator_engagement_store  # noqa: E402
from agent_runtime.store import InMemoryOperatorEngagementStore, PostgresOperatorEngagementStore  # noqa: E402
from agent_runtime.validate import AgentRuntimeConfigError  # noqa: E402

# Callers that must fail loudly without postgres (production / Docker paths).
PRODUCTION_CALLERS: dict[str, str] = {
    "api_app.py": "load_settings(); default allow_in_memory=False",
    "agent_hitl_bridge.py": "load_settings(); default allow_in_memory=False",
    "signal_reconciler.py": "runtime_context.settings; default allow_in_memory=False",
    "agent_reconcile.py": "runtime_context.settings; default allow_in_memory=False",
    "mcp_service.py": "load_agent_runtime_settings(); default allow_in_memory=False",
    "daszek_engagement_feed/__init__.py": "settings from caller; default allow_in_memory=False",
    "scripts/seed_materialize_ctfu5.py": "load_settings() + isinstance(PostgresOperatorEngagementStore) guard",
}

# Tests patch the store directly or use allow_in_memory=True — not production callers.
TEST_PATCH_CALLERS = {
    "tests/test_daszek_engagement_feed_pr_e_complete.py": "patch build_operator_engagement_store -> InMemory",
}


def _find_call_sites(root: Path) -> list[dict[str, str | int | bool]]:
    sites: list[dict[str, str | int | bool]] = []
    skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
    for path in sorted(root.rglob("*.py")):
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "build_operator_engagement_store":
                continue
            allow_in_memory = False
            for kw in node.keywords:
                if kw.arg == "allow_in_memory" and isinstance(kw.value, ast.Constant):
                    allow_in_memory = bool(kw.value.value)
            sites.append(
                {
                    "file": rel,
                    "line": int(getattr(node, "lineno", 0)),
                    "allow_in_memory": allow_in_memory,
                    "is_test": rel.startswith("tests/"),
                }
            )
    return sites


def _run_guard_unit_tests() -> None:
    tests = Path(__file__).resolve().parents[1] / "tests" / "test_engagement_store_fallback_guard.py"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(tests), "-q"],
        cwd=str(TOOL_DIR),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pytest failed:\n{proc.stdout}\n{proc.stderr}")


def _assert_guard_behavior() -> dict[str, object]:
    settings = SimpleNamespace(mailbox_memory_database_url="")
    with patch.dict("os.environ", {}, clear=True):
        try:
            build_operator_engagement_store(settings)
            raise AssertionError("expected AgentRuntimeConfigError without database URL")
        except AgentRuntimeConfigError:
            pass
        store = build_operator_engagement_store(settings, allow_in_memory=True)
        if not isinstance(store, InMemoryOperatorEngagementStore):
            raise AssertionError("allow_in_memory=True must return InMemoryOperatorEngagementStore")
    pg_settings = SimpleNamespace(
        mailbox_memory_database_url="postgresql://proof:proof@127.0.0.1:5432/mailbox"
    )
    pg_store = build_operator_engagement_store(pg_settings)
    if not isinstance(pg_store, PostgresOperatorEngagementStore):
        raise AssertionError("settings URL must yield PostgresOperatorEngagementStore")
    return {"guard_behavior": "ok"}


def _audit_callers(sites: list[dict[str, str | int | bool]]) -> dict[str, object]:
    production_hits: list[str] = []
    explicit_in_memory: list[str] = []
    test_hits: list[str] = []
    unknown: list[str] = []

    for site in sites:
        rel = str(site["file"])
        label = f"{rel}:{site['line']}"
        if rel.endswith("engagement_store_fallback_guard_proof.py"):
            continue
        if site.get("is_test"):
            test_hits.append(label)
            continue
        if site.get("allow_in_memory"):
            explicit_in_memory.append(label)
            continue
        matched = False
        for key in PRODUCTION_CALLERS:
            if key in rel.replace("\\", "/"):
                production_hits.append(label)
                matched = True
                break
        if not matched:
            if "agent_reconcile.py" in rel and "def build_operator_engagement_store" in label:
                matched = True
            elif rel.endswith("agent_reconcile.py"):
                production_hits.append(label)
                matched = True
        if not matched:
            unknown.append(label)

    if unknown:
        raise RuntimeError(f"unclassified build_operator_engagement_store callers: {unknown}")
    if not production_hits and not explicit_in_memory:
        raise RuntimeError("no build_operator_engagement_store call sites found")

    return {
        "call_sites_total": len(sites),
        "production_callers": len(production_hits),
        "explicit_allow_in_memory": explicit_in_memory,
        "test_callers": len(test_hits),
        "production_policy": PRODUCTION_CALLERS,
        "test_patch_policy": TEST_PATCH_CALLERS,
    }


def main() -> int:
    report: dict[str, object] = {"proof": "engagement_store_fallback_guard", "ok": False}
    try:
        sites = _find_call_sites(TOOL_DIR)
        report["guard"] = _assert_guard_behavior()
        report["callers"] = _audit_callers(sites)
        _run_guard_unit_tests()
        report["pytest"] = "pass"
        report["ok"] = True
        print(json.dumps(report, indent=2, ensure_ascii=True))
        print("ENGAGEMENT_STORE_FALLBACK_GUARD_PROOF_OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
        print(f"ENGAGEMENT_STORE_FALLBACK_GUARD_PROOF_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
