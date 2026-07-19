from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.materialize import _execute_composite_step
from agent_runtime.tools import write_executors
from case_engagement_bridge import resolve_engagement_id
from correlation_registry.store import InMemoryCorrelationRegistryStore
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2, OperationalStatus


def test_composite_plan_propagates_case_id_to_follow_up_step() -> None:
  created_case_id = "case_test_abc123"
  captured_args: list[dict] = []

  def fake_create_case(args, **kwargs):
      captured_args.append(dict(args))
      return {"status": "ok", "case_id": created_case_id, "summary": "created"}

  def fake_add_note(args, **kwargs):
      captured_args.append(dict(args))
      return {"status": "ok", "summary": "note added"}

  executors = {
      "create_case": fake_create_case,
      "add_case_note": fake_add_note,
  }

  mailbox_store = MagicMock()
  mailbox_store.upsert_case = MagicMock()

  payload = {
      "steps": [
          {
              "operation": "create_case",
              "target": "",
              "args": {"customer_email": "klient@example.com", "customer_name": "Klient"},
          },
          {
              "operation": "add_case_note",
              "target": "",
              "args": {"note": "Pierwsza notatka"},
          },
      ]
  }

  import agent_runtime.materialize as materialize_mod

  original = materialize_mod.WRITE_EXECUTORS if hasattr(materialize_mod, "WRITE_EXECUTORS") else None
  from agent_runtime.tools import write_executors

  original_map = dict(write_executors.WRITE_EXECUTORS)
  write_executors.WRITE_EXECUTORS.clear()
  write_executors.WRITE_EXECUTORS.update(executors)
  try:
      result = _execute_composite_step(payload, mailbox_store=mailbox_store)
  finally:
      write_executors.WRITE_EXECUTORS.clear()
      write_executors.WRITE_EXECUTORS.update(original_map)

  assert result["action"] == "composite_executed"
  assert result["case_id"] == created_case_id
  assert len(captured_args) == 2
  assert captured_args[1]["case_id"] == created_case_id
  assert captured_args[1]["target"] == created_case_id


def test_composite_create_case_registers_correlation_registry() -> None:
    registry = InMemoryCorrelationRegistryStore()
    registry.bootstrap()
    identity_id = registry.create_identity(email="composite@example.com")
    engagement_id = registry.resolve_or_create_engagement(identity_id=identity_id)

    mailbox_store = MagicMock()
    mailbox_store.upsert_case = MagicMock()

    snapshot = EngagementSnapshotV2(
        engagement_id=engagement_id,
        case_id="",
        version=1,
        operational_status=OperationalStatus(code="pending_operator", steps_remaining=1),
    )

    payload = {
        "steps": [
            {
                "operation": "create_case",
                "target": "",
                "args": {
                    "case_id": "case_composite_registry",
                    "customer_email": "composite@example.com",
                    "customer_name": "Composite Lead",
                },
            },
        ]
    }

    result = _execute_composite_step(
        payload,
        mailbox_store=mailbox_store,
        engagement_snapshot=snapshot,
        correlation_store=registry,
    )

    assert result["action"] == "composite_executed"
    assert result["case_id"] == "case_composite_registry"
    assert resolve_engagement_id("case_composite_registry", registry_store=registry) == engagement_id
    mailbox_store.upsert_case.assert_called_once()


# COMPOSITE_CREATE_CASE_REGISTRY_PROOF_OK
