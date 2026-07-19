from __future__ import annotations

import sys
import types
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory_store import PostgresMailboxMemoryStore, _stable_advisory_lock_key


def test_stable_advisory_lock_key_is_deterministic_signed_bigint() -> None:
    first = _stable_advisory_lock_key(scope="case_row", owner_id="case-lock-1")
    second = _stable_advisory_lock_key(scope="case_row", owner_id="case-lock-1")
    other = _stable_advisory_lock_key(scope="case_row", owner_id="case-lock-2")

    assert first == second
    assert first != other
    assert -(2**63) <= first <= (2**63) - 1


def test_postgres_upsert_case_defaults_timestamps_when_missing() -> None:
    store = PostgresMailboxMemoryStore("postgresql://example.invalid/test")
    captured: dict[str, object] = {}

    def fake_upsert(_sql: str, prepared: dict[str, object]) -> None:
        captured.update(prepared)

    store._upsert = fake_upsert  # type: ignore[method-assign]
    store.upsert_case(
        {
            "case_id": "_operator_desk",
            "case_key": "_operator_desk",
            "thread_id": "",
            "case_family": "internal_coordination",
            "mailbox": "system",
            "subject": "Operator desk (identity)",
            "status": "open",
            "customer_name": "",
            "customer_email": "",
            "metadata": {"kind": "system_desk", "role": "entity_link_escalations"},
        }
    )

    assert captured["created_at"] is not None
    assert captured["updated_at"] is not None


def test_postgres_upsert_case_defaults_required_fields_when_missing() -> None:
    store = PostgresMailboxMemoryStore("postgresql://example.invalid/test")
    captured: dict[str, object] = {}

    def fake_upsert(_sql: str, prepared: dict[str, object]) -> None:
        captured.update(prepared)

    store._upsert = fake_upsert  # type: ignore[method-assign]
    store.upsert_case({"case_id": "case_minimal"})

    assert captured["case_id"] == "case_minimal"
    assert captured["case_key"] == ""
    assert captured["thread_id"] == ""
    assert captured["case_family"] == "unknown"
    assert captured["mailbox"] == ""
    assert captured["subject"] == ""
    assert captured["status"] == "open"
    assert captured["customer_name"] == ""
    assert captured["customer_email"] == ""
    assert captured["latest_signal_id"] == ""
    assert captured["last_source_kinds_seen"] == "[]"
    assert captured["metadata"] == "{}"


def test_postgres_connect_uses_bounded_connect_timeout(monkeypatch) -> None:
    store = PostgresMailboxMemoryStore("postgresql://example.invalid/test")
    captured: dict[str, object] = {}

    def fake_connect(database_url: str, **kwargs: object) -> object:
        captured["database_url"] = database_url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=fake_connect))

    store._connect()

    assert captured["database_url"] == "postgresql://example.invalid/test"
    assert captured["kwargs"] == {"connect_timeout": 15}


def test_postgres_mutate_case_uses_one_cursor_and_preserves_requested_case_id() -> None:
    store = PostgresMailboxMemoryStore("postgresql://example.invalid/test")
    observed: dict[str, object] = {}

    class FakeCursor:
        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def execute(self, sql: str, params: dict[str, object] | None = None) -> None:
            observed.setdefault("executed", []).append((sql, params))

        def fetchone(self) -> dict[str, object]:
            return {
                "case_id": "case-original",
                "case_key": "CASE-ORIGINAL",
                "thread_id": "thr-original",
                "case_family": "mailbox_memory_test",
                "mailbox": "test@example.com",
                "subject": "Before mutate",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "latest_signal_id": "",
                "latest_signal_at": None,
                "last_rebuild_at": None,
                "last_projection_refresh_at": None,
                "last_source_kinds_seen": [],
                "metadata": {},
                "created_at": "2026-07-13T10:00:00+02:00",
                "updated_at": "2026-07-13T10:00:00+02:00",
            }

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_obj = FakeCursor()

        def __enter__(self) -> "FakeConnection":
            observed["connection_entered"] = True
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            observed["connection_exited"] = True
            return False

        def cursor(self) -> FakeCursor:
            return self.cursor_obj

        def commit(self) -> None:
            observed["committed"] = True

    fake_connection = FakeConnection()

    def fake_connect(*, row_factory: bool = False) -> FakeConnection:
        observed["row_factory"] = row_factory
        return fake_connection

    def fake_acquire_owner_lock(cur: FakeCursor, *, scope: str, owner_id: str) -> None:
        observed["lock_cursor"] = cur
        observed["lock_scope"] = scope
        observed["lock_owner_id"] = owner_id

    def fake_upsert_case_payload(payload: dict[str, object], *, cur: object | None = None) -> None:
        observed["payload"] = dict(payload)
        observed["upsert_cursor"] = cur

    store._connect = fake_connect  # type: ignore[method-assign]
    store._acquire_owner_lock = fake_acquire_owner_lock  # type: ignore[method-assign]
    store._upsert_case_payload = fake_upsert_case_payload  # type: ignore[method-assign]

    result = store.mutate_case(
        "case-original",
        lambda row: {**row, "case_id": "case-overwrite-attempt", "subject": "After mutate"},
    )

    assert observed["row_factory"] is True
    assert observed["lock_scope"] == "case_row"
    assert observed["lock_owner_id"] == "case-original"
    assert observed["lock_cursor"] is fake_connection.cursor_obj
    assert observed["upsert_cursor"] is fake_connection.cursor_obj
    assert observed["committed"] is True
    assert observed["connection_entered"] is True
    assert observed["connection_exited"] is True
    assert observed["payload"]["case_id"] == "case-original"
    assert result["case_id"] == "case-original"
    assert result["subject"] == "After mutate"
