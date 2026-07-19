#!/usr/bin/env python3
"""Proof: agent run turn journal guard against silent InMemory fallback.

Audits build_turn_journal / execute_agent_run callers and runs guard unit tests.

Stdout on success: AGENT_RUN_TURN_JOURNAL_FALLBACK_GUARD_PROOF_OK
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.run import build_turn_journal  # noqa: E402
from agent_runtime.settings import AgentRuntimeSettings  # noqa: E402
from agent_runtime.turn_journal import InMemoryAgentTurnJournal  # noqa: E402
from agent_runtime.validate import AgentRuntimeConfigError  # noqa: E402

PRODUCTION_CALLERS: dict[str, str] = {
    "run.py": "execute_agent_run -> build_turn_journal when enabled; no silent InMemory",
    "mcp_service.py": "from_env build_turn_journal; trigger passes self.turn_journal; get_agent_turns errors if None",
    "event_spine/timeline.py": "PostgresAgentTurnJournal(db_url) direct",
}

TEST_PATCH_CALLERS = {
    "tests/test_agent_pr_c_complete.py": "execute_agent_run with explicit turn_journal",
    "tests/test_signal_reconciler_agent_pr_d.py": "patch execute_agent_run",
    "tests/test_digital_twin_cel_radlin_dod.py": "patch execute_agent_run",
    "agent_runtime/mcp_service.py": "evaluate_agent_mcp_smoke passes explicit InMemory journal",
}


def _find_call_sites(root: Path) -> list[dict[str, str | int | bool]]:
    sites: list[dict[str, str | int | bool]] = []
    skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
    targets = {"build_turn_journal", "execute_agent_run"}
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
            for kw in node.keywords:
                if kw.arg == "allow_in_memory" and isinstance(kw.value, ast.Constant):
                    allow_in_memory = bool(kw.value.value)
            sites.append(
                {
                    "file": rel,
                    "line": int(getattr(node, "lineno", 0)),
                    "fn": name,
                    "allow_in_memory": allow_in_memory,
                    "is_test": rel.startswith("tests/"),
                }
            )
    return sites


def _run_guard_unit_tests() -> None:
    tests = Path(__file__).resolve().parents[1] / "tests" / "test_agent_run_turn_journal_fallback_guard.py"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(tests), "-q"],
        cwd=str(TOOL_DIR),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pytest failed:\n{proc.stdout}\n{proc.stderr}")


def _assert_guard_behavior() -> dict[str, object]:
    settings = AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=3,
        mailbox_database_url="",
    )
    with __import__("unittest.mock", fromlist=["patch"]).patch.dict("os.environ", {}, clear=True):
        try:
            build_turn_journal(settings)
            raise AssertionError("expected AgentRuntimeConfigError without database URL")
        except AgentRuntimeConfigError:
            pass
        journal = build_turn_journal(settings, allow_in_memory=True)
        if not isinstance(journal, InMemoryAgentTurnJournal):
            raise AssertionError("allow_in_memory=True must return InMemoryAgentTurnJournal")
    return {"guard_behavior": "ok"}


def _audit_callers(sites: list[dict[str, str | int | bool]]) -> dict[str, object]:
    production_hits: list[str] = []
    explicit_in_memory: list[str] = []
    test_hits: list[str] = []
    unknown: list[str] = []

    for site in sites:
        rel = str(site["file"])
        label = f"{rel}:{site['line']}:{site['fn']}"
        if rel.endswith("agent_run_turn_journal_fallback_guard_proof.py"):
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
        if not matched and rel.endswith("agent_runtime/run.py") and site["fn"] == "build_turn_journal":
            matched = True
        if not matched and "agent_reconcile.py" in rel:
            production_hits.append(label)
            matched = True
        if not matched:
            unknown.append(label)

    if unknown:
        raise RuntimeError(f"unclassified agent run turn journal callers: {unknown}")

    return {
        "call_sites_total": len(sites),
        "production_callers": len(production_hits),
        "explicit_allow_in_memory": explicit_in_memory,
        "test_callers": len(test_hits),
        "production_policy": PRODUCTION_CALLERS,
        "test_patch_policy": TEST_PATCH_CALLERS,
        "removed_patterns": ["agent_turn_journal_in_memory_only", "AgentMcpService._journal silent InMemory"],
    }


def main() -> int:
    report: dict[str, object] = {"proof": "agent_run_turn_journal_fallback_guard", "ok": False}
    try:
        sites = _find_call_sites(TOOL_DIR)
        report["guard"] = _assert_guard_behavior()
        report["callers"] = _audit_callers(sites)
        _run_guard_unit_tests()
        report["pytest"] = "pass"
        report["ok"] = True
        print(json.dumps(report, indent=2, ensure_ascii=True))
        print("AGENT_RUN_TURN_JOURNAL_FALLBACK_GUARD_PROOF_OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=True, indent=2), file=sys.stderr)
        print(f"AGENT_RUN_TURN_JOURNAL_FALLBACK_GUARD_PROOF_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
