"""Agent runtime settings (PR-C) — loaded from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PlannerLLMEndpoint:
    """One OpenAI-compatible chat-completions endpoint in the agent planner chain."""

    label: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    # DeepSeek thinking mode (DEEPSEEK-MIGRATION-1): empty/False for every non-DeepSeek
    # endpoint, so `extra_body` is never sent to providers that don't expect it.
    reasoning_effort: str = ""
    thinking_enabled: bool = False


DEFAULT_AGENT_MODEL = "gpt-4o-mini"
DEFAULT_AGENT_MODEL_FALLBACK = ""
DEFAULT_AGENT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_AGENT_RUNTIME_MODE = "prep"
DEFAULT_AGENT_MAX_ROUNDS = 12
DEFAULT_KALK_TOP_TIMEOUT_SEC = 4
DEFAULT_KALK_TOP_MAX_RETRIES = 3
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_REASONING_EFFORT = "high"
ENV_FILE_OVERRIDE_VAR = "GMAIL_AGENT_ENV_FILE"
CONFIG_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CONFIG_DIR.parent.parent
_DOTENV_LOADED = False


@dataclass(frozen=True)
class AgentRuntimeSettings:
    enabled: bool
    mode: str  # prep | primary | legacy
    model: str
    model_fallback: str
    max_rounds: int
    openai_api_key: str = field(repr=False)
    openai_base_url: str
    kalk_top_base_url: str
    kalk_top_agent_key: str = field(repr=False)
    kalk_top_timeout_sec: int
    kalk_top_max_retries: int
    constitution_path: str = ""
    rag_enabled: bool = False
    mailbox_database_url: str = ""
    staging_ttl_hours: int = 72


def load_agent_runtime_settings() -> AgentRuntimeSettings:
    from agent_runtime.validate import AgentRuntimeConfigError

    _load_agent_runtime_env_file()
    mode = (os.getenv("AGENT_RUNTIME_MODE", DEFAULT_AGENT_RUNTIME_MODE) or DEFAULT_AGENT_RUNTIME_MODE).strip().lower()
    if mode not in {"prep", "primary", "legacy"}:
        raise AgentRuntimeConfigError(
            f"AGENT_RUNTIME_MODE invalid: {mode!r} (expected prep|primary|legacy)"
        )
    return AgentRuntimeSettings(
        enabled=_parse_bool(os.getenv("AGENT_RUNTIME_ENABLED"), default=False),
        mode=mode,
        model=(os.getenv("AGENT_MODEL", DEFAULT_AGENT_MODEL) or DEFAULT_AGENT_MODEL).strip(),
        model_fallback=(
            os.getenv("AGENT_MODEL_FALLBACK", DEFAULT_AGENT_MODEL_FALLBACK) or DEFAULT_AGENT_MODEL_FALLBACK
        ).strip(),
        max_rounds=_parse_positive_int(os.getenv("AGENT_MAX_ROUNDS"), default=DEFAULT_AGENT_MAX_ROUNDS),
        openai_api_key=(os.getenv("AGENT_OPENAI_API_KEY") or os.getenv("OPENAI_COMPAT_API_KEY") or "").strip(),
        openai_base_url=(
            os.getenv("AGENT_OPENAI_BASE_URL")
            or os.getenv("OPENAI_COMPAT_BASE_URL")
            or DEFAULT_AGENT_OPENAI_BASE_URL
        ).strip()
        or DEFAULT_AGENT_OPENAI_BASE_URL,
        kalk_top_base_url=(os.getenv("KALK_TOP_BASE_URL") or "").strip().rstrip("/"),
        kalk_top_agent_key=(os.getenv("KALK_TOP_AGENT_KEY") or os.getenv("TOPINSTAL_CALC_AGENT_API_KEY") or "").strip(),
        kalk_top_timeout_sec=_parse_positive_int(
            os.getenv("KALK_TOP_TIMEOUT_SEC"),
            default=DEFAULT_KALK_TOP_TIMEOUT_SEC,
        ),
        kalk_top_max_retries=_parse_positive_int(
            os.getenv("KALK_TOP_MAX_RETRIES"),
            default=DEFAULT_KALK_TOP_MAX_RETRIES,
        ),
        constitution_path=(os.getenv("AGENT_CONSTITUTION_PATH") or "").strip(),
        rag_enabled=_parse_bool(os.getenv("AGENT_CONSTITUTION_RAG_ENABLED"), default=False),
        mailbox_database_url=(
            os.getenv("MAILBOX_MEMORY_DATABASE_URL")
            or os.getenv("DATABASE_URL")
            or ""
        ).strip(),
        staging_ttl_hours=_parse_positive_int(os.getenv("AGENT_STAGING_TTL_HOURS"), default=72),
    )


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_positive_int(raw: str | None, *, default: int) -> int:
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _env_first(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _pool_label(name: str, index: int) -> str:
    return name if index == 1 else f"{name}_{index}"


def build_agent_planner_endpoints(settings: AgentRuntimeSettings) -> list[PlannerLLMEndpoint]:
    """Ordered planner chain: DeepSeek → Cerebras → NVIDIA → Groq → OpenRouter → optional native OpenAI/Cursor.

    DeepSeek is priority-1 (DEEPSEEK-MIGRATION-1): tried before the previously-first provider
    (Cerebras) when ``DEEPSEEK_API_KEY`` is configured. Absent that key, no DeepSeek endpoint is
    appended and the chain is unchanged from before this migration.
    """
    from api_key_pool import parse_api_key_pool

    _load_agent_runtime_env_file()
    endpoints: list[PlannerLLMEndpoint] = []
    model = str(settings.model or DEFAULT_AGENT_MODEL).strip() or DEFAULT_AGENT_MODEL
    fallback_model = str(settings.model_fallback or "").strip()

    deepseek_url = _env_first("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL
    deepseek_model = _env_first("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL
    deepseek_thinking_enabled = _parse_bool(os.getenv("DEEPSEEK_THINKING_ENABLED"), default=True)
    deepseek_reasoning_effort = (
        _env_first("DEEPSEEK_REASONING_EFFORT") or DEFAULT_DEEPSEEK_REASONING_EFFORT
    )
    for index, deepseek_key in enumerate(
        parse_api_key_pool(
            os.getenv("DEEPSEEK_API_KEYS", ""),
            os.getenv("DEEPSEEK_API_KEY", ""),
        ),
        start=1,
    ):
        endpoints.append(
            PlannerLLMEndpoint(
                _pool_label("deepseek", index),
                deepseek_url,
                deepseek_key,
                deepseek_model,
                reasoning_effort=deepseek_reasoning_effort if deepseek_thinking_enabled else "",
                thinking_enabled=deepseek_thinking_enabled,
            )
        )

    cerebras_url = _env_first("AGENT_CEREBRAS_BASE_URL", "CEREBRAS_BASE_URL") or "https://api.cerebras.ai/v1"
    cerebras_model = _env_first("AGENT_CEREBRAS_MODEL", "CEREBRAS_MODEL") or model
    for index, cerebras_key in enumerate(
        parse_api_key_pool(
            os.getenv("CEREBRAS_API_KEYS", ""),
            os.getenv("CEREBRAS_API_KEY", ""),
            os.getenv("cerebras_api_key", ""),
            os.getenv("AGENT_CEREBRAS_API_KEY", ""),
        ),
        start=1,
    ):
        endpoints.append(PlannerLLMEndpoint(_pool_label("cerebras", index), cerebras_url, cerebras_key, cerebras_model))

    nvidia_url = (
        _env_first("AGENT_NVIDIA_BASE_URL", "NVIDIA_BASE_URL")
        or "https://integrate.api.nvidia.com/v1"
    )
    nvidia_model = (
        _env_first("AGENT_NVIDIA_MODEL", "NVIDIA_MODEL")
        or "meta/llama-3.3-70b-instruct"
    )
    for index, nvidia_key in enumerate(
        parse_api_key_pool(
            os.getenv("NVIDIA_API_KEYS", ""),
            os.getenv("NVIDIA_API_KEY", ""),
            os.getenv("nvidia_api_key", ""),
            os.getenv("AGENT_NVIDIA_API_KEY", ""),
        ),
        start=1,
    ):
        endpoints.append(PlannerLLMEndpoint(_pool_label("nvidia", index), nvidia_url, nvidia_key, nvidia_model))

    groq_url = _env_first("AGENT_GROQ_BASE_URL") or "https://api.groq.com/openai/v1"
    groq_model = _env_first("AGENT_GROQ_MODEL", "GROQ_MODEL") or model
    for index, groq_key in enumerate(
        parse_api_key_pool(
            os.getenv("GROQ_API_KEYS", ""),
            os.getenv("GROQ_API_KEY", ""),
            os.getenv("GROQ_API_VL", ""),
            os.getenv("AGENT_GROQ_API_KEY", ""),
        ),
        start=1,
    ):
        endpoints.append(PlannerLLMEndpoint(_pool_label("groq", index), groq_url, groq_key, groq_model))

    openrouter_url = (
        str(settings.openai_base_url or "").strip()
        or os.getenv("OPENROUTER_BASE_URL", "").strip()
        or "https://openrouter.ai/api/v1"
    )
    openrouter_keys = parse_api_key_pool(
        os.getenv("OPENROUTER_API_KEYS", ""),
        os.getenv("OPENROUTER_API_KEY", ""),
        os.getenv("AGENT_OPENAI_API_KEY", ""),
        os.getenv("OPENAI_COMPAT_API_KEY", ""),
        os.getenv("AGENT_OPENAI_NATIVE_API_KEY", ""),
        str(settings.openai_api_key or ""),
    )
    for index, openrouter_key in enumerate(openrouter_keys, start=1):
        label = _pool_label("openrouter", index)
        endpoints.append(PlannerLLMEndpoint(label, openrouter_url, openrouter_key, model))
        if fallback_model:
            endpoints.append(
                PlannerLLMEndpoint(f"{label}_fallback", openrouter_url, openrouter_key, fallback_model)
            )

    native_key = _env_first("AGENT_OPENAI_NATIVE_API_KEY", "OPENAI_API_KEY")
    if native_key:
        native_url = _env_first("AGENT_OPENAI_NATIVE_BASE_URL") or DEFAULT_AGENT_OPENAI_BASE_URL
        endpoints.append(PlannerLLMEndpoint("openai", native_url, native_key, model))

    cursor_key = _env_first("AGENT_CURSOR_API_KEY", "CURSOR_API_KEY")
    if cursor_key:
        cursor_url = _env_first("AGENT_CURSOR_BASE_URL", "CURSOR_API_BASE_URL") or "https://api.cursor.com/v1"
        endpoints.append(PlannerLLMEndpoint("cursor", cursor_url, cursor_key, model))

    if not endpoints and str(settings.openai_api_key or "").strip():
        endpoints.append(
            PlannerLLMEndpoint(
                "openai_compat",
                openrouter_url or DEFAULT_AGENT_OPENAI_BASE_URL,
                str(settings.openai_api_key),
                model,
            )
        )
    return endpoints


def _load_agent_runtime_env_file() -> Path | None:
    """Load the same local dotenv source the Gmail structured runtime uses."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return None
    provider_env_names = (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEYS",
        "CEREBRAS_API_KEY",
        "CEREBRAS_API_KEYS",
        "NVIDIA_API_KEY",
        "NVIDIA_API_KEYS",
        "GROQ_API_KEY",
        "GROQ_API_KEYS",
        "OPENROUTER_API_KEY",
        "OPENROUTER_API_KEYS",
        "AGENT_OPENAI_API_KEY",
        "OPENAI_COMPAT_API_KEY",
        "OPENAI_API_KEY",
        "CURSOR_API_KEY",
    )
    if any(os.getenv(name) for name in provider_env_names):
        _DOTENV_LOADED = True
        return None
    _DOTENV_LOADED = True
    try:
        from dotenv import load_dotenv
    except Exception:
        return None

    explicit = os.getenv(ENV_FILE_OVERRIDE_VAR, "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend([CONFIG_DIR / ".env", REPO_ROOT / ".env"])
    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return candidate
    return None
