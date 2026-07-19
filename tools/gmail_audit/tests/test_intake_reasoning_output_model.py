"""Smoke: intake_reasoning stage wired to output_model with fallback envelope."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from central_llm_stage import run_central_structured_stage
from context_assembler import AssembledContext
from llm_contracts.intake_reasoning import IntakeReasoningResult
from tests.test_central_llm_stage import _minimal_settings


def _minimal_intake_v1_payload() -> dict:
    return {
        "schema_version": "1.0",
        "source": {"channel": "gmail", "mailbox": "inbox", "observed_at": "2026-05-29T10:00:00+00:00"},
        "message": {
            "message_id": "m1",
            "date": "2026-05-29T10:00:00+00:00",
            "sender": "client@example.com",
            "to": ["sales@topinstal.pl"],
            "subject": "Pompa ciepla",
            "has_attachments": False,
        },
        "thread": {
            "thread_id": "t1",
            "thread_position": "new_thread",
            "is_reply_or_forward": False,
            "thread_summary": "Lead HVAC",
        },
        "business_area": "sales",
        "primary_signal": {
            "code": "lead_inquiry",
            "name": "Lead",
            "description": "Zapytanie",
            "business_significance": "Nowy lead",
        },
        "secondary_signals": [],
        "case_assessment": {
            "case_family": "lead_opportunity",
            "is_new_case": True,
            "state_detected": "new",
            "state_change": {"detected": False},
        },
        "decision": {"action": "create_case", "action_rationale": "Nowy lead"},
        "priority": "medium",
        "confidence": {
            "signal_confidence": 0.7,
            "case_link_confidence": 0.0,
            "decision_confidence": 0.6,
            "extraction_confidence": 0.5,
        },
        "review": {"required": False, "flags": []},
        "reason": "Lead HVAC",
        "extracted_data": {
            "entities": {},
            "dates": [],
            "amounts": [],
            "references": {},
            "deadlines": [],
        },
    }


def test_intake_reasoning_output_model_validates() -> None:
    settings = _minimal_settings()
    payload = _minimal_intake_v1_payload()
    fake_stage = {
        "stage_name": "intake_reasoning",
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
                stage_name="intake_reasoning",
                task_instructions="intake",
                prompt_input={"x": 1},
                query_text="pompa",
                json_schema={},
                schema_name="intake_output_v1",
                output_model=IntakeReasoningResult,
            )
    assert out is not None
    assert out["parse_status"] == "pydantic_validated"
    parsed = IntakeReasoningResult.model_validate(out["response_json"])
    assert isinstance(parsed, IntakeReasoningResult)
    assert parsed.schema_version == "1.0"


def test_intake_reasoning_output_model_failure_returns_envelope() -> None:
    settings = _minimal_settings()
    bad_payload = _minimal_intake_v1_payload()
    del bad_payload["confidence"]
    fake_stage = {
        "stage_name": "intake_reasoning",
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
                stage_name="intake_reasoning",
                task_instructions="intake",
                prompt_input={"x": 1},
                query_text="pompa",
                json_schema={},
                schema_name="intake_output_v1",
                output_model=IntakeReasoningResult,
            )
    assert out is not None
    assert out["parse_status"] == "pydantic_validated"
    parsed = IntakeReasoningResult.model_validate(out["response_json"])
    assert isinstance(parsed, IntakeReasoningResult)
    assert parsed.confidence.signal_confidence >= 0.0
