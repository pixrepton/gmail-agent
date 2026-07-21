from __future__ import annotations

import json
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import eval_understanding_judge as judge_module  # noqa: E402
from eval_understanding_judge import (  # noqa: E402
    GROQ_PROVIDER,
    OPENAI_NATIVE_PROVIDER,
    OPENROUTER_PROVIDER,
    build_calibration_manifest,
    build_run_judge_manifest,
    build_judge_input,
    compare_runs,
    compare_with_human,
    judge_contract,
    normalize_judge_result,
    run_judge,
    select_judge_config,
    _sanitize_error,
)


def _case() -> dict:
    return {
        "id": "FU-05",
        "input": {"subject": "Re: Oferta", "body": "Wrocimy do tematu za miesiac."},
        "ground_truth": {"understanding": {"must": ["rozpoznanie odroczenia, nie odmowy ani akceptacji"]}},
    }


def _output() -> dict:
    return {"id": "FU-05", "understanding": {"summary_pl": "Klient odklada decyzje na okolo miesiac."}}


def test_judge_contract_freezes_one_provider_no_fallback() -> None:
    contract = judge_contract({"cases": [_case()]})

    assert contract["config"]["provider"] in {"anthropic", OPENAI_NATIVE_PROVIDER, OPENROUTER_PROVIDER, GROQ_PROVIDER}
    assert contract["config"]["model"]
    assert contract["config"]["temperature"] == 0.0
    assert contract["config"]["fallback"] == "none"


def test_select_judge_config_uses_native_openai_when_anthropic_missing(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENT_OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("AGENT_MODEL", "openai/gpt-4o-mini")

    config = select_judge_config()

    assert config.provider == OPENAI_NATIVE_PROVIDER
    assert config.model == "gpt-4o-mini"
    assert config.fallback == "none"


def test_select_judge_config_uses_openrouter_for_openrouter_key(monkeypatch) -> None:
    monkeypatch.setattr(judge_module, "_dotenv_value", lambda _path, _key: "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("AGENT_OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "openai/gpt-4o-mini")

    config = select_judge_config()

    assert config.provider == OPENROUTER_PROVIDER
    assert config.model == "openai/gpt-4o-mini"
    assert config.fallback == "none"


def test_select_judge_config_prefers_groq_before_openrouter(monkeypatch) -> None:
    monkeypatch.setattr(judge_module, "_dotenv_value", lambda _path, _key: "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")

    config = select_judge_config()

    assert config.provider == GROQ_PROVIDER
    assert config.model == "llama-3.3-70b-versatile"
    assert config.fallback == "none"


def test_judge_input_excludes_human_labels_and_old_scores() -> None:
    output = {
        **_output(),
        "understanding": {
            "summary_pl": "Klient odklada decyzje.",
            "schema_version": "understanding_output.v1",
            "created_at": "2026-07-20T00:00:00Z",
            "facts_explicit": [{"value": "x"}],
        },
        "primary_outcome": "CLEAN_PASS",
        "rubric_scores": {"x": 1},
    }
    payload = build_judge_input(_case(), output)
    dumped = str(payload)

    assert "human" not in dumped.lower()
    assert "CLEAR_PASS" not in dumped
    assert "rubric_scores" not in dumped
    assert payload["actual_understanding_output"]["summary_pl"]
    assert "schema_version" not in payload["actual_understanding_output"]
    assert "created_at" not in payload["actual_understanding_output"]


def test_normalize_judge_result_aggregates_dimension_failures() -> None:
    result = normalize_judge_result(
        {
            "case_id": "FU-05",
            "dimensions": {
                "essence": {"applicable": True, "verdict": "PASS", "reason_code": "ok", "evidence": "captured"},
                "intent": {"applicable": True, "verdict": "FAIL", "reason_code": "wrong", "evidence": "acceptance"},
            },
            "overall_verdict": "CLEAR_PASS",
            "unsafe_misinterpretation": False,
        }
    )

    assert result["overall_verdict"] == "CLEAR_FAIL"
    assert result["passed"] is False
    assert result["dimensions"]["intent"]["score"] == 0.0


def test_run_judge_schema_validation_with_fake_invoker() -> None:
    def fake(_system: str, _user: str, case_id: str) -> dict:
        return {
            "case_id": case_id,
            "dimensions": {
                "essence": {"applicable": True, "verdict": "PASS", "reason_code": "captured", "evidence": "ok"}
            },
            "overall_verdict": "CLEAR_PASS",
            "unsafe_misinterpretation": False,
        }

    result = run_judge(build_judge_input(_case(), _output()), invoke=fake)

    assert result["status"] == "SCORED"
    assert result["overall_verdict"] == "CLEAR_PASS"


def test_run_judge_validation_failure_is_judge_error() -> None:
    result = run_judge(build_judge_input(_case(), _output()), invoke=lambda *_args: {"case_id": "FU-05"})

    assert result["status"] == "JUDGE_ERROR"


def test_provider_error_sanitizes_api_key_fragments() -> None:
    text = _sanitize_error("Incorrect API key provided: sk-or-v1abc123xyz.")

    assert "sk-or" not in text
    assert "[REDACTED_API_KEY]" in text


def test_calibration_comparison_metrics() -> None:
    rows = [
        {"case_id": "A", "overall_verdict": "CLEAR_PASS"},
        {"case_id": "B", "overall_verdict": "BORDERLINE"},
        {"case_id": "C", "overall_verdict": "CLEAR_FAIL"},
    ]
    labels = {"A": "CLEAR_PASS", "B": "BORDERLINE", "C": "CLEAR_PASS"}

    compared = compare_with_human(rows, labels)

    assert compared["metrics"]["exact_3class_agreement"] == 2
    assert compared["metrics"]["major_disagreement"] == 1


def test_stability_detects_critical_flips() -> None:
    compared = compare_runs(
        [{"case_id": "A", "overall_verdict": "CLEAR_PASS"}],
        [{"case_id": "A", "overall_verdict": "CLEAR_FAIL"}],
    )

    assert compared["metrics"]["critical_flips"] == 1


def test_build_calibration_manifest_uses_existing_sample_only() -> None:
    manifest = build_calibration_manifest({"cases": [_case()]}, {"cases": [_output()]}, {"FU-05": "BORDERLINE"})

    assert manifest["cases"][0]["case_id"] == "FU-05"
    assert manifest["cases"][0]["human_label_available"] is True


def test_build_run_judge_manifest_has_no_human_label_leakage() -> None:
    manifest = build_run_judge_manifest({"cases": [_case()]}, {"cases": [_output()]})

    assert manifest["case_ids"] == ["FU-05"]
    payload = json.dumps(manifest, ensure_ascii=False)
    assert "human_label" not in payload
