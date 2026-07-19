"""Tests for operator_memory module."""
from __future__ import annotations

import unittest
from typing import Any


class FakeCursor:
    def __init__(self):
        self._rows: list[Any] = []
        self._idx = 0

    def execute(self, sql: str, params: tuple | None = None) -> None:
        return None

    def fetchone(self) -> Any:
        if self._idx < len(self._rows):
            r = self._rows[self._idx]
            self._idx += 1
            return r
        return None

    def fetchall(self) -> list[Any]:
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TestOperatorMemory(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConn()

    def test_ensure_table_runs(self):
        from operator_memory import ensure_operator_memory_table
        ensure_operator_memory_table(self.conn)
        self.assertTrue(True)

    def test_save_conversation_turn_returns_id(self):
        from operator_memory import save_conversation_turn
        mid = save_conversation_turn(
            self.conn, session_id="s1",
            user_input="test", agent_response="ok",
            operator_id="op1",
        )
        self.assertTrue(mid.startswith("opmem_"))

    def test_save_preference(self):
        from operator_memory import save_preference
        result = save_preference(self.conn, key="language", value="pl", operator_id="op1")
        self.assertTrue(result)

    def test_save_client_context(self):
        from operator_memory import save_client_context
        mid = save_client_context(self.conn, client_name="Kowalski", note="Wazny klient", operator_id="op1")
        self.assertTrue(mid.startswith("opmem_"))


if __name__ == "__main__":
    unittest.main()
