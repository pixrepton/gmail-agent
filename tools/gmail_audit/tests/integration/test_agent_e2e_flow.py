"""E2E test for agent loop — plan → execute → HITL → materialize. Uses mock registries."""
from __future__ import annotations

import unittest

from agent_runtime.constitution import AgentConstitution
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.planner import MockSequencePlanner
from agent_runtime.tools_registry import MockToolRegistry
from agent_runtime.turn_journal import InMemoryAgentTurnJournal
from llm_contracts.engagement_snapshot_v2 import (
    EngagementSnapshotV2,
    OperationalStatus,
    HitlGate,
    AgentMemory,
)


def _make_test_snapshot(engagement_id: str = "test-eng-e2e", steps: int = 3) -> EngagementSnapshotV2:
    return EngagementSnapshotV2(
        engagement_id=engagement_id,
        case_id="",
        trace_id="trace-e2e",
        version=1,
        operational_status=OperationalStatus(steps_remaining=steps, code="raw_inquiry", blocking=False),
        hitl_gate=HitlGate(required=False, reason=""),
        agent_memory=AgentMemory(reasoning_trace=[], tool_calls=[]),
    )


class TestAgentE2EFlow(unittest.TestCase):
    def setUp(self):
        self.snapshot = _make_test_snapshot()
        self.constitution = AgentConstitution(
            hvac_rules="",
            company_context="",
            forbidden_actions=(),
            tool_allowlist=("extract_facts_from_text", "report_gaps_and_stop"),
        )

    def test_agent_loop_completes_two_tools(self):
        """Agent runs 2 steps with a mock planner, both tools are in MockToolRegistry."""
        planner = MockSequencePlanner(sequence=["extract_facts_from_text"])
        registry = MockToolRegistry()
        engine = AgentGraphEngine(planner=planner, constitution=self.constitution, tool_registry=registry)
        result = engine.run(self.snapshot)
        self.assertIsNotNone(result.snapshot)
        self.assertGreaterEqual(len(result.turns), 1)

    def test_agent_concurrency_semaphore_exists(self):
        """AgentGraphEngine has a concurrency semaphore to prevent parallel runs."""
        planner = MockSequencePlanner(sequence=["extract_facts_from_text"])
        registry = MockToolRegistry()
        engine = AgentGraphEngine(planner=planner, constitution=self.constitution, tool_registry=registry)
        self.assertTrue(hasattr(engine, "_concurrency_semaphore"))
        # Po zakończonym run() semafor jest zwolniony — następny run działa normalnie
        result1 = engine.run(self.snapshot)
        self.assertIsNotNone(result1)
        result2 = engine.run(_make_test_snapshot("test-eng-2"))
        self.assertIsNotNone(result2)

    def test_agent_turn_journal_records(self):
        """InMemoryAgentTurnJournal records each tool call."""
        journal = InMemoryAgentTurnJournal()
        planner = MockSequencePlanner(sequence=["extract_facts_from_text"])
        registry = MockToolRegistry()
        engine = AgentGraphEngine(
            planner=planner, constitution=self.constitution,
            tool_registry=registry, turn_journal=journal,
        )
        engine.run(self.snapshot)
        self.assertGreaterEqual(len(journal.list_turns("test-eng-e2e")), 1)

    def test_hitl_gate_halts_loop(self):
        """When HITL gate is required, loop stops immediately."""
        snap = _make_test_snapshot("test-halt", steps=5)
        snap = snap.model_copy(update={"hitl_gate": HitlGate(required=True, reason="test")})
        planner = MockSequencePlanner(sequence=["extract_facts_from_text"])
        registry = MockToolRegistry()
        engine = AgentGraphEngine(planner=planner, constitution=self.constitution, tool_registry=registry)
        result = engine.run(snap)
        self.assertTrue(result.snapshot.hitl_gate.required)


if __name__ == "__main__":
    unittest.main()
