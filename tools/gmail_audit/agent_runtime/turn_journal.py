"""Append-only agent turn journal (episodic memory, PR-A/B)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Mapping

from agent_runtime.tool_result import ToolCallPlan, ToolResult

TURN_JOURNAL_CONNECT_TIMEOUT = int(os.getenv("TURN_JOURNAL_CONNECT_TIMEOUT", "10"))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_turn_id(engagement_id: str, version: int, tool_name: str) -> str:
    seed = f"{engagement_id}:{version}:{tool_name}:{_utc_now_iso()}"
    return "turn_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:22]


class AgentTurnJournal(ABC):
    @abstractmethod
    def append_turn(
        self,
        *,
        engagement_id: str,
        snapshot_version: int,
        trace_id: str,
        plan: ToolCallPlan,
        result: ToolResult,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def list_turns(self, engagement_id: str, *, limit: int = 50) -> list[dict[str, Any]]: ...


class InMemoryAgentTurnJournal(AgentTurnJournal):
    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def append_turn(
        self,
        *,
        engagement_id: str,
        snapshot_version: int,
        trace_id: str,
        plan: ToolCallPlan,
        result: ToolResult,
    ) -> dict[str, Any]:
        row = {
            "turn_id": _new_turn_id(engagement_id, snapshot_version, plan.tool_name),
            "engagement_id": engagement_id,
            "snapshot_version": int(snapshot_version),
            "tool_name": plan.tool_name,
            "tool_args_redacted": _redact_args(plan.arguments),
            "plan_correlation": _plan_correlation(plan),
            "tool_status": result.status,
            "turn_summary_pl": result.turn_summary_pl,
            "tokens_used": int(result.tokens_used),
            "trace_id": str(trace_id or ""),
            "created_at": _utc_now_iso(),
        }
        self._rows.append(row)
        return row

    def list_turns(self, engagement_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = [r for r in self._rows if r.get("engagement_id") == engagement_id]
        return rows[-limit:]


class PostgresAgentTurnJournal(AgentTurnJournal):
    def __init__(self, database_url: str) -> None:
        self.database_url = str(database_url or "").strip()
        if not self.database_url:
            raise ValueError("database_url required for PostgresAgentTurnJournal")

    def append_turn(
        self,
        *,
        engagement_id: str,
        snapshot_version: int,
        trace_id: str,
        plan: ToolCallPlan,
        result: ToolResult,
    ) -> dict[str, Any]:
        turn_id = _new_turn_id(engagement_id, snapshot_version, plan.tool_name)
        row = {
            "turn_id": turn_id,
            "engagement_id": engagement_id,
            "snapshot_version": int(snapshot_version),
            "tool_name": plan.tool_name,
            "tool_args_redacted": _redact_args(plan.arguments),
            "plan_correlation": _plan_correlation(plan),
            "tool_status": result.status,
            "turn_summary_pl": result.turn_summary_pl,
            "tokens_used": int(result.tokens_used),
            "trace_id": str(trace_id or ""),
        }
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_runtime_turns (
                        turn_id, engagement_id, snapshot_version, tool_name,
                        tool_args_redacted, plan_correlation, tool_status, turn_summary_pl,
                        tokens_used, trace_id, created_at
                    ) VALUES (
                        %(turn_id)s, %(engagement_id)s, %(snapshot_version)s, %(tool_name)s,
                        %(tool_args_redacted)s::jsonb, %(plan_correlation)s::jsonb,
                        %(tool_status)s, %(turn_summary_pl)s,
                        %(tokens_used)s, %(trace_id)s, NOW()
                    )
                    """,
                    {
                        **row,
                        "tool_args_redacted": json.dumps(row["tool_args_redacted"], ensure_ascii=False),
                        "plan_correlation": json.dumps(row["plan_correlation"], ensure_ascii=False),
                    },
                )
            conn.commit()
        row["created_at"] = _utc_now_iso()
        return row

    def list_turns(self, engagement_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect(row_factory=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM agent_runtime_turns
                    WHERE engagement_id = %(engagement_id)s
                    ORDER BY created_at ASC
                    LIMIT %(limit)s
                    """,
                    {"engagement_id": engagement_id, "limit": int(limit)},
                )
                rows = cur.fetchall()
        return [dict(r) for r in rows]

    def _connect(self, *, row_factory: bool = False):
        try:
            import psycopg  # type: ignore[import-not-found]
            from psycopg.rows import dict_row  # type: ignore[import-not-found]
        except ImportError:
            raise RuntimeError(
                "psycopg is required for PostgresAgentTurnJournal. "
                "Install it with: pip install psycopg[binary]"
            ) from None

        kwargs: dict[str, Any] = {"connect_timeout": TURN_JOURNAL_CONNECT_TIMEOUT}
        if row_factory:
            kwargs["row_factory"] = dict_row
        return psycopg.connect(self.database_url, **kwargs)


def _redact_args(arguments: Mapping[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in dict(arguments).items():
        lowered = str(key).lower()
        if any(token in lowered for token in ("secret", "password", "token", "api_key")):
            redacted[key] = "[redacted]"
        else:
            redacted[key] = value
    return redacted


def _plan_correlation(plan: ToolCallPlan) -> dict[str, str]:
    return {
        "policy_decision_id": str(plan.policy_decision_id or ""),
        "action_proposal_id": str(plan.action_proposal_id or ""),
        "status": str(plan.correlation_status or ""),
    }
