"""AI-OS 4.3 — PDF readiness honesty (cieplo dedup + diagram contract)."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.agent_reconcile import (
    _check_cieplo_dedup,
    _cieplo_pipeline_verified_complete,
    _cieplo_readiness_from_signal,
)
from signal_contract import build_canonical_signal


def _signal_with_payload(payload: dict) -> object:
    payload = dict(payload)
    payload.setdefault("source_repo", "cieplo-orchestrator")
    return build_canonical_signal(
        signal_kind="os_event",
        source_kind="os_event",
        source_ref={"source_repo": "cieplo-orchestrator"},
        observed_at="2026-08-06T12:00:00Z",
        signal_summary_pl="cieplo workflow",
        payload=payload,
    )


def test_cieplo_dedup_ignores_keyword_without_readiness_fields() -> None:
    signal = _signal_with_payload({"event_type": "cieplo.workflow.pdf_ready"})
    intake = {
        "message": {
            "source_repo": "cieplo-orchestrator",
            "subject": "pdf_ready notification",
            "body_text": "pdf gotowy do wysylki",
        }
    }
    result = _check_cieplo_dedup(signal, intake)
    assert result["skip"] is False
    assert result["reason"] == "cieplo_in_progress_or_unverified"


def test_cieplo_dedup_skips_only_verified_pdf_ready() -> None:
    signal = _signal_with_payload(
        {
            "event_type": "cieplo.workflow.pdf_ready",
            "payload_extra": {
                "document_readiness_status": "READY",
                "document_actual_format": "pdf",
                "artifact_verified": True,
            },
        }
    )
    result = _check_cieplo_dedup(signal, {})
    assert result["skip"] is True
    assert result["reason"] == "cieplo_already_completed"


def test_cieplo_degraded_never_counts_as_verified_complete() -> None:
    readiness = _cieplo_readiness_from_signal(
        _signal_with_payload(
            {
                "event_type": "cieplo.workflow.document_degraded",
                "workflow_state": "DOCUMENT_READY_DEGRADED",
                "payload_extra": {
                    "document_readiness_status": "DEGRADED",
                    "document_actual_format": "docx",
                    "artifact_verified": False,
                },
            }
        ),
        {},
    )
    assert _cieplo_pipeline_verified_complete(readiness) is False


def test_daszek_diagram_degraded_not_linked_to_email_sent() -> None:
    manifest_path = (
        Path(__file__).resolve().parents[4]
        / "daszek"
        / "public"
        / "system-diagrams-manifest.js"
    )
    text = manifest_path.read_text(encoding="utf-8")
    assert "DOCUMENT_READY_DEGRADED --> EMAIL_SENT" not in text
    assert "DOCUMENT_READY_DEGRADED --> FAILED_RETRYABLE" in text
