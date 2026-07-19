#!/usr/bin/env python3
"""Proof: build_agent_job_store guard against silent InMemory fallback.

Audits all callers and runs guard unit tests.

Stdout on success: AGENT_JOB_STORE_FALLBACK_GUARD_PROOF_OK
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

from agent_runtime.jobs import build_agent_job_store  # noqa: E402
from agent_runtime.jobs import InMemoryAgentJobStore  # noqa: E402
from agent_runtime.validate import AgentRuntimeConfigError  # noqa: E402

PRODUCTION_CALLERS: dict[str, str] = {
    "agent_reconcile.py": "runtime_context.settings; default allow_in_memory=False",
}

TEST_PATCH_CALLERS = {
    "tests/test_signal_reconciler_agent_pr_d.py": "autouse patch build_agent_job_store -> InMemory",
    "tests/test_digital_twin_cel_radlin_dod.py": "autouse patch build_agent_job_store -> InMemory",
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
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
            if name != "build_agent_job_store":
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
    tests = Path(__file__).resolve().parents[1] / "tests" / "test_agent_job_store_fallback_guard.py"
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
            build_agent_job_store(settings)
            raise AssertionError("expected AgentRuntimeConfigError without database URL")
        except AgentRuntimeConfigError:
            pass
        store = build_agent_job_store(settings, allow_in_memory=True)
        if not isinstance(store, InMemoryAgentJobStore):
            raise AssertionError("allow_in_memory=True must return InMemoryAgentJobStore")
    return {"guard_behavior": "ok"}


def _audit_callers(sites: list[dict[str, str | int | bool]]) -> dict[str, object]:
    production_hits: list[str] = []
    explicit_in_memory: list[str] = []
    test_hits: list[str] = []
    unknown: list[str] = []

    for site in sites:
        rel = str(site["file"])
        label = f"{rel}:{site['line']}"
        if rel.endswith("agent_job_store_fallback_guard_proof.py"):
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
        if not matched and "agent_reconcile.py" in rel:
            production_hits.append(label)
            matched = True
        if not matched and rel.endswith("agent_runtime/jobs.py"):
            matched = True
        if not matched:
            unknown.append(label)

    if unknown:
        raise RuntimeError(f"unclassified build_agent_job_store callers: {unknown}")

    return {
        "call_sites_total": len(sites),
        "production_callers": len(production_hits),
        "explicit_allow_in_memory": explicit_in_memory,
        "test_callers": len(test_hits),
        "production_policy": PRODUCTION_CALLERS,
        "test_patch_policy": TEST_PATCH_CALLERS,
    }


def main() -> int:
    report: dict[str, object] = {"proof": "agent_job_store_fallback_guard", "ok": False}
    try:
        sites = _find_call_sites(TOOL_DIR)
        report["guard"] = _assert_guard_behavior()
        report["callers"] = _audit_callers(sites)
        _run_guard_unit_tests()
        report["pytest"] = "pass"
        report["ok"] = True
        print(json.dumps(report, indent=2, ensure_ascii=True))
        print("AGENT_JOB_STORE_FALLBACK_GUARD_PROOF_OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=True, indent=2), file=sys.stderr)
        print(f"AGENT_JOB_STORE_FALLBACK_GUARD_PROOF_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
