"""Case OS full product finalization tests (P1+–P6)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app
from case_os_platform import merge_decision_view_with_pipeline_proposals, resolve_feed_action_proposals
from mailbox_memory_models import CaseContextPack
from skrzat_rag_advisory import build_rag_advisory_slice


def test_p1_rag_advisory_slice_from_assembled_context() -> None:
    assembled = {
        "case_id_used": "case_rag_1",
        "company_context": "TOP-INSTAL oferuje pompy ciepła.",
        "relevant_chunks": [{"chunk_id": "c1", "chunk_text": "regulamin"}],
        "case_facts": {"city": "Warszawa"},
    }
    pack = {"case_id": "case_rag_1", "pack_build": "case_context_pack.vnext.3"}
    slice_ = build_rag_advisory_slice(assembled, case_context_pack=pack)
    assert slice_["schema_version"] == "case_os.rag_advisory_slice.v1"
    assert slice_["company_knowledge_chunks"] == 1
    assert slice_["case_facts_count"] == 1
    assert slice_["boundary"] == "D1_advisory_only"
    # No caller ever wired the cross-repo core_chat.unified_pipeline import (it always
    # returned {} silently, since gmail-agent's PYTHONPATH never includes the separate
    # rag-chat-asystent repo) -- removed rather than left as dead, always-empty output.
    assert "unified_pipeline" not in slice_


def test_p2_merge_decision_view_includes_pipeline_proposals() -> None:
    proposals = resolve_feed_action_proposals(
        vnext_proposals=[],
        case_intelligence={
            "action_proposals_v2": [
                {
                    "proposal_id": "prop-e2e",
                    "action_type": "prepare_reply_draft",
                    "summary_pl": "Odpowiedz klientowi",
                    "status": "proposed",
                }
            ]
        },
        decision_view={"why_pl": "Brakuje mocy urządzenia", "policy_decision_id": "pd-1"},
    )
    dv, merged = merge_decision_view_with_pipeline_proposals({"headline_co_pl": "Sprawa"}, proposals)
    assert len(merged) == 1
    assert dv["action_proposals"][0]["proposal_id"] == "prop-e2e"
    assert dv["why_pl"] == "Brakuje mocy urządzenia"
    assert dv["proposal_summary_pl"]


class _Runtime:
    def __init__(self) -> None:
        self.pack = CaseContextPack(
            case_id="case_product_1",
            active_facts=[{"fact_id": "fact-1", "fact_key": "status", "value": "open", "source_ref": "gmail:msg-1"}],
            conflicting_facts=[],
            completeness_gaps=["Missing protocol"],
            source_refs=[{"type": "gmail_message", "message_id": "msg-1", "source_ref": "gmail:msg-1"}],
        )

    def get_context_pack(self, *, case_id: str = "", message_id: str = "", query_text: str = "") -> CaseContextPack:
        if case_id == self.pack.case_id:
            return self.pack
        return CaseContextPack(case_id="")


def test_p1_skrzat_returns_rag_advisory_and_lineage() -> None:
    fake_assembled = {
        "case_id_used": "case_product_1",
        "company_context": "Firma",
        "relevant_chunks": [],
        "case_facts": {"status": "open"},
    }
    app = create_app(runtime_provider=lambda: _Runtime(), cohort_reader=lambda _r: None, registry_provider=lambda: None)
    client = TestClient(app)
    os.environ["NODE_B_REGISTRY_TOKEN"] = "test-registry-token"
    try:
        with patch("skrzat_copilot.assemble_skrzat_context_audit", return_value=fake_assembled):
            resp = client.post(
                "/cases/case_product_1/skrzat/ask",
                json={"question": "Czego brakuje?", "mode": "ask"},
                headers={"Authorization": "Bearer test-registry-token"},
            )
    finally:
        os.environ.pop("NODE_B_REGISTRY_TOKEN", None)
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("context_pack_lineage", {}).get("case_id") == "case_product_1"
    rag = body.get("rag_advisory") or {}
    assert rag.get("schema_version") == "case_os.rag_advisory_slice.v1"
    assert rag.get("case_id") == "case_product_1"


def test_p3_generator_resolves_engagement_id() -> None:
    workspace = Path(__file__).resolve().parents[4]
    gen = workspace / "top-instal-generator" / "core" / "application" / "GenerateOfferDocumentUseCase.php"
    text = gen.read_text(encoding="utf-8")
    assert "resolve_engagement_id" in text
    assert "engagementId" in text
