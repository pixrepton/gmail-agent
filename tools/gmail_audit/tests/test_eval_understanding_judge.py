from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    monkeypatch.delenv("AGENT_GROQ_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")

    config = select_judge_config()

    assert config.provider == GROQ_PROVIDER
    assert config.model == "llama-3.3-70b-versatile"
    assert config.fallback == "none"


def test_groq_key_pool_merges_plural_and_singular(monkeypatch) -> None:
    monkeypatch.setattr(judge_module, "_env_file_candidates", lambda: [])
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    monkeypatch.delenv("AGENT_GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_primary")
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_second,gsk_third")
    monkeypatch.setenv("AGENT_GROQ_API_KEY", "gsk_primary")

    assert judge_module._groq_key_pool() == ("gsk_second", "gsk_third", "gsk_primary")


def test_secret_value_prefers_gmail_agent_env_file(monkeypatch, tmp_path: Path) -> None:
    override = tmp_path / "local-vps.env"
    override.write_text("GROQ_API_KEY=gsk_from_override\nGROQ_API_KEYS=gsk_a,gsk_b\n", encoding="utf-8")
    stale = tmp_path / "stale.env"
    stale.write_text("GROQ_API_KEY=gsk_stale\n", encoding="utf-8")
    monkeypatch.setenv("GMAIL_AGENT_ENV_FILE", str(override))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    monkeypatch.setattr(
        judge_module,
        "_env_file_candidates",
        lambda: [override, stale],
    )

    assert judge_module._secret_value("GROQ_API_KEY") == "gsk_from_override"
    assert judge_module._groq_key_pool() == ("gsk_a", "gsk_b", "gsk_from_override")


def test_openai_compatible_invoke_rotates_groq_keys_on_401(monkeypatch) -> None:
    import groq_client

    groq_client.reset_groq_key_rotation_counter_for_tests()
    calls: list[str] = []

    class _Resp:
        def __init__(self, status_code: int, text: str = "", payload: dict | None = None):
            self.status_code = status_code
            self.text = text
            self._payload = payload or {}

        def json(self):
            return self._payload

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        del url, json, timeout
        auth = (headers or {}).get("authorization", "")
        key = auth.replace("Bearer ", "")
        calls.append(key)
        if key == "gsk_bad":
            return _Resp(401, '{"error":{"message":"Invalid API Key"}}')
        return _Resp(
            200,
            payload={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"case_id":"FU-05","dimensions":{"essence":{"applicable":true,'
                                '"verdict":"PASS","reason_code":"ok","evidence":"e"}},'
                                '"overall_verdict":"CLEAR_PASS","unsafe_misinterpretation":false}'
                            )
                        }
                    }
                ]
            },
        )

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(judge_module, "_groq_key_pool", lambda: ("gsk_bad", "gsk_good"))
    monkeypatch.setattr(judge_module, "_secret_value", lambda _key: "")

    payload = judge_module._openai_compatible_invoke(
        "system",
        "user",
        "FU-05",
        judge_module.JudgeConfig(provider=GROQ_PROVIDER, model="llama-3.3-70b-versatile"),
    )

    assert calls == ["gsk_bad", "gsk_good"]
    assert payload["case_id"] == "FU-05"


