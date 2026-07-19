"""
Shim for backward compatibility — re-exports from split `mailbox_memory/` package.

Original monolithic file (2546L) split during Enterprise Quality Sprint into:
  mailbox_memory/protocol.py  — MailboxMemoryStore(Protocol)
  mailbox_memory/schema.py    — DDL + helper functions
  mailbox_memory/inmemory.py  — InMemoryMailboxMemoryStore
  mailbox_memory/postgres.py  — PostgresMailboxMemoryStore
"""
from __future__ import annotations

from mailbox_memory.inmemory import InMemoryMailboxMemoryStore  # noqa: PLC0415, F401
from mailbox_memory.postgres import PostgresMailboxMemoryStore  # noqa: PLC0415, F401
from mailbox_memory.protocol import MailboxMemoryStore  # noqa: PLC0415, F401
from mailbox_memory.schema import (  # noqa: PLC0415, F401
    MAILBOX_MEMORY_SCHEMA_SQL,
    _case_payload_with_defaults,
    _coerce_iso,
    _cosine_similarity,
    _json_dump,
    _parse_vector_literal_coords,
    _stable_advisory_lock_key,
    _vector_literal,
    build_mailbox_memory_vector_schema_sql,
)

__all__ = [
    "InMemoryMailboxMemoryStore",
    "MAILBOX_MEMORY_SCHEMA_SQL",
    "MailboxMemoryStore",
    "PostgresMailboxMemoryStore",
]
