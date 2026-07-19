"""Tests for pre-LLM inference enrichment and attachment cache refresh."""

from __future__ import annotations

from attachment_intelligence import refresh_attachment_intelligence_with_intake_context
from inference_enrichment import (
    build_attachment_evidence_envelope,
    build_prior_thread_context_envelope,
    enrich_snapshot_for_inference,
    envelopes_for_telemetry,
)
from intake_payload import build_inference_payload, build_intake_reasoning_payload


def _minimal_snapshot() -> dict:
    return {
        "snapshot_version": "1",
        "mailbox": "x@test.local",
        "observed_at": "2026-01-01T00:00:00",
        "source_message": {
            "message_id": "m1",
            "thread_id": "t1",
            "subject": "Test",
            "body": "Hello",
            "snippet": "Hello",
            "sender": "a@b.c",
            "has_attachments": False,
            "attachment_names": [],
        },
        "context_messages": [],
        "normalized_subject": "test",
        "thread_context_quality": "weak",
        "thread_context": {"quality": "weak", "reasons": []},
        "case_link_candidates": [],
        "routing_hints": {"self_forward": False, "reasons": []},
    }


def test_enrich_snapshot_sets_envelope_and_cache() -> None:
    snap = _minimal_snapshot()
    cfg: dict = {"attachment_fetcher": None, "attachment_max_bytes": 8_000_000}
    enrich_snapshot_for_inference(snap, cfg)
    assert "inference_enrichment" in snap
    assert cfg.get("cached_attachment_intelligence") is not None
    tel = envelopes_for_telemetry(snap)
    assert "approx_enrichment_json_chars" in tel


def test_inference_payload_includes_enrichment() -> None:
    snap = _minimal_snapshot()
    enrich_snapshot_for_inference(snap, {"attachment_fetcher": None, "attachment_max_bytes": 8_000_000})
    package = build_inference_payload(snap)
    assert "inference_enrichment" in package["payload"]
    assert "inference_enrichment_attached" in package["metrics"]["notes"]


def test_intake_reasoning_payload_includes_enrichment() -> None:
    snap = _minimal_snapshot()
    enrich_snapshot_for_inference(snap, {"attachment_fetcher": None, "attachment_max_bytes": 8_000_000})
    p = build_intake_reasoning_payload(snap, context_bundle={})
    assert "inference_enrichment" in p


def test_attachment_evidence_envelope_bounded() -> None:
    env = build_attachment_evidence_envelope(
        {
            "attachments": [
                {
                    "attachment_business_type": "invoice",
                    "attachment_summary_pl": "Faktura za usługi",
                    "attachment_risk_flags": ["financial_document_present"],
                    "case_relevance": "significant",
                    "extraction_confidence": 0.2,
                    "operator_attention_hint": "check_attachment",
                    "extracted_text_preview": "FV 123 kwota 100 PLN",
                    "extraction_method": "pdf_text_layer",
                    "file_name": "a.pdf",
                }
            ],
            "attachment_count": 1,
            "has_significant_attachments": True,
        }
    )
    assert env["attachments"]
    assert env["attachments"][0]["scan_without_text_layer"] is False


def test_refresh_attachment_intel_updates_summary() -> None:
    cached = {
        "attachments": [
            {
                "attachment_business_type": "invoice",
                "file_name": "x.pdf",
                "extracted_text_preview": "",
                "extraction_confidence": 0.0,
                "attachment_risk_flags": [],
                "case_relevance": "background",
                "operator_attention_hint": "none",
            }
        ],
        "attachment_count": 1,
        "significant_count": 0,
        "combined_risk_flags": [],
        "summary_pl": "x",
        "has_significant_attachments": False,
    }
    out = refresh_attachment_intelligence_with_intake_context(
        cached,
        intake_result={"decision": {"action": "review"}},
        case_link_result={"selected_case_key": "ck1"},
    )
    assert len(out["attachments"]) == 1


def test_prior_thread_envelope_from_remote_memory() -> None:
    snap = _minimal_snapshot()
    env = build_prior_thread_context_envelope(
        snap,
        {
            "canonical_thread_summary": "Klient czeka na wycenę",
            "unresolved_questions": ["Jaki model pompy?"],
            "commitments_made": [],
            "key_facts_so_far": ["Lokalizacja: Kraków"],
            "last_decision": "",
            "thread_id": "t1",
        },
    )
    assert "Kraków" in " ".join(env.get("key_facts_so_far") or [])
