#!/usr/bin/env python3
"""stdio MCP server for agent runtime operator tools (PR-G)."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from agent_runtime.mcp_service import (
    MCP_TOOL_NAMES,
    AgentMcpService,
    dispatch_mcp_tool,
)

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Install MCP SDK: pip install 'mcp>=1.6.0,<2.0.0' (from gmail_audit requirements.txt)"
    ) from exc


def _tool_schemas() -> list[Tool]:
    return [
        Tool(
            name="get_engagement_snapshot",
            description="Read EngagementSnapshot.v2 summary by engagement_id or case_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "engagement_id": {"type": "string"},
                    "case_id": {"type": "string"},
                    "include_full": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include full EngagementSnapshot.v2 JSON (bounded use)",
                    },
                },
            },
        ),
        Tool(
            name="list_active_engagements",
            description="List recent operator engagements with optional status / blocking-gap / HITL filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter operational_status.code (e.g. pending_operator)",
                    },
                    "blocking_gaps_only": {"type": "boolean", "default": False},
                    "hitl_required_only": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
                },
            },
        ),
        Tool(
            name="trigger_agent_run",
            description="Manually run one AgentGraph pass (debug/replay). Requires agent enabled unless allow_when_disabled.",
            inputSchema={
                "type": "object",
                "properties": {
                    "engagement_id": {"type": "string"},
                    "signal": {"type": "object", "description": "Optional signal payload for ToolExecutionContext"},
                    "allow_when_disabled": {"type": "boolean", "default": False},
                },
                "required": ["engagement_id"],
            },
        ),
        Tool(
            name="approve_hitl_action",
            description="Clear HITL gate and enable a pending action on the engagement snapshot (operator SoT).",
            inputSchema={
                "type": "object",
                "properties": {
                    "engagement_id": {"type": "string"},
                    "action_id": {"type": "string"},
                    "operator_id": {"type": "string"},
                },
                "required": ["engagement_id", "action_id"],
            },
        ),
        Tool(
            name="get_agent_turns",
            description="List episodic agent turns from the turn journal for an engagement.",
            inputSchema={
                "type": "object",
                "properties": {
                    "engagement_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                },
                "required": ["engagement_id"],
            },
        ),
    ]


class AgentRuntimeMcpServer:
    def __init__(self, service: AgentMcpService | None = None) -> None:
        self._service = service or AgentMcpService.from_env()
        self.server = Server("gmail-agent-runtime")
        self._register()

    def _register(self) -> None:
        service = self._service

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            names = {t.name for t in _tool_schemas()}
            assert names == set(MCP_TOOL_NAMES)
            return _tool_schemas()

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
            try:
                payload = dispatch_mcp_tool(service, name, arguments or {})
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                return [TextContent(type="text", text=text)]
            except Exception as exc:
                err = {"ok": False, "error": str(exc), "tool": name}
                return [TextContent(type="text", text=json.dumps(err, ensure_ascii=False, indent=2))]

    async def run(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(),
            )


async def run_stdio_server(service: AgentMcpService | None = None) -> None:
    server = AgentRuntimeMcpServer(service=service)
    await server.run()


def main() -> None:
    asyncio.run(run_stdio_server())


if __name__ == "__main__":
    main()
