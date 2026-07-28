"""Tests for central_llm_stage routing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from central_llm_stage import (
    anthropic_configured,
    merge_system_prompt,
    primary_llm_provider,
    run_central_structured_stage,
)
from config import Settings
from context_assembler import AssembledContext
from llm_client import TopInstalLLMError
from llm_contracts.signal_extraction import SignalExtractionResult


def _minimal_settings(**overrides: object) -> Settings:
    base = {
        "llm_backend": "groq",
        "openai_compat_base_url": "",
        "openai_compat_api_key": "",
        "groq_api_key": "gsk_test",
        "google_access_token": "",
        "google_client_id": "",
        "google_client_secret": "",
        "google_refresh_token": "",
        "google_token_endpoint": "https://oauth2.googleapis.com/token",
        "google_oauth_scopes": ("https://www.googleapis.com/auth/gmail.readonly",),
        "groq_model": "openai/gpt-oss-120b",
        "groq_native_model": "openai/gpt-oss-120b",
        "groq_base_url": "https://api.groq.com",
        "daszek_base_url": "",
        "daszek_login": "",
        "daszek_password": "",
        "daszek_v2_push_enabled": False,
        "case_guidance_enabled": False,
        "case_guidance_model": "openai/gpt-oss-120b",
        "case_guidance_remote_state_enabled": True,
        "anthropic_api_key": "",
        "anthropic_model": "claude-sonnet-4-20250514",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_anthropic_configured() -> None:
    assert not anthropic_configured(_minimal_settings())
    assert anthropic_configured(_minimal_settings(anthropic_api_key="sk-ant"))


def test_primary_llm_provider_groq_without_anthropic() -> None:
    assert primary_llm_provider(_minimal_settings()) == "groq"


def test_primary_llm_provider_anthropic_when_key_set() -> None:
    assert primary_llm_provider(_minimal_settings(anthropic_api_key="sk-ant")) == "anthropic"


def test_merge_system_prompt_appends_stage_instructions() -> None:
    assembled = AssembledContext(company_context="Firma", assembled_at="2026-05-24T00:00:00+00:00")
    with patch("central_llm_stage.ContextAssembler") as mock_cls:
        mock_cls.return_value.to_system_prompt.return_value = "SYSTEM"
        prompt = merge_system_prompt(assembled, "Do HVAC extraction.")
    assert "SYSTEM" in prompt
    assert "Do HVAC extraction." in prompt


def test_run_central_structured_stage_uses_groq_when_no_anthropic_key() -> None:
    settings = _minimal_settings()
    groq_json = json.dumps({"hvac_intent": "quote", "raw_geographic_signal": "Jaworzno"})
    fake_stage = {
        "stage_name": "signal_extraction",
        "response_text": groq_json,
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
        with patch("central_llm_stage.run_structured_stage", return_value=fake_stage) as mock_run:
            out = run_central_structured_stage(
                settings,
                stage_name="signal_extraction",
                task_instructions="extract",
                prompt_input={"message": "test"},
                query_text="pompa ciepla",
                json_schema=SignalExtractionResult.model_json_schema(),
                schema_name="signal_extraction_v1",
                output_model=SignalExtractionResult,
                temperature=0.37,
                correlation_id="msg-central-temperature",
            )
    assert out is not None
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["temperature"] == 0.37
    assert mock_run.call_args.kwargs["correlation_id"] == "msg-central-temperature"
    assert out["parse_status"] == "pydantic_validated"
    assert out["response_json"]["hvac_intent"] == "quote"
    SignalExtractionResult.model_validate(out["response_json"])
    assert "assembled_context" in out
    assert out["central_llm_provider"] == "groq"


def test_groq_invalid_json_with_output_model_returns_pydantic_failed() -> None:
    settings = _minimal_settings()
    fake_stage = {
        "stage_name": "signal_extraction",
        "response_text": "not json at all",
        "response_json": {},
        "request_meta": {},
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
                stage_name="signal_extraction",
                task_instructions="extract",
                prompt_input={"message": "test"},
                query_text="pompa",
                json_schema=SignalExtractionResult.model_json_schema(),
                schema_name="signal_extraction_v1",
                output_model=SignalExtractionResult,
            )
    assert out is not None
    assert out["parse_status"] == "pydantic_failed"
    assert (out.get("request_meta") or {}).get("pydantic_errors")


def test_run_central_structured_stage_applies_context_budget() -> None:
    settings = _minimal_settings()
    groq_json = json.dumps({"hvac_intent": "quote", "raw_geographic_signal": "Jaworzno"})
    fake_stage = {
        "stage_name": "business_reasoning",
        "response_text": groq_json,
        "response_json": {},
        "request_meta": {"llm_selected_provider": "groq"},
        "model_name": "openai/gpt-oss-120b",
        "attempt_count": 1,
    }
    oversized = AssembledContext(
        company_context="c" * 8000,
        relevant_chunks=[{"chunk_id": f"c{i}", "chunk_text": "t" * 1200, "score": 0.5} for i in range(8)],
        case_facts={"heated_area_m2": 140},
        assembled_at="2026-05-24T00:00:00+00:00",
    )
    with patch("central_llm_stage.build_context_assembler") as mock_asm:
        mock_asm.return_value.assemble.return_value = oversized
        with patch("central_llm_stage.run_structured_stage", return_value=fake_stage) as mock_run:
            out = run_central_structured_stage(
                settings,
                stage_name="business_reasoning",
                task_instructions="reason",
                prompt_input={"message": "test"},
                query_text="pompa ciepla",
                json_schema={"type": "object"},
                schema_name="business_reasoning_v1",
            )
    assert out is not None
    mock_run.assert_called_once()
    assembled = out.get("assembled_context") or {}
    budget = assembled.get("context_budget") or {}
    assert budget.get("applied") is True
    assert len(assembled.get("relevant_chunks") or []) <= 3
    system_arg = mock_run.call_args.kwargs.get("instructions")
    assert system_arg is not None
    assert len(system_arg) < 8000 + 8000


def test_anthropic_path_validates_pydantic_output_model() -> None:
    settings = _minimal_settings(anthropic_api_key="sk-ant-test")
    payload = {"hvac_intent": "install", "raw_geographic_signal": "Katowice"}
    with patch("central_llm_stage.build_context_assembler") as mock_asm:
        mock_asm.return_value.assemble.return_value = AssembledContext(
            company_context="ctx",
            assembled_at="2026-05-24T00:00:00+00:00",
        )
        with patch(
            "central_llm_stage._call_anthropic_raw_text",
            return_value=json.dumps(payload),
        ) as mock_anthropic:
            out = run_central_structured_stage(
                settings,
                stage_name="signal_extraction",
                task_instructions="extract",
                prompt_input={"message": "test"},
                query_text="pompa",
                json_schema=SignalExtractionResult.model_json_schema(),
                schema_name="signal_extraction_v1",
                output_model=SignalExtractionResult,
                temperature=0.21,
            )
    assert out is not None
    assert mock_anthropic.call_args.kwargs["temperature"] == 0.21
    assert out["request_meta"]["llm_temperature_requested"] == 0.21
    assert out["request_meta"]["llm_determinism_guaranteed"] is False
    assert out["parse_status"] == "pydantic_validated"
    assert out["central_llm_provider"] == "anthropic"
    assert out["response_json"]["hvac_intent"] == "install"


def test_anthropic_failure_preserves_temperature_and_correlation_on_groq_fallback() -> None:
    settings = _minimal_settings(anthropic_api_key="sk-ant-test")
    groq_json = json.dumps({"hvac_intent": "quote", "raw_geographic_signal": "Jaworzno"})
    fake_stage = {
        "stage_name": "signal_extraction",
        "response_text": groq_json,
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
        with patch(
            "central_llm_stage._call_anthropic_raw_text",
            side_effect=TopInstalLLMError("provider unavailable"),
        ):
            with patch("central_llm_stage.run_structured_stage", return_value=fake_stage) as mock_run:
                out = run_central_structured_stage(
                    settings,
                    stage_name="signal_extraction",
                    task_instructions="extract",
                    prompt_input={"message": "test"},
                    query_text="pompa",
                    json_schema=SignalExtractionResult.model_json_schema(),
                    schema_name="signal_extraction_v1",
                    output_model=SignalExtractionResult,
                    temperature=0.23,
                    correlation_id="msg-anthropic-fallback",
                    max_retries=0,
                )

    assert out is not None
    assert mock_run.call_args.kwargs["temperature"] == 0.23
    assert mock_run.call_args.kwargs["correlation_id"] == "msg-anthropic-fallback"
