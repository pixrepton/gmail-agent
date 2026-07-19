"""Tests for email_personalization stage (Fala C1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from central_llm_stage import run_central_structured_stage
from context_assembler import AssembledContext
from email_personalizer import (
    allowed_price_tokens,
    run_email_personalization,
    verify_no_hallucinated_prices,
)
from llm_contracts.email_result import EmailPersonalizationResult
from tests.test_central_llm_stage import _minimal_settings


def test_email_personalization_output_model_validates() -> None:
    settings = _minimal_settings()
    payload = {
        "subject": "Twoja oferta Panasonic",
        "body": "Dzień dobry,\nw załączniku przesyłamy ofertę.",
        "tone_used": "professional",
    }
    fake_stage = {
        "stage_name": "email_personalization",
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
                stage_name="email_personalization",
                task_instructions="personalize",
                prompt_input={"offer_summary": {"gross_pln": 45000}},
                query_text="oferta pompy",
                json_schema={},
                schema_name="email_personalization_v1",
                output_model=EmailPersonalizationResult,
            )
    assert out is not None
    assert out["parse_status"] == "pydantic_validated"
    assert "assembled_context" in out


def test_price_guard_rejects_unknown_amount() -> None:
    allowed = allowed_price_tokens({"gross_pln": 45000})
    assert verify_no_hallucinated_prices("Kwota 45 000 PLN brutto.", allowed)
    assert not verify_no_hallucinated_prices("Promocja tylko 99 999 PLN.", allowed)
    assert verify_no_hallucinated_prices("Budynek z 2010 roku.", allowed)


def test_run_email_personalization_rejects_hallucinated_price() -> None:
    settings = _minimal_settings()
    offer = {"pricing": {"totals": {"gross": 45000}}}
    bad_payload = {
        "subject": "Oferta",
        "body": "Cena specjalna 99 999 PLN.",
        "tone_used": "professional",
    }
    fake_stage = {
        "stage_name": "email_personalization",
        "response_text": json.dumps(bad_payload, ensure_ascii=False),
        "response_json": {},
        "request_meta": {"llm_selected_provider": "groq"},
        "model_name": "openai/gpt-oss-120b",
        "attempt_count": 1,
    }
    with patch("email_personalizer.run_central_structured_stage", return_value=fake_stage):
        result = run_email_personalization(
            settings=settings,
            offer=offer,
            cieplo_url="https://cieplo.app/wynik/x",
            contact_email="biuro@topinstal.com.pl",
        )
    assert result["execution_metadata"]["parse_status"] == "fallback"
    assert result["execution_metadata"]["error"] == "price_hallucination_guard"
