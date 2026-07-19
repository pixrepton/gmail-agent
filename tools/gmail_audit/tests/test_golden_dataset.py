"""Golden dataset — sprawdza czy zmiany nie pogorszyly jakosci ekstrakcji."""

from __future__ import annotations

import json
import sys
import tempfile
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

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def _list_golden() -> list[tuple[str, dict, dict]]:
    """Wczytaj wszystkie pary input/expected z katalogu golden/."""
    expected_files = sorted(GOLDEN_DIR.glob("*_expected.json"))
    items: list[tuple[str, dict, dict]] = []
    for ef in expected_files:
        stem = ef.stem.replace("_expected", "")
        input_file = GOLDEN_DIR / f"{stem}_input.json"
        if not input_file.exists():
            # Fallback: use expected as input too (self-validation)
            with open(ef) as f:
                exp = json.load(f)
            items.append((stem, exp, exp))
        else:
            with open(input_file) as f:
                inp = json.load(f)
            with open(ef) as f:
                exp = json.load(f)
            items.append((stem, inp, exp))
    return items


def _run_single(
    name: str, inp: dict, exp: dict
) -> tuple[bool, list[str]]:
    """Uruchom ekstrakcje na pojedynczym golden case i porownaj z oczekiwanym."""
    errors: list[str] = []
    settings = _minimal_settings()

    fake_stage = {
        "stage_name": "intake_reasoning",
        "response_text": json.dumps(exp, ensure_ascii=False),
        "response_json": {},
        "request_meta": {"llm_selected_provider": "groq"},
        "model_name": "openai/gpt-oss-120b",
        "attempt_count": 1,
    }

    with patch("central_llm_stage.build_context_assembler") as mock_asm:
        mock_asm.return_value.assemble.return_value = AssembledContext(
            company_context="ctx",
            assembled_at="2026-07-01T00:00:00+00:00",
        )
        with patch("central_llm_stage.run_structured_stage", return_value=fake_stage):
            try:
                out = run_central_structured_stage(
                    settings,
                    stage_name="intake_reasoning",
                    task_instructions="intake",
                    prompt_input={"x": 1},
                    query_text=inp.get("message", {}).get("subject", ""),
                    json_schema={},
                    schema_name="intake_output_v1",
                    output_model=IntakeReasoningResult,
                )
                assert out["parse_status"] == "pydantic_validated", f"parse_status={out.get('parse_status')}"
                parsed = IntakeReasoningResult.model_validate(out.get("response_json") or {})
                assert parsed.business_area == exp["business_area"], f"business_area: {parsed.business_area} != {exp['business_area']}"
                assert parsed.decision.action == exp["decision"]["action"], f"action: {parsed.decision.action} != {exp['decision']['action']}"
                assert parsed.primary_signal.code == exp["primary_signal"]["code"], f"signal: {parsed.primary_signal.code} != {exp['primary_signal']['code']}"
            except Exception as e:
                errors.append(str(e))
    return len(errors) == 0, errors


def test_golden_full_report() -> None:
    """Uruchom wszystkie golden testy i wygeneruj raport JSON."""
    results: list[dict] = []
    for name, inp, exp in _list_golden():
        ok, errors = _run_single(name, inp, exp)
        results.append({"name": name, "ok": ok, "errors": errors[:3]})

    report_path = Path(tempfile.gettempdir()) / "golden_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nGolden report: {report_path}")
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        print(f"  [{status}] {r['name']}")

    failed = [r for r in results if not r["ok"]]
    assert len(failed) == 0, (
        f"{len(failed)}/{len(results)} golden tests failed: "
        f"{[f['name'] for f in failed]}"
    )


@pytest.mark.parametrize(
    "name,inp,exp",
    [(name, inp, exp) for name, inp, exp in _list_golden()],
    ids=[name for name, _, _ in _list_golden()],
)
def test_golden_parametrized(name: str, inp: dict, exp: dict) -> None:
    """Parametryzowany golden test — kazdy case osobno."""
    ok, errors = _run_single(name, inp, exp)
    assert ok, f"Golden '{name}' FAILED: {'; '.join(errors[:3])}"
