"""Task rows from agent actions (thin feed PR-E)."""

from __future__ import annotations

from typing import Any

from daszek_v3_operational_feed_contract import strip_forbidden_nested
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2


def snapshot_to_feed_tasks(snapshot: EngagementSnapshotV2) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for action in snapshot.actions:
        if not action.enabled:
            continue
        tasks.append(
            strip_forbidden_nested(
                {
                    "task_id": f"task-{snapshot.engagement_id}-{action.id}",
                    "title": f"Akcja: {action.id}",
                    "summary": str(action.payload_pl or action.disabled_reason_pl or "")[:500],
                    "linked_case_id": snapshot.case_id,
                    "source_type": "agent_action",
                    "status": "pending_approval" if snapshot.hitl_gate.required else "open",
                    "requires_approval": snapshot.hitl_gate.required,
                }
            )
        )
    return tasks
