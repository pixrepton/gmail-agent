from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_context_contract import (
    CONTRACT_NAME,
    CONTRACT_VERSION,
    PACK_BUILD,
    SCHEMA_VERSION,
    build_case_context_pack_vnext,
    feed_projection_summary_line,
    finalize_downstream_signal,
    normalize_evidence_refs,
    operator_feed_context_quality,
    sort_conflicts_for_operator_projection,
)
from mailbox_memory_models import CaseContextPack


FORBIDDEN_RAW_KEYS = {"body", "snippet", "prompt", "raw_llm", "raw_response", "raw_body", "message_body"}
EVIDENCE_REF_KEYS = {
    "source_type",
    "source_id",
    "timestamp",
    "source_timestamp",
    "source_owner",
    "field",
    "chunk_id",
    "document_id",
    "message_id",
    "calendar_event_id",
    "quote_id",
    "evidence_role",
    "confidence",
    "trust_level",
    "freshness",
    "can_answer_customer",
    "used_for",
    "valid_until",
}


def test_vnext_contract_promotes_evidence_conflicts_gaps_and_signals() -> None:
    pack = CaseContextPack(
        case_id="case_contract_1",
        snapshot={
            "status": "open",
            "summary_text": "Klient pyta o serwis.",
            "key_facts": [{"fact_key": "device_model", "value": "Panasonic 9 kW"}],
        },
        active_facts=[
            {
                "fact_id": "fact_device",
                "fact_key": "device_model",
                "value": "Panasonic 9 kW",
                "confidence": 0.91,
                "source_ref": "gmail:msg-1",
                "source_type": "gmail_message",
                "status": "active",
            }
        ],
        conflicting_facts=[
            {
                "fact_key": "device_power",
                "values": ["8 kW", "10 kW"],
                "source_refs": [{"source_type": "gmail_message", "source_id": "msg-1"}],
            }
        ],
        drive_documents_summary=[
            {
                "document_id": "drv-1",
                "file_name": "protokol.pdf",
                "document_kind": "service_protocol",
                "source_ref": "drive:drv-1",
                "summary_text": "Protokol serwisowy.",
            }
        ],
        completeness_gaps=["Missing customer answer: service date"],
        graph_hints=[
            {
                "relation_type": "case_has_device",
                "related_title": "Panasonic 9 kW",
                "related_node_type": "Device",
                "confidence": 0.88,
                "source_ref": "drive:drv-1",
            }
        ],
        source_refs=[{"type": "gmail_message", "message_id": "msg-1", "source_ref": "gmail:msg-1"}],
        next_action={"next_action": "request_missing_info", "rationale": "Need service date."},
        action_proposals=[
            {
                "proposal_id": "ap-1",
                "action_type": "prepare_reply_draft",
                "risk_class": "low",
                "requires_review": True,
                "status": "proposed",
            }
        ],
    )

    contract = build_case_context_pack_vnext(pack)

    assert contract["contract_name"] == CONTRACT_NAME
    assert contract["schema_version"] == SCHEMA_VERSION
    assert contract["contract_version"] == CONTRACT_VERSION
    assert contract["pack_build"] == PACK_BUILD
    assert contract["case_id"] == "case_contract_1"
    assert contract["evidence_cards"]
    assert contract["evidence_cards"][0]["source_type"] in {"gmail_message", "fact", "drive_document", "document_chunk", "source_ref", "unknown"}
    assert contract["conflicting_facts"][0]["severity"] == "warning"
    assert contract["completeness_gaps"][0]["severity"] == "warning"
    assert contract["related_entities"][0]["entity_type"] == "Device"
    assert contract["service_signals"][0]["policy_status"] == "allowed_for_projection"
    assert contract["marketing_signals"] == []
    assert contract["proposed_next_actions"][0]["proposal_id"] == "ap-1"
    _assert_downstream_signal_shape(contract["service_signals"][0])


