"""B3: SIGNAL_EXTRACTION_MODE routes message facts through LLM hvac_signals."""

from __future__ import annotations

from mailbox_memory_runtime import MailboxMemoryRuntime, build_case_context_pack, facts_from_hvac_signals
from mailbox_memory_store import InMemoryMailboxMemoryStore
from llm_contracts.signal_extraction import SignalExtractionResult
from pathlib import Path


def test_facts_from_hvac_signals_maps_building_and_area() -> None:
    rows = facts_from_hvac_signals(
        {"building_type": "dom jednorodzinny", "heated_area_m2": 120.0},
        case_id="c1",
        message_id="m1",
        observed_at="2026-01-01T00:00:00+00:00",
        source_type="message",
        source_ref="m1",
        entity_scope="customer",
        metadata={"extraction_path": "llm_intake"},
    )
    keys = {row["fact_key"] for row in rows}
    assert "building_type" in keys
    assert "heated_area_m2" in keys


def test_floor_heating_signal_is_retained_and_materialized_as_case_facts() -> None:
    signals = SignalExtractionResult.model_validate(
        {"floor_heating_existing": True, "floor_heating_scope": "parter"}
    ).model_dump()
    rows = facts_from_hvac_signals(
        signals,
        case_id="c1",
        message_id="m1",
        observed_at="2026-01-01T00:00:00+00:00",
        source_type="message",
        source_ref="m1",
        entity_scope="case",
        metadata={"extraction_path": "llm_intake"},
    )

    by_key = {row["fact_key"]: row for row in rows}
    assert by_key["floor_heating_existing"]["normalized_value"] == "True"
    assert by_key["floor_heating_scope"]["normalized_value"] == "parter"


def test_current_floor_heating_signal_conflicts_without_legal_supersession() -> None:
    store = InMemoryMailboxMemoryStore()
    base = {
        "case_id": "c1",
        "source_type": "message",
        "entity_scope": "case",
        "metadata": {"extraction_path": "llm_intake"},
    }
    store.append_facts_with_supersession(
        facts_from_hvac_signals(
            {"floor_heating_existing": False},
            message_id="m0",
            source_ref="m0",
            observed_at="2026-01-01T00:00:00+00:00",
            **base,
        )
    )
    stats = store.append_facts_with_supersession(
        facts_from_hvac_signals(
            {"floor_heating_existing": True, "floor_heating_scope": "parter"},
            message_id="m1",
            source_ref="m1",
            observed_at="2026-01-01T01:00:00+00:00",
            **base,
        )
    )

    pack = build_case_context_pack(store=store, case_id="c1").to_dict()
    by_key = {row["fact_key"]: row for row in pack["active_facts"]}
    conflicts = list(pack.get("conflicting_facts") or [])
    assert stats["superseded"] == 0
    assert by_key["floor_heating_existing"]["normalized_value"] == "True"
    assert by_key["floor_heating_existing"]["source_ref"] == "m1"
    assert any(item.get("fact_key") == "floor_heating_existing" for item in conflicts)
    assert by_key["floor_heating_scope"]["normalized_value"] == "parter"


def test_message_source_facts_llm_skips_regex_when_signals_present() -> None:
    runtime = MailboxMemoryRuntime(
        store=InMemoryMailboxMemoryStore(),
        blob_root=Path("."),
        stage_mode="live",
        signal_extraction_mode="llm",
    )
    facts, path = runtime._message_source_facts(
        case_id="c1",
        message_id="m1",
        message={"subject": "120 m2 dom", "body": "nie używaj regex"},
        observed_at="2026-01-01T00:00:00+00:00",
        hvac_signals={"building_type": "budynek wielorodzinny", "heated_area_m2": 120},
    )
    assert path == "llm_intake"
    assert any(row["fact_key"] == "building_type" for row in facts)
    assert not any(row.get("fact_value") == "nie używaj regex" for row in facts)


def test_message_source_facts_default_mode_is_llm() -> None:
    runtime = MailboxMemoryRuntime(
        store=InMemoryMailboxMemoryStore(),
        blob_root=Path("."),
        stage_mode="live",
    )
    facts, path = runtime._message_source_facts(
        case_id="c1",
        message_id="m1",
        message={"subject": "120 m2 dom", "body": "regex-only body text"},
        observed_at="2026-01-01T00:00:00+00:00",
        hvac_signals={"building_type": "dom jednorodzinny", "heated_area_m2": 120},
    )
    assert runtime.signal_extraction_mode == "llm"
    assert path == "llm_intake"
    assert any(row["fact_key"] == "building_type" for row in facts)
