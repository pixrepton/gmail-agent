"""Stateful agent runtime (PR-A/B: store, constitution, graph)."""

from agent_runtime.bootstrap import bootstrap_agent_runtime
from agent_runtime.constitution import AgentConstitution, load_constitution, load_live
from agent_runtime.agent_reconcile import (
    agent_runtime_reconcile_active,
    build_agent_reconcile_result,
    legacy_downstream_reconcile_active,
    resolve_case_id_for_agent,
    run_agent_reconcile,
)
from agent_runtime.digital_twin_dod import (
    DigitalTwinDodReport,
    assert_digital_twin_dod,
    build_digital_twin_doctor_check,
    evaluate_digital_twin_dod,
)
from agent_runtime.manifest import attach_agent_runtime_manifest, build_agent_runtime_manifest_slice
from agent_runtime.primary_cutover import (
    agent_runtime_primary_active,
    build_primary_cutover_doctor_check,
    validate_primary_cutover_settings,
)
from agent_runtime.engagement_resolver import EngagementResolution, resolve_engagement_for_case
from agent_runtime.feed_projection import build_v2_projection_from_engagement
from agent_runtime.turn_journal import AgentTurnJournal, InMemoryAgentTurnJournal, PostgresAgentTurnJournal
from agent_runtime.graph import AgentGraphEngine, AgentGraphRunResult
from agent_runtime.run import AgentRunResult, build_turn_journal, execute_agent_run, load_run_constitution
from agent_runtime.mcp_service import (
    AgentMcpService,
    MCP_TOOL_NAMES,
    build_agent_mcp_doctor_check,
    dispatch_mcp_tool,
    evaluate_agent_mcp_smoke,
    mcp_tool_catalog,
)
from agent_runtime.validate import AgentRuntimeConfigError, build_agent_doctor_check, validate_agent_runtime_settings
from agent_runtime.settings import AgentRuntimeSettings, load_agent_runtime_settings
from agent_runtime.store import (
    AgentConcurrencyError,
    InMemoryOperatorEngagementStore,
    OperatorEngagementStore,
    PostgresOperatorEngagementStore,
)
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from agent_runtime.tools_registry import AgentToolRegistry, MockToolRegistry

__all__ = [
    "AgentConcurrencyError",
    "AgentConstitution",
    "AgentGraphEngine",
    "AgentGraphRunResult",
    "AgentMcpService",
    "AgentRunResult",
    "MCP_TOOL_NAMES",
    "AgentRuntimeConfigError",
    "AgentRuntimeSettings",
    "AgentToolRegistry",
    "AgentTurnJournal",
    "EngagementResolution",
    "InMemoryAgentTurnJournal",
    "InMemoryOperatorEngagementStore",
    "MockToolRegistry",
    "OperatorEngagementStore",
    "PostgresAgentTurnJournal",
    "PostgresOperatorEngagementStore",
    "ToolCallPlan",
    "ToolResult",
    "DigitalTwinDodReport",
    "agent_runtime_primary_active",
    "agent_runtime_reconcile_active",
    "assert_digital_twin_dod",
    "attach_agent_runtime_manifest",
    "build_agent_runtime_manifest_slice",
    "build_digital_twin_doctor_check",
    "build_primary_cutover_doctor_check",
    "evaluate_digital_twin_dod",
    "bootstrap_agent_runtime",
    "build_agent_doctor_check",
    "build_agent_mcp_doctor_check",
    "dispatch_mcp_tool",
    "evaluate_agent_mcp_smoke",
    "mcp_tool_catalog",
    "build_agent_reconcile_result",
    "build_v2_projection_from_engagement",
    "build_turn_journal",
    "legacy_downstream_reconcile_active",
    "resolve_case_id_for_agent",
    "execute_agent_run",
    "load_run_constitution",
    "validate_agent_runtime_settings",
    "validate_primary_cutover_settings",
    "load_agent_runtime_settings",
    "load_constitution",
    "load_live",
    "resolve_engagement_for_case",
    "run_agent_reconcile",
]
