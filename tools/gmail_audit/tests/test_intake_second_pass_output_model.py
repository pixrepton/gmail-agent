"""Smoke: intake_second_pass stage wired to output_model with fallback envelope."""

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
from llm_contracts.intake_second_pass import IntakeSecondPassResult
from tests.test_central_llm_stage import _minimal_settings


def test_intake_second_pass_output_model_validates() -> None:
    settings = _minimal_settings()
    payload = {
        "schema_version": "1.0",
        "supplement_notes_pl": "ok",
        "suggested_review_escalation": False,
        "additional_review_flags": [],
        "evidence_assessment_pl": "fine",
    }
    fake_stage = {
        "stage_name": "intake_second_pass",
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
                stage_name="intake_second_pass",
                task_instructions="second pass",
                prompt_input={"x": 1},
                query_text="lead",
                json_schema={},
                schema_name="intake_second_pass_v1",
                output_model=IntakeSecondPassResult,
            )
    assert out is not None
    assert out["parse_status"] == "pydantic_validated"
    parsed = IntakeSecondPassResult.model_validate(out["response_json"])
    assert isinstance(parsed, IntakeSecondPassResult)


def test_intake_second_pass_output_model_failure_returns_envelope() -> None:
    settings = _minimal_settings()
    bad_payload = {
        "schema_version": "1.0",
        "supplement_notes_pl": "ok",
        # suggested_review_escalation missing -> ValidationError
        "additional_review_flags": [],
        "evidence_assessment_pl": "fine",
    }
    fake_stage = {
        "stage_name": "intake_second_pass",
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
                stage_name="intake_second_pass",
                task_instructions="second pass",
                prompt_input={"x": 1},
                query_text="lead",
                json_schema={},
                schema_name="intake_second_pass_v1",
                output_model=IntakeSecondPassResult,
            )
    assert out is not None
    assert out["parse_status"] == "pydantic_failed"
    errors = (out.get("request_meta") or {}).get("pydantic_errors") or []
    assert errors
    with pytest.raises(ValidationError):
        IntakeSecondPassResult.model_validate(out["response_json"])