def test_vnext_marketing_signal_when_media_present() -> None:
    pack = CaseContextPack(
        case_id="case_media",
        drive_documents_summary=[
            {"document_id": "m1", "file_name": "zdjecia.zip", "document_kind": "media_bundle", "source_ref": "drive:m1"}
        ],
        source_refs=[{"type": "gmail_message", "message_id": "msg-1", "source_ref": "gmail:msg-1"}],
    )
    contract = build_case_context_pack_vnext(pack)
    assert len(contract["marketing_signals"]) == 1
    m = contract["marketing_signals"][0]
    assert m["type"] == "marketing"
    _assert_downstream_signal_shape(m)
    assert m["evidence_refs"] is not None
    assert m["policy_status"]


def _assert_downstream_signal_shape(sig: dict) -> None:
    for key in (
        "type",
        "subtype",
        "summary",
        "recommended_operator_action",
        "risk_level",
        "requires_approval",
        "evidence_refs",
        "policy_status",
        "status",
    ):
        assert key in sig
    assert isinstance(sig["evidence_refs"], list)


def test_finalize_downstream_signal_fills_defaults() -> None:
    out = finalize_downstream_signal({"case_id": "c1", "type": "service", "subtype": "x"})
    assert out["risk_level"] == "low"
    assert out["requires_approval"] is True
    assert out["evidence_refs"] == []
    assert out["policy_status"] == "allowed_for_projection"
    assert out["status"] == "new"


def test_vnext_standardizes_evidence_refs_and_context_quality() -> None:
    pack = CaseContextPack(
        case_id="case_quality_1",
        snapshot={"status": "open", "customer": {"email": "", "name": "Jan"}},
        active_facts=[
            {
                "fact_key": "device_power",
                "normalized_value": "8 kW",
                "source_ref": "gmail:m1",
                "source_type": "gmail_message",
                "message_id": "m1",
                "observed_at": "2026-05-01T10:00:00+00:00",
                "confidence": 0.92,
                "body": "private mail body",
            },
            {
                "fact_key": "device_power",
                "normalized_value": "10 kW",
                "source_ref": "drive:d1",
                "source_type": "drive_document",
                "document_id": "d1",
                "confidence": 0.88,
                "snippet": "private doc snippet",
            },
        ],
        completeness_gaps=[
            {
                "type": "missing_customer_data",
                "summary": "Brak e-maila kontaktowego klienta.",
                "required_for": "closure",
                "source_refs": [
                    {
                        "source_type": "gmail_message",
                        "source_id": "m1",
                        "evidence_role": "invalid_role",
                        "raw_body": "private",
                    }
                ],
            }
        ],
    )

    contract = build_case_context_pack_vnext(pack, generated_at="2026-05-01T12:00:00+00:00")

    quality = contract["context_quality"]
    assert quality["conflict_count"] >= 1
    assert quality["gap_count"] >= 1
    assert quality["source_diversity_count"] >= 2
    assert "has_blocking_conflicts" in quality
    assert "ready_for_operator_review" in quality

    conflict = contract["conflicting_facts"][0]
    assert conflict["conflict_id"].startswith("conflict_")
    assert conflict["status"] in {"open", "needs_review", "resolved", "weak_evidence"}
    assert conflict["evidence_refs"]
    for ref in conflict["evidence_refs"]:
        assert set(ref) <= EVIDENCE_REF_KEYS
        assert ref["source_type"]
        assert ref["source_id"]
        assert ref.get("evidence_role") in {"supports", "contradicts"}

    gap = contract["completeness_gaps"][0]
    assert gap["gap_id"].startswith("gap_")
    assert gap["type"] == "missing_contact"
    assert gap["required_for"] == "operator_review"
    assert gap["status"] in {"open", "needs_review", "resolved", "not_applicable", "weak_evidence"}
    assert gap["evidence_refs"][0]["evidence_role"] == "weak_signal"

    rendered = repr(contract)
    for bad in FORBIDDEN_RAW_KEYS:
        assert bad not in rendered
    assert "private mail body" not in rendered
    assert "private doc snippet" not in rendered


