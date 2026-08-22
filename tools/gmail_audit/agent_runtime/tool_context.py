"""Execution context passed to every agent tool (PR-C)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.constitution import AgentConstitution
from agent_runtime.settings import AgentRuntimeSettings, load_agent_runtime_settings
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2


@dataclass
class ToolExecutionContext:
    snapshot: EngagementSnapshotV2
    settings: AgentRuntimeSettings
    mailbox_store: Any | None = None
    signal_payload: dict[str, Any] = field(default_factory=dict)
    tool_usage: dict[str, int] = field(default_factory=dict)
    constitution: AgentConstitution | None = None
    #: P1.2: store-backed DecisionRevisionLedger (P1.1P) so the reference
    #: monitor can bind the envelope to the current durable CAD revision.
    decision_revision_ledger: Any | None = None

    @classmethod
    def from_snapshot(
        cls,
        snapshot: EngagementSnapshotV2,
        *,
        settings: AgentRuntimeSettings | None = None,
        mailbox_store: Any | None = None,
        signal_payload: dict[str, Any] | None = None,
        constitution: AgentConstitution | None = None,
        decision_revision_ledger: Any | None = None,
    ) -> ToolExecutionContext:
        return cls(
            snapshot=snapshot,
            settings=settings or load_agent_runtime_settings(),
            mailbox_store=mailbox_store,
            signal_payload=dict(signal_payload or {}),
            constitution=constitution,
            decision_revision_ledger=decision_revision_ledger,
        )

    def record_tool_use(self, tool_name: str) -> int:
        name = str(tool_name or "").strip()
        count = int(self.tool_usage.get(name, 0)) + 1
        self.tool_usage[name] = count
        return count

    def tool_use_count(self, tool_name: str) -> int:
        return int(self.tool_usage.get(str(tool_name or "").strip(), 0))
