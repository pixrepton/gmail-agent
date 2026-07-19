#!/usr/bin/env python3
"""PR-G: In-process MCP smoke gate (all tools, no OpenAI/Postgres). Exit 0 = PASS."""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.mcp_service import evaluate_agent_mcp_smoke


def main() -> int:
    report = evaluate_agent_mcp_smoke()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
