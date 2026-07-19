from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_routing import desk_eligible
from daszek_v3_operational_feed import build_operational_feed_from_mailbox_store
from mailbox_memory_store import InMemoryMailboxMemoryStore


def _case_row(
    *,
    case_id: str,
    case_family: str,
    priority_label: str,
    requires_action: bool = True,
    source_kind: str = "gmail_inbound",
    export_case_type: str = "lead_oferta",
) -> dict:
    return {
        "case_id": case_id,
        "case_key": case_id,
        "subject": case_id,
        "status": "open",
        "case_family": case_family,
        "updated_at": "2026-07-07T10:00:00+00:00",
        "metadata": {
            "priority_label": priority_label,
            "requires_action": requires_action,
            "source_kind": source_kind,
            "export_case_type": export_case_type,
        },
    }


def test_lead_p1_is_desk_eligible() -> None:
    row = _case_row(case_id="lead-p1", case_family="lead_opportunity", priority_label="P1 - pilne")
    assert desk_eligible(row) is True


def test_p3_case_not_desk_eligible() -> None:
    row = _case_row(
        case_id="acct-p3",
        case_family="accounting",
        priority_label="P3 - do przejrzenia",
        export_case_type="ksiegowosc_podatki",
    )
    assert desk_eligible(row) is False


def test_accounting_p2_on_desk() -> None:
    row = _case_row(
        case_id="acct-p2",
        case_family="accounting",
        priority_label="P2 - waĹĽne",
        export_case_type="ksiegowosc_podatki",
    )
    assert desk_eligible(row) is True


def test_noise_not_desk_eligible() -> None:
    row = _case_row(
        case_id="noise-1",
        case_family="reference_only",
        priority_label="pomijany",
        requires_action=False,
        export_case_type="noise",
    )
    assert desk_eligible(row) is False


def test_cieplo_orchestrated_info_on_desk_without_action() -> None:
    row = _case_row(
        case_id="cieplo-info",
        case_family="lead_opportunity",
        priority_label="P4 - informacja",
        requires_action=False,
        source_kind="cieplo_orchestrated",
        export_case_type="lead_oferta",
    )
    assert desk_eligible(row) is True


def test_internal_task_p1_eligible_in_desk_helper() -> None:
    """desk_eligible() supports legacy manual tasks; feed still excludes via Phase 1 boundary."""
    row = {
        "case_id": "task-p1",
        "case_family": "internal_task",
        "metadata": {"priority_label": "P1 - pilne", "source_kind": "manual"},
    }
    assert desk_eligible(row) is True


def test_internal_task_normal_priority_not_desk_eligible() -> None:
    row = {
        "case_id": "task-norm",
        "case_family": "internal_task",
        "metadata": {"priority_label": "normalny", "source_kind": "manual"},
    }
    assert desk_eligible(row) is False


def test_bootstrap_export_desk_ratio_in_seventy_to_eighty_band() -> None:
    """synthetic bootstrap fixture: P1+P2 stays in the historical 68-82% desk band."""
    import json

    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_bootstrap_cases.json"
    rows = json.loads(fixture_path.read_text(encoding="utf-8"))
    total = len(rows)
    desk_on_bootstrap = 0
    for row in rows:
        if not row.get("expected_upsert", True):
            continue
        case_row = {
            "case_family": row["expected_case_family"],
            "metadata": {
                "requires_action": row["expected_requires_action"],
                "source_kind": "gmail_inbound",
                "export_case_type": row["expected_case_type"],
                "priority_label": "P1 - pilne" if row["expected_desk_eligible"] else "P3 - do przejrzenia",
            },
        }
        if desk_eligible(case_row):
            desk_on_bootstrap += 1
    assert total >= 100
    ratio_all = desk_on_bootstrap / total
    assert 0.68 <= ratio_all <= 0.82, (
        f"desk share of synthetic bootstrap {ratio_all:.1%} ({desk_on_bootstrap}/{total}) outside 68-82%"
    )

def test_operational_feed_excludes_p3_from_desk_and_cases() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case(
        _case_row(case_id="case-p1", case_family="lead_opportunity", priority_label="P1 - pilne")
    )
    store.upsert_snapshot(
        "case-p1",
        {"snapshot_json": {"status": "open", "summary_text": "Lead", "recommended_next_action": "Oferta"}},
    )
    store.upsert_case(
        _case_row(
            case_id="case-p3",
            case_family="accounting",
            priority_label="P3 - do przejrzenia",
            export_case_type="ksiegowosc_podatki",
        )
    )
    store.upsert_snapshot(
        "case-p3",
        {"snapshot_json": {"status": "open", "summary_text": "ZUS", "recommended_next_action": "Review"}},
    )

    snap = build_operational_feed_from_mailbox_store(store, case_limit=10, task_limit=5, snapshot_id="desk-filter")
    feed = snap["feed"]
    case_ids = {str(c.get("case_id") or "") for c in feed.get("cases", []) if isinstance(c, dict)}
    desk_case_ids = {str(d.get("case_id") or "") for d in feed.get("desk", []) if isinstance(d, dict)}

    assert "case-p1" in case_ids
    assert "case-p3" not in case_ids
    assert "case-p3" not in desk_case_ids
    meta = feed.get("feed_meta", {})
    assert meta.get("desk_filter") == "P1_P2_operational"
    assert meta.get("operational_case_count", 0) >= 2
    assert meta.get("desk_eligible_count", 0) == 1

