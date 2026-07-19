"""Tests for central ContextAssembler."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from context_assembler import (
    AssembledContext,
    ContextAssembler,
    ContextBudgetLimits,
    apply_context_token_budget,
    assembled_context_to_dict,
    default_company_context_path,
    estimate_context_tokens,
)

COMPANY_CONTEXT_SAMPLE = "# TOP-INSTAL — Kontekst dla AI\n\nOZC is sacred.\n"


def _case_loader(case_id: str, query_text: str, max_chunks: int) -> tuple[dict, list]:
    _ = query_text
    facts = {"heated_area_m2": 140, "case_ref": case_id}
    chunks = [
        {"chunk_id": "c1", "chunk_text": "Panasonic 9 kW", "score": 0.91},
        {"chunk_id": "c2", "chunk_text": "Buffer 100L", "score": 0.5},
    ][:max_chunks]
    return facts, chunks


def test_default_company_context_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "company_context.md"
    custom.write_text(COMPANY_CONTEXT_SAMPLE, encoding="utf-8")
    monkeypatch.setenv("TOPINSTAL_COMPANY_CONTEXT_PATH", str(custom))
    assert default_company_context_path() == custom


def test_default_company_context_path_module_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOPINSTAL_COMPANY_CONTEXT_PATH", raising=False)
    expected = Path(__file__).resolve().parent.parent / "data" / "company_context.md"
    assert default_company_context_path() == expected


def test_assembled_context_defaults() -> None:
    ctx = AssembledContext()
    assert ctx.version == "1.0"
    assert ctx.company_context == ""
    assert ctx.case_facts == {}
    assert ctx.relevant_chunks == []
    assert ctx.chunks_count == 0
    assert ctx.facts_count == 0


def test_assemble_loads_company_context_without_case() -> None:
    assembler = ContextAssembler(company_context_path=Path("ignored.md"))
    with patch.object(assembler, "_read_company_context", return_value=COMPANY_CONTEXT_SAMPLE):
        ctx = assembler.assemble("query o OZC", engagement_id="eng-1")
    assert "OZC is sacred" in ctx.company_context
    assert ctx.engagement_id == "eng-1"
    assert ctx.case_id_used == ""
    assert ctx.facts_count == 0
    assert ctx.chunks_count == 0
    assert ctx.assembled_at.endswith("+00:00") or "T" in ctx.assembled_at


def test_assemble_with_case_loader_respects_max_chunks() -> None:
    assembler = ContextAssembler(
        company_context_path=Path("ignored.md"),
        case_loader=_case_loader,
    )
    with patch.object(assembler, "_read_company_context", return_value=COMPANY_CONTEXT_SAMPLE):
        ctx = assembler.assemble("dobór pompy", case_id="case-42", max_chunks=1)
    assert ctx.case_id_used == "case-42"
    assert ctx.facts_count == 2
    assert ctx.chunks_count == 1
    assert ctx.case_facts["heated_area_m2"] == 140


def test_assemble_reads_company_context_from_file() -> None:
    from context_assembler import default_company_context_path

    path = default_company_context_path()
    if not path.is_file():
        pytest.skip("company_context.md not present (set TOPINSTAL_COMPANY_CONTEXT_PATH)")
    assembler = ContextAssembler(company_context_path=path)
    ctx = assembler.assemble("test")
    assert "TOP-INSTAL" in ctx.company_context


def test_assemble_open_mock_syntax() -> None:
    """Regression: mock_open via new_callable — not mock_open(...) directly in patch()."""
    fake_path = Path("company_context.md")
    assembler = ContextAssembler(company_context_path=fake_path)
    with patch.object(Path, "is_file", return_value=True):
        with patch("builtins.open", mock_open(read_data=COMPANY_CONTEXT_SAMPLE)):
            with patch.object(Path, "read_text", return_value=COMPANY_CONTEXT_SAMPLE):
                ctx = assembler.assemble("hvac lead")
    assert ctx.company_context.startswith("# TOP-INSTAL")


def test_to_system_prompt_includes_sections() -> None:
    assembler = ContextAssembler(company_context_path=Path("ignored.md"))
    ctx = AssembledContext(
        company_context="Firma HVAC",
        case_facts={"ozc_kw": 9.5},
        relevant_chunks=[{"chunk_text": "bufor 100L"}],
        engagement_id="eng-9",
        assembled_at="2026-05-24T12:00:00+00:00",
        case_id_used="case-1",
        chunks_count=1,
        facts_count=1,
    )
    prompt = assembler.to_system_prompt(ctx)
    assert "Firma HVAC" in prompt
    assert "ozc_kw" in prompt
    assert "bufor 100L" in prompt
    assert "Engagement ID: eng-9" in prompt
    assert "Case ID: case-1" in prompt


def test_assembled_context_to_dict_roundtrip() -> None:
    ctx = AssembledContext(company_context="x", facts_count=0, chunks_count=0)
    payload = assembled_context_to_dict(ctx)
    assert payload["version"] == "1.0"
    assert payload["company_context"] == "x"


def test_estimate_context_tokens_non_empty() -> None:
    assert estimate_context_tokens("one two three four") >= 4


def test_apply_context_token_budget_trims_oversized_chunks() -> None:
    limits = ContextBudgetLimits(max_chunks=2, max_chunk_chars=80, max_context_tokens=500)
    chunks = [
        {"chunk_id": "low", "chunk_text": "x" * 200, "score": 0.1},
        {"chunk_id": "high", "chunk_text": "y" * 200, "score": 0.9},
        {"chunk_id": "mid", "chunk_text": "z" * 200, "score": 0.5},
    ]
    ctx = AssembledContext(
        company_context="Firma",
        case_facts={f"fact_{index}": "v" * 300 for index in range(30)},
        relevant_chunks=chunks,
    )
    trimmed, meta = apply_context_token_budget(ctx, limits=limits, stage_name="business_reasoning")
    assert len(trimmed.relevant_chunks) <= 2
    assert all(len(str(c.get("chunk_text") or "")) <= 80 for c in trimmed.relevant_chunks)
    assert trimmed.relevant_chunks[0]["chunk_id"] == "high"
    assert meta["applied"] is True
    assert meta["dropped_chunks"] >= 1
    assert meta["dropped_facts"] >= 1


def test_assembled_context_to_dict_includes_context_budget() -> None:
    ctx = AssembledContext(company_context="x")
    budget = {"applied": True, "token_estimate": 42}
    payload = assembled_context_to_dict(ctx, context_budget=budget)
    assert payload["context_budget"]["token_estimate"] == 42
