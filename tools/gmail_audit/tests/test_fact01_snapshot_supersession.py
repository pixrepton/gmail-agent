"""FACT-01: snapshot / hot-state readers must drop superseded before ranking."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_snapshot_manager import (
    _build_case_snapshot_hot_state,
    _fact_conflicts,
    _select_last_facts,
)
from mailbox_memory.inmemory import InMemoryMailboxMemoryStore
from mailbox_memory_runtime import (
    build_case_context_pack,
    build_case_snapshot,
    build_current_case_context_snapshot,
)
from signal_contract import build_canonical_signal


def _active_superseded_pair(*, case_id: str = "case_fact01") -> list[dict]:
    """Same (case_id, entity_scope, fact_key): superseded has higher confidence than active.

    entity_scope=case so hot-state `_fact_conflicts` (case/customer/location/asset) sees it.
    """
    return [
        {
            "fact_id": "fact_v1",
            "case_id": case_id,
            "message_id": "msg1",
            "document_id": "doc1",
            "entity_scope": "case",
            "fact_key": "heated_area_m2",
            "normalized_value": "120",
            "raw_value": "120",
            "confidence": 0.95,
            "observed_at": "2026-08-03T08:00:00Z",
            "source_type": "document_extraction",
            "source_ref": "doc:doc1",
            "status": "superseded",
            "metadata": {},
        },
        {
            "fact_id": "fact_v2",
            "case_id": case_id,
            "message_id": "msg2",
            "document_id": "doc2",
            "entity_scope": "case",
            "fact_key": "heated_area_m2",
            "normalized_value": "140",
            "raw_value": "140",
            "confidence": 0.6,
            "observed_at": "2026-08-03T09:00:00Z",
            "source_type": "document_extraction",
            "source_ref": "doc:doc2",
            "status": "active",
            "metadata": {},
        },
    ]


def _case_record(case_id: str = "case_fact01") -> dict:
    return {
        "case_id": case_id,
        "case_key": "CASE-FACT01",
        "case_family": "lead_opportunity",
        "status": "open",
        "customer_name": "",
        "customer_email": "",
        "metadata": {},
    }


def test_build_case_snapshot_ignores_superseded_before_ranking() -> None:
    snapshot = build_case_snapshot(
        case_id="case_fact01",
        case_record=_case_record(),
        messages=[],
        facts=_active_superseded_pair(),
        documents=[],
        events=[],
        next_action={},
    )

    area_facts = [item for item in snapshot["key_facts"] if item.get("fact_key") == "heated_area_m2"]
    assert len(area_facts) == 1
    assert area_facts[0]["value"] == "140"
    assert not any(item.get("fact_key") == "heated_area_m2" for item in snapshot["conflicting_facts"])
    assert not any("Konflikt danych dla heated_area_m2" in str(q) for q in snapshot["open_questions"])
    assert snapshot["status"] == "open"


def test_select_last_facts_and_fact_conflicts_ignore_superseded() -> None:
    facts = _active_superseded_pair()
    last = _select_last_facts(facts, limit=6)
    area_values = {item["value"] for item in last if item.get("fact_key") == "heated_area_m2"}
    assert area_values == {"140"}

    conflicts = _fact_conflicts(facts)
    assert "heated_area_m2" not in conflicts


def test_hot_state_builder_excludes_superseded_from_key_facts_and_conflicts() -> None:
    store = InMemoryMailboxMemoryStore()
    case_id = "case_fact01"
    store.cases = {case_id: _case_record(case_id)}
    # Seed already-superseded pair directly (both rows visible to fetch_facts_for_case).
    store.facts["seed"] = list(_active_superseded_pair(case_id=case_id))

    signal = build_canonical_signal(
        signal_kind="email_received",
        source_kind="gmail",
        source_ref={"message_id": "msg2"},
        observed_at="2026-08-03T09:00:00Z",
        effective_at="2026-08-03T09:00:00Z",
        case_key_hint="CASE-FACT01",
        thread_key_hint="CASE-FACT01",
        business_lane="lead",
        signal_summary_pl="Korekta powierzchni",
        payload={"case_id": case_id, "case_key": "CASE-FACT01"},
        artifacts={"raw_observation_id": "obs-fact01"},
        revision_marker="rev-fact01",
        created_by_runtime="test",
    )

    hot_state = _build_case_snapshot_hot_state(
        store=store,
        case_id=case_id,
        signal=signal,
        version=1,
        prior_versions=[],
        trace_id="trace-fact01",
    )

    area_facts = [item for item in hot_state["key_facts"] if item.get("fact_key") == "heated_area_m2"]
    assert len(area_facts) == 1
    assert area_facts[0]["value"] == "140"
    assert not any(item.get("fact_key") == "heated_area_m2" for item in hot_state["active_conflicts"])
    assert not any("heated_area_m2" in str(loop) for loop in hot_state["open_loops"])
    last_values = {item["value"] for item in hot_state["last_facts"] if item.get("fact_key") == "heated_area_m2"}
    assert last_values == {"140"}


def test_hot_state_overlay_does_not_revive_superseded_key_facts() -> None:
    """FACT-05 leftover: context_snapshot_source=case_snapshot_hot_state must not
    overwrite filtered live key_facts with an unfiltered hot-state ranking.
    """
    store = InMemoryMailboxMemoryStore()
    case_id = "case_fact01_overlay"
    store.cases = {case_id: _case_record(case_id)}
    facts = _active_superseded_pair(case_id=case_id)
    store.facts["seed"] = list(facts)

    store.append_case_snapshot_version(
        {
            "snapshot_id": "snap-bad-hot",
            "case_id": case_id,
            "version": 1,
            "source_signal_id": "sig-bad",
            "confidence": 0.9,
            "snapshot_json": {
                "schema_version": "case_snapshot_hot_state.v1",
                "case": {
                    "case_id": case_id,
                    "case_key": "CASE-FACT01",
                    "lifecycle_status": "awaiting_review",
                    "summary_text": "Bad hot state with superseded value.",
                },
                "key_facts": [
                    {
                        "fact_key": "heated_area_m2",
                        "entity_scope": "case",
                        "value": "120",
                        "confidence": 0.95,
                        "source_ref": "doc:doc1",
                    }
                ],
                "active_conflicts": [
                    {
                        "fact_key": "heated_area_m2",
                        "entity_scope": "case",
                        "values": ["120", "140"],
                    }
                ],
                "open_loops": ["Resolve conflict for heated_area_m2: 120, 140."],
                "recommended_next_step": "review",
                "snapshot_meta": {
                    "version": 1,
                    "source_signal_id": "sig-bad",
                    "created_at": "2026-08-03T09:30:00Z",
                },
            },
            "created_at": "2026-08-03T09:30:00Z",
        }
    )

    snapshot = build_current_case_context_snapshot(
        store=store,
        case_id=case_id,
        case_record=_case_record(case_id),
        messages=[],
        facts=facts,
        documents=[],
        events=[],
        next_action={},
        drive_enrichment={},
    )

    assert snapshot["context_snapshot_source"] == "case_snapshot_hot_state"
    area_facts = [item for item in snapshot["key_facts"] if item.get("fact_key") == "heated_area_m2"]
    assert len(area_facts) == 1
    assert area_facts[0]["value"] == "140"
    assert not any(item.get("fact_key") == "heated_area_m2" for item in snapshot["conflicting_facts"])


def test_pack_path_snapshot_and_hot_state_agree_on_active_only() -> None:
    store = InMemoryMailboxMemoryStore()
    case_id = "case_fact01_pack"
    store.cases = {case_id: _case_record(case_id)}
    base = {
        "case_id": case_id,
        "message_id": "msg1",
        "document_id": "doc1",
        "entity_scope": "case",
        "fact_key": "heated_area_m2",
        "raw_value": "120",
        "confidence": 0.95,
        "observed_at": "2026-08-03T08:00:00Z",
        "source_type": "document_extraction",
        "source_ref": "doc:doc1",
        "status": "active",
        "metadata": {},
    }
    row_v1 = {**base, "fact_id": "fact_v1", "normalized_value": "120"}
    row_v2 = {
        **base,
        "fact_id": "fact_v2",
        "document_id": "doc2",
        "source_ref": "doc:doc2",
        "normalized_value": "140",
        "confidence": 0.6,
        "observed_at": "2026-08-03T09:00:00Z",
        "metadata": {
            "allow_subject_supersession": True,
            "source_origin": "OPERATOR",
            "evidence_authority": "OPERATOR_STATEMENT",
            "instruction_authority": "NONE",
        },
    }
    assert store.append_facts_with_supersession([row_v1])["inserted"] == 1
    assert store.append_facts_with_supersession([row_v2])["superseded"] == 1

    pack = build_case_context_pack(store=store, case_id=case_id)
    assert len(pack.active_facts) == 1
    assert pack.active_facts[0]["normalized_value"] == "140"
    assert pack.conflicting_facts == []

    snapshot = pack.snapshot if isinstance(pack.snapshot, dict) else {}
    area_facts = [item for item in list(snapshot.get("key_facts") or []) if item.get("fact_key") == "heated_area_m2"]
    assert len(area_facts) == 1
    assert area_facts[0]["value"] == "140"
    assert not any(item.get("fact_key") == "heated_area_m2" for item in list(snapshot.get("conflicting_facts") or []))
