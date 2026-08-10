"""DEEPSEEK-TEMP-BRIDGE-01 §13: the bridge must slot into the existing tier/fallback contract.

Switching the DeepSeek tier's host must not change *when* the router fallback runs. Proven at
the tier boundary in `run_central_structured_stage`, deterministically, with no live provider:

    bridge success                  -> Groq is NOT called
    bridge retryable failure        -> Groq is reached
    bridge quota/auth failure       -> attributable, and follows the existing provider policy
    Groq failure, Cerebras absent   -> bounded terminal result

No new retry system is introduced; this rides the repaired stage-deadline model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import central_llm_stage as cls  # noqa: E402
from groq_client import GroqClientError  # noqa: E402


class _Settings:
    """Minimal settings with the bridge selected as the DeepSeek host."""
    deepseek_host = "deepseek_nvidia"
    deepseek_base_url = "https://api.deepseek.com/v1"
    deepseek_model = "deepseek-v4-flash"
    deepseek_api_key = ""
    deepseek_api_keys = ()
    deepseek_nvidia_base_url = "https://integrate.api.nvidia.com/v1"
    deepseek_nvidia_model = "deepseek-ai/deepseek-r1"
    deepseek_nvidia_api_key = "nv-key"
    deepseek_nvidia_api_keys = ("nv-key",)
    deepseek_thinking_enabled = False
    deepseek_reasoning_effort = "medium"
    anthropic_api_key = ""
    http_timeout = 60
    http_max_retries = 4
    llm_stage_budget_sec = 180
    llm_min_attempt_sec = 5
    groq_model = "openai/gpt-oss-120b"


def _stage_result(provider: str) -> dict:
    return {
        "stage_name": "business_reasoning",
        "model_name": "m",
        "attempt_count": 1,
        "response_text": '{"ok": true}',
        "parse_status": "received",
        "response_json": {"ok": True},
        "request_meta": {"central_llm_provider": provider},
        "prompt_input": {},
    }


@pytest.fixture()
def stage_env(monkeypatch):
    """Neutralise context assembly and caching so only provider routing is under test."""
    class _Assembled:
        case_id_used = None
    monkeypatch.setattr(cls, "build_context_assembler",
                        lambda s: type("A", (), {"assemble": lambda *a, **k: _Assembled()})())
    monkeypatch.setattr(cls, "overlay_pack_onto_assembled", lambda a, b: a)
    monkeypatch.setattr(cls, "apply_context_token_budget", lambda a, **k: (a, {}))
    monkeypatch.setattr(cls, "assembled_context_to_dict", lambda a, **k: {})
    monkeypatch.setattr(cls, "merge_system_prompt", lambda a, b: "sys")
    monkeypatch.setattr(cls, "_get_cache_db_url", lambda: "")
    return monkeypatch


def _run(**kwargs):
    return cls.run_central_structured_stage(
        _Settings(), stage_name="business_reasoning", task_instructions="i",
        prompt_input={}, query_text="q", json_schema={}, schema_name="s", **kwargs
    )


def test_bridge_success_means_groq_is_never_called(stage_env):
    groq_calls: list[int] = []
    stage_env.setattr(cls, "run_deepseek_structured_stage",
                      lambda *a, **k: _stage_result("deepseek_nvidia"))
    stage_env.setattr(cls, "_call_groq_structured_stage",
                      lambda *a, **k: groq_calls.append(1) or _stage_result("groq"))

    out = _run()

    assert groq_calls == [], "the router tier must not run when the DeepSeek tier succeeded"
    assert out["central_llm_provider"] == "deepseek_nvidia"


def test_bridge_retryable_failure_reaches_groq(stage_env):
    groq_calls: list[int] = []

    def _bridge_fails(*_a, **_k):
        raise GroqClientError("503 Server Error", details={"status_code": 503})

    stage_env.setattr(cls, "run_deepseek_structured_stage", _bridge_fails)
    stage_env.setattr(cls, "_call_groq_structured_stage",
                      lambda *a, **k: (groq_calls.append(1), _stage_result("groq"))[1])

    out = _run()

    assert groq_calls == [1], "a retryable bridge failure must fall through to the router tier"
    assert out["central_llm_provider"] == "groq"


def test_bridge_quota_failure_is_attributable_and_follows_existing_policy(stage_env):
    """A 402 on the bridge must be recorded as such, then handled by the existing policy."""
    captured: dict = {}

    def _bridge_402(*_a, **_k):
        raise GroqClientError("402 Insufficient Balance",
                              details={"status_code": 402, "error_class": "quota_exhausted"})

    class _Logger:
        def error(self, msg, extra=None):
            if msg == "LLM_DEEPSEEK_FAILED":
                captured.update((extra or {}).get("x", {}))
        def warning(self, *a, **k): pass
        def info(self, *a, **k): pass

    stage_env.setattr(cls, "logger", _Logger())
    stage_env.setattr(cls, "run_deepseek_structured_stage", _bridge_402)
    stage_env.setattr(cls, "_call_groq_structured_stage", lambda *a, **k: _stage_result("groq"))

    out = _run()

    assert captured.get("stage") == "business_reasoning"
    assert captured.get("status_code") == 402 or captured.get("error_class") == "quota_exhausted"
    assert out["central_llm_provider"] == "groq", "existing policy: fall through, do not fail the stage"


def test_groq_failure_without_cerebras_is_a_bounded_terminal_result(stage_env):
    def _bridge_fails(*_a, **_k):
        raise GroqClientError("503", details={"status_code": 503})

    def _groq_fails(*_a, **_k):
        raise GroqClientError(
            "All LLM providers exhausted: groq failed (server_error); cerebras unconfigured.",
            details={"terminal_failure_reason": "provider_chain_exhausted"},
        )

    stage_env.setattr(cls, "run_deepseek_structured_stage", _bridge_fails)
    stage_env.setattr(cls, "_call_groq_structured_stage", _groq_fails)

    with pytest.raises(GroqClientError) as caught:
        _run()

    assert "exhausted" in str(caught.value).lower()
    assert caught.value.details.get("terminal_failure_reason") == "provider_chain_exhausted"
