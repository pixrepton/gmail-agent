from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from datetime import datetime, timezone

from case_context_contract import build_case_context_pack_vnext

from daszek_v3_operational_feed import (
    SCHEMA_VERSION,
    build_feed_and_api_case_dict,
    build_operational_feed_from_mailbox_store,
    build_operational_feed_snapshot,
    json_safe_deep,
    _case_detail_payload,
    _derive_desk_items,
    _is_gateb_test_artifact,
    _omit_empty_list_fields,
    _state_freshness_label,
    _vnext_to_feed_case_row,
)
from mailbox_memory_models import CaseContextPack
from mailbox_memory_store import InMemoryMailboxMemoryStore


def test_build_operational_feed_snapshot_shape_and_sanitization() -> None:
    cockpit = {
        "ok": True,
        "desk": {
            "items": [
                {"note_id": "n1", "title": "Follow up", "case_id": "c1", "summary": "Call supplier"},
                {"note_id": "n2", "title": "Review", "case_id": "c2"},
            ],
        },
        "cases": {
            "items": [
                {
                    "case_id": "c1",
                    "title": "Sprawa A",
                    "summary": " heating",
                    "evidence_cards": [{"evidence_id": "e1", "summary": "mail meta"}],
                    "completeness_gaps": [{"summary": "missing phone", "severity": "warning"}],
                    "conflicting_facts": [{"summary": "qty mismatch", "severity": "info"}],
                    "service_signals": [{"summary": "service hint"}],
                    "marketing_signals": [{"summary": "mkt hint"}],
                    "action_proposals": [{"proposal_id": "p1", "title": "draft reply"}],
                    "proposed_next_actions": [],
                },
                {"case_id": "c2", "title": "Sprawa B"},
            ],
        },
    }
    day = {"sections": [{"key": "teraz", "title": "Teraz", "items": []}]}
    tasks = [{"id": "t1", "title": "Task one", "status": "open", "note": "check"}]

    snap = build_operational_feed_snapshot(cockpit=cockpit, day=day, tasks=tasks, snapshot_id="snap-test-1")

    assert snap["schema_name"] == "daszek_operational_feed_snapshot"
    assert snap["schema_version"] == "1.3"
    assert snap["snapshot_id"] == "snap-test-1"
    assert snap["read_only"] is True
    assert snap["creates_cases"] is False
    assert snap["executes_actions"] is False

    feed = snap["feed"]
    assert len(feed["desk"]) == 2
    assert len(feed["cases"]) == 2
    assert len(feed["action_items"]) == 1
    assert "tasks" not in feed
    assert feed["action_items"][0]["source_type"] == "v1_task"
    assert "case_details" in feed
    assert "c1" in feed["case_details"]
    detail = feed["case_details"]["c1"]
    assert detail["case"]["case_id"] == "c1"
    assert detail["feed_read_only_stub"] is True

    for bad in ("body", "email_body", "snippet", "raw_llm", "raw_response", "raw_body", "message_body"):
        assert bad not in snap


def test_operational_feed_snapshot_decision_view_merge() -> None:
    from decision_projection_blocks import build_decision_view_blocks

    cockpit = {
        "desk": {"items": []},
        "cases": {"items": [{"case_id": "c1", "title": "Sprawa A"}]},
    }
    ci = {
        "understanding_output": {"operator_explanation": {"essence_pl": "Test essence"}},
        "decision_pipeline": {"outputs": {"decision_candidate": {"decision_candidate_id": "dc_1"}}},
        "policy_decision": {"policy_decision_id": "pd_1", "status": "allowed"},
        "action_proposals_v2": [{"proposal_id": "ap_1"}],
    }
    dv = build_decision_view_blocks(case_intelligence=ci)
    snap = build_operational_feed_snapshot(
        cockpit=cockpit,
        day=None,
        tasks=None,
        snapshot_id="snap-dv-1",
        case_decision_views={"c1": dv},
    )
    assert snap["feed"]["case_details"]["c1"].get("decision_view", {}).get("decision_candidate_id") == "dc_1"


