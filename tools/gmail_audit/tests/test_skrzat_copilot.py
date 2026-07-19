"""Tests for skrzat_copilot (Fala C2)."""



from __future__ import annotations



import json

import sys

from pathlib import Path

from unittest.mock import patch



TOOL_DIR = Path(__file__).resolve().parent.parent

if str(TOOL_DIR) not in sys.path:

    sys.path.insert(0, str(TOOL_DIR))



from context_assembler import AssembledContext

from skrzat_copilot import resolve_skrzat_answer, run_skrzat_llm_answer

from skrzat_runtime import answer_case_question

from tests.test_central_llm_stage import _minimal_settings

from tests.test_skrzat_runtime import _trays





def _assembled_dict() -> dict:

    return {

        "company_context": "TOP-INSTAL",

        "assembled_at": "2026-05-30T12:00:00Z",

        "case_id_used": "case_skrzat_1",

    }





def test_resolve_skrzat_deterministic_includes_context_audit() -> None:

    settings = _minimal_settings(skrzat_answer_mode="deterministic")

    with patch("skrzat_copilot.assemble_skrzat_context_audit", return_value=_assembled_dict()):

        envelope = resolve_skrzat_answer(

            settings=settings,

            context_tray_set=_trays(),

            question="Czego brakuje?",

            mode="investigate",

            query_text="Czego brakuje?",

        )

    assert envelope["schema_version"] == "conversation_answer_envelope.v1"

    assert envelope["read_only"] is True

    assert envelope["action_allowed"] is False

    audit = envelope["context_audit"]

    assert audit["answer_mode"] == "deterministic"

    assert audit["parse_status"] == "deterministic"

    assert audit["stage_name"] == "skrzat_copilot"

    assert audit["assembled_context"]["case_id_used"] == "case_skrzat_1"
    metrics = envelope.get("quality_metrics") or {}
    assert metrics.get("schema_version") == "projection_quality_metrics.v1"
    assert metrics.get("skrzat_answer_has_evidence") is True


def test_resolve_skrzat_llm_pydantic_validated() -> None:

    settings = _minimal_settings(skrzat_answer_mode="llm")

    llm_payload = {

        "answer_text": "Brakuje adresu montazu wedlug tacki gaps.",

        "evidence_refs": [{"source_id": "gmail:m1", "summary": "Customer message"}],

        "gap_refs": [{"summary": "Missing address"}],

        "conflict_refs": [{"summary": "Power mismatch"}],

        "warnings": [],

        "operator_caution_pl": "",

    }

    fake_stage = {

        "stage_name": "skrzat_copilot",

        "response_text": json.dumps(llm_payload, ensure_ascii=False),

        "parse_status": "pydantic_validated",

        "assembled_context": _assembled_dict(),

        "request_meta": {"llm_selected_provider": "groq"},

        "model_name": "openai/gpt-oss-120b",

        "attempt_count": 1,

    }

    with patch("skrzat_copilot.assemble_skrzat_context_audit", return_value=_assembled_dict()):

        with patch("skrzat_copilot.run_central_structured_stage", return_value=fake_stage):

            envelope = resolve_skrzat_answer(

                settings=settings,

                context_tray_set=_trays(),

                question="Czego brakuje?",

                mode="investigate",

                query_text="Czego brakuje?",

            )

    assert envelope["context_audit"]["answer_mode"] == "llm"

    assert envelope["context_audit"]["parse_status"] == "pydantic_validated"

    assert "Missing address" in envelope["answer_text"] or envelope["gaps"]





def test_resolve_skrzat_llm_fallback_to_deterministic() -> None:

    settings = _minimal_settings(skrzat_answer_mode="llm")

    with patch("skrzat_copilot.assemble_skrzat_context_audit", return_value=_assembled_dict()):

        with patch("skrzat_copilot.run_central_structured_stage", return_value=None):

            envelope = resolve_skrzat_answer(

                settings=settings,

                context_tray_set=_trays(),

                question="Status?",

                mode="ask",

                query_text="Status?",

            )

    deterministic = answer_case_question(_trays(), question="Status?", mode="ask")

    assert envelope["context_audit"]["answer_mode"] == "deterministic_fallback"

    assert envelope["context_audit"]["parse_status"] == "fallback"

    assert envelope["answer_text"] == deterministic["answer_text"]





def test_run_skrzat_llm_answer_pydantic_failed_fallback() -> None:

    settings = _minimal_settings()

    fake_stage = {

        "stage_name": "skrzat_copilot",

        "response_text": "{}",

        "parse_status": "pydantic_failed",

        "assembled_context": _assembled_dict(),

        "request_meta": {"pydantic_errors": ["missing answer_text"]},

    }

    with patch("skrzat_copilot.run_central_structured_stage", return_value=fake_stage):

        envelope, meta = run_skrzat_llm_answer(

            settings=settings,

            context_tray_set=_trays(),

            question="Czego brakuje?",

            mode="investigate",

            query_text="Czego brakuje?",

            assembled_context=_assembled_dict(),

        )

    assert meta["parse_status"] == "fallback"

    assert meta["answer_mode"] == "deterministic_fallback"

    assert envelope["read_only"] is True





def test_skrzat_unknown_mode_unchanged_in_runtime() -> None:

    answer = answer_case_question(_trays(), question="Status?", mode="act")

    assert answer["mode"] == "ask"

    assert any("unsupported_mode" in str(w.get("warning_code", "")) for w in answer["warnings"])