def test_contact_email_conflict_projection_summary_redacts_addresses() -> None:
    pack = CaseContextPack(
        case_id="case_pii_email",
        conflicting_facts=[
            {
                "fact_key": "customer_email",
                "values": ["client@example.com", "other@example.org"],
                "summary": "client@example.com vs other@example.org",
                "source_refs": [],
            }
        ],
    )
    contract = build_case_context_pack_vnext(pack, generated_at="2026-05-15T12:00:00+00:00")
    c0 = contract["conflicting_facts"][0]
    assert "example.com" not in str(c0.get("projection_summary") or "")
    assert "example.org" not in str(c0.get("projection_summary") or "")
    assert "e-mail" in str(c0.get("projection_summary") or "").lower() or "email" in str(c0.get("projection_summary") or "").lower()
    assert c0.get("sensitive_value_redacted") is True
    assert c0.get("evidence_status") == "missing"
    assert c0.get("decision_usable") is False


def test_contact_phone_conflict_projection_summary_redacts_numbers() -> None:
    pack = CaseContextPack(
        case_id="case_pii_phone",
        conflicting_facts=[
            {
                "fact_key": "customer_phone",
                "values": ["+48 600 700 800", "601-602-603"],
                "summary": "Call both numbers",
                "source_refs": [],
            }
        ],
    )
    contract = build_case_context_pack_vnext(pack, generated_at="2026-05-15T12:00:00+00:00")
    c0 = contract["conflicting_facts"][0]
    ps = str(c0.get("projection_summary") or "")
    assert "600" not in ps
    assert "601" not in ps
    assert "telefon" in ps.lower() or "numery" in ps.lower()
    assert c0.get("decision_usable") is False


def test_weak_city_conflict_suppressed_from_operator_projection_top() -> None:
    pack = CaseContextPack(
        case_id="case_city_noise",
        conflicting_facts=[
            {
                "fact_key": "city",
                "values": ["promo.newsletter@x.pl", "https://ads.example/track"],
                "summary": "Rozne wartosci dla city: promo.newsletter@x.pl, https://ads.example/track",
                "source_refs": [],
            },
            {
                "fact_key": "device_power",
                "values": ["8 kW", "10 kW"],
                "summary": "Power mismatch",
                "source_refs": [
                    {"source_type": "gmail_message", "source_id": "m1"},
                    {"source_type": "gmail_message", "source_id": "m2"},
                ],
            },
        ],
    )
    contract = build_case_context_pack_vnext(pack, generated_at="2026-05-15T12:00:00+00:00")
    by_key = {c["fact_key"]: c for c in contract["conflicting_facts"]}
    city_row = by_key["city"]
    assert city_row.get("exclude_from_operator_projection_top") is True
    assert city_row.get("severity") == "info"
    assert city_row.get("evidence_status") == "missing"
    ordered = sort_conflicts_for_operator_projection(contract["conflicting_facts"])
    top3 = [r for r in ordered[:3] if not r.get("exclude_from_operator_projection_top")]
    assert top3 and top3[0].get("fact_key") == "device_power"


def test_context_quality_evidence_warnings_imply_not_ready_for_decision() -> None:
    pack = CaseContextPack(
        case_id="case_ctx_ready",
        snapshot={"status": "open", "customer": {"email": "", "name": "Jan"}},
        active_facts=[
            {
                "fact_key": "note",
                "normalized_value": "x",
                "source_ref": "",
                "source_type": "gmail_message",
                "status": "inferred",
            }
        ],
    )
    contract = build_case_context_pack_vnext(pack, generated_at="2026-05-15T12:00:00+00:00")
    q = contract["context_quality"]
    assert "ready_for_operator_review" in q
    assert "ready_for_decision" in q
    assert q.get("operator_review_possible") is True
    assert q.get("ready_for_decision") is False
    assert q.get("action_readiness") == "review_only"
    assert "weak_or_missing_evidence" in (q.get("not_ready_reasons") or [])