def test_exporter_cli_writes_file(tmp_path: Path) -> None:
    cockpit_path = tmp_path / "cockpit.json"
    cockpit_path.write_text(
        json.dumps(
            {
                "desk": {"items": [{"note_id": "n1", "title": "x", "case_id": "c-x"}]},
                "cases": {"items": [{"case_id": "c-x", "title": "Case"}]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out_path = tmp_path / "out.json"
    import subprocess

    subprocess.run(
        [
            sys.executable,
            str(TOOL_DIR / "daszek_v3_operational_feed.py"),
            "--cockpit-json",
            str(cockpit_path),
            "--snapshot-id",
            "cli-test",
            "--out",
            str(out_path),
        ],
        check=True,
        cwd=str(REPO_ROOT),
    )
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["snapshot_id"] == "cli-test"
    assert data["feed"]["desk"]


def test_mailbox_memory_feed_has_contract_counts_and_no_forbidden_keys() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case(
        {
            "case_id": "case-mm-1",
            "case_key": "ck1",
            "subject": "Serwis rooftop",
            "status": "open",
            "case_family": "service",
            "updated_at": "2026-05-01T10:00:00+00:00",
            "latest_signal_at": "2026-05-01T09:00:00+00:00",
            "metadata": {"priority_label": "P1 - pilne", "requires_action": True},
        }
    )
    store.upsert_snapshot(
        "case-mm-1",
        {
            "snapshot_json": {
                "status": "open",
                "summary_text": "Skrót sprawy bez treści maila.",
                "recommended_next_action": "Potwierdź termin",
            }
        },
    )
    store.upsert_next_action(
        "case-mm-1",
        {"next_action": "Zadzwoń do klienta", "rationale": "Umówiony callback"},
    )
    store.upsert_action_proposal(
        {
            "proposal_id": "prop-mm-1",
            "case_id": "case-mm-1",
            "title": "Wyślij potwierdzenie",
            "status": "proposed",
            "requires_approval": True,
            "risk_level": "medium",
            "reason": "Standard",
        }
    )

    snap = build_operational_feed_from_mailbox_store(
        store,
        case_limit=5,
        task_limit=10,
        snapshot_id="snap-mm-test",
    )

    assert snap["schema_name"] == "daszek_operational_feed_snapshot"
    assert snap["snapshot_id"] == "snap-mm-test"
    assert snap["feed"]["cases"]
    assert snap["counts"]["cases"] >= 1
    assert snap["feed"]["action_items"]
    assert "tasks" not in snap["feed"]
    detail = snap["feed"]["case_details"]["case-mm-1"]
    assert detail["case"]["case_id"] == "case-mm-1"
    assert isinstance(detail.get("decision_view"), dict)
    assert detail["decision_view"].get("headline_co_pl") or detail["decision_view"].get("decision_summary")
    for bad in ("body", "email_body", "snippet", "raw_llm", "raw_response", "raw_body", "message_body", "prompt"):
        assert bad not in json.dumps(json_safe_deep(snap), ensure_ascii=False)


def test_vnext_feed_case_row_top_conflicts_are_projection_safe() -> None:
    pack = CaseContextPack(
        case_id="case_feed_pii",
        conflicting_facts=[
            {
                "fact_key": "customer_email",
                "values": ["a@b.co", "c@d.co"],
                "summary": "a@b.co vs c@d.co",
                "source_refs": [],
            }
        ],
    )
    vnext = build_case_context_pack_vnext(pack, generated_at="2026-05-15T12:00:00+00:00")
    row = _vnext_to_feed_case_row({"case_id": "case_feed_pii", "subject": "T"}, vnext)
    assert row["top_conflicts"] == []
    blob = json.dumps(json_safe_deep(row), ensure_ascii=False)
    assert "@" not in blob
    assert "a@b.co" not in blob
    assert '"values"' not in blob
    for cf in row.get("conflicting_facts") or []:
        assert isinstance(cf, dict)
        assert "values" not in cf
        assert "facts_in_conflict" not in cf


def test_vnext_feed_case_row_top_conflicts_excludes_weak_no_evidence_conflicts() -> None:
    pack = CaseContextPack(
        case_id="case_feed_weak_conflicts",
        conflicting_facts=[
            {
                "fact_key": "customer_email",
                "values": ["a@b.co", "c@d.co"],
                "summary": "a@b.co vs c@d.co",
                "source_refs": [],
            },
            {
                "fact_key": "city",
                "values": ["promo.newsletter@x.pl", "https://ads.example/track"],
                "summary": "Noisy city conflict",
                "source_refs": [],
            },
            {
                "fact_key": "device_power",
                "values": ["8 kW", "10 kW"],
                "summary": "Power conflict without evidence",
                "source_refs": [],
            },
        ],
    )
    vnext = build_case_context_pack_vnext(pack, generated_at="2026-05-15T12:00:00+00:00")

    row = _vnext_to_feed_case_row({"case_id": "case_feed_weak_conflicts", "subject": "T"}, vnext)

    assert row["top_conflicts"] == []
    assert len(row["conflicting_facts"]) == 3
    assert all(item.get("decision_usable") is False for item in row["conflicting_facts"])
    blob = json.dumps(json_safe_deep(row), ensure_ascii=False)
    assert '"values"' not in blob
    assert '"facts_in_conflict"' not in blob
    assert "a@b.co" not in blob
    assert "promo.newsletter@x.pl" not in blob


def test_vnext_feed_case_row_top_conflicts_keeps_supported_decision_usable_conflict() -> None:
    pack = CaseContextPack(
        case_id="case_feed_supported_conflict",
        conflicting_facts=[
            {
                "fact_key": "device_power",
                "values": ["8 kW", "10 kW"],
                "summary": "Power mismatch",
                "source_refs": [
                    {"source_type": "gmail_message", "source_id": "m1"},
                    {"source_type": "drive_document", "source_id": "d1"},
                ],
            }
        ],
    )
    vnext = build_case_context_pack_vnext(pack, generated_at="2026-05-15T12:00:00+00:00")

    row = _vnext_to_feed_case_row({"case_id": "case_feed_supported_conflict", "subject": "T"}, vnext)

    assert len(row["top_conflicts"]) == 1
    assert row["top_conflicts"][0]["decision_usable"] is True
    assert row["top_conflicts"][0]["evidence_ref_count"] == 2


def test_operator_feed_exports_context_quality_allowlist() -> None:
    pack = CaseContextPack(
        case_id="case_feed_context_quality",
        active_facts=[
            {
                "fact_key": "note",
                "normalized_value": "x",
                "source_ref": "",
                "source_type": "gmail_message",
                "status": "inferred",
            }
        ],
        conflicting_facts=[
            {
                "fact_key": "customer_email",
                "values": ["client@example.invalid", "other@example.invalid"],
                "summary": "client@example.invalid",
                "source_refs": [],
            }
        ],
    )
    vnext = build_case_context_pack_vnext(pack, generated_at="2026-05-15T12:00:00+00:00")

    row = _vnext_to_feed_case_row({"case_id": "case_feed_context_quality", "subject": "T"}, vnext)
    detail = _case_detail_payload(
        feed_case_row=row,
        vnext=vnext,
        proposals=[],
        executions=[],
        timeline=[],
    )

    quality = row["context_quality"]
    assert quality["ready_for_decision"] is False
    assert quality["operator_review_possible"] is True
    assert quality["action_readiness"] == "review_only"
    assert "weak_or_missing_evidence" in quality["not_ready_reasons"]
    assert detail["case"]["context_quality"] == quality
    rendered = json.dumps(json_safe_deep({"row": row, "detail": detail}), ensure_ascii=False)
    for bad in ("body", "snippet", "prompt", "raw_llm", "raw_response", "message_body", "values", "facts_in_conflict"):
        assert f'"{bad}"' not in rendered
    assert "client@example.invalid" not in rendered


def test_case_detail_payload_operator_case_has_no_raw_contact_or_values_keys() -> None:
    probe_email = "pii-probe-detail-zz@example.invalid"
    probe_phone = "+48 912 345 777"
    pack = CaseContextPack(
        case_id="case_detail_probe",
        conflicting_facts=[
            {
                "fact_key": "customer_email",
                "values": [probe_email, "other@example.invalid"],
                "summary": probe_email,
                "source_refs": [],
            },
            {
                "fact_key": "customer_phone",
                "values": [probe_phone, "+48 600 111 222"],
                "summary": probe_phone,
                "source_refs": [],
            },
        ],
        active_facts=[
            {
                "fact_key": "customer_email",
                "normalized_value": probe_email,
                "source_ref": "",
                "source_type": "gmail_message",
            }
        ],
    )
    vnext = build_case_context_pack_vnext(pack, generated_at="2026-05-15T12:00:00+00:00")
    feed_case = _vnext_to_feed_case_row({"case_id": "case_detail_probe", "subject": "S"}, vnext)
    detail = _case_detail_payload(
        feed_case_row=feed_case,
        vnext=vnext,
        proposals=[],
        executions=[],
        timeline=[],
    )
    case_blob = json.dumps(json_safe_deep(detail.get("case") or {}), ensure_ascii=False)
    assert probe_email not in case_blob
    assert probe_phone.replace(" ", "") not in case_blob.replace(" ", "")
    assert '"values"' not in case_blob
    assert "_internal_only" not in case_blob


def test_cockpit_snapshot_case_row_has_no_raw_contact_in_operator_fields() -> None:
    probe = "pii-probe-cockpit-zz@example.invalid"
    cockpit = {
        "desk": {"items": []},
        "cases": {
            "items": [
                {
                    "case_id": "case-cockpit-pii",
                    "title": "Cockpit case",
                    "conflicting_facts": [
                        {
                            "fact_key": "customer_email",
                            "values": [probe, "x@y.zz"],
                            "summary": f"raw {probe}",
                            "severity": "warning",
                        }
                    ],
                }
            ]
        },
    }
    snap = build_operational_feed_snapshot(cockpit=cockpit, day=None, tasks=None, snapshot_id="snap-cockpit-pii")
    case0 = snap["feed"]["cases"][0]
    blob = json.dumps(json_safe_deep(case0), ensure_ascii=False)
    assert probe not in blob
    assert "@" not in blob
    assert '"values"' not in blob
    assert "_internal_only" not in blob


def test_operational_feed_exports_projection_safe_context_summary_only() -> None:
    cockpit = {
        "cases": {
            "items": [
                {
                    "case_id": "case-safe",
                    "title": "Projection safe",
                    "context_pack_version": "vNext-2026-04",
                    "has_blocking_conflicts": True,
                    "has_blocking_gaps": False,
                    "top_conflicts": [
                        {"summary": "Conflict 1", "severity": "blocking", "body": "private"},
                        {"summary": "Conflict 2", "severity": "warning", "raw_body": "private"},
                        {"summary": "Conflict 3", "severity": "warning", "message_body": "private"},
                        {"summary": "Conflict 4", "severity": "warning"},
                    ],
                    "top_gaps": [
                        {"summary": "Gap 1", "severity": "warning", "snippet": "private"},
                        {"summary": "Gap 2", "severity": "info", "prompt": "private"},
                        {"summary": "Gap 3", "severity": "info", "raw_response": "private"},
                        {"summary": "Gap 4", "severity": "info"},
                    ],
                    "badges": ["blocking_conflict", "needs_operator_review"],
                }
            ]
        }
    }

    snap = build_operational_feed_snapshot(cockpit=cockpit, day=None, tasks=None, snapshot_id="snap-safe")
    case = snap["feed"]["cases"][0]

    assert case["context_pack_version"] == "vNext-2026-04"
    assert case["has_blocking_conflicts"] is True
    assert case["has_blocking_gaps"] is False
    assert len(case["top_conflicts"]) == 3
    assert len(case["top_gaps"]) == 3
    assert "blocking_conflict" in case["badges"]
    rendered = json.dumps(json_safe_deep(snap), ensure_ascii=False)
    for bad in ("body", "snippet", "prompt", "raw_llm", "raw_response", "raw_body", "message_body"):
        assert bad not in rendered
    assert "private" not in rendered


def test_mailbox_memory_feed_serializes_datetime_fields_to_json() -> None:
    """Postgres/psycopg often returns datetime objects; export must json.dumps without error."""

    store = InMemoryMailboxMemoryStore()
    store.upsert_case(
        {
            "case_id": "case-dt-1",
            "case_key": "ck-dt",
            "subject": "Serwis z datetime",
            "status": "open",
            "case_family": "service",
            "updated_at": datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
            "latest_signal_at": datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            "metadata": {"priority_label": "P1 - pilne", "requires_action": True},
        }
    )
    store.upsert_snapshot(
        "case-dt-1",
        {"snapshot_json": {"status": "open", "summary_text": "Skrót.", "recommended_next_action": "Zadzwoń"}},
    )
    snap = build_operational_feed_from_mailbox_store(store, case_limit=5, task_limit=5, snapshot_id="snap-dt")
    text = json.dumps(json_safe_deep(snap), ensure_ascii=False)
    assert "case-dt-1" in text
    assert "2026-05-01" in text
    loaded = json.loads(text)
    assert loaded["feed"]["cases"][0]["updated_at"].startswith("2026-05-01")


def test_derive_desk_items_at_most_one_per_case() -> None:
    feed_case = {
        "case_id": "case-desk-1",
        "title": "Pompa ciepła — oferta",
        "summary": "Klient pyta o montaż.",
        "operator_essence_pl": "Zapytanie o pompę ciepła — przygotuj ofertę.",
        "priority": "high",
        "has_blocking_gaps": True,
        "has_blocking_conflicts": True,
        "badges": {"blocking_gap": True, "blocking_conflict": True},
        "completeness_gaps": [{"summary": "Brak mocy instalacji"}],
        "conflicting_facts": [{"summary": "Różna powierzchnia"}],
        "action_proposals": [{"title": "Propozycja", "status": "proposed"}],
    }
    items, next_ix = _derive_desk_items(feed_case, 0)
    assert len(items) == 1
    assert next_ix == 1
    assert items[0]["case_id"] == "case-desk-1"
    assert "Sprzeczność" in items[0]["title"]


def test_gateb_test_artifact_filtered() -> None:
    assert _is_gateb_test_artifact(snapshot_id="gateb-proof-1")
    assert _is_gateb_test_artifact(case_id="gate_7", title="BADBADBADBADBAD")
    assert not _is_gateb_test_artifact(case_id="case-real-1", title="Oferta pompy ciepła")


def test_omit_empty_list_fields_drops_empty_arrays() -> None:
    row = _omit_empty_list_fields(
        {
            "case_id": "c1",
            "service_signals": [],
            "marketing_signals": [],
            "summary": "ok",
        }
    )
    assert "service_signals" not in row
    assert "marketing_signals" not in row
    assert row["summary"] == "ok"


def test_schema_version_shim() -> None:
    """SCHEMA_VERSION is 1.3; legacy v1 payloads get an ingest warning."""
    from daszek_v3_operational_feed import (
        SCHEMA_VERSION,
        _apply_schema_version_shim,
        _LEGACY_SCHEMA_VERSIONS,
        _apply_contract_validation,
    )

    assert SCHEMA_VERSION == "1.3"
    assert "1" in _LEGACY_SCHEMA_VERSIONS

    # Shim should not break v1.1 input
    cockpit = {"cases": {"items": [{"case_id": "c1"}]}}
    out = _apply_schema_version_shim(cockpit)
    assert out["cases"]["items"][0]["case_id"] == "c1"

    # _apply_contract_validation emits warning for legacy version
    payload = {
        "schema_name": "daszek_operational_feed_snapshot",
        "schema_version": "1",
        "snapshot_id": "test-legacy-1",
        "created_at": "2026-07-05T12:00:00Z",
        "generated_at": "2026-07-05T12:00:00Z",
        "title": "Test legacy",
        "subtitle": "",
        "read_only": True,
        "creates_cases": False,
        "executes_actions": False,
        "gate_claim": False,
        "feed": {
            "feed_meta": {
                "exporter": "test",
                "contract_module": "test",
            },
            "desk": [],
            "day": {"sections": []},
            "cases": [],
            "tasks": [],
            "case_details": {},
        },
    }
    from daszek_v3_operational_feed_contract import validate_operational_feed_snapshot
    import unittest.mock as mock
    with mock.patch(
        "daszek_v3_operational_feed.validate_operational_feed_snapshot",
        return_value=mock.MagicMock(ok=True, warnings=[]),
    ):
        result = _apply_contract_validation(payload)
    warnings = result.get("warnings", [])
    assert any("schema_version" in w and "outdated" in w for w in warnings)


def test_feed_and_api_case_dict_parity() -> None:
    """Same input must produce same output via both public and private path."""
    from mailbox_memory_models import CaseContextPack
    from case_context_contract import build_case_context_pack_vnext

    case_row = {
        "case_id": "parity-test-1",
        "case_key": "pk1",
        "subject": "Serwis pompy",
        "status": "open",
        "case_family": "service",
        "updated_at": "2026-07-01T10:00:00+00:00",
        "latest_signal_at": "2026-07-01T09:00:00+00:00",
        "metadata": {"priority": "high"},
    }
    pack = CaseContextPack(
        case_id="parity-test-1",
        snapshot={"status": "open", "summary_text": "Serwis pompy ciepła."},
        active_facts=[{"fact_key": "device_model", "value": "Panasonic 9 kW", "source_ref": "gmail:msg-1", "source_type": "gmail_message", "status": "confirmed"}],
        next_action={"next_action": "Zadzwoń do klienta", "rationale": "Callback"},
    )
    vnext = build_case_context_pack_vnext(pack, generated_at="2026-07-01T12:00:00+00:00")

    # Both paths must produce identical dict for same input
    from_private = _vnext_to_feed_case_row(case_row, vnext)
    from_public = build_feed_and_api_case_dict(case_row, vnext)

    assert from_private == from_public
    assert from_public["case_id"] == "parity-test-1"
    assert from_public["family"] == "service"
    assert from_public["priority"] == "high"
    assert "feed_case_dict" not in from_public  # not injected by the shared function itself


def test_runtime_state_freshness_label() -> None:
    """_state_freshness_label returns correct label based on time delta."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)

    # "fresh" — less than 5 minutes ago
    fresh_ts = (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
    assert _state_freshness_label(fresh_ts) == "fresh"

    # "aging" — between 5 and 30 minutes
    aging_ts = (now - timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
    assert _state_freshness_label(aging_ts) == "aging"

    # "stale" — more than 30 minutes
    stale_ts = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    assert _state_freshness_label(stale_ts) == "stale"

    # "unknown" — None or empty
    assert _state_freshness_label(None) == "unknown"
    assert _state_freshness_label("") == "unknown"
