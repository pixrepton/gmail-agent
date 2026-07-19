"""Real-Postgres regression test for the operator-action connection-ownership fix.

Reproduces the exact failure mode found during the Phase 0 D1 live-API proof:
`POST /cases/{case_id}/operator-action` performed a real, durable mutation (an
`operator_response_records` row was inserted) but the route then raised
`psycopg.OperationalError: the connection is closed` and returned HTTP 500, because
`record_operator_response()` wrapped its work in `with conn:`, which closes the connection
on block exit in this psycopg version -- even though the caller (`process_operator_action`,
called from the route handler's own `with conn: ... conn.commit()` block) still needed that
same connection for the subsequent parent-observation-count query and for
`maybe_create_learning_candidate()`.

The in-memory fake store used by the rest of the D1 test suite never shares one real psycopg
connection across nested helper calls, so it cannot catch this class of bug -- hence a real
Postgres test that exercises the actual call chain with one shared connection.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import psycopg

from divergence_loop import process_operator_action

POSTGRES_TEST_DATABASE_URL = os.getenv("MAILBOX_MEMORY_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL, reason="MAILBOX_MEMORY_TEST_DATABASE_URL is not set"
)


def _seed_open_proposal(conn, unique: str) -> tuple[str, str]:
    case_id = f"opact_case_{unique}"
    proposal_id = f"opact_prop_{unique}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_proposal_records "
            "(proposal_id, engagement_id, case_id, created_at, proposal_type, "
            "proposal_content_json, proposal_reasoning_pl, source_pipeline) "
            "VALUES (%s,%s,%s, now(), %s, %s::jsonb, %s, %s)",
            (proposal_id, f"opact_eng_{unique}", case_id, "some_other_type", "{}", "regression", "regression"),
        )
    conn.commit()
    return case_id, proposal_id


def _cleanup(conn, case_id: str, proposal_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM operator_response_records WHERE proposal_id=%s", (proposal_id,))
        cur.execute("DELETE FROM agent_proposal_records WHERE proposal_id=%s", (proposal_id,))
    conn.commit()


def test_process_operator_action_completes_without_closing_callers_connection() -> None:
    conn = psycopg.connect(POSTGRES_TEST_DATABASE_URL)
    unique = uuid.uuid4().hex[:10]
    case_id, proposal_id = _seed_open_proposal(conn, unique)
    try:
        # RED (pre-fix): record_operator_response()'s `with conn:` closed the connection: the
        # durable operator_response_records insert had already committed by that point, but the
        # very next statement in process_operator_action (the parent-observation-count query,
        # then maybe_create_learning_candidate) raised psycopg.OperationalError on the closed
        # connection, which propagated out of the route handler as an unhandled 500 -- durable
        # mutation, reported failure.
        results = process_operator_action(
            conn,
            case_id=case_id,
            case_family="regression_family",
            operator_action_type="different_action",
        )

        assert not conn.closed, "process_operator_action must not close a connection it does not own"

        assert len(results) == 1
        assert results[0]["response_type"] == "DIVERGENT_ACTION"
        assert results[0]["proposal_id"] == proposal_id

        # Caller (the route handler) must still be able to commit and use the connection
        # afterward, exactly as api_app.py's `with conn: ... conn.commit()` does.
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT response_type FROM operator_response_records WHERE proposal_id=%s",
                (proposal_id,),
            )
            row = cur.fetchone()
        assert row is not None, "operator_response_records row must be durably persisted"
        assert row[0] == "DIVERGENT_ACTION"
    finally:
        if conn.closed:
            conn = psycopg.connect(POSTGRES_TEST_DATABASE_URL)
        _cleanup(conn, case_id, proposal_id)
        conn.close()
