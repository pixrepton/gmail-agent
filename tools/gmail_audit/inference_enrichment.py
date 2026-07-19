"""Pre-first-pass bounded attachment + thread envelopes for intake inference (semantic alignment)."""

from __future__ import annotations

import json
import os
from typing import Any

from attachment_intelligence import build_attachment_intelligence
from thread_memory import build_thread_memory


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _compact(text: str, limit: int) -> str:
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 12)] + " [truncated]"


def build_attachment_evidence_envelope(
    attachment_intelligence: dict[str, Any],
    *,
    max_attachments: int = 3,
    max_key_facts_per: int = 4,
    max_summary: int = 320,
) -> dict[str, Any]:
    """Bounded, prompt-safe summary of attachment intelligence (no raw OCR dumps)."""
    raw_list = list(attachment_intelligence.get("attachments") or [])
    out_attachments: list[dict[str, Any]] = []
    high_value_types = {
        "technical_pdf",
        "project_document",
        "delivery_confirmation",
        "invoice",
        "technical_photo",
        "product_datasheet",
    }
    for rec in raw_list[:max_attachments]:
        if not isinstance(rec, dict):
            continue
        method = str(rec.get("extraction_method") or "")
        scan_no_text = method == "pdf_no_text_layer"
        preview = str(rec.get("extracted_text_preview") or "").strip()
        key_facts: list[str] = []
        if preview:
            sentences = [s.strip() for s in preview.replace("\r", "\n").split("\n") if s.strip()]
            for chunk in sentences[:max_key_facts_per]:
                key_facts.append(_compact(chunk, 160))
        summary_pl = _compact(str(rec.get("attachment_summary_pl") or ""), max_summary)
        out_attachments.append(
            {
                "attachment_business_type": str(rec.get("attachment_business_type") or "unknown"),
                "attachment_summary_pl": summary_pl,
                "attachment_key_facts": key_facts[:max_key_facts_per],
                "attachment_risk_flags": list(rec.get("attachment_risk_flags") or [])[:6],
                "attachment_case_relevance": str(rec.get("case_relevance") or "background"),
                "attachment_extraction_confidence": float(rec.get("extraction_confidence") or 0.0),
                "attachment_attention_hint": str(rec.get("operator_attention_hint") or "none"),
                "scan_without_text_layer": scan_no_text,
                "file_name": _compact(str(rec.get("file_name") or ""), 120),
            }
        )
    combined = "none"
    if attachment_intelligence.get("has_significant_attachments"):
        combined = "check_attachment"
    elif any(str(a.get("attachment_business_type")) in high_value_types for a in out_attachments):
        combined = "note_value_documents"
    return {
        "attachments": out_attachments,
        "combined_attention_hint": combined,
        "attachment_count": int(attachment_intelligence.get("attachment_count") or 0),
        "significant_hint": bool(attachment_intelligence.get("has_significant_attachments")),
    }


def build_prior_thread_context_envelope(
    snapshot: dict[str, Any],
    existing_thread_memory: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bounded prior-thread context for first-pass reasoning (from Daszek + snapshot)."""
    tm = build_thread_memory(
        snapshot,
        intake_result={},
        case_link_result={},
        business_result={},
        existing_thread_memory=existing_thread_memory,
    )
    versions = list(tm.get("latest_attachment_versions") or [])
    return {
        "prior_thread_memory_pl": _compact(str(tm.get("canonical_thread_summary") or ""), 520),
        "open_questions": [ _compact(str(q), 200) for q in (tm.get("unresolved_questions") or [])[:6]],
        "last_decision": _compact(str(tm.get("last_decision") or ""), 240),
        "commitments_made": [_compact(str(c), 200) for c in (tm.get("commitments_made") or [])[:6]],
        "key_facts_so_far": [_compact(str(f), 200) for f in (tm.get("key_facts_so_far") or [])[:8]],
        "thread_state_hint": str(tm.get("thread_state") or "unknown"),
        "latest_attachment_versions_summary": [_compact(str(x), 120) for x in versions[:6]],
        "thread_id": str(tm.get("thread_id") or ""),
        "message_count": int(tm.get("message_count") or 0),
    }


def enrich_snapshot_for_inference(snapshot: dict[str, Any], stage_config: dict[str, Any]) -> dict[str, Any]:
    """
    Single seam: hydrate attachment intelligence once, build bounded envelopes, attach to snapshot.

    Mutates snapshot under snapshot['inference_enrichment'] and stores full attachment intel in
    stage_config['cached_attachment_intelligence'] for reuse by build_case_intelligence_layer.
    """
    att_fetcher = stage_config.get("attachment_fetcher")
    att_max = int(stage_config.get("attachment_max_bytes") or 8_000_000)

    att_intel = build_attachment_intelligence(
        snapshot,
        intake_result=None,
        case_link_result=None,
        attachment_fetcher=att_fetcher if callable(att_fetcher) else None,
        attachment_max_bytes=att_max,
    )
    stage_config["cached_attachment_intelligence"] = att_intel

    att_env = build_attachment_evidence_envelope(att_intel)
    thread_env = build_prior_thread_context_envelope(snapshot, stage_config.get("existing_thread_memory"))

    enrichment = {
        "version": "1",
        "attachment_evidence_envelope": att_env,
        "prior_thread_context_envelope": thread_env,
        "payload_budget_notes": {
            "max_attachment_evidence_chars": _env_float("INFERENCE_ATTACHMENT_ENVELOPE_CHAR_BUDGET", 4500),
        },
    }
    snapshot["inference_enrichment"] = enrichment
    return enrichment


def envelopes_for_telemetry(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Lightweight flags for run summary (no secrets)."""
    inf = snapshot.get("inference_enrichment") if isinstance(snapshot.get("inference_enrichment"), dict) else {}
    att = inf.get("attachment_evidence_envelope") if isinstance(inf.get("attachment_evidence_envelope"), dict) else {}
    th = inf.get("prior_thread_context_envelope") if isinstance(inf.get("prior_thread_context_envelope"), dict) else {}
    att_count = int(att.get("attachment_count") or 0)
    att_in_prompt = bool(att.get("attachments"))
    thread_nonempty = bool(
        str(th.get("prior_thread_memory_pl") or "").strip()
        or (th.get("open_questions") or [])
        or (th.get("key_facts_so_far") or [])
    )
    return {
        "first_pass_attachment_envelope": att_in_prompt,
        "first_pass_thread_context_envelope": thread_nonempty,
        "attachment_envelope_items": len(att.get("attachments") or []) if att_in_prompt else 0,
        "approx_enrichment_json_chars": len(json.dumps(inf, ensure_ascii=False)) if inf else 0,
    }
