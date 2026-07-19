#!/usr/bin/env python3
"""Seed one pending composite materialize proposal for CT-FU-5 Daszek proxy test."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_reconcile import build_operator_engagement_store
from agent_runtime.store import PostgresOperatorEngagementStore
from config import load_settings
from llm_contracts.engagement_snapshot_v2 import (
    AgentMemory,
    EngagementSnapshotV2,
    MaterializeProposalItem,
    OperationalStatus,
)


def main() -> int:
    # Must use load_settings() so tools/gmail_audit/.env is loaded — load_agent_runtime_settings()
    # alone reads os.environ only and silently falls back to InMemoryOperatorEngagementStore.
    settings = load_settings(require_groq=False, require_google=False)
    store = build_operator_engagement_store(settings)
    if not isinstance(store, PostgresOperatorEngagementStore):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "PostgresOperatorEngagementStore required for CT-FU-5 seed",
                    "store": type(store).__name__,
                    "hint": "Set MAILBOX_MEMORY_DATABASE_URL in tools/gmail_audit/.env",
                }
            ),
            file=sys.stderr,
        )
        return 2

    store.bootstrap()

    engagement_id = f"eng_ar_ct_fu5_{uuid.uuid4().hex[:8]}"
    proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
    case_id = f"case_{uuid.uuid4().hex[:10]}"
    snapshot = EngagementSnapshotV2(
        engagement_id=engagement_id,
        case_id="",
        version=1,
        operational_status=OperationalStatus(code="pending_operator", steps_remaining=1),
        agent_memory=AgentMemory(
            materialize_proposals=[
                MaterializeProposalItem(
                    proposal_id=proposal_id,
                    proposal_type="composite_plan",
                    status="pending",
                    payload_json={
                        "steps": [
                            {
                                "operation": "create_case",
                                "target": "",
                                "args": {
                                    "case_id": case_id,
                                    "customer_email": "ctfu5@example.com",
                                    "customer_name": "CT-FU-5 Proof",
                                },
                            }
                        ]
                    },
                )
            ]
        ),
    )
    store.insert_snapshot(snapshot)
    if store.load_snapshot(engagement_id) is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "insert_snapshot did not persist to Postgres",
                    "engagement_id": engagement_id,
                }
            ),
            file=sys.stderr,
        )
        return 3

    print(
        json.dumps(
            {
                "ok": True,
                "engagement_id": engagement_id,
                "proposal_id": proposal_id,
                "case_id_seed": case_id,
                "store": "postgres",
                "database_url_host": str(getattr(settings, "mailbox_memory_database_url", "") or "").split("@")[-1][:80],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
