"""P1.5: durable Postgres fact consolidation round-trip (restart-safe)."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

POSTGRES_TEST_DATABASE_URL = os.getenv("MAILBOX_MEMORY_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="MAILBOX_MEMORY_TEST_DATABASE_URL is not set",
)

from mailbox_memory import PostgresMailboxMemoryStore  # noqa: E402
from mailbox_memory_runtime import split_conflicting_facts  # noqa: E402


def _mail_fact(*, case_id: str, fact_key: str, value: str, message_id: str, observed_at: str) -> dict:
    suffix = uuid.uuid5(uuid.NAMESPACE_URL, case_id).hex[:8]
    return {
        "fact_id": f"pg_mail_{suffix}_{message_id}_{fact_key}",
        "case_id": case_id,
        "message_id": message_id,
        "document_id": "",
        "entity_scope": "customer",
        "fact_key": fact_key,
        "normalized_value": value,
        "raw_value": value,
        "confidence": 0.8,
        "observed_at": observed_at,
        "source_type": "gmail_message",
        "source_ref": message_id,
        "status": "active",
        "metadata": {"source_origin": "CUSTOMER_EMAIL", "evidence_authority": "CUSTOMER_STATEMENT", "instruction_authority": "NONE"},
    }


def _doc_fact(*, case_id: str, fact_key: str, value: str, document_id: str, observed_at: str) -> dict:
    suffix = uuid.uuid5(uuid.NAMESPACE_URL, case_id).hex[:8]
    return {
        "fact_id": f"pg_doc_{suffix}_{document_id}_{fact_key}",
        "case_id": case_id,
        "message_id": "",
        "document_id": document_id,
        "entity_scope": "document",
        "fact_key": fact_key,
        "normalized_value": value,
        "raw_value": value,
        "confidence": 0.9,
        "observed_at": observed_at,
        "source_type": "structured_document_parse",
        "source_ref": f"structured_parse:docling:{document_id}:1",
        "status": "active",
        "metadata": {
            "source_origin": "ATTACHMENT",
            "evidence_authority": "CUSTOMER_DOCUMENT",
            "instruction_authority": "NONE",
            "evidence_ref": {"source_type": "document", "source_id": document_id, "page": 1},
        },
    }


def _unique_case(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _fresh_store(case_id: str) -> PostgresMailboxMemoryStore:
    store = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
    store.upsert_case({"case_id": case_id, "status": "open"})
    return store


def test_postgres_same_value_consolidation_survives_restart() -> None:
    case_id = _unique_case("case_pg_consol")
    store = _fresh_store(case_id)
    store.append_facts_with_supersession(
        [_mail_fact(case_id=case_id, fact_key="device_model", value="WH-XYZ", message_id="m1", observed_at="2026-08-23T10:00:00Z")]
    )
    store.append_facts_with_supersession(
        [_doc_fact(case_id=case_id, fact_key="device_model", value="WH-XYZ", document_id="doc1", observed_at="2026-08-23T11:00:00Z")]
    )
    # Restart: a brand-new store instance rebuilds from Postgres.
    store2 = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
    active, conflicts = split_conflicting_facts(store2.fetch_facts_for_case(case_id))
    values = {str(f.get("normalized_value")) for f in active if f.get("fact_key") == "device_model"}
    assert values == {"WH-XYZ"}
    assert not conflicts
    rows = [f for f in store2.fetch_facts_for_case(case_id) if f.get("fact_key") == "device_model"]
    origins = {str((f.get("metadata") or {}).get("source_origin")) for f in rows}
    assert origins == {"CUSTOMER_EMAIL", "ATTACHMENT"}
def test_postgres_conflict_and_supersession_survive_restart() -> None:
    case_id = _unique_case("case_pg_conf")
    store = _fresh_store(case_id)
    store.append_facts_with_supersession(
        [_mail_fact(case_id=case_id, fact_key="device_model", value="WH-XYZ", message_id="m1", observed_at="2026-08-23T10:00:00Z")]
    )
    store.append_facts_with_supersession(
        [_doc_fact(case_id=case_id, fact_key="device_model", value="WH-ABC", document_id="doc1", observed_at="2026-08-23T11:00:00Z")]
    )
    store2 = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
    _, conflicts = split_conflicting_facts(store2.fetch_facts_for_case(case_id))
    assert any(c.get("fact_key") == "device_model" for c in conflicts)
    # Legal supersession: explicit operator correction in BOTH consolidation
    # domains (customer + document) resolves the premise; a one-sided
    # correction would leave a conservative mixed conflict.
    store2.append_facts_with_supersession(
        [
            {
                **_mail_fact(case_id=case_id, fact_key="device_model", value="WH-FINAL", message_id="m2", observed_at="2026-08-23T12:00:00Z"),
                "entity_scope": "customer",
                "metadata": {"source_origin": "OPERATOR", "evidence_authority": "OPERATOR_STATEMENT", "instruction_authority": "NONE"},
            },
            {
                **_doc_fact(case_id=case_id, fact_key="device_model", value="WH-FINAL", document_id="doc2", observed_at="2026-08-23T12:00:00Z"),
                "metadata": {"source_origin": "OPERATOR", "evidence_authority": "OPERATOR_STATEMENT", "instruction_authority": "NONE"},
            }
        ]
    )
    store3 = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
    active, conflicts_after = split_conflicting_facts(store3.fetch_facts_for_case(case_id))
    values = {str(f.get("normalized_value")) for f in active if f.get("fact_key") == "device_model"}
    assert values == {"WH-FINAL"}
    assert not any(c.get("fact_key") == "device_model" for c in conflicts_after)
