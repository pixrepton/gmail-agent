"""Mailbox memory persistence layer — split into sub-modules.

Module structure:
  protocol.py   — MailboxMemoryStore(Protocol) interface
  schema.py     — DDL + helper functions
  inmemory.py   — InMemoryMailboxMemoryStore
  postgres.py   — PostgresMailboxMemoryStore
"""
from .active_facts import fetch_current_facts_for_case, is_live_fact  # noqa: PLC0415, F401
from .inmemory import InMemoryMailboxMemoryStore  # noqa: PLC0415, F401
from .postgres import PostgresMailboxMemoryStore  # noqa: PLC0415, F401
from .protocol import MailboxMemoryStore  # noqa: PLC0415, F401
from .schema import (  # noqa: PLC0415, F401
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
    "fetch_current_facts_for_case",
    "is_live_fact",
]