def test_operator_feed_context_quality_exports_allowlisted_readiness_only() -> None:
    quality = {
        "ready_for_decision": False,
        "operator_review_possible": True,
        "action_readiness": "review_only",
        "not_ready_reasons": ["weak_or_missing_evidence", "evidence_warnings"],
        "weak_evidence_count": 3,
        "evidence_warning_count": 1,
        "conflict_count": 2,
        "gap_count": 1,
        "has_blocking_conflicts": False,
        "has_blocking_gaps": False,
        "body": "private",
        "raw_response": {"x": "private"},
        "values": ["client@example.com"],
    }

    out = operator_feed_context_quality(quality)

    assert out == {
        "ready_for_decision": False,
        "operator_review_possible": True,
        "action_readiness": "review_only",
        "not_ready_reasons": ["weak_or_missing_evidence", "evidence_warnings"],
        "weak_evidence_count": 3,
        "evidence_warning_count": 1,
        "conflict_count": 2,
        "gap_count": 1,
        "has_blocking_conflicts": False,
        "has_blocking_gaps": False,
    }
    rendered = repr(out)
    assert "private" not in rendered
    assert "example.com" not in rendered


def test_feed_projection_summary_line_prefers_safe_fields() -> None:
    line = feed_projection_summary_line(
        {"summary": "raw@x.com", "projection_summary": "Operator-safe line.", "safe_summary": "Operator-safe line."}
    )
    assert line == "Operator-safe line."
    assert "@" not in line


def test_vnext_legacy_gap_strings_map_to_mvp_enums() -> None:
    pack = CaseContextPack(case_id="case_gap_enum", completeness_gaps=["Missing customer answer: service date"])
    contract = build_case_context_pack_vnext(pack, generated_at="2026-05-01T12:00:00+00:00")

    gap = contract["completeness_gaps"][0]
    assert gap["type"] == "missing_scheduling_evidence"
    assert gap["required_for"] == "scheduling"
    assert gap["status"] == "weak_evidence"
    assert "gap without evidence" in " ".join(contract["warnings"]).lower()


def test_signal_rules_priority() -> None:
    """SIGNAL_RULES must be executed in priority order (ascending)."""
    from case_context_contract import SIGNAL_RULES, build_downstream_signals

    svc_prio = [r["priority"] for r in SIGNAL_RULES if r["family"] == "service"]
    mkt_prio = [r["priority"] for r in SIGNAL_RULES if r["family"] == "marketing"]
    # svc_warranty (10) should come before mkt_media (20)
    all_prios = [(r["rule_id"], r["priority"]) for r in SIGNAL_RULES]
    for i in range(len(all_prios) - 1):
        assert all_prios[i][1] <= all_prios[i + 1][1], (
            f"Rules not sorted by priority: {all_prios[i]} before {all_prios[i + 1]}"
        )

    # Verify the rules are actually applied in priority order
    assert svc_prio == [10], f"Expected service priority 10, got {svc_prio}"
    assert mkt_prio == [20], f"Expected marketing priority 20, got {mkt_prio}"

    # Smoke test: build_downstream_signals iterates rules
    svc, mkt = build_downstream_signals(
        case_id="test-prio",
        drive_documents=[{"document_kind": "service_protocol"}],
        gaps=[],
        graph_hints=[],
        evidence_cards=[],
    )
    assert len(svc) == 1
    assert svc[0]["rule_id"] == "svc_warranty"
    assert len(mkt) == 0  # no media present


def test_signal_rule_id_present() -> None:
    """Every signal emitted by build_downstream_signals must carry rule_id."""
    from case_context_contract import build_downstream_signals

    # Case with both service and marketing triggers
    svc, mkt = build_downstream_signals(
        case_id="test-rule-id",
        drive_documents=[
            {"document_kind": "service_protocol"},
            {"document_kind": "media_bundle"},
        ],
        gaps=[{"summary": "Brak serwisu"}],
        graph_hints=[{"relation_type": "media_asset"}],
        evidence_cards=[],
    )
    all_signals = svc + mkt
    assert len(all_signals) >= 1
    for sig in all_signals:
        assert "rule_id" in sig, f"Signal missing rule_id: {sig.get('signal_id')}"
        assert str(sig["rule_id"]).strip(), f"Empty rule_id: {sig}"
