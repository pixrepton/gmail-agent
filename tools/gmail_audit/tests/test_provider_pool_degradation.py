"""CL-05: a degraded provider pool must degrade, not disqualify what still works.

Live read-only triage of the running deployment found the pool in this state:

    openai_chat (primary)   HTTP 402  "Insufficient credits"   -> QUOTA_CAPACITY
    groq slot 1/2/3         HTTP 200                            -> HEALTHY
    groq slot 4             HTTP 401  "Invalid API Key"         -> CREDENTIAL_DEFECT
    cerebras / nvidia / anthropic  unconfigured                 -> EXPECTED_UNCONFIGURED

That exposed a product defect rather than only a credentials problem: `auth` was classified
non-retryable, and the router treated "not retryable" as "stop the whole chain". Because
`_rotate_groq_key_pool` moves the starting slot on every call, roughly one call in four began on
the dead key and aborted without ever trying the three healthy ones.

Retryability and chain-termination are now separate questions: a rejected credential disqualifies
itself and nothing else.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from llm_provider_router import (  # noqa: E402
    LLMProvider,
    LLMRouter,
    LLMRouterError,
    ProviderErrorInfo,
    classify_provider_error,
)


class _Err(RuntimeError):
    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


def _provider(name: str, call, *, configured: bool = True, missing: str = "") -> LLMProvider:
    return LLMProvider(
        provider=name, backend=name, model=f"{name}-model",
        call=call, configured=configured, missing_config=missing,
    )


def _raises(exc: Exception, log: list | None = None, tag: str = ""):
    def _call():
        if log is not None:
            log.append(tag)
        raise exc
    return _call


def _ok(name: str, log: list | None = None):
    def _call():
        if log is not None:
            log.append(name)
        return {"result": name}, {}
    return _call


# ── classification ────────────────────────────────────────────────────────────────────


def test_auth_is_not_retryable_but_is_not_chain_terminal():
    info = classify_provider_error(_Err("401 Unauthorized: Invalid API Key", details={"status_code": 401}))
    assert info.error_class == "auth"
    assert info.retryable is False, "never retry the same rejected credential"
    assert info.stops_chain is False, "but the next credential must still get a turn"


def test_quota_exhausted_is_retryable_on_the_next_provider():
    info = classify_provider_error(_Err("402 Insufficient credits", details={"status_code": 402}))
    assert info.error_class == "quota_exhausted"
    assert info.stops_chain is False


def test_contract_errors_still_stop_the_chain():
    """A malformed request fails identically everywhere; trying more providers is pure waste."""
    info = classify_provider_error(_Err("unsupported response shape"))
    assert info.error_class == "contract"
    assert info.stops_chain is True


def test_default_chain_termination_matches_historical_behavior():
    assert ProviderErrorInfo("x", retryable=False).stops_chain is True
    assert ProviderErrorInfo("x", retryable=True).stops_chain is False


# ── the live pool, reproduced ─────────────────────────────────────────────────────────


def test_dead_key_first_still_reaches_a_healthy_key():
    """The exact rotation case: slot 4 leads, slots 1-3 are healthy."""
    log: list[str] = []
    providers = [
        _provider("groq#4", _raises(_Err("401 Unauthorized: Invalid API Key", details={"status_code": 401}), log, "groq#4")),
        _provider("groq#1", _ok("groq#1", log)),
    ]
    response, meta = LLMRouter(providers).run()

    assert response == {"result": "groq#1"}
    assert log == ["groq#4", "groq#1"], "the dead key must be tried once, then yield"
    assert meta["llm_fallback_used"] is True
    attempts = meta["llm_provider_attempts"]
    assert attempts[0]["error_class"] == "auth"
    assert attempts[0]["retryable"] is False


def test_live_pool_shape_degrades_to_the_healthy_remainder():
    """Primary out of credits, one dead key, three healthy: the call still succeeds."""
    log: list[str] = []
    providers = [
        _provider("openai_chat", _raises(_Err("402 Insufficient credits", details={"status_code": 402}), log, "openai_chat")),
        _provider("groq#4", _raises(_Err("401 Invalid API Key", details={"status_code": 401}), log, "groq#4")),
        _provider("groq#1", _ok("groq#1", log)),
        _provider("cerebras", _ok("cerebras"), configured=False, missing="CEREBRAS_API_KEY"),
    ]
    response, meta = LLMRouter(providers).run()

    assert response == {"result": "groq#1"}
    assert log == ["openai_chat", "groq#4", "groq#1"]
    classes = [a["error_class"] for a in meta["llm_provider_attempts"]]
    assert classes[:3] == ["quota_exhausted", "auth", None]


def test_unconfigured_provider_is_skipped_not_called():
    called: list[str] = []
    providers = [
        _provider("cerebras", _ok("cerebras", called), configured=False, missing="CEREBRAS_API_KEY"),
        _provider("groq#1", _ok("groq#1", called)),
    ]
    _, meta = LLMRouter(providers).run()

    assert called == ["groq#1"], "an unconfigured provider must never be invoked"
    assert meta["llm_provider_attempts"][0]["status"] == "skipped"
    assert meta["llm_provider_attempts"][0]["error_class"] == "config"


def test_entire_pool_unusable_fails_closed_with_every_reason_recorded():
    providers = [
        _provider("openai_chat", _raises(_Err("402 Insufficient credits", details={"status_code": 402}))),
        _provider("groq#4", _raises(_Err("401 Invalid API Key", details={"status_code": 401}))),
        _provider("cerebras", _ok("cerebras"), configured=False, missing="CEREBRAS_API_KEY"),
    ]
    with pytest.raises(LLMRouterError) as caught:
        LLMRouter(providers).run()

    details = caught.value.details
    classes = [a["error_class"] for a in details["llm_provider_attempts"]]
    assert classes == ["quota_exhausted", "auth", "config"]
    assert details["terminal_failure_reason"] in {"provider_chain_exhausted", "provider_chain_failed"}


def test_auth_failure_does_not_mutate_configuration():
    """Degradation must be per-call. A hidden persistent disable would be an invisible mutation."""
    providers = [
        _provider("groq#4", _raises(_Err("401 Invalid API Key", details={"status_code": 401}))),
        _provider("groq#1", _ok("groq#1")),
    ]
    first, _ = LLMRouter(providers).run()
    second, meta = LLMRouter(providers).run()

    assert first == second == {"result": "groq#1"}
    # The dead key is still attempted on the next call: nothing was permanently disabled behind
    # the operator's back. Removing it is an explicit configuration change, not a runtime effect.
    assert meta["llm_provider_attempts"][0]["provider"] == "groq#4"
