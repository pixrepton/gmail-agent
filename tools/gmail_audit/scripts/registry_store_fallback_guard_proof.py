#!/usr/bin/env python3
"""Proof: build_registry_for_reconcile guard against silent InMemory fallback.

Audits all callers and runs guard unit tests.

Stdout on success: REGISTRY_STORE_FALLBACK_GUARD_PROOF_OK
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

from agent_runtime.agent_reconcile import build_registry_for_reconcile  # noqa: E402
from agent_runtime.validate import AgentRuntimeConfigError  # noqa: E402
from correlation_registry.store import InMemoryCorrelationRegistryStore  # noqa: E402

PRODUCTION_CALLERS: dict[str, str] = {
    "agent_reconcile.py": "runtime_context.settings; default allow_in_memory=False",
    "materialize_bridge.py": "load_settings(); catches ConfigError -> graceful degrade",
    "engagement_resolver.py": "database_url only -> in_memory=False; no URL -> RuntimeError",
    "api_app.py": "build_correlation_registry_service(db_url) direct with URL guard",
    "mailbox_memory_runtime.py": "explicit in_memory=True only inside allow_in_memory block",
    "run_backfill_correlation_registry.py": "explicit db_url from CLI/env",
}

TEST_PATCH_CALLERS = {
    "tests/test_signal_reconciler_agent_pr_d.py": "patch build_registry_for_reconcile -> registry",
    "tests/test_digital_twin_cel_radlin_dod.py": "patch build_registry_for_reconcile -> registry",
    "tests/test_agent_pr_a_b_complete.py": "resolve_engagement_for_case(registry=...) explicit",
}


def _find_call_sites(root: Path) -> list[dict[str, str | int | bool]]:
    sites: list[dict[str, str | int | bool]] = []
    skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
    targets = {"build_registry_for_reconcile", "build_correlation_registry_service"}
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
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
            if name not in targets:
                continue
            allow_in_memory = False
            silent_in_memory_expr = False
            for kw in node.keywords:
                if kw.arg == "allow_in_memory" and isinstance(kw.value, ast.Constant):
                    allow_in_memory = bool(kw.value.value)
                if kw.arg == "in_memory" and isinstance(kw.value, ast.UnaryOp):
                    silent_in_memory_expr = True
            sites.append(
                {
                    "file": rel,
                    "line": int(getattr(node, "lineno", 0)),
                    "fn": name,
                    "allow_in_memory": allow_in_memory,
                    "silent_in_memory_expr": silent_in_memory_expr,
                    "is_test": rel.startswith("tests/"),
                }
            )
    return sites


def _run_guard_unit_tests() -> None:
    tests = Path(__file__).resolve().parents[1] / "tests" / "test_registry_store_fallback_guard.py"
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
            build_registry_for_reconcile(settings)
            raise AssertionError("expected AgentRuntimeConfigError without database URL")
        except AgentRuntimeConfigError:
            pass
        registry = build_registry_for_reconcile(settings, allow_in_memory=True)
        if registry is None or not isinstance(registry.store, InMemoryCorrelationRegistryStore):
            raise AssertionError("allow_in_memory=True must return InMemoryCorrelationRegistryStore")
    return {"guard_behavior": "ok"}


def _audit_callers(sites: list[dict[str, str | int | bool]]) -> dict[str, object]:
    production_hits: list[str] = []
    explicit_in_memory: list[str] = []
    silent_patterns: list[str] = []
    test_hits: list[str] = []
    unknown: list[str] = []

    for site in sites:
        rel = str(site["file"])
        label = f"{rel}:{site['line']}:{site['fn']}"
        if rel.endswith("registry_store_fallback_guard_proof.py"):
            continue
        if site.get("is_test"):
            test_hits.append(label)
            continue
        if site.get("silent_in_memory_expr"):
            silent_patterns.append(label)
            continue
        if site.get("allow_in_memory") or (
            site.get("fn") == "build_correlation_registry_service" and site.get("allow_in_memory")
        ):
            explicit_in_memory.append(label)
            continue
        if site.get("fn") == "build_correlation_registry_service":
            if "mailbox_memory_runtime.py" in rel:
                production_hits.append(label)
                continue
            if "api_app.py" in rel or "run_backfill" in rel:
                production_hits.append(label)
                continue
        matched = False
        for key in PRODUCTION_CALLERS:
            if key in rel.replace("\\", "/"):
                production_hits.append(label)
                matched = True
                break
        if not matched and site.get("fn") == "build_registry_for_reconcile":
            if "agent_reconcile.py" in rel and "def build_registry_for_reconcile" not in label:
                production_hits.append(label)
                matched = True
        if not matched:
            unknown.append(label)

    if silent_patterns:
        raise RuntimeError(f"silent in_memory=not url patterns remain: {silent_patterns}")
    if unknown:
        raise RuntimeError(f"unclassified registry factory callers: {unknown}")

    return {
        "call_sites_total": len(sites),
        "production_callers": len(production_hits),
        "explicit_allow_in_memory": explicit_in_memory,
        "test_callers": len(test_hits),
        "production_policy": PRODUCTION_CALLERS,
        "test_patch_policy": TEST_PATCH_CALLERS,
    }


def main() -> int:
    report: dict[str, object] = {"proof": "registry_store_fallback_guard", "ok": False}
    try:
        sites = _find_call_sites(TOOL_DIR)
        report["guard"] = _assert_guard_behavior()
        report["callers"] = _audit_callers(sites)
        _run_guard_unit_tests()
        report["pytest"] = "pass"
        report["ok"] = True
        print(json.dumps(report, indent=2, ensure_ascii=True))
        print("REGISTRY_STORE_FALLBACK_GUARD_PROOF_OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=True, indent=2), file=sys.stderr)
        print(f"REGISTRY_STORE_FALLBACK_GUARD_PROOF_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
