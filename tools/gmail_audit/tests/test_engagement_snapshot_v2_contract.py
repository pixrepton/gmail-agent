from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.store import (
    AgentConcurrencyError,
    InMemoryOperatorEngagementStore,
    build_snapshot_from_signal,
)
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2, OperationalStatus


def _minimal_snapshot_dict(**overrides: object) -> dict:
    base = {
        "engagement_id": "eng_test_001",
        "case_id": "case_test_001",
        "version": 1,
        "trace_id": "sig_test_001",
        "operational_status": {"code": "raw_inquiry", "steps_remaining": 12, "blocking": False},
        "hvac_profile": {"location": {}},
        "gaps": [],
        "agent_memory": {
            "reasoning_trace": [],
            "tool_calls": [],
            "constitution_sections_used": [],
        },
        "actions": [],
        "hitl_gate": {"required": False, "reason": ""},
    }
    base.update(overrides)
    return base


def test_illegal_root_field_raises_validation_error() -> None:
    payload = _minimal_snapshot_dict(not_allowed_field="x")
    with pytest.raises(ValidationError):
        EngagementSnapshotV2.model_validate(payload)


def test_illegal_nested_field_raises_validation_error() -> None:
    payload = _minimal_snapshot_dict(
        hvac_profile={"location": {}, "unknown_hvac_key": 1},
    )
    with pytest.raises(ValidationError):
        EngagementSnapshotV2.model_validate(payload)


def test_build_snapshot_from_signal_does_not_persist() -> None:
    store = InMemoryOperatorEngagementStore()
    snapshot = build_snapshot_from_signal(
        signal={"signal_id": "sig_radlin_001"},
        case_id="case_radlin_001",
        engagement_id="eng_radlin_001",
    )
    assert snapshot.operational_status.code == "raw_inquiry"
    assert snapshot.version == 1
    assert store.load_snapshot("eng_radlin_001") is None


def test_init_snapshot_from_signal_raw_inquiry_version_one() -> None:
    store = InMemoryOperatorEngagementStore()
    snapshot = store.init_snapshot_from_signal(
        signal={"signal_id": "sig_radlin_001"},
        case_id="case_radlin_001",
        engagement_id="eng_radlin_001",
    )
    assert snapshot.operational_status.code == "raw_inquiry"
    assert snapshot.version == 1
    assert snapshot.trace_id == "sig_radlin_001"
    assert snapshot.case_id == "case_radlin_001"
    assert snapshot.engagement_id == "eng_radlin_001"


def test_save_load_round_trip_identity() -> None:
    store = InMemoryOperatorEngagementStore()
    created = store.init_snapshot_from_signal(
        signal={"signal_id": "sig_roundtrip"},
        case_id="case_rt",
        engagement_id="eng_rt",
    )
    created.hvac_profile.heated_area_m2 = 128
    created.hvac_profile.location.city = "Radlin"
    created.operational_status = OperationalStatus(code="enriching", steps_remaining=10)
    new_version = store.save_snapshot(created, expected_version=1)
    assert new_version == 2

    loaded = store.load_snapshot("eng_rt")
    assert loaded is not None
    assert loaded.engagement_id == created.engagement_id
    assert loaded.case_id == created.case_id
    assert loaded.version == 2
    assert loaded.operational_status.code == "enriching"
    assert loaded.hvac_profile.heated_area_m2 == 128
    assert loaded.hvac_profile.location.city == "Radlin"


def test_optimistic_lock_conflict_raises() -> None:
    store = InMemoryOperatorEngagementStore()
    snapshot = store.init_snapshot_from_signal(
        signal={"signal_id": "sig_lock"},
        case_id="case_lock",
        engagement_id="eng_lock",
    )
    store.save_snapshot(snapshot, expected_version=1)

    stale = snapshot.model_copy(update={"version": 1})
    stale.operational_status = OperationalStatus(code="enriching", steps_remaining=11)
    with pytest.raises(AgentConcurrencyError):
        store.save_snapshot(stale, expected_version=1)

    loaded = store.load_snapshot("eng_lock")
    assert loaded is not None
    assert loaded.version == 2
    assert loaded.operational_status.code == "raw_inquiry"
