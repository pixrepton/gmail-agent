"""Smoke: reply_drafter stage wired to output_model with fallback envelope."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from central_llm_stage import run_central_structured_stage
from context_assembler import AssembledContext
from llm_contracts.reply_draft import ReplyDraftResult
from tests.test_central_llm_stage import _minimal_settings


def test_reply_draft_output_model_validates() -> None:
    settings = _minimal_settings()
    payload = {
        "draft_enabled": True,
        "drafts": [
            {
                "variant": "short_operational",
                "subject_suggestion": "Re: oferta",
                "body": "Dzień dobry.",
                "goal": "respond_safely",
            }
        ],
        "do_not_send_reasons": [],
    }
    fake_stage = {
        "stage_name": "reply_drafter",
        "response_text": json.dumps(payload, ensure_ascii=False),
        "response_json": {},
        "request_meta": {"llm_selected_provider": "groq"},
        "model_name": "openai/gpt-oss-120b",
        "attempt_count": 1,
    }
    with patch("central_llm_stage.build_context_assembler") as mock_asm:
        mock_asm.return_value.assemble.return_value = AssembledContext(
            company_context="ctx",
            assembled_at="2026-05-24T00:00:00+00:00",
        )
        with patch("central_llm_stage.run_structured_stage", return_value=fake_stage):
            out = run_central_structured_stage(
                settings,
                stage_name="reply_drafter",
                task_instructions="draft",
                prompt_input={"x": 1},
                query_text="reply",
                json_schema={},
                schema_name="reply_draft_v1",
                output_model=ReplyDraftResult,
            )
    assert out is not None
    assert out["parse_status"] == "pydantic_validated"
    parsed = ReplyDraftResult.model_validate(out["response_json"])
    assert isinstance(parsed, ReplyDraftResult)


def test_reply_draft_output_model_failure_returns_envelope() -> None:
    settings = _minimal_settings()
    bad_payload = {"draft_enabled": True, "do_not_send_reasons": []}
    fake_stage = {
        "stage_name": "reply_drafter",
        "response_text": json.dumps(bad_payload, ensure_ascii=False),
        "response_json": {},
        "request_meta": {"llm_selected_provider": "groq"},
        "model_name": "openai/gpt-oss-120b",
        "attempt_count": 1,
    }
    with patch("central_llm_stage.build_context_assembler") as mock_asm:
        mock_asm.return_value.assemble.return_value = AssembledContext(
            company_context="ctx",
            assembled_at="2026-05-24T00:00:00+00:00",
        )
        with patch("central_llm_stage.run_structured_stage", return_value=fake_stage):
            out = run_central_structured_stage(
                settings,
                stage_name="reply_drafter",
                task_instructions="draft",
                prompt_input={"x": 1},
                query_text="reply",
                json_schema={},
                schema_name="reply_draft_v1",
                output_model=ReplyDraftResult,
            )
    assert out is not None
    assert out["parse_status"] == "pydantic_failed"
    assert (out.get("request_meta") or {}).get("pydantic_errors")
    with pytest.raises(ValidationError):
        ReplyDraftResult.model_validate(out["response_json"])
