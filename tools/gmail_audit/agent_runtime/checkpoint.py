"""AgentRun checkpoints — LangGraph pattern native (RFC agent-run-checkpoint-v1)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2

POSTGRES_CONNECT_TIMEOUT_SEC = 10

CHECKPOINT_DDL = """
CREATE TABLE IF NOT EXISTS agent_run_checkpoints (
    run_id TEXT NOT NULL,
    engagement_id TEXT NOT NULL,
    turn_idx INT NOT NULL,
    snapshot_json JSONB NOT NULL,
    planner_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'running',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, turn_idx)
);
CREATE INDEX IF NOT EXISTS idx_agent_run_checkpoints_engagement
    ON agent_run_checkpoints(engagement_id, created_at DESC);
"""


@dataclass(frozen=True)
class AgentRunCheckpoint:
    run_id: str
    engagement_id: str
    turn_idx: int
    snapshot: EngagementSnapshotV2
    planner_state: dict[str, Any]
    status: str


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


class AgentRunCheckpointStore:
    def __init__(self, database_url: str) -> None:
        self._url = str(database_url or "").strip()
        if not self._url:
            raise ValueError("database_url required for AgentRunCheckpointStore")

    def ensure_schema(self) -> None:
        import psycopg

        with psycopg.connect(self._url, connect_timeout=POSTGRES_CONNECT_TIMEOUT_SEC) as conn:
            with conn.cursor() as cur:
                cur.execute(CHECKPOINT_DDL)
            conn.commit()

    def save_checkpoint(
        self,
        *,
        run_id: str,
        engagement_id: str,
        turn_idx: int,
        snapshot: EngagementSnapshotV2,
        planner_state: dict[str, Any] | None = None,
        status: str = "running",
    ) -> None:
        if snapshot is None:
            return
        import psycopg

        with psycopg.connect(self._url, connect_timeout=POSTGRES_CONNECT_TIMEOUT_SEC) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_run_checkpoints
                        (run_id, engagement_id, turn_idx, snapshot_json, planner_state, status)
                    VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s)
                    ON CONFLICT (run_id, turn_idx) DO UPDATE SET
                        snapshot_json = EXCLUDED.snapshot_json,
                        planner_state = EXCLUDED.planner_state,
                        status = EXCLUDED.status
                    """,
                    (
                        run_id,
                        engagement_id,
                        int(turn_idx),
                        json.dumps(snapshot.model_dump(mode="python")),
                        json.dumps(dict(planner_state or {})),
                        status,
                    ),
                )
            conn.commit()

    def load_latest(self, run_id: str) -> AgentRunCheckpoint | None:
        import psycopg

        with psycopg.connect(self._url, connect_timeout=POSTGRES_CONNECT_TIMEOUT_SEC) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT engagement_id, turn_idx, snapshot_json, planner_state, status
                    FROM agent_run_checkpoints
                    WHERE run_id = %s
                    ORDER BY turn_idx DESC
                    LIMIT 1
                    """,
                    (run_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        engagement_id, turn_idx, snapshot_json, planner_state, status = row
        snap = EngagementSnapshotV2.model_validate(snapshot_json)
        return AgentRunCheckpoint(
            run_id=run_id,
            engagement_id=str(engagement_id),
            turn_idx=int(turn_idx),
            snapshot=snap,
            planner_state=dict(planner_state or {}),
            status=str(status or "running"),
        )


class InMemoryAgentRunCheckpointStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, int], dict[str, Any]] = {}

    def ensure_schema(self) -> None:
        return None

    def save_checkpoint(
        self,
        *,
        run_id: str,
        engagement_id: str,
        turn_idx: int,
        snapshot: EngagementSnapshotV2,
        planner_state: dict[str, Any] | None = None,
        status: str = "running",
    ) -> None:
        self._rows[(run_id, int(turn_idx))] = {
            "engagement_id": engagement_id,
            "snapshot": snapshot,
            "planner_state": dict(planner_state or {}),
            "status": status,
        }

    def load_latest(self, run_id: str) -> AgentRunCheckpoint | None:
        matches = [(idx, row) for (rid, idx), row in self._rows.items() if rid == run_id]
        if not matches:
            return None
        turn_idx, row = max(matches, key=lambda x: x[0])
        return AgentRunCheckpoint(
            run_id=run_id,
            engagement_id=str(row["engagement_id"]),
            turn_idx=int(turn_idx),
            snapshot=row["snapshot"],
            planner_state=dict(row.get("planner_state") or {}),
            status=str(row.get("status") or "running"),
        )


__all__ = [
    "AgentRunCheckpoint",
    "AgentRunCheckpointStore",
    "CHECKPOINT_DDL",
    "InMemoryAgentRunCheckpointStore",
    "new_run_id",
]
