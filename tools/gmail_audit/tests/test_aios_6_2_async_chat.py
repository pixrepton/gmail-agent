"""AI-OS 6.2 — async agent chat job queue."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_chat_jobs import enqueue_agent_chat_job, fetch_agent_chat_job


class _FakeCursor:
    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.last_sql = ""

    def execute(self, sql, params=None):
        self.last_sql = str(sql)
        if "INSERT INTO agent_chat_jobs" in sql:
            self.rows.append(params)
        return self

    def fetchone(self):
        if self.rows:
            p = self.rows[0]
            return (p[0], p[1], p[2], p[3], "queued", p[5], {}, {}, "", None, None, None, None)
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _FakeConn:
    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        pass


def test_enqueue_and_fetch_agent_chat_job() -> None:
    conn = _FakeConn()
    with patch("agent_runtime.agent_chat_jobs.ensure_agent_chat_jobs_table"):
        job_id = enqueue_agent_chat_job(
            conn,
            command_id="cmd_1",
            session_id="sess_1",
            case_id="case_1",
            request={"user_input": "hello"},
        )
    assert job_id.startswith("chatjob_")
    conn.cursor_obj.rows = [
        (
            job_id,
            "cmd_1",
            "sess_1",
            "case_1",
            "queued",
            '{"user_input": "hello"}',
        )
    ]
    job = fetch_agent_chat_job(conn, job_id)
    assert job is not None
    assert job["command_id"] == "cmd_1"
