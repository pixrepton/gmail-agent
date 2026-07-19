"""Tests for llm_provider_router.py - dataclass and router construction."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from llm_provider_router import LLMProvider, LLMRouterError, ProviderErrorInfo, LLMRouter


def _dummy_call() -> tuple[dict, dict]:
    return {"result": "ok"}, {"model": "test"}


class TestLLMProviderDataclass:
    def test_minimal_creation(self):
        provider = LLMProvider(
            provider="openai",
            backend="openai",
            model="gpt-4",
            call=_dummy_call,
        )
        assert provider.provider == "openai"
        assert provider.backend == "openai"
        assert provider.model == "gpt-4"
        assert provider.call is _dummy_call
        assert provider.configured is True
        assert provider.missing_config == ""

    def test_creation_with_all_fields(self):
        provider = LLMProvider(
            provider="anthropic",
            backend="anthropic",
            model="claude-3-opus",
            call=_dummy_call,
            configured=False,
            missing_config="ANTHROPIC_API_KEY",
        )
        assert provider.provider == "anthropic"
        assert provider.configured is False
        assert provider.missing_config == "ANTHROPIC_API_KEY"

    def test_provider_types_are_strings(self):
        provider = LLMProvider(
            provider="ollama", backend="ollama", model="llama3", call=_dummy_call
        )
        assert isinstance(provider.provider, str)
        assert isinstance(provider.backend, str)
        assert isinstance(provider.model, str)

    def test_call_is_callable(self):
        provider = LLMProvider(
            provider="test", backend="test", model="test", call=_dummy_call
        )
        result, meta = provider.call()
        assert result == {"result": "ok"}
        assert meta == {"model": "test"}

    def test_default_configured_is_true(self):
        provider = LLMProvider(
            provider="x", backend="x", model="x", call=_dummy_call
        )
        assert provider.configured is True
        assert provider.missing_config == ""


class TestProviderErrorInfoDataclass:
    def test_creation_retryable(self):
        info = ProviderErrorInfo(error_class="rate_limit", retryable=True)
        assert info.error_class == "rate_limit"
        assert info.retryable is True

    def test_creation_non_retryable(self):
        info = ProviderErrorInfo(error_class="auth", retryable=False)
        assert info.error_class == "auth"
        assert info.retryable is False

    def test_error_class_types(self):
        for cls in (
            "rate_limit",
            "quota_exhausted",
            "not_found",
            "server_error",
            "timeout",
            "network",
            "auth",
            "config",
            "contract",
        ):
            info = ProviderErrorInfo(
                error_class=cls,
                retryable=cls
                in (
                    "rate_limit",
                    "quota_exhausted",
                    "not_found",
                    "server_error",
                    "timeout",
                    "network",
                ),
            )
            assert info.error_class == cls
            assert isinstance(info.retryable, bool)


class TestLLMRouterBasic:
    def test_init_with_empty_list(self):
        router = LLMRouter(providers=[])
        assert router.providers == []

    def test_init_with_single_provider(self):
        provider = LLMProvider(
            provider="openai", backend="openai", model="gpt-4", call=_dummy_call
        )
        router = LLMRouter(providers=[provider])
        assert len(router.providers) == 1
        assert router.providers[0] is provider

    def test_init_with_multiple_providers(self):
        p1 = LLMProvider(provider="a", backend="a", model="m1", call=_dummy_call)
        p2 = LLMProvider(provider="b", backend="b", model="m2", call=_dummy_call)
        router = LLMRouter(providers=[p1, p2])
        assert len(router.providers) == 2

    def test_providers_are_ordered(self):
        p1 = LLMProvider(provider="first", backend="x", model="m1", call=_dummy_call)
        p2 = LLMProvider(provider="second", backend="x", model="m2", call=_dummy_call)
        router = LLMRouter(providers=[p1, p2])
        names = [p.provider for p in router.providers]
        assert names == ["first", "second"]

    def test_get_providers_returns_list(self):
        p1 = LLMProvider(provider="a", backend="a", model="m1", call=_dummy_call)
        router = LLMRouter(providers=[p1])
        result = router.providers
        assert isinstance(result, list)
        assert len(result) == 1

    def test_empty_router_returns_empty_list(self):
        router = LLMRouter(providers=[])
        assert router.providers == []
        assert len(router.providers) == 0

    def test_add_provider_via_list_append(self):
        router = LLMRouter(providers=[])
        p = LLMProvider(provider="added", backend="x", model="m", call=_dummy_call)
        router.providers.append(p)
        assert len(router.providers) == 1
        assert router.providers[0].provider == "added"


class TestLLMRouterRealRedundancy:
    """DELIVERY-1 RC-1: an unconfigured provider must be SKIPPED, not treated as
    chain-terminal — otherwise its position in LLM_FALLBACK_PROVIDERS silently
    determines whether a later, perfectly usable provider is ever reached. Three
    provider names in a list is not redundancy unless failure of one (configured
    or not) actually reaches the next.
    """

    def test_unconfigured_provider_mid_chain_does_not_block_later_provider(self):
        def fail_call():
            raise RuntimeError("429 rate limited")

        def unconfigured_call():
            raise AssertionError("unconfigured provider's call() must never be invoked")

        used: list[str] = []

        def success_call():
            used.append("nvidia")
            return {"ok": True}, {}

        providers = [
            LLMProvider(provider="primary", backend="x", model="m1", call=fail_call),
            LLMProvider(
                provider="cerebras",
                backend="x",
                model="m2",
                call=unconfigured_call,
                configured=False,
                missing_config="CEREBRAS_API_KEY",
            ),
            LLMProvider(provider="nvidia", backend="x", model="m3", call=success_call),
        ]
        router = LLMRouter(providers=providers)

        response, meta = router.run()

        assert used == ["nvidia"]
        assert meta["llm_selected_provider"] == "nvidia"
        cerebras_attempts = [a for a in meta["llm_provider_attempts"] if a["provider"] == "cerebras"]
        assert len(cerebras_attempts) == 1
        assert cerebras_attempts[0]["status"] == "skipped"
        assert cerebras_attempts[0]["error_class"] == "config"

    def test_unconfigured_provider_as_last_entry_still_raises_explicit_error(self):
        def fail_call():
            raise RuntimeError("429 rate limited")

        providers = [
            LLMProvider(provider="primary", backend="x", model="m1", call=fail_call),
            LLMProvider(
                provider="cerebras",
                backend="x",
                model="m2",
                call=lambda: (_ for _ in ()).throw(AssertionError("must not call")),
                configured=False,
                missing_config="CEREBRAS_API_KEY",
            ),
        ]
        router = LLMRouter(providers=providers)

        with pytest.raises(LLMRouterError) as exc_info:
            router.run()

        message_and_details = str(exc_info.value) + str(exc_info.value.details)
        assert "CEREBRAS_API_KEY" in message_and_details
        assert "primary" in message_and_details or any(
            a["provider"] == "primary" for a in exc_info.value.details.get("llm_provider_attempts", [])
        )

    def test_all_providers_unconfigured_raises_no_provider_configured(self):
        providers = [
            LLMProvider(
                provider="groq",
                backend="x",
                model="m1",
                call=lambda: (_ for _ in ()).throw(AssertionError("must not call")),
                configured=False,
                missing_config="GROQ_API_KEY",
            ),
            LLMProvider(
                provider="cerebras",
                backend="x",
                model="m2",
                call=lambda: (_ for _ in ()).throw(AssertionError("must not call")),
                configured=False,
                missing_config="CEREBRAS_API_KEY",
            ),
        ]
        router = LLMRouter(providers=providers)

        with pytest.raises(LLMRouterError) as exc_info:
            router.run()

        message_and_details = str(exc_info.value) + str(exc_info.value.details)
        assert "GROQ_API_KEY" in message_and_details
        assert "CEREBRAS_API_KEY" in message_and_details
