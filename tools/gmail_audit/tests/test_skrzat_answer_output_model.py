"""Tests for SkrzatAnswerResult Pydantic contract (Fala C2)."""



from __future__ import annotations



import sys

from pathlib import Path



import pytest

from pydantic import ValidationError



TOOL_DIR = Path(__file__).resolve().parent.parent

if str(TOOL_DIR) not in sys.path:

    sys.path.insert(0, str(TOOL_DIR))



from llm_contracts.skrzat_answer import SkrzatAnswerResult





def test_skrzat_answer_output_model_validates_minimal() -> None:

    model = SkrzatAnswerResult.model_validate({})

    assert model.answer_text == ""

    assert model.evidence_refs == []

    assert model.gap_refs == []

    assert model.conflict_refs == []

    assert model.warnings == []

    assert model.operator_caution_pl == ""





def test_skrzat_answer_output_model_accepts_full_payload() -> None:

    payload = {

        "answer_text": "Brakuje adresu montazu.",

        "evidence_refs": [{"source_id": "gmail:m1", "summary": "Klient pyta o serwis"}],

        "gap_refs": [{"summary": "Missing address"}],

        "conflict_refs": [],

        "warnings": ["slabe dowody"],

        "operator_caution_pl": "Zweryfikuj moc urzadzenia recznie.",

    }

    model = SkrzatAnswerResult.model_validate(payload)

    assert "adresu" in model.answer_text

    assert len(model.evidence_refs) == 1

    assert model.operator_caution_pl.startswith("Zweryfikuj")





def test_skrzat_answer_output_model_ignores_extra_fields() -> None:

    model = SkrzatAnswerResult.model_validate({"answer_text": "OK", "unknown_field": 99})

    assert model.answer_text == "OK"





def test_skrzat_answer_output_model_rejects_invalid_evidence_refs() -> None:

    with pytest.raises(ValidationError):

        SkrzatAnswerResult.model_validate({"evidence_refs": "not-a-list"})