def test_openai_compatible_invoke_uses_next_groq_key_on_429(monkeypatch) -> None:
    import groq_client

    groq_client.reset_groq_key_rotation_counter_for_tests()
    calls: list[str] = []
    bodies: list[dict] = []

    class _Resp:
        def __init__(self, status_code: int, text: str = "", payload: dict | None = None):
            self.status_code = status_code
            self.text = text
            self._payload = payload or {}

        def json(self):
            return self._payload

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        del url, timeout
        key = (headers or {}).get("authorization", "").replace("Bearer ", "")
        calls.append(key)
        bodies.append(json)
        if key == "gsk_limited":
            return _Resp(429, '{"error":{"message":"rate limit exceeded"}}')
        return _Resp(
            200,
            payload={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"case_id":"FU-05","dimensions":{"essence":{"applicable":true,'
                                '"verdict":"PASS","reason_code":"ok","evidence":"e"}},'
                                '"overall_verdict":"CLEAR_PASS","unsafe_misinterpretation":false}'
                            )
                        }
                    }
                ]
            },
        )

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(judge_module, "_groq_key_pool", lambda: ("gsk_limited", "gsk_good"))
    monkeypatch.setattr(judge_module.time, "sleep", lambda _seconds: None)

    payload = judge_module._openai_compatible_invoke(
        "system",
        "user",
        "FU-05",
        judge_module.JudgeConfig(provider=GROQ_PROVIDER, model="llama-3.3-70b-versatile"),
    )

    assert calls == ["gsk_limited", "gsk_good"]
    assert bodies[0] == bodies[1]
    assert bodies[0]["model"] == "llama-3.3-70b-versatile"
    assert bodies[0]["temperature"] == 0.0
    assert payload["case_id"] == "FU-05"


def test_openai_compatible_invoke_rotates_groq_starting_key_between_calls(monkeypatch) -> None:
    import groq_client
    import requests

    groq_client.reset_groq_key_rotation_counter_for_tests()
    calls: list[str] = []

    class _Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"case_id":"FU-05","dimensions":{"essence":{"applicable":true,'
                                '"verdict":"PASS","reason_code":"ok","evidence":"e"}},'
                                '"overall_verdict":"CLEAR_PASS","unsafe_misinterpretation":false}'
                            )
                        }
                    }
                ]
            }

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        del url, json, timeout
        calls.append((headers or {}).get("authorization", "").replace("Bearer ", ""))
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(judge_module, "_groq_key_pool", lambda: ("gsk_one", "gsk_two", "gsk_three"))
    config = judge_module.JudgeConfig(provider=GROQ_PROVIDER, model="llama-3.3-70b-versatile")

    judge_module._openai_compatible_invoke("system", "user", "FU-05", config)
    judge_module._openai_compatible_invoke("system", "user", "FU-05", config)

    assert calls == ["gsk_one", "gsk_two"]


def test_openai_compatible_invoke_fails_closed_after_all_groq_keys_are_limited(monkeypatch) -> None:
    import groq_client
    import requests

    groq_client.reset_groq_key_rotation_counter_for_tests()
    calls: list[str] = []

    class _Resp:
        status_code = 429
        text = '{"error":{"message":"rate limit exceeded"}}'

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        del url, json, timeout
        calls.append((headers or {}).get("authorization", "").replace("Bearer ", ""))
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(judge_module, "_groq_key_pool", lambda: ("gsk_one", "gsk_two", "gsk_three"))

    with pytest.raises(judge_module.TopInstalLLMError) as exc_info:
        judge_module._openai_compatible_invoke(
            "system",
            "user",
            "FU-05",
            judge_module.JudgeConfig(provider=GROQ_PROVIDER, model="llama-3.3-70b-versatile"),
        )

    assert calls == ["gsk_one", "gsk_two", "gsk_three"]
    assert exc_info.value.details["key_pool_size"] == 3
    assert exc_info.value.details["terminal_failure_reason"] == "provider_chain_failed"


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


def test_run_judge_recomputes_invalid_redundant_overall_from_valid_dimensions() -> None:
    def fake(_system: str, _user: str, case_id: str) -> dict:
        return {
            "case_id": case_id,
            "dimensions": {
                "essence": {
                    "applicable": True,
                    "verdict": "FAIL",
                    "reason_code": "missed_essence",
                    "evidence": "The output missed the required distinction.",
                }
            },
            "overall_verdict": "FAIL",
            "unsafe_misinterpretation": False,
        }

    result = run_judge(build_judge_input(_case(), _output()), invoke=fake)

    assert result["status"] == "SCORED"
    assert result["overall_verdict"] == "CLEAR_FAIL"
    assert result["dimensions"]["essence"]["verdict"] == "FAIL"


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
