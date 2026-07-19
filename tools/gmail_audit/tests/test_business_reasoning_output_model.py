"""Smoke: business_reasoning stage wired to output_model with fallback envelope."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from central_llm_stage import run_central_structured_stage
from llm_contracts.business_reasoning import BusinessReasoningResult
from tests.test_central_llm_stage import _minimal_settings
from context_assembler import AssembledContext


def test_business_reasoning_output_model_validates() -> None:
    settings = _minimal_settings()
    payload = {
        "business_interpretation": "ok",
        "business_area": "lead",
        "customer_state_guess": "interested",
        "recommended_next_action": "wait",
        "recommended_action_reason": "r",
        "missing_information": [],
        "risks": [],
        "urgency": "normal",
        "operator_note": "n",
        "confidence": {"business_confidence": 0.5, "action_confidence": 0.6},
    }
    fake_stage = {
        "stage_name": "business_reasoning",
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
                stage_name="business_reasoning",
                task_instructions="reason",
                prompt_input={"x": 1},
                query_text="lead",
                json_schema={},  # schema is not used in this mocked run_structured_stage path
                schema_name="business_reasoning_v1",
                output_model=BusinessReasoningResult,
            )
    assert out is not None
    assert out["parse_status"] == "pydantic_validated"
    parsed = BusinessReasoningResult.model_validate(out["response_json"])
    assert isinstance(parsed, BusinessReasoningResult)


def test_business_reasoning_output_model_failure_returns_envelope() -> None:
    settings = _minimal_settings()
    bad_payload = {
        "business_interpretation": "ok",
        "business_area": "lead",
        "customer_state_guess": "interested",
        "recommended_next_action": "wait",
        "recommended_action_reason": "r",
        "missing_information": [],
        "risks": [],
        "urgency": "normal",
        "operator_note": "n",
        # confidence missing -> ValidationError
    }
    fake_stage = {
        "stage_name": "business_reasoning",
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
                stage_name="business_reasoning",
                task_instructions="reason",
                prompt_input={"x": 1},
                query_text="lead",
                json_schema={},
                schema_name="business_reasoning_v1",
                output_model=BusinessReasoningResult,
            )
    assert out is not None
    assert out["parse_status"] == "pydantic_failed"
    errors = (out.get("request_meta") or {}).get("pydantic_errors") or []
    assert errors
    with pytest.raises(ValidationError):
        BusinessReasoningResult.model_validate(out["response_json"])


def test_business_reasoning_output_model_allows_missing_operator_note() -> None:
    settings = _minimal_settings()
    payload = {
        "business_interpretation": "ok",
        "business_area": "lead",
        "customer_state_guess": "interested",
        "recommended_next_action": "wait",
        "recommended_action_reason": "r",
        "missing_information": [],
        "risks": [],
        "urgency": "normal",
        "confidence": {"business_confidence": 0.5, "action_confidence": 0.6},
    }
    fake_stage = {
        "stage_name": "business_reasoning",
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
                stage_name="business_reasoning",
                task_instructions="reason",
                prompt_input={"x": 1},
                query_text="lead",
                json_schema={},
                schema_name="business_reasoning_v1",
                output_model=BusinessReasoningResult,
            )
    assert out is not None
    assert out["parse_status"] == "pydantic_validated"
    parsed = BusinessReasoningResult.model_validate(out["response_json"])
    assert parsed.operator_note is None
