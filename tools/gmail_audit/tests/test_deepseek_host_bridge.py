"""DEEPSEEK-TEMP-BRIDGE-01: a temporary host bridge that cannot quietly become the target.

DeepSeek Direct billing is unavailable, so the DeepSeek *tier* can temporarily be served by
NVIDIA NIM hosting a DeepSeek model. The tier expresses a logical model intent ("use DeepSeek");
who hosts it is a separate, operator-controlled decision.

These tests pin the properties that keep that arrangement honest:

* the canonical target is the default and never silently loses its identity;
* the bridge is reachable only by explicit configuration -- never by reacting to a billing error;
* telemetry can always tell which host produced a result;
* switching hosts changes endpoint/credential/model and nothing else;
* the governance registry and the runtime cannot drift apart.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from config import (  # noqa: E402
    DEEPSEEK_HOST_DIRECT,
    DEEPSEEK_HOST_NVIDIA,
    DEEPSEEK_HOSTS,
    DEFAULT_DEEPSEEK_HOST,
)
from groq_client import (  # noqa: E402
    DEEPSEEK_MODEL_FAMILY,
    EQUIVALENCE_PROVEN,
    EQUIVALENCE_UNPROVEN,
    ROLE_CANONICAL_TARGET,
    ROLE_TEMPORARY_OPERATIONAL_BRIDGE,
    _deepseek_providers,
    deepseek_configured,
    resolve_deepseek_host,
)


# The bridge has no default model, so tests must state one explicitly -- exactly as an
# operator must. This stands in for the intended DeepSeek V4 Flash NIM id.
EXPLICIT_BRIDGE_MODEL = "deepseek-ai/deepseek-v4-flash"


def _settings(host: str, *, nvidia_key: str = "", direct_key: str = "direct-key",
              nvidia_model: str = EXPLICIT_BRIDGE_MODEL) -> SimpleNamespace:
    return SimpleNamespace(
        deepseek_host=host,
        deepseek_base_url="https://api.deepseek.com/v1",
        deepseek_model="deepseek-v4-flash",
        deepseek_api_key=direct_key,
        deepseek_api_keys=(direct_key,) if direct_key else (),
        deepseek_nvidia_base_url="https://integrate.api.nvidia.com/v1",
        deepseek_nvidia_model=nvidia_model,
        deepseek_nvidia_api_key=nvidia_key,
        deepseek_nvidia_api_keys=(nvidia_key,) if nvidia_key else (),
        deepseek_thinking_enabled=False,
        deepseek_reasoning_effort="medium",
    )


# ── canonical target stays canonical ──────────────────────────────────────────────────


def test_default_host_is_the_canonical_target():
    assert DEFAULT_DEEPSEEK_HOST == DEEPSEEK_HOST_DIRECT
    host = resolve_deepseek_host(_settings(DEFAULT_DEEPSEEK_HOST))
    assert host.host == "deepseek_direct"
    assert host.role == ROLE_CANONICAL_TARGET


def test_canonical_host_keeps_the_historical_telemetry_label():
    """Renaming it would make future canonical runs non-comparable with the 32/38 and 38/38 baselines."""
    assert resolve_deepseek_host(_settings(DEEPSEEK_HOST_DIRECT)).provider == "deepseek"


def test_bridge_has_a_distinct_identity_and_role():
    host = resolve_deepseek_host(_settings(DEEPSEEK_HOST_NVIDIA, nvidia_key="nv"))
    assert host.provider == "deepseek_nvidia" != "deepseek"
    assert host.role == ROLE_TEMPORARY_OPERATIONAL_BRIDGE


def test_switching_host_changes_only_endpoint_credential_and_model():
    direct = resolve_deepseek_host(_settings(DEEPSEEK_HOST_DIRECT))
    bridge = resolve_deepseek_host(_settings(DEEPSEEK_HOST_NVIDIA, nvidia_key="nv"))

    assert direct.base_url != bridge.base_url
    assert direct.model != bridge.model
    assert direct.api_keys != bridge.api_keys
    # Everything else about the tier is shared by construction: both go through the same
    # _deepseek_providers builder, the same adapter and the same router.
    assert direct.role != bridge.role


# ── the bridge model must be explicit: fail closed, never guessed ────────────────────


def test_bridge_has_no_default_model():
    """The bridge changes the HOST; changing the model too would confound any comparison.

    An earlier revision defaulted to `deepseek-ai/deepseek-r1`, which would have changed provider
    *and* model simultaneously — so a bridge result could not be compared with a canonical one,
    and nobody could tell which of the two changes explained a difference.
    """
    import dataclasses

    import config

    assert not hasattr(config, "DEFAULT_DEEPSEEK_NVIDIA_MODEL")
    field = next(f for f in dataclasses.fields(config.Settings) if f.name == "deepseek_nvidia_model")
    assert field.default == "", "the bridge must not ship a default model"


def test_missing_bridge_model_makes_the_host_unconfigured():
    host = resolve_deepseek_host(_settings(DEEPSEEK_HOST_NVIDIA, nvidia_key="nv", nvidia_model=""))
    assert host.configured is False
    assert "DEEPSEEK_NVIDIA_MODEL" in host.missing_config


def test_missing_model_and_key_are_both_named():
    """One pass, not a discovery sequence."""
    host = resolve_deepseek_host(_settings(DEEPSEEK_HOST_NVIDIA, nvidia_key="", nvidia_model=""))
    assert "DEEPSEEK_NVIDIA_API_KEY" in host.missing_config
    assert "DEEPSEEK_NVIDIA_MODEL" in host.missing_config


def test_selecting_the_bridge_without_a_model_is_a_config_error(monkeypatch):
    from config import ConfigError, load_settings

    monkeypatch.setenv("AI_OS_PRIMARY_PROVIDER", "deepseek_nvidia")
    monkeypatch.delenv("DEEPSEEK_NVIDIA_MODEL", raising=False)
    with pytest.raises(ConfigError) as caught:
        load_settings(require_groq=False, require_google=False)
    assert "DEEPSEEK_NVIDIA_MODEL" in str(caught.value)


def test_canonical_mode_is_unaffected_by_an_unset_bridge_model(monkeypatch):
    """A deployment running canonically must not break on a bridge variable it never uses."""
    from config import load_settings

    monkeypatch.delenv("AI_OS_PRIMARY_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPSEEK_NVIDIA_MODEL", raising=False)
    settings = load_settings(require_groq=False, require_google=False)
    assert settings.deepseek_host == DEEPSEEK_HOST_DIRECT
    assert resolve_deepseek_host(settings).model  # canonical model still resolves


def test_bridge_never_borrows_the_generic_nvidia_model(monkeypatch):
    """NVIDIA_MODEL feeds the separate `nvidia` router slot and is not a DeepSeek model."""
    from config import load_settings

    monkeypatch.setenv("AI_OS_PRIMARY_PROVIDER", "deepseek_nvidia")
    monkeypatch.setenv("NVIDIA_MODEL", "gpt-oss-120b")
    monkeypatch.setenv("DEEPSEEK_NVIDIA_MODEL", EXPLICIT_BRIDGE_MODEL)
    settings = load_settings(require_groq=False, require_google=False)
    host = resolve_deepseek_host(settings)
    assert host.model == EXPLICIT_BRIDGE_MODEL
    assert host.model != "gpt-oss-120b"


def test_bridge_never_substitutes_the_canonical_model():
    """Sending `deepseek-v4-flash` to NVIDIA NIM would be wrong *and* invisible."""
    providers = _deepseek_providers(
        _settings(DEEPSEEK_HOST_NVIDIA, nvidia_key="nv", nvidia_model=""),
        instructions="x", user_payload="{}", json_schema={}, schema_name="s",
        model=None, mode="default", temperature=0,
    )
    assert len(providers) == 1
    assert providers[0].configured is False
    assert providers[0].model != "deepseek-v4-flash", "must not fall back to the canonical model id"


# ── configuration is the only way to switch ──────────────────────────────────────────


def test_unconfigured_bridge_is_reported_not_silently_skipped():
    host = resolve_deepseek_host(_settings(DEEPSEEK_HOST_NVIDIA, nvidia_key=""))
    assert host.configured is False
    assert host.missing_config == "DEEPSEEK_NVIDIA_API_KEY"
    assert deepseek_configured(_settings(DEEPSEEK_HOST_NVIDIA, nvidia_key="")) is False


def test_unconfigured_bridge_yields_a_skipped_provider_with_bridge_identity():
    providers = _deepseek_providers(
        _settings(DEEPSEEK_HOST_NVIDIA, nvidia_key=""),
        instructions="x", user_payload="{}", json_schema={}, schema_name="s",
        model=None, mode="default", temperature=0,
    )
    assert len(providers) == 1
    assert providers[0].configured is False
    assert providers[0].provider == "deepseek_nvidia"
    assert providers[0].missing_config == "DEEPSEEK_NVIDIA_API_KEY"


def test_an_unknown_host_value_is_a_config_error_not_a_silent_default(monkeypatch):
    """A typo must not quietly route a whole measurement to a different provider."""
    from config import ConfigError, load_settings

    monkeypatch.setenv("AI_OS_PRIMARY_PROVIDER", "deepseek_nvidiaa")
    with pytest.raises(ConfigError) as caught:
        load_settings(require_groq=False, require_google=False)
    assert "AI_OS_PRIMARY_PROVIDER" in str(caught.value)


def test_env_switch_selects_the_bridge(monkeypatch):
    from config import load_settings

    monkeypatch.setenv("AI_OS_PRIMARY_PROVIDER", "deepseek_nvidia")
    monkeypatch.setenv("DEEPSEEK_NVIDIA_MODEL", EXPLICIT_BRIDGE_MODEL)
    settings = load_settings(require_groq=False, require_google=False)
    assert settings.deepseek_host == DEEPSEEK_HOST_NVIDIA
    assert resolve_deepseek_host(settings).role == ROLE_TEMPORARY_OPERATIONAL_BRIDGE
    assert resolve_deepseek_host(settings).model == EXPLICIT_BRIDGE_MODEL


def test_host_selection_is_independent_of_llm_backend(monkeypatch):
    """LLM_BACKEND drives URL/model resolution elsewhere; it must not gate the bridge."""
    from config import load_settings

    for backend in ("openai_chat", "groq"):
        monkeypatch.setenv("LLM_BACKEND", backend)
        monkeypatch.setenv("AI_OS_PRIMARY_PROVIDER", "deepseek_nvidia")
        monkeypatch.setenv("DEEPSEEK_NVIDIA_MODEL", EXPLICIT_BRIDGE_MODEL)
        settings = load_settings(require_groq=False, require_google=False)
        host = resolve_deepseek_host(settings)
        assert host.host == DEEPSEEK_HOST_NVIDIA, f"backend={backend} changed host selection"
        assert host.base_url.startswith("https://integrate.api.nvidia.com")


# ── no hidden automatic switching ─────────────────────────────────────────────────────


def test_no_code_path_selects_the_bridge_from_a_billing_or_quota_error():
    """An automatic billing-triggered switch would silently change which model was measured.

    Checked structurally: the host resolver must not read any error/quota/billing state.
    """
    source = (TOOL_DIR / "groq_client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    resolver = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "resolve_deepseek_host"
    )
    names = {
        n.attr if isinstance(n, ast.Attribute) else n.id
        for n in ast.walk(resolver)
        if isinstance(n, (ast.Attribute, ast.Name))
    }
    forbidden = {"status_code", "error_class", "quota_exhausted", "exc", "balance", "insufficient"}
    assert not (names & forbidden), f"host resolution must not depend on error state: {names & forbidden}"


# ── telemetry provenance ──────────────────────────────────────────────────────────────


def test_bridge_calls_carry_host_provenance(monkeypatch):
    """A bridge measurement must never be indistinguishable from a canonical one."""
    captured: dict = {}

    def fake_post(settings, **kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "{}"}}]}, {}

    monkeypatch.setattr("groq_client._post_openai_chat_structured", fake_post)
    providers = _deepseek_providers(
        _settings(DEEPSEEK_HOST_NVIDIA, nvidia_key="nv"),
        instructions="x", user_payload="{}", json_schema={}, schema_name="s",
        model=None, mode="default", temperature=0,
    )
    _response, meta = providers[0].call()

    assert meta["llm_logical_model_intent"] == "deepseek"
    assert meta["llm_deepseek_host"] == "deepseek_nvidia"
    assert meta["llm_provider_role"] == "TEMPORARY_OPERATIONAL_BRIDGE"
    assert meta["llm_logical_model_family"] == "DeepSeek V4 Flash"
    assert meta["llm_exact_model_equivalence"] == "UNPROVEN"
    assert meta["llm_canonical_target"] == "deepseek_direct"
    # and the request genuinely went to the bridge endpoint/model
    assert captured["base_url"].startswith("https://integrate.api.nvidia.com")
    # family check only - snapshot equivalence is deliberately not asserted anywhere
    assert captured["model"] == EXPLICIT_BRIDGE_MODEL


def test_canonical_calls_are_labelled_canonical(monkeypatch):
    monkeypatch.setattr(
        "groq_client._post_openai_chat_structured",
        lambda settings, **kw: ({"choices": [{"message": {"content": "{}"}}]}, {}),
    )
    providers = _deepseek_providers(
        _settings(DEEPSEEK_HOST_DIRECT),
        instructions="x", user_payload="{}", json_schema={}, schema_name="s",
        model=None, mode="default", temperature=0,
    )
    _response, meta = providers[0].call()
    assert meta["llm_deepseek_host"] == "deepseek_direct"
    assert meta["llm_provider_role"] == "CANONICAL_TARGET"


# ── governance registry cannot drift from the runtime ────────────────────────────────


def test_provider_roles_registry_matches_the_runtime():
    registry = json.loads((TOOL_DIR / "provider_roles.json").read_text(encoding="utf-8"))

    assert registry["canonical_target"] == DEEPSEEK_HOST_DIRECT
    assert registry["logical_model_intent"] == "deepseek"
    assert set(registry["active_host_allowed_values"]) == set(DEEPSEEK_HOSTS)
    assert registry["active_host_env_var"] == "AI_OS_PRIMARY_PROVIDER"

    for host_name, key in ((DEEPSEEK_HOST_DIRECT, "direct-key"), (DEEPSEEK_HOST_NVIDIA, "nv")):
        entry = registry["providers"][host_name]
        resolved = resolve_deepseek_host(
            _settings(host_name, nvidia_key=key if host_name == DEEPSEEK_HOST_NVIDIA else "")
        )
        assert entry["role"] == resolved.role, host_name
        assert entry["telemetry_provider_label"] == resolved.provider, host_name
        assert entry["logical_model_family"] == resolved.model_family, host_name
        assert entry["exact_model_equivalence_to_canonical"] == (
            resolved.exact_model_equivalence_to_canonical
        ), host_name

    assert registry["logical_model_family"] == DEEPSEEK_MODEL_FAMILY
    assert registry["canonical_target_provider"] == DEEPSEEK_HOST_DIRECT
    assert registry["canonical_target_model"] == "deepseek-v4-flash"


# ── the corrected semantics: family preserved, snapshot equivalence not claimed ──────


def test_bridge_does_not_claim_proven_model_equivalence():
    """The bridge is 'same family, unproven snapshot' - asserting more would contaminate every
    measurement taken while it is active."""
    bridge = resolve_deepseek_host(_settings(DEEPSEEK_HOST_NVIDIA, nvidia_key="nv"))
    assert bridge.exact_model_equivalence_to_canonical == EQUIVALENCE_UNPROVEN
    assert bridge.role == ROLE_TEMPORARY_OPERATIONAL_BRIDGE


def test_canonical_host_is_equivalent_to_itself():
    canonical = resolve_deepseek_host(_settings(DEEPSEEK_HOST_DIRECT))
    assert canonical.exact_model_equivalence_to_canonical == EQUIVALENCE_PROVEN
    assert canonical.role == ROLE_CANONICAL_TARGET


def test_both_hosts_serve_the_same_declared_family():
    """What the bridge genuinely preserves, and the only identity claim it may make."""
    assert (
        resolve_deepseek_host(_settings(DEEPSEEK_HOST_NVIDIA, nvidia_key="nv")).model_family
        == resolve_deepseek_host(_settings(DEEPSEEK_HOST_DIRECT)).model_family
        == DEEPSEEK_MODEL_FAMILY
    )


def test_registry_no_longer_claims_preserved_model_identity():
    """Regression guard on the withdrawn claim itself.

    The old wording ('preserving the logical model identity') read as a proof that was never
    performed. If it reappears in the model policy, the correction has been undone.
    """
    registry = json.loads((TOOL_DIR / "provider_roles.json").read_text(encoding="utf-8"))
    bridge = registry["providers"][DEEPSEEK_HOST_NVIDIA]
    assert "preserving the logical model identity" not in bridge["model_policy"]
    assert bridge["temporary_model_snapshot"] == "deepseek-ai/deepseek-v4-flash-0731"


def test_registry_records_why_the_bridge_exists_and_how_to_leave_it():
    registry = json.loads((TOOL_DIR / "provider_roles.json").read_text(encoding="utf-8"))
    bridge = registry["providers"][DEEPSEEK_HOST_NVIDIA]
    assert bridge["reason"] == "DEEPSEEK_DIRECT_BILLING_UNAVAILABLE"
    assert bridge["return_runbook"].endswith("RETURN_TO_DEEPSEEK_DIRECT.md")
    assert "expires_when" in bridge
