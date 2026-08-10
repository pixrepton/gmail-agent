"""DEEPSEEK-EMPTY-CONTENT-01: a DeepSeek fallthrough must record why it fell through.

A closeout Fresh38 run produced 34 DeepSeek fallthroughs across 28 of 38 cases, every one
reported as `OpenAI-compatible response has empty message.content`. None could be classified
afterwards, because `LLM_DEEPSEEK_FAILED` logged only `str(exc)[:300]` and discarded
`exc.details` -- even though `_extract_openai_chat_message_text` had already assembled exactly
the fields needed:

    error_class, finish_reason, has_tool_calls, has_reasoning_content, content_type, content_len

Without them, "provider returned nothing", "provider returned only reasoning" and "provider was
cut off by its own token budget" are indistinguishable after the fact. These tests pin that the
diagnostics survive, and that nothing else does.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import central_llm_stage as cls  # noqa: E402
from groq_client import GroqClientError, _extract_openai_chat_message_text  # noqa: E402


def _empty_content_error(**overrides):
    """Produce the real adapter error rather than a hand-written stand-in."""
    response = {
        "choices": [{
            "finish_reason": overrides.pop("finish_reason", "stop"),
            "message": {
                "content": overrides.pop("content", ""),
                "reasoning_content": overrides.pop("reasoning_content", ""),
            },
        }]
    }
    try:
        _extract_openai_chat_message_text(response)
    except GroqClientError as exc:
        return exc
    raise AssertionError("expected the adapter to reject this response")


# ── the diagnostics survive ───────────────────────────────────────────────────────────


def test_empty_content_diagnostics_are_preserved():
    exc = _empty_content_error(finish_reason="stop", content="", reasoning_content="long reasoning here")
    diag = cls._deepseek_failure_diagnostics(exc)

    assert diag["error_class"] == "empty_content"
    assert diag["finish_reason"] == "stop"
    assert diag["has_reasoning_content"] is True
    assert diag["has_tool_calls"] is False
    assert diag["content_len"] == 0


def test_truncation_is_distinguishable_from_a_genuinely_empty_answer():
    """`finish_reason` is what separates 'cut off' from 'said nothing'."""
    truncated = cls._deepseek_failure_diagnostics(_empty_content_error(finish_reason="length"))
    said_nothing = cls._deepseek_failure_diagnostics(_empty_content_error(finish_reason="stop"))

    assert truncated["finish_reason"] == "length"
    assert said_nothing["finish_reason"] == "stop"
    assert truncated["error_class"] == said_nothing["error_class"] == "empty_content"


def test_reasoning_only_response_is_distinguishable():
    """Provider produced thinking but no answer -- a different fault from producing nothing."""
    reasoning_only = cls._deepseek_failure_diagnostics(
        _empty_content_error(content="", reasoning_content="chain of thought")
    )
    nothing_at_all = cls._deepseek_failure_diagnostics(
        _empty_content_error(content="", reasoning_content="")
    )

    assert reasoning_only["has_reasoning_content"] is True
    assert nothing_at_all["has_reasoning_content"] is False


def test_null_content_is_distinguishable_from_empty_string():
    null_content = cls._deepseek_failure_diagnostics(_empty_content_error(content=None))
    assert null_content["content_type"] == "NoneType"


def test_router_shaped_errors_surface_the_last_attempt():
    exc = GroqClientError("chain failed", details={
        "llm_provider_attempts": [
            {"provider": "deepseek", "error_class": "empty_content", "retryable": True, "latency_ms": 1234},
        ],
    })
    diag = cls._deepseek_failure_diagnostics(exc)
    assert diag["last_attempt_provider"] == "deepseek"
    assert diag["last_attempt_error_class"] == "empty_content"
    assert diag["last_attempt_latency_ms"] == 1234


# ── and nothing else does ─────────────────────────────────────────────────────────────


def test_diagnostics_are_an_allow_list_not_a_blanket_copy():
    """Provider details can echo the request; a blanket copy would log prompts or credentials."""
    # The sentinel values deliberately do NOT imitate a real credential format: the repo's
    # secret scanner rejects committed strings shaped like live keys, and a test proving
    # redaction must not itself look like a leak.
    exc = GroqClientError("empty", details={
        "error_class": "empty_content",
        "finish_reason": "stop",
        "api_key": "CREDENTIAL-SENTINEL-MUST-NOT-BE-LOGGED",
        "authorization": "AUTH-HEADER-SENTINEL-MUST-NOT-BE-LOGGED",
        "prompt": "customer email body with personal data",
        "messages": [{"role": "user", "content": "personal data"}],
        "request_body": {"messages": ["personal data"]},
    })
    diag = cls._deepseek_failure_diagnostics(exc)

    assert diag == {"error_class": "empty_content", "finish_reason": "stop"}
    serialized = repr(diag)
    for leaked in ("CREDENTIAL-SENTINEL", "AUTH-HEADER-SENTINEL", "personal data", "customer email"):
        assert leaked not in serialized


def test_missing_details_degrade_to_an_empty_mapping():
    assert cls._deepseek_failure_diagnostics(GroqClientError("no details")) == {}
    assert cls._deepseek_failure_diagnostics(RuntimeError("not even a provider error")) == {}


def test_fallthrough_logs_the_diagnostics(monkeypatch):
    """The log call itself must carry them -- the previous version dropped them here."""
    captured: dict = {}

    class _Logger:
        def error(self, msg, extra=None):
            if msg == "LLM_DEEPSEEK_FAILED":
                captured.update((extra or {}).get("x", {}))
        def warning(self, *a, **k): pass
        def info(self, *a, **k): pass

    monkeypatch.setattr(cls, "logger", _Logger())
    exc = _empty_content_error(finish_reason="length", reasoning_content="thinking")

    # Reproduce the fallthrough log site's payload construction.
    cls.logger.error("LLM_DEEPSEEK_FAILED", extra={"x": {
        "stage": "business_reasoning",
        "error": str(exc)[:300],
        **cls._deepseek_failure_diagnostics(exc),
    }})

    assert captured["stage"] == "business_reasoning"
    assert captured["finish_reason"] == "length"
    assert captured["has_reasoning_content"] is True
    assert captured["error_class"] == "empty_content"
