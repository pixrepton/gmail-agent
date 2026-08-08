"""FACT-SUPERSESSION-WRITE-01: write-side supersession for replace/merge/retry."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.tools.write_executors import execute_merge_cases
from mailbox_memory.inmemory import InMemoryMailboxMemoryStore
from mailbox_memory.active_facts import fetch_current_facts_for_case


def _row(
    *,
    case_id: str,
    message_id: str,
    fact_id: str,
    fact_key: str,
    value: str,
    entity_scope: str = "customer",
    observed_at: str = "2026-08-08T10:00:00Z",
) -> dict[str, Any]:
    return {
        "fact_id": fact_id,
        "case_id": case_id,
        "message_id": message_id,
        "document_id": "",
        "entity_scope": entity_scope,
        "fact_key": fact_key,
        "normalized_value": value,
        "raw_value": value,
        "confidence": 0.8,
        "observed_at": observed_at,
        "source_type": "message",
        "source_ref": message_id,
        "status": "active",
        "metadata": {},
    }


def _active_values(store: InMemoryMailboxMemoryStore, case_id: str, fact_key: str) -> list[str]:
    return sorted(
        str(item.get("normalized_value") or "")
        for item in store.fetch_active_facts_for_case(case_id)
        if str(item.get("fact_key") or "") == fact_key
    )


def test_A_two_messages_same_logical_identity_no_dual_current() -> None:
    store = InMemoryMailboxMemoryStore()
    store.replace_message_facts(
        message_id="msg_a",
        rows=[_row(case_id="case_w1", message_id="msg_a", fact_id="fa", fact_key="customer_phone", value="111")],
    )
    store.replace_message_facts(
        message_id="msg_b",
        rows=[
            _row(
                case_id="case_w1",
                message_id="msg_b",
                fact_id="fb",
                fact_key="customer_phone",
                value="222",
                observed_at="2026-08-08T11:00:00Z",
            )
        ],
    )

    assert _active_values(store, "case_w1", "customer_phone") == ["222"]
    history = [
        item
        for item in store.fetch_facts_for_case("case_w1")
        if str(item.get("fact_key")) == "customer_phone"
    ]
    superseded = [item for item in history if str(item.get("status")) == "superseded"]
    assert any(str(item.get("normalized_value")) == "111" for item in superseded)
    current = fetch_current_facts_for_case(store, "case_w1")
    assert [c["normalized_value"] for c in current if c.get("fact_key") == "customer_phone"] == ["222"]


def test_B_re_ingest_same_message_replaces_snapshot_without_duals() -> None:
    store = InMemoryMailboxMemoryStore()
    store.replace_message_facts(
        message_id="msg_a",
        rows=[_row(case_id="case_w2", message_id="msg_a", fact_id="fa_x", fact_key="city", value="Radlin")],
    )
    store.replace_message_facts(
        message_id="msg_a",
        rows=[
            _row(
                case_id="case_w2",
                message_id="msg_a",
                fact_id="fa_y",
                fact_key="city",
                value="Rybnik",
                observed_at="2026-08-08T12:00:00Z",
            )
        ],
    )
    assert _active_values(store, "case_w2", "city") == ["Rybnik"]
    # Old source snapshot for this message must not remain active.
    active_from_msg = [
        item
        for item in store.fetch_active_facts_for_case("case_w2")
        if str(item.get("message_id")) == "msg_a" and str(item.get("fact_key")) == "city"
    ]
    assert len(active_from_msg) == 1
    assert active_from_msg[0]["normalized_value"] == "Rybnik"


def test_C_same_value_idempotent_via_replace_then_append() -> None:
    store = InMemoryMailboxMemoryStore()
    row = _row(case_id="case_w3", message_id="msg_a", fact_id="fx", fact_key="city", value="Radlin")
    store.replace_message_facts(message_id="msg_a", rows=[row])
    stats = store.append_facts_with_supersession(
        [{**row, "fact_id": "fx2", "message_id": "msg_b", "source_ref": "msg_b"}]
    )
    assert stats["unchanged"] == 1
    assert stats["inserted"] == 0
    assert _active_values(store, "case_w3", "city") == ["Radlin"]


def test_D_case_merge_produces_single_current_per_identity() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_a", "status": "open"})
    store.upsert_case({"case_id": "case_b", "status": "open"})
    store.replace_message_facts(
        message_id="ma",
        rows=[
            _row(
                case_id="case_a",
                message_id="ma",
                fact_id="fa",
                fact_key="customer_phone",
                value="111",
                observed_at="2026-08-08T12:00:00Z",
            )
        ],
    )
    store.replace_message_facts(
        message_id="mb",
        rows=[
            _row(
                case_id="case_b",
                message_id="mb",
                fact_id="fb",
                fact_key="customer_phone",
                value="222",
                observed_at="2026-08-08T13:00:00Z",
            )
        ],
    )

    result = execute_merge_cases(
        {"source_case_id": "case_a", "target_case_id": "case_b"},
        mailbox_store=store,
        correlation_store=None,
    )
    assert result["status"] == "ok"
    assert _active_values(store, "case_b", "customer_phone") == ["222"]
    # Source phone must be represented on target (rewritten case_id) without dual-active.
    target_history = [
        item
        for item in store.fetch_facts_for_case("case_b")
        if str(item.get("fact_key")) == "customer_phone"
    ]
    assert any(str(item.get("normalized_value")) == "222" and str(item.get("status")) == "active" for item in target_history)
    assert any(
        str(item.get("normalized_value")) == "111" and str(item.get("status")) == "superseded"
        for item in target_history
    )


def test_E_retry_same_replace_is_idempotent_for_current_state() -> None:
    store = InMemoryMailboxMemoryStore()
    rows = [_row(case_id="case_w5", message_id="msg_a", fact_id="fa", fact_key="city", value="Radlin")]
    store.replace_message_facts(message_id="msg_a", rows=rows)
    store.replace_message_facts(message_id="msg_a", rows=rows)
    assert _active_values(store, "case_w5", "city") == ["Radlin"]
    assert len([f for f in store.fetch_active_facts_for_case("case_w5") if f.get("fact_key") == "city"]) == 1


def test_replace_then_other_message_does_not_reactivate_deleted_snapshot() -> None:
    store = InMemoryMailboxMemoryStore()
    store.replace_message_facts(
        message_id="msg_a",
        rows=[_row(case_id="case_w6", message_id="msg_a", fact_id="fa", fact_key="city", value="A")],
    )
    store.replace_message_facts(
        message_id="msg_b",
        rows=[
            _row(
                case_id="case_w6",
                message_id="msg_b",
                fact_id="fb",
                fact_key="city",
                value="B",
                observed_at="2026-08-08T14:00:00Z",
            )
        ],
    )
    # Re-ingest empty-ish replacement for msg_a must not revive A over B.
    store.replace_message_facts(
        message_id="msg_a",
        rows=[
            _row(
                case_id="case_w6",
                message_id="msg_a",
                fact_id="fa2",
                fact_key="building_type",
                value="house",
                observed_at="2026-08-08T15:00:00Z",
            )
        ],
    )
    assert _active_values(store, "case_w6", "city") == ["B"]


def test_same_message_distinct_values_remain_legal_conflict() -> None:
    """Body vs attachment disagreement in one snapshot stays dual-active."""
    store = InMemoryMailboxMemoryStore()
    store.replace_message_facts(
        message_id="msg_one",
        rows=[
            _row(
                case_id="case_conflict",
                message_id="msg_one",
                fact_id="f_body",
                fact_key="heated_area_m2",
                value="180",
                entity_scope="building",
            ),
            _row(
                case_id="case_conflict",
                message_id="msg_one",
                fact_id="f_doc",
                fact_key="heated_area_m2",
                value="190",
                entity_scope="building",
                observed_at="2026-08-08T10:00:01Z",
            ),
        ],
    )
    assert sorted(_active_values(store, "case_conflict", "heated_area_m2")) == ["180", "190"]


def test_inmemory_transactional_replace_rolls_back_on_mid_failure() -> None:
    """Forced failure mid-replace leaves prior current intact."""

    class BoomStore(InMemoryMailboxMemoryStore):
        def _apply_replaced_message_fact_rows(self, message_id: str, rows: list[dict[str, Any]]) -> None:
            raise RuntimeError("forced failure after snapshot retire")

    store = BoomStore()
    store.facts["msg_a"] = [
        _row(case_id="case_w7", message_id="msg_a", fact_id="fa", fact_key="city", value="A")
    ]

    with pytest.raises(RuntimeError, match="forced failure"):
        store.replace_message_facts(
            message_id="msg_b",
            rows=[
                _row(
                    case_id="case_w7",
                    message_id="msg_b",
                    fact_id="fb",
                    fact_key="city",
                    value="B",
                    observed_at="2026-08-08T16:00:00Z",
                )
            ],
        )

    assert _active_values(store, "case_w7", "city") == ["A"]
