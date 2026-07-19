from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_context_contract import build_case_context_pack_vnext, format_vnext_human_summary
from case_context_deterministic import (
    collect_cross_source_predicate_conflicts,
    merge_conflicts_deterministic,
    merge_gaps_deterministic,
    normalize_value_for_conflict,
)
from mailbox_memory_models import CaseContextPack


def test_normalize_value_collapses_whitespace_and_case() -> None:
    assert normalize_value_for_conflict("  Foo  BAR ") == "foo bar"
    assert normalize_value_for_conflict("10 kW") == normalize_value_for_conflict("10  kw")


def test_cross_source_conflict_requires_two_evidence_refs() -> None:
    facts = [
        {
            "fact_key": "device_power",
            "normalized_value": "8 kW",
            "source_ref": "gmail:m1",
            "source_type": "gmail_message",
        },
        {
            "fact_key": "device_power",
            "normalized_value": "10 kW",
            "source_ref": "drive:d1",
            "source_type": "drive_document",
        },
    ]
    rows = collect_cross_source_predicate_conflicts(facts, case_id="c1")
    assert len(rows) == 1
    assert rows[0]["type"] == "document_vs_email"
    assert len(rows[0]["source_refs"]) >= 2


def test_merge_conflicts_skips_duplicate_predicate_value_set() -> None:
    existing = [
        {
            "fact_key": "device_power",
            "values": ["8 kw", "10 kw"],
            "summary": "dup",
            "source_refs": [{"source_type": "gmail_message", "source_id": "a"}],
        }
    ]
    facts = [
        {"fact_key": "device_power", "normalized_value": "8 kW", "source_ref": "g1", "source_type": "gmail_message"},
        {"fact_key": "device_power", "normalized_value": "10 kW", "source_ref": "d1", "source_type": "drive_document"},
    ]
    merged = merge_conflicts_deterministic(existing, case_id="c1", active_facts=facts, snapshot={}, next_action={})
    assert len(merged) == 1


def test_vnext_includes_version_and_human_summary() -> None:
    pack = CaseContextPack(
        case_id="c_human",
        snapshot={"status": "open", "summary_text": "Test", "customer": {"email": "a@b.c", "name": "AB"}},
        active_facts=[{"fact_key": "device_model", "normalized_value": "X", "source_ref": "s1", "source_type": "gmail_message"}],
    )
    contract = build_case_context_pack_vnext(pack, generated_at="2026-01-01T00:00:00+00:00")
    assert contract.get("version")
    assert contract.get("generated_at") == "2026-01-01T00:00:00+00:00"
    assert isinstance(contract.get("warnings"), list)
    text = format_vnext_human_summary(contract)
    assert "c_human" in text
    assert "Summary:" in text


def test_merge_gaps_respects_existing_summaries() -> None:
    existing = [{"summary": "Already listed", "type": "missing_evidence", "severity": "info", "status": "open", "case_id": "c2"}]
    merged = merge_gaps_deterministic(
        existing,
        case_id="c2",
        snapshot={"customer": {"email": "", "name": ""}},
        active_facts=[],
        drive_documents_summary=[],
        source_refs=[],
    )
    summaries = {str(x.get("summary")) for x in merged if isinstance(x, dict)}
    assert "Already listed" in summaries


def test_vnext_conflict_and_gap_ids_are_stable() -> None:
    pack = CaseContextPack(
        case_id="case_stable_ids",
        active_facts=[
            {"fact_key": "case_status", "normalized_value": "open", "source_ref": "gmail:m1", "source_type": "gmail_message"},
            {"fact_key": "case_status", "normalized_value": "closed", "source_ref": "drive:d1", "source_type": "drive_document"},
        ],
        completeness_gaps=[{"type": "missing_drive_link", "summary": "Drive reference exists without linked document."}],
    )

    first = build_case_context_pack_vnext(pack, generated_at="2026-05-01T00:00:00+00:00")
    second = build_case_context_pack_vnext(pack, generated_at="2026-05-02T00:00:00+00:00")

    assert first["conflicting_facts"][0]["conflict_id"] == second["conflicting_facts"][0]["conflict_id"]
    assert first["completeness_gaps"][0]["gap_id"] == second["completeness_gaps"][0]["gap_id"]


def test_existing_weak_conflict_dedupes_against_generated_evidence_rich_conflict() -> None:
    pack = CaseContextPack(
        case_id="case_dedupe_prefers_evidence",
        active_facts=[
            {"fact_key": "device_power", "normalized_value": "8 kW", "source_ref": "gmail:m1", "source_type": "gmail_message"},
            {"fact_key": "device_power", "normalized_value": "10 kW", "source_ref": "drive:d1", "source_type": "drive_document"},
        ],
        conflicting_facts=[{"fact_key": "device_power", "values": ["10 kw", "8 kw"], "summary": "legacy duplicate"}],
    )

    contract = build_case_context_pack_vnext(pack, generated_at="2026-05-01T00:00:00+00:00")
    conflicts = [c for c in contract["conflicting_facts"] if c["fact_key"] == "device_power"]

    assert len(conflicts) == 1
    assert conflicts[0]["evidence_refs"]
    assert conflicts[0]["status"] == "open"


def test_empty_context_does_not_create_blocking_gaps() -> None:
    contract = build_case_context_pack_vnext(CaseContextPack(case_id="case_empty"))

    assert contract["context_quality"]["gap_count"] == 0
    assert contract["context_quality"]["has_blocking_gaps"] is False
    assert contract["context_quality"]["ready_for_operator_review"] is True
