from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.mcp_service import (
    MCP_TOOL_NAMES,
    evaluate_agent_mcp_smoke,
    mcp_tool_catalog,
)
from agent_runtime.validate import build_agent_doctor_check
from agent_runtime.settings import AgentRuntimeSettings


def test_mcp_tool_catalog_matches_names() -> None:
    catalog = mcp_tool_catalog()
    assert {row["name"] for row in catalog} == set(MCP_TOOL_NAMES)
    fixture = TOOL_DIR / "agent_runtime" / "fixtures" / "mcp_tool_catalog.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert {t["name"] for t in data["tools"]} == set(MCP_TOOL_NAMES)


def test_evaluate_agent_mcp_smoke_all_checks_pass() -> None:
    report = evaluate_agent_mcp_smoke()
    assert report["ok"] is True
    assert report["passed"] == report["total"]
    assert report["total"] >= 10


def test_agent_mcp_smoke_gate_script_exit_zero() -> None:
    script = TOOL_DIR / "scripts" / "agent_mcp_smoke_gate.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(TOOL_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True


def test_doctor_nested_mcp_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_MODE", "prep")
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-test")
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
    )
    check = build_agent_doctor_check(settings)
    assert "mcp" in check
    assert check["mcp"]["id"] == "agent_runtime_mcp"
    assert len(check["mcp"]["tools"]) == 5


def test_gmail_intake_has_agent_mcp_serve_command() -> None:
    from gmail_intake import build_parser

    parser = build_parser()
    args = parser.parse_args(["agent-mcp-serve"])
    assert args.command == "agent-mcp-serve"
