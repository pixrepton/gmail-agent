"""Configuration loading and validation for Gmail audit and intake tools."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from api_key_pool import parse_api_key_pool
from dotenv import dotenv_values, load_dotenv


DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_GROQ_BASE_URL = "https://api.groq.com"
# OpenAI-compatible inference API (official docs: https://inference-docs.cerebras.ai/introduction).
DEFAULT_CEREBRAS_OPENAI_BASE_URL = "https://api.cerebras.ai/v1"
DEFAULT_NVIDIA_OPENAI_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_NVIDIA_MODEL = "meta/llama-3.3-70b-instruct"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DEFAULT_GOOGLE_OAUTH_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
DEFAULT_HTTP_TIMEOUT = 60
DEFAULT_HTTP_MAX_RETRIES = 4
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
# DeepSeek official OpenAI-compatible endpoint (https://api-docs.deepseek.com). Priority-1
# structured-stage provider (OPERATOR_DECISIONS.md DEEPSEEK-MIGRATION-1): tried before the
# previously-first provider (Anthropic override, else the groq/cerebras/nvidia router chain).
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_REASONING_EFFORT = "high"

# ── DeepSeek tier hosting (DEEPSEEK-TEMP-BRIDGE-01) ──────────────────────────────────
# The DeepSeek tier expresses a *logical model intent* ("use DeepSeek"), which is separate from
# *who hosts it*. `deepseek_direct` is the canonical target and the default. `deepseek_nvidia`
# is a temporary bridge for use while DeepSeek Direct billing is unavailable; it keeps the same
# tier, prompts and schema contracts, and only changes the endpoint, credential and model id.
#
# The switch is explicit and operator-controlled. There is deliberately no automatic
# "fall back to NVIDIA when DeepSeek returns 402" behaviour: a hidden billing-triggered provider
# switch would silently change which model produced a measurement.
DEEPSEEK_HOST_DIRECT = "deepseek_direct"
DEEPSEEK_HOST_NVIDIA = "deepseek_nvidia"
DEEPSEEK_HOSTS = (DEEPSEEK_HOST_DIRECT, DEEPSEEK_HOST_NVIDIA)
DEFAULT_DEEPSEEK_HOST = DEEPSEEK_HOST_DIRECT
# NVIDIA NIM hosts DeepSeek models under their own ids. This must NOT default to NVIDIA_MODEL:
# that variable feeds the generic `nvidia` router slot (currently `gpt-oss-120b`) and reusing it
# would change the logical model while pretending only the host changed.
DEFAULT_DEEPSEEK_NVIDIA_MODEL = "deepseek-ai/deepseek-r1"
DEFAULT_DEEPSEEK_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_HTTP_RETRY_BASE_DELAY = 2.0
# Total wall-clock budget for one structured LLM stage, owned by the stage itself and shared
# down through provider fallback and per-attempt HTTP timeouts (see llm_deadline.py). It must
# stay comfortably above HTTP_TIMEOUT so retry and fallback are reachable rather than decorative.
DEFAULT_LLM_STAGE_BUDGET_SEC = 180
# Below this much remaining budget an HTTP attempt cannot produce a useful result, so the chain
# stops and reports instead of burning the remainder on a guaranteed timeout.
DEFAULT_LLM_MIN_ATTEMPT_SEC = 5
CANONICAL_ENV_FILENAMES = (".env",)
LEGACY_ENV_FILENAMES = (".env.local",)
DISCOVERABLE_ENV_FILENAMES = CANONICAL_ENV_FILENAMES + LEGACY_ENV_FILENAMES
CONFIG_DIR = Path(__file__).resolve().parent
ENV_FILE_OVERRIDE_VAR = "GMAIL_AGENT_ENV_FILE"
DEFAULT_MAILBOX_MEMORY_STAGE_MODE = "disabled"
DEFAULT_MAILBOX_MEMORY_BLOB_ROOT = Path(__file__).resolve().parent / "data" / "mailbox_memory" / "blobs"
DEFAULT_GOOGLE_DRIVE_BATCH_PAGE_SIZE = 100
DEFAULT_GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES = 10_000_000
DEFAULT_ATTACHMENT_EXTRACTION_ENABLED = True
DEFAULT_ATTACHMENT_EXTRACTION_MAX_BYTES = 8_000_000
DEFAULT_SIGNAL_RUNTIME_MODE = "active"
DEFAULT_GMAIL_INGRESS_OWNER = "signal_worker"
DEFAULT_SIGNAL_EXTRACTION_MODE = "llm"
DEFAULT_SKRZAT_ANSWER_MODE = "deterministic"
DEFAULT_PROJECTION_COMPOSER_MODE = "adaptive"
DEFAULT_EVENT_SPINE_PROCESSOR_MODE = "off"
DEFAULT_EVENT_SPINE_PROCESSOR_POLL_INTERVAL_SEC = 15
DEFAULT_EVENT_SPINE_PROCESSOR_BATCH_SIZE = 25
DEFAULT_INTAKE_LLM_BEFORE_SIGNAL = False
DEFAULT_DASZEK_OPERATIONAL_FEED_AUTO_PUSH = True
DEFAULT_DASZEK_OPERATIONAL_FEED_PUSH_MIN_INTERVAL_SEC = 60
DEFAULT_DASZEK_OPERATIONAL_FEED_CASE_LIMIT = 50
DEFAULT_DASZEK_OPERATIONAL_FEED_TASK_LIMIT = 80
DEFAULT_GMAIL_HISTORY_POLL_INTERVAL_SEC = 120
# SPINE-WORKER-TICK-01: drain agent_chat_jobs more often than Gmail history poll.
DEFAULT_AGENT_CHAT_JOBS_TICK_INTERVAL_SEC = 15
DEFAULT_AGENT_CHAT_JOBS_MAX_PER_TICK = 5
DEFAULT_DRIVE_CHANGES_POLL_INTERVAL_SEC = 180
DEFAULT_GMAIL_AGENT_OTEL_ENABLED = False
DEFAULT_GMAIL_AGENT_OTEL_LOCAL_MIRROR_ENABLED = True
DEFAULT_OTEL_SERVICE_NAME = "gmail-agent"
DEFAULT_MAILBOX_MEMORY_VECTOR_ENABLED = False
DEFAULT_DOCLING_ENABLED = False
# Single operator-facing production profile: strict validation when set (see docs/runbooks).
CANONICAL_PRODUCTION_RUNTIME_PROFILE = "canonical_production"
CASE_OS_RUNTIME_PROFILE_ENV = "CASE_OS_RUNTIME_PROFILE"
CASE_OS_PROFILE_MINIMAL = "minimal"
CASE_OS_PROFILE_FULL = "full"
EMERGENCY_INTELLIGENCE_KILLSWITCH_ENV = "EMERGENCY_INTELLIGENCE_KILLSWITCH"
CASE_OS_INTELLIGENCE_FLAG_NAMES: tuple[str, ...] = (
    "CASE_INTELLIGENCE_VNEXT_ENABLED",
    "UNDERSTANDING_OUTPUT_ENABLED",
    "DECISION_PIPELINE_ENABLED",
    "SERVICE_REQUEST_PLAYBOOK_ENABLED",
    "ACTION_PROPOSAL_V2_ENABLED",
)
GOOGLE_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
FORBIDDEN_GOOGLE_WRITE_SCOPES = (
    GOOGLE_GMAIL_SEND_SCOPE,
    GOOGLE_GMAIL_COMPOSE_SCOPE,
    GOOGLE_CALENDAR_EVENTS_SCOPE,
)
DEFAULT_DOCLING_MAX_PAGES = 40
DEFAULT_DOCLING_TIMEOUT_SEC = 45
DEFAULT_ATTACHMENT_PARSER_CHAIN = "docling,unstructured,legacy"
DEFAULT_UNSTRUCTURED_ENABLED = False
DEFAULT_DOCUMENT_STRUCTURED_FACTS = True
TRACKED_CONFIG_KEYS = (
    "LLM_BACKEND",
    "LLM_PRIMARY_PROVIDER",
    "LLM_FALLBACK_PROVIDERS",
    "LLM_STRUCTURED_PROVIDER_ALTERNATION",
    "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_MODEL",
    "OPENAI_COMPAT_API_KEY",
    "GROQ_API_KEY",
    "GROQ_API_KEYS",
    "GROQ_API_VL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "TOPINSTAL_COMPANY_CONTEXT_PATH",
    "GROQ_MODEL",
    "GROQ_BASE_URL",
    "CEREBRAS_API_KEY",
    "CEREBRAS_API_KEYS",
    "cerebras_api_key",
    "CEREBRAS_MODEL",
    "cerebras_model",
    "CEREBRAS_BASE_URL",
    "cerebras_base_url",
    "NVIDIA_API_KEY",
    "nvidia_api_key",
    "NVIDIA_MODEL",
    "nvidia_model",
    "NVIDIA_BASE_URL",
    "nvidia_base_url",
    "GOOGLE_ACCESS_TOKEN",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "GOOGLE_TOKEN_ENDPOINT",
    "GOOGLE_OAUTH_SCOPES",
    "HTTP_TIMEOUT",
    "HTTP_MAX_RETRIES",
    "HTTP_RETRY_BASE_DELAY",
    "DASZEK_BASE_URL",
    "DASZEK_LOGIN",
    "DASZEK_PASSWORD",
    "DASZEK_V2_PUSH",
    "DASZEK_V2_READBACK_ENABLED",
    "DASZEK_V2_DESK_RELAX_REJECTED",
    "DASZEK_V2_DESK_INCLUDE_IGNORE",
    "DASZEK_OPERATIONAL_FEED_AUTO_PUSH",
    "DASZEK_OPERATIONAL_FEED_PUSH_MIN_INTERVAL_SEC",
    "DASZEK_OPERATIONAL_FEED_CASE_LIMIT",
    "DASZEK_OPERATIONAL_FEED_TASK_LIMIT",
    "DASZEK_BRIDGE_TOKEN",
    "DASZEK_NODE_B_SERVICE_TOKEN",
    "CASE_GUIDANCE_ENABLED",
    "CASE_GUIDANCE_MODEL",
    "CASE_GUIDANCE_REMOTE_STATE",
    "ATTACHMENT_EXTRACTION_ENABLED",
    "ATTACHMENT_EXTRACTION_MAX_BYTES",
    "MAILBOX_MEMORY_DATABASE_URL",
    "DATABASE_URL",
    "MAILBOX_MEMORY_STAGE_MODE",
    "MAILBOX_MEMORY_STAGE_ALLOWLIST",
    "MAILBOX_MEMORY_BLOB_ROOT",
    "GOOGLE_DRIVE_ENABLED",
    "GOOGLE_DRIVE_CREDENTIALS_PATH",
    "GOOGLE_DRIVE_SHARED_DRIVE_ID",
    "GOOGLE_DRIVE_ROOT_FOLDER_ID",
    "GOOGLE_DRIVE_BATCH_PAGE_SIZE",
    "GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES",
    "GOOGLE_DRIVE_INGEST_ENABLED",
    "GOOGLE_DRIVE_GRAPH_ENABLED",
    "GOOGLE_CALENDAR_ENABLED",
    "GOOGLE_CALENDAR_ID",
    "NEO4J_PILOT_ENABLED",
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
    "GMAIL_AGENT_OTEL_ENABLED",
    "GMAIL_AGENT_OTEL_LOCAL_MIRROR_ENABLED",
    "OTEL_SERVICE_NAME",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "MAILBOX_MEMORY_VECTOR_ENABLED",
    "OPENAI_COMPAT_EMBEDDING_BASE_URL",
    "OPENAI_COMPAT_EMBEDDING_API_KEY",
    "OPENAI_COMPAT_EMBEDDING_MODEL",
    "OPENAI_COMPAT_EMBEDDING_DIMENSIONS",
    "DOCLING_ENABLED",
    "DOCLING_MAX_PAGES",
    "DOCLING_TIMEOUT_SEC",
    "DOCUMENT_INTELLIGENCE_PROMOTE_FACTS",
    "CASE_OS_RUNTIME_PROFILE",
    "GMAIL_AGENT_RUNTIME_PROFILE",
    "SIGNAL_RUNTIME_MODE",
    "USE_SIGNAL_RUNTIME",
    "UNIFIED_SIGNAL_RUNTIME_ENABLED",
    "SIGNAL_JOURNAL_JSONL_MIRROR_ENABLED",
    "GMAIL_CHANGE_DETECTION_ENABLED",
    "DRIVE_CHANGE_DETECTION_ENABLED",
    "SIGNAL_WORKER_ENABLED",
    "GMAIL_INGRESS_OWNER",
    "SIGNAL_RUNTIME_COMPAT",
    "INTAKE_LLM_BEFORE_SIGNAL",
    "EVENT_SPINE_PROCESSOR_ENABLED",
    "EVENT_SPINE_PROCESSOR_MODE",
    "EVENT_SPINE_PROCESSOR_POLL_INTERVAL_SEC",
    "EVENT_SPINE_PROCESSOR_BATCH_SIZE",
    "GMAIL_HISTORY_POLL_INTERVAL_SEC",
    "DRIVE_CHANGES_POLL_INTERVAL_SEC",
    "CASE_INTELLIGENCE_VNEXT_ENABLED",
    "UNDERSTANDING_OUTPUT_ENABLED",
    "DECISION_PIPELINE_ENABLED",
    "SERVICE_REQUEST_PLAYBOOK_ENABLED",
    "ACTION_PROPOSAL_V2_ENABLED",
    "DECISION_PIPELINE_DRY_RUN_ONLY",
    "AGENT_RUNTIME_ENABLED",
    "AGENT_RUNTIME_MODE",
    "AGENT_MODEL",
    "AGENT_MODEL_FALLBACK",
    "AGENT_MAX_ROUNDS",
    "AGENT_OPENAI_API_KEY",
    "AGENT_OPENAI_BASE_URL",
    "AGENT_CONSTITUTION_PATH",
    "AGENT_CONSTITUTION_RAG_ENABLED",
    "KALK_TOP_BASE_URL",
    "KALK_TOP_AGENT_KEY",
    "KALK_TOP_TIMEOUT_SEC",
    "KALK_TOP_MAX_RETRIES",
    "DASZEK_FEED_SOURCE",
    "AGENT_MCP_ALLOW_DEBUG",
)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(slots=True)
class Settings:
    """LLM routing: `groq` uses Groq Responses API; `openai_chat` uses Chat Completions (Ollama, Cerebras, etc.)."""


    llm_backend: str
    openai_compat_base_url: str
    openai_compat_api_key: str
    groq_api_key: str
    google_access_token: str
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    google_token_endpoint: str
    google_oauth_scopes: tuple[str, ...]
    groq_model: str
    groq_base_url: str
    daszek_base_url: str
    daszek_login: str
    daszek_password: str
    daszek_v2_push_enabled: bool
    case_guidance_enabled: bool
    case_guidance_model: str
    case_guidance_remote_state_enabled: bool
    # Always resolved from GROQ_MODEL regardless of llm_backend (unlike groq_model, which is
    # backend-context-dependent — see load_settings()). Defaulted so existing direct Settings(...)
    # construction sites (tests, doctor checks) that predate this field keep working; production
    # config always goes through load_settings(), which sets it explicitly.
    groq_native_model: str = DEFAULT_GROQ_MODEL
    daszek_operational_feed_auto_push_enabled: bool = DEFAULT_DASZEK_OPERATIONAL_FEED_AUTO_PUSH
    daszek_operational_feed_push_min_interval_sec: int = DEFAULT_DASZEK_OPERATIONAL_FEED_PUSH_MIN_INTERVAL_SEC
    daszek_operational_feed_case_limit: int = DEFAULT_DASZEK_OPERATIONAL_FEED_CASE_LIMIT
    daszek_operational_feed_task_limit: int = DEFAULT_DASZEK_OPERATIONAL_FEED_TASK_LIMIT
    attachment_extraction_enabled: bool = DEFAULT_ATTACHMENT_EXTRACTION_ENABLED
    attachment_extraction_max_bytes: int = DEFAULT_ATTACHMENT_EXTRACTION_MAX_BYTES
    mailbox_memory_database_url: str = ""
    mailbox_memory_blob_root: Path = DEFAULT_MAILBOX_MEMORY_BLOB_ROOT
    mailbox_memory_stage_mode: str = DEFAULT_MAILBOX_MEMORY_STAGE_MODE
    mailbox_memory_stage_allowlist: tuple[str, ...] = ()
    google_drive_enabled: bool = False
    google_drive_credentials_path: Path | None = None
    google_drive_shared_drive_id: str = ""
    google_drive_root_folder_id: str = ""
    google_drive_batch_page_size: int = DEFAULT_GOOGLE_DRIVE_BATCH_PAGE_SIZE
    google_drive_max_download_bytes: int = DEFAULT_GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES
    google_drive_ingest_enabled: bool = False
    google_drive_graph_enabled: bool = False
    google_calendar_enabled: bool = False
    google_calendar_id: str = "primary"
    neo4j_pilot_enabled: bool = False
    neo4j_uri: str = ""
    neo4j_username: str = ""
    neo4j_password: str = field(default="", repr=False)
    neo4j_database: str = "neo4j"
    gmail_agent_otel_enabled: bool = DEFAULT_GMAIL_AGENT_OTEL_ENABLED
    gmail_agent_otel_local_mirror_enabled: bool = DEFAULT_GMAIL_AGENT_OTEL_LOCAL_MIRROR_ENABLED
    otel_service_name: str = DEFAULT_OTEL_SERVICE_NAME
    otel_exporter_otlp_endpoint: str = ""
    otel_exporter_otlp_headers: str = field(default="", repr=False)
    mailbox_memory_vector_enabled: bool = DEFAULT_MAILBOX_MEMORY_VECTOR_ENABLED
    openai_compat_embedding_base_url: str = ""
    openai_compat_embedding_api_key: str = field(default="", repr=False)
    openai_compat_embedding_model: str = ""
    openai_compat_embedding_dimensions: int = 0
    docling_enabled: bool = DEFAULT_DOCLING_ENABLED
    docling_max_pages: int = DEFAULT_DOCLING_MAX_PAGES
    docling_timeout_sec: int = DEFAULT_DOCLING_TIMEOUT_SEC
    attachment_parser_chain_raw: str = DEFAULT_ATTACHMENT_PARSER_CHAIN
    attachment_parser_chain: tuple[str, ...] = ()
    unstructured_enabled: bool = DEFAULT_UNSTRUCTURED_ENABLED
    document_structured_facts_enabled: bool = DEFAULT_DOCUMENT_STRUCTURED_FACTS
    signal_runtime_mode: str = DEFAULT_SIGNAL_RUNTIME_MODE
    signal_extraction_mode: str = DEFAULT_SIGNAL_EXTRACTION_MODE
    skrzat_answer_mode: str = DEFAULT_SKRZAT_ANSWER_MODE
    projection_composer_mode: str = DEFAULT_PROJECTION_COMPOSER_MODE
    signal_journal_jsonl_mirror_enabled: bool = False
    gmail_change_detection_enabled: bool = False
    drive_change_detection_enabled: bool = False
    signal_worker_enabled: bool = False
    gmail_ingress_owner: str = DEFAULT_GMAIL_INGRESS_OWNER
    signal_runtime_compat: bool = False
    intake_llm_before_signal: bool = DEFAULT_INTAKE_LLM_BEFORE_SIGNAL
    event_spine_processor_enabled: bool = False
    event_spine_processor_mode: str = DEFAULT_EVENT_SPINE_PROCESSOR_MODE
    event_spine_processor_poll_interval_sec: int = DEFAULT_EVENT_SPINE_PROCESSOR_POLL_INTERVAL_SEC
    event_spine_processor_batch_size: int = DEFAULT_EVENT_SPINE_PROCESSOR_BATCH_SIZE
    case_intelligence_vnext_enabled: bool = False
    understanding_output_enabled: bool = False
    decision_pipeline_enabled: bool = False
    service_request_playbook_enabled: bool = False
    action_proposal_v2_enabled: bool = False
    decision_pipeline_dry_run_only: bool = True
    gmail_history_poll_interval_sec: int = DEFAULT_GMAIL_HISTORY_POLL_INTERVAL_SEC
    agent_chat_jobs_tick_interval_sec: int = DEFAULT_AGENT_CHAT_JOBS_TICK_INTERVAL_SEC
    agent_chat_jobs_max_per_tick: int = DEFAULT_AGENT_CHAT_JOBS_MAX_PER_TICK
    drive_changes_poll_interval_sec: int = DEFAULT_DRIVE_CHANGES_POLL_INTERVAL_SEC
    http_timeout: int = DEFAULT_HTTP_TIMEOUT
    http_max_retries: int = DEFAULT_HTTP_MAX_RETRIES
    http_retry_base_delay: float = DEFAULT_HTTP_RETRY_BASE_DELAY
    llm_stage_budget_sec: int = DEFAULT_LLM_STAGE_BUDGET_SEC
    llm_min_attempt_sec: int = DEFAULT_LLM_MIN_ATTEMPT_SEC
    env_path: Path | None = None
    config_sources: dict[str, str] = field(default_factory=dict, repr=False)
    config_warnings: list[str] = field(default_factory=list, repr=False)
    google_access_token_had_bearer_prefix: bool = False
    google_runtime_access_token: str = field(default="", repr=False)
    google_runtime_access_token_expires_at: float = field(default=0.0, repr=False)
    google_runtime_token_type: str = field(default="", repr=False)
    google_active_token_source: str = field(default="", repr=False)
    runtime_profile: str = ""
    case_os_runtime_profile: str = CASE_OS_PROFILE_MINIMAL
    daszek_bridge_token: str = field(default="", repr=False)
    daszek_node_b_service_token: str = field(default="", repr=False)
    daszek_v2_readback_enabled: bool = False
    daszek_v2_desk_relax_rejected: bool = False
    daszek_v2_desk_include_ignore: bool = False
    llm_primary_provider: str = "groq"
    llm_fallback_providers: tuple[str, ...] = ()
    groq_api_keys: tuple[str, ...] = ()
    cerebras_api_key: str = field(default="", repr=False)
    cerebras_api_keys: tuple[str, ...] = ()
    cerebras_model: str = DEFAULT_GROQ_MODEL
    cerebras_base_url: str = DEFAULT_CEREBRAS_OPENAI_BASE_URL
    openrouter_api_keys: tuple[str, ...] = ()
    openrouter_base_url: str = DEFAULT_OPENROUTER_BASE_URL
    openrouter_model: str = ""
    nvidia_api_key: str = field(default="", repr=False)
    nvidia_api_keys: tuple[str, ...] = ()
    nvidia_model: str = DEFAULT_NVIDIA_MODEL
    nvidia_base_url: str = DEFAULT_NVIDIA_OPENAI_BASE_URL
    #: When True and ``LLM_BACKEND=groq`` with both Groq and Cerebras configured, each structured
    #: LLM call uses a single provider, rotating ``groq`` -> ``cerebras`` -> ``groq`` per request
    #: (process-local counter). Primary/fallback ordering is bypassed for those calls.
    #: Default ON when env unset and both API keys are present; set ``LLM_STRUCTURED_PROVIDER_ALTERNATION=0`` to disable.
    llm_structured_provider_alternation: bool = True
    anthropic_api_key: str = field(default="", repr=False)
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_api_keys: tuple[str, ...] = ()
    deepseek_model: str = DEFAULT_DEEPSEEK_MODEL
    deepseek_base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    deepseek_thinking_enabled: bool = True
    deepseek_reasoning_effort: str = DEFAULT_DEEPSEEK_REASONING_EFFORT
    # Which host serves the DeepSeek tier. Canonical target is always deepseek_direct; the
    # nvidia host is a temporary, explicitly-selected bridge (DEEPSEEK-TEMP-BRIDGE-01).
    deepseek_host: str = DEFAULT_DEEPSEEK_HOST
    deepseek_nvidia_api_key: str = field(default="", repr=False)
    deepseek_nvidia_api_keys: tuple[str, ...] = ()
    deepseek_nvidia_model: str = DEFAULT_DEEPSEEK_NVIDIA_MODEL
    deepseek_nvidia_base_url: str = DEFAULT_DEEPSEEK_NVIDIA_BASE_URL

    @property
    def responses_url(self) -> str:
        """Return the normalized Groq Responses API endpoint."""
        base = self.groq_base_url.rstrip("/")
        if base.endswith("/openai/v1"):
            return f"{base}/responses"
        if base.endswith("/openai"):
            return f"{base}/v1/responses"
        return f"{base}/openai/v1/responses"

    @property
    def openai_chat_completions_url(self) -> str:
        """OpenAI-compatible Chat Completions URL (e.g. Ollama `http://127.0.0.1:11434/v1` → `.../v1/chat/completions`)."""
        raw = (self.openai_compat_base_url or "").strip().rstrip("/")
        if not raw:
            return ""
        if raw.endswith("/v1"):
            return f"{raw}/chat/completions"
        return f"{raw}/v1/chat/completions"

    @property
    def cerebras_chat_completions_url(self) -> str:
        """Cerebras OpenAI-compatible Chat Completions URL."""
        raw = (self.cerebras_base_url or "").strip().rstrip("/")
        if not raw:
            return ""
        if raw.endswith("/v1"):
            return f"{raw}/chat/completions"
        return f"{raw}/v1/chat/completions"

    @property
    def nvidia_chat_completions_url(self) -> str:
        """NVIDIA NIM OpenAI-compatible Chat Completions URL."""
        raw = (self.nvidia_base_url or "").strip().rstrip("/")
        if not raw:
            return ""
        if raw.endswith("/v1"):
            return f"{raw}/chat/completions"
        return f"{raw}/v1/chat/completions"

    @property
    def has_google_access_token(self) -> bool:
        """Return True when a static access token was configured."""
        return bool(self.google_access_token)

    @property
    def google_refresh_missing_fields(self) -> list[str]:
        """Return missing ENV keys for the refresh-token flow."""
        missing: list[str] = []
        if not self.google_client_id:
            missing.append("GOOGLE_CLIENT_ID")
        if not self.google_client_secret:
            missing.append("GOOGLE_CLIENT_SECRET")
        if not self.google_refresh_token:
            missing.append("GOOGLE_REFRESH_TOKEN")
        return missing

    @property
    def has_google_refresh_flow(self) -> bool:
        """Return True when the full refresh-token flow is configured."""
        return not self.google_refresh_missing_fields

    @property
    def google_refresh_partially_configured(self) -> bool:
        """Return True when refresh-token ENV exists but is incomplete."""
        configured_any = any(
            (
                self.google_client_id,
                self.google_client_secret,
                self.google_refresh_token,
            )
        )
        return configured_any and not self.has_google_refresh_flow

    @property
    def google_auth_mode(self) -> str:
        """Return the operator-facing Google auth mode."""
        if self.has_google_refresh_flow:
            return "refresh_token_auto"
        if self.has_google_access_token and self.google_refresh_partially_configured:
            return "access_token_manual_with_incomplete_refresh"
        if self.has_google_access_token:
            return "access_token_manual"
        if self.google_refresh_partially_configured:
            return "refresh_token_incomplete"
        return "missing"

    @property
    def mailbox_memory_enabled(self) -> bool:
        return bool(self.mailbox_memory_database_url) and self.mailbox_memory_stage_mode in {"shadow", "live"}

    @property
    def signal_runtime_enabled(self) -> bool:
        return self.signal_runtime_mode == "active"

    @property
    def canonical_production_active(self) -> bool:
        return (self.runtime_profile or "").strip().lower() == CANONICAL_PRODUCTION_RUNTIME_PROFILE


def _parse_runtime_profile() -> str:
    raw = os.getenv("GMAIL_AGENT_RUNTIME_PROFILE", "").strip().lower()
    if raw in {"", "default", "slice", "pack_slice"}:
        return ""
    if raw == CANONICAL_PRODUCTION_RUNTIME_PROFILE:
        return CANONICAL_PRODUCTION_RUNTIME_PROFILE
    raise ConfigError(
        "GMAIL_AGENT_RUNTIME_PROFILE must be empty, `default`, or "
        f"`{CANONICAL_PRODUCTION_RUNTIME_PROFILE}`. Got {raw!r}."
    )


def resolve_case_os_runtime_profile_name() -> str:
    """Return validated Case OS runtime profile (default: full).

    Emergency killswitch: EMERGENCY_INTELLIGENCE_KILLSWITCH=1 or CASE_OS_RUNTIME_PROFILE=minimal.
    """
    if _env_is_truthy(EMERGENCY_INTELLIGENCE_KILLSWITCH_ENV):
        return CASE_OS_PROFILE_MINIMAL
    raw = os.getenv(CASE_OS_RUNTIME_PROFILE_ENV, CASE_OS_PROFILE_FULL).strip().lower()
    if not raw:
        return CASE_OS_PROFILE_FULL
    if raw in {CASE_OS_PROFILE_MINIMAL, CASE_OS_PROFILE_FULL}:
        return raw
    raise ConfigError(
        f"{CASE_OS_RUNTIME_PROFILE_ENV} must be `{CASE_OS_PROFILE_MINIMAL}` or "
        f"`{CASE_OS_PROFILE_FULL}`. Got {raw!r}."
    )


def _case_os_profile_env_overrides(profile: str) -> dict[str, str]:
    """Map Case OS runtime profile to env overrides (single resolution point)."""
    # DQ-17: the profile speaks the canonical setting only. It must not emit the
    # deprecated AGENT_RUNTIME_ENABLED, or it would manufacture the very
    # mode/enabled contradiction the decision exists to forbid.
    if profile == CASE_OS_PROFILE_MINIMAL:
        overrides = {name: "0" for name in CASE_OS_INTELLIGENCE_FLAG_NAMES}
        overrides["AGENT_RUNTIME_MODE"] = "legacy"
        overrides["DECISION_PIPELINE_DRY_RUN_ONLY"] = "1"
        return overrides
    if profile == CASE_OS_PROFILE_FULL:
        overrides = {name: "1" for name in CASE_OS_INTELLIGENCE_FLAG_NAMES}
        overrides["AGENT_RUNTIME_MODE"] = "prep"
        overrides["DECISION_PIPELINE_DRY_RUN_ONLY"] = "0"
        overrides["DASZEK_FEED_SOURCE"] = "engagement_snapshot_v2"
        return overrides
    raise ConfigError(
        f"{CASE_OS_RUNTIME_PROFILE_ENV} must be `{CASE_OS_PROFILE_MINIMAL}` or "
        f"`{CASE_OS_PROFILE_FULL}`. Got {profile!r}."
    )


def validate_agent_runtime_mode_not_primary() -> None:
    """Permanent product rule: primary mode is not allowed (operator never wants autonomous outbound path)."""
    mode = (os.getenv("AGENT_RUNTIME_MODE", "") or "").strip().lower()
    if mode == "primary":
        raise ConfigError(
            "AGENT_RUNTIME_MODE=primary jest trwale wyłączony. "
            "Jedyny dozwolony tryb agenta to prep (HITL + policy guardrails). "
            "Usuń primary z env lub ustaw AGENT_RUNTIME_MODE=prep."
        )


AGENT_RUNTIME_PLANE_ENV_NAMES = ("AGENT_RUNTIME_MODE",)


def apply_case_os_agent_runtime_plane() -> str:
    """Resolve `AGENT_RUNTIME_MODE` from the Case OS profile alone (DQ-17).

    A restricting profile (`minimal`, incl. the emergency killswitch) assigns
    unconditionally — it must be able to force the agent off regardless of what an
    operator set. The permissive profile (`full`) only supplies the default branch
    via `setdefault`, so it never silently overwrites an explicit `AGENT_RUNTIME_MODE`
    the operator or the agent dotenv already set.

    Loads the agent dotenv first, so an operator's explicit local `AGENT_RUNTIME_MODE`
    is visible to the `setdefault` below regardless of whether this function or
    `load_agent_runtime_settings()` happens to run first in the process (RC-15).
    """
    try:
        from agent_runtime.settings import ensure_agent_runtime_env_loaded
    except Exception:  # pragma: no cover - agent runtime not importable
        pass
    else:
        ensure_agent_runtime_env_loaded()
    profile = resolve_case_os_runtime_profile_name()
    overrides = _case_os_profile_env_overrides(profile)
    restricting = profile == CASE_OS_PROFILE_MINIMAL
    for key in AGENT_RUNTIME_PLANE_ENV_NAMES:
        if key not in overrides:
            continue
        if restricting:
            os.environ[key] = overrides[key]
        else:
            os.environ.setdefault(key, overrides[key])
    return profile


def apply_case_os_runtime_profile_overrides() -> str:
    """Apply profile-derived env overrides before flag parsing. Returns profile name."""
    validate_agent_runtime_mode_not_primary()
    profile = resolve_case_os_runtime_profile_name()
    for key, value in _case_os_profile_env_overrides(profile).items():
        if key in AGENT_RUNTIME_PLANE_ENV_NAMES:
            continue
        os.environ[key] = value
    apply_case_os_agent_runtime_plane()
    return profile


def case_os_runtime_profile_flag_values(settings: Settings) -> dict[str, bool]:
    """Resolved intelligence flags from Settings (for logging and proof)."""
    return {
        "CASE_INTELLIGENCE_VNEXT_ENABLED": settings.case_intelligence_vnext_enabled,
        "UNDERSTANDING_OUTPUT_ENABLED": settings.understanding_output_enabled,
        "DECISION_PIPELINE_ENABLED": settings.decision_pipeline_enabled,
        "SERVICE_REQUEST_PLAYBOOK_ENABLED": settings.service_request_playbook_enabled,
        "ACTION_PROPOSAL_V2_ENABLED": settings.action_proposal_v2_enabled,
    }


def format_case_os_runtime_profile_startup_line(settings: Settings) -> str:
    """One-line startup summary: profile + derived flags + agent runtime mode."""
    from agent_runtime.settings import load_agent_runtime_settings

    flags = case_os_runtime_profile_flag_values(settings)
    flag_bits = ",".join(f"{name}={'1' if enabled else '0'}" for name, enabled in flags.items())
    agent = load_agent_runtime_settings()
    dry = "1" if settings.decision_pipeline_dry_run_only else "0"
    return (
        f"CASE_OS_RUNTIME profile={settings.case_os_runtime_profile} "
        f"flags=[{flag_bits}] "
        f"AGENT_RUNTIME_MODE={agent.mode} "
        f"agent_runtime_enabled={'1' if agent.enabled else '0'} "
        f"DECISION_PIPELINE_DRY_RUN_ONLY={dry}"
    )


def log_case_os_runtime_profile_startup(settings: Settings) -> None:
    """Log resolved Case OS runtime profile at application startup."""
    import logging

    logging.getLogger("gmail_agent.config").info(format_case_os_runtime_profile_startup_line(settings))


def canonical_production_violations(settings: Settings) -> list[str]:
    """Return human-readable violations when ``settings`` does not match the canonical production contract."""
    violations: list[str] = []
    if not str(settings.mailbox_memory_database_url or "").strip():
        violations.append("MAILBOX_MEMORY_DATABASE_URL must be set for canonical_production.")
    if settings.mailbox_memory_stage_mode != "live":
        violations.append(
            "MAILBOX_MEMORY_STAGE_MODE must be `live` for canonical_production (not shadow/disabled)."
        )
    if not settings.mailbox_memory_vector_enabled:
        violations.append("MAILBOX_MEMORY_VECTOR_ENABLED must be 1 for canonical_production.")
    if not str(settings.openai_compat_embedding_model or "").strip():
        violations.append("OPENAI_COMPAT_EMBEDDING_MODEL must be set when vectors are required.")
    if int(settings.openai_compat_embedding_dimensions or 0) <= 0:
        violations.append("OPENAI_COMPAT_EMBEDDING_DIMENSIONS must be a positive integer.")
    if not settings.docling_enabled:
        violations.append("DOCLING_ENABLED must be 1 for canonical_production.")
    if not settings.attachment_extraction_enabled:
        violations.append("ATTACHMENT_EXTRACTION_ENABLED must be 1 for canonical_production.")
    if not settings.google_drive_enabled or not settings.google_drive_ingest_enabled:
        violations.append("GOOGLE_DRIVE_ENABLED and GOOGLE_DRIVE_INGEST_ENABLED must be 1 for canonical_production.")
    if not str(settings.google_drive_root_folder_id or "").strip():
        violations.append("GOOGLE_DRIVE_ROOT_FOLDER_ID must be set for canonical_production Drive ingress.")
    if GOOGLE_DRIVE_READONLY_SCOPE not in settings.google_oauth_scopes:
        violations.append(
            f"GOOGLE_OAUTH_SCOPES must include {GOOGLE_DRIVE_READONLY_SCOPE} for canonical_production."
        )
    if settings.google_calendar_enabled and GOOGLE_CALENDAR_READONLY_SCOPE not in settings.google_oauth_scopes:
        violations.append(
            f"GOOGLE_OAUTH_SCOPES must include {GOOGLE_CALENDAR_READONLY_SCOPE} when Google Calendar is enabled."
        )
    if not settings.neo4j_pilot_enabled:
        violations.append("NEO4J_PILOT_ENABLED must be 1 for canonical_production.")
    if not (str(settings.neo4j_uri or "").strip() and str(settings.neo4j_username or "").strip()):
        violations.append("NEO4J_URI and NEO4J_USERNAME must be set when Neo4j pilot is required.")
    if not str(settings.neo4j_password or "").strip():
        violations.append("NEO4J_PASSWORD must be set when Neo4j pilot is required.")
    if not settings.gmail_agent_otel_local_mirror_enabled:
        violations.append(
            "GMAIL_AGENT_OTEL_LOCAL_MIRROR_ENABLED must be 1 for canonical_production (local telemetry mirror)."
        )
    if not str(settings.daszek_base_url or "").strip():
        violations.append("DASZEK_BASE_URL must be set for canonical_production Daszek readiness.")
    if not str(settings.daszek_login or "").strip():
        violations.append("DASZEK_LOGIN must be set for canonical_production Daszek readiness.")
    if not str(settings.daszek_password or "").strip():
        violations.append("DASZEK_PASSWORD must be set for canonical_production Daszek readiness.")
    try:
        from agent_runtime.settings import load_agent_runtime_settings

        agent = load_agent_runtime_settings()
        if agent.enabled:
            if str(agent.mode or "").strip().lower() != "primary":
                violations.append(
                    "AGENT_RUNTIME_MODE must be `primary` when the agent runtime is enabled in canonical_production."
                )
            feed_src = (os.getenv("DASZEK_FEED_SOURCE") or "").strip().lower()
            if feed_src in {"legacy", "mailbox_memory", "projection_v3"}:
                violations.append(
                    "DASZEK_FEED_SOURCE must not be legacy when agent runtime is enabled in canonical_production."
                )
            if not str(agent.openai_api_key or "").strip():
                violations.append("AGENT_OPENAI_API_KEY must be set when agent runtime is enabled in canonical_production.")
    except Exception as exc:
        import logging; logging.getLogger("config").warning("config: canonical_production validation error: %s", exc)
    return violations


def _validate_openai_compat_base_url(raw: str, *, field_name: str) -> None:
    parsed = urlparse(raw.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(
            f"{field_name} must be a valid http(s) URL with a host, "
            "e.g. http://127.0.0.1:11434/v1 for Ollama OpenAI compatibility."
        )


def _openai_compat_origin(raw: str) -> str:
    """Normalize scheme://host[:port] for comparing chat vs embedding OpenAI-compat bases."""
    text = (raw or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if parsed.port:
        return f"{parsed.scheme}://{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}"


def _provider_from_backend(llm_backend: str, selected_raw: str) -> str:
    if selected_raw == "cerebras":
        return "cerebras"
    if llm_backend == "openai_chat":
        return "openai_chat"
    return "groq"


def _parse_llm_provider(raw: str, *, default: str, field_name: str) -> str:
    provider = (raw or "").strip().lower() or default
    if provider not in {"groq", "openai_chat", "cerebras", "nvidia", "openrouter"}:
        raise ConfigError(
            f"{field_name} must be one of: groq, openai_chat, cerebras, nvidia, openrouter."
        )
    return provider


def _parse_llm_fallback_providers(raw: str, *, primary_provider: str) -> tuple[str, ...]:
    text = (raw or "").strip().lower()
    if not text:
        return ()
    providers: list[str] = []
    for item in text.replace(",", " ").split():
        provider = _parse_llm_provider(item, default="", field_name="LLM_FALLBACK_PROVIDERS")
        if provider == primary_provider or provider in providers:
            continue
        providers.append(provider)
    return tuple(providers)


def _first_nonempty_env(*keys: str) -> str:
    for key in keys:
        val = os.getenv(key, "").strip()
        if val:
            return val
    return ""


def load_settings(*, require_groq: bool = True, require_google: bool = True) -> Settings:
    """Load settings from .env and environment variables."""
    initial_env_values = _capture_initial_env_values()
    env_path = _load_env_file()
    case_os_runtime_profile = apply_case_os_runtime_profile_overrides()
    config_sources, config_warnings = _collect_config_source_details(
        initial_env_values,
        env_path,
    )

    llm_backend_raw = (os.getenv("LLM_BACKEND", "groq").strip().lower() or "groq")
    llm_backend_selected_raw = llm_backend_raw
    if llm_backend_raw == "cerebras":
        llm_backend = "openai_chat"
        config_warnings.append(
            "LLM_BACKEND=cerebras maps to OpenAI-compatible Chat Completions (Cerebras inference API)."
        )
    elif llm_backend_raw in ("groq", "openai_chat"):
        llm_backend = llm_backend_raw
    else:
        raise ConfigError("LLM_BACKEND must be `groq`, `openai_chat`, or `cerebras`.")

    cerebras_key = _first_nonempty_env("CEREBRAS_API_KEY", "cerebras_api_key")
    cerebras_api_keys = parse_api_key_pool(
        os.getenv("CEREBRAS_API_KEYS", ""),
        os.getenv("CEREBRAS_API_KEY", ""),
        os.getenv("cerebras_api_key", ""),
        os.getenv("AGENT_CEREBRAS_API_KEY", ""),
    )
    if cerebras_api_keys:
        cerebras_key = cerebras_api_keys[0]
    elif cerebras_key:
        cerebras_api_keys = (cerebras_key,)
    cerebras_model_env = _first_nonempty_env("CEREBRAS_MODEL", "cerebras_model")
    cerebras_base_override = _first_nonempty_env("CEREBRAS_BASE_URL", "cerebras_base_url")
    cerebras_base_url = cerebras_base_override or DEFAULT_CEREBRAS_OPENAI_BASE_URL
    cerebras_model = cerebras_model_env or DEFAULT_GROQ_MODEL

    nvidia_key = _first_nonempty_env("NVIDIA_API_KEY", "nvidia_api_key")
    nvidia_api_keys = parse_api_key_pool(
        os.getenv("NVIDIA_API_KEYS", ""),
        os.getenv("NVIDIA_API_KEY", ""),
        os.getenv("nvidia_api_key", ""),
        os.getenv("AGENT_NVIDIA_API_KEY", ""),
    )
    if nvidia_api_keys:
        nvidia_key = nvidia_api_keys[0]
    elif nvidia_key:
        nvidia_api_keys = (nvidia_key,)
    nvidia_model_env = _first_nonempty_env("NVIDIA_MODEL", "nvidia_model")
    nvidia_base_override = _first_nonempty_env("NVIDIA_BASE_URL", "nvidia_base_url")
    nvidia_base_url = nvidia_base_override or DEFAULT_NVIDIA_OPENAI_BASE_URL
    nvidia_model = nvidia_model_env or DEFAULT_NVIDIA_MODEL

    backend_primary_provider = _provider_from_backend(llm_backend, llm_backend_selected_raw)
    llm_primary_provider = _parse_llm_provider(
        os.getenv("LLM_PRIMARY_PROVIDER", ""),
        default=backend_primary_provider,
        field_name="LLM_PRIMARY_PROVIDER",
    )
    llm_fallback_providers = _parse_llm_fallback_providers(
        os.getenv("LLM_FALLBACK_PROVIDERS", ""),
        primary_provider=llm_primary_provider,
    )

    openai_compat_base_url = os.getenv("OPENAI_COMPAT_BASE_URL", "").strip()
    openai_compat_api_key = os.getenv("OPENAI_COMPAT_API_KEY", "").strip()

    if llm_backend == "openai_chat":
        compat_host_is_cerebras = False
        if openai_compat_base_url:
            try:
                compat_host_is_cerebras = "cerebras.ai" in (
                    urlparse(openai_compat_base_url).hostname or ""
                ).lower()
            except ValueError:
                compat_host_is_cerebras = False
        if not openai_compat_api_key and cerebras_key:
            if llm_backend_selected_raw == "cerebras" or compat_host_is_cerebras:
                openai_compat_api_key = cerebras_key
        if not openai_compat_base_url and cerebras_key and llm_backend_selected_raw == "cerebras":
            openai_compat_base_url = cerebras_base_url

    groq_api_keys = parse_api_key_pool(
        os.getenv("GROQ_API_KEYS", ""),
        os.getenv("GROQ_API_KEY", ""),
        os.getenv("GROQ_API_VL", ""),
        os.getenv("AGENT_GROQ_API_KEY", ""),
    )
    groq_api_key = groq_api_keys[0] if groq_api_keys else os.getenv("GROQ_API_KEY", "").strip()

    openrouter_base_url = (
        os.getenv("OPENROUTER_BASE_URL", "").strip()
        or os.getenv("AGENT_OPENAI_BASE_URL", "").strip()
        or os.getenv("OPENAI_COMPAT_BASE_URL", "").strip()
        or DEFAULT_OPENROUTER_BASE_URL
    )
    openrouter_model = (
        os.getenv("OPENROUTER_MODEL", "").strip()
        or os.getenv("OPENAI_COMPAT_MODEL", "").strip()
        or os.getenv("AGENT_MODEL", "").strip()
        or DEFAULT_GROQ_MODEL
    )
    openrouter_api_keys = parse_api_key_pool(
        os.getenv("OPENROUTER_API_KEYS", ""),
        os.getenv("OPENROUTER_API_KEY", ""),
        os.getenv("AGENT_OPENAI_API_KEY", ""),
        os.getenv("OPENAI_COMPAT_API_KEY", ""),
        os.getenv("AGENT_OPENAI_NATIVE_API_KEY", ""),
    )
    if openrouter_api_keys and not openai_compat_api_key:
        openai_compat_api_key = openrouter_api_keys[0]
    if openrouter_base_url and not openai_compat_base_url:
        openai_compat_base_url = openrouter_base_url

    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    anthropic_model = (
        os.getenv("ANTHROPIC_MODEL", "").strip() or DEFAULT_ANTHROPIC_MODEL
    )

    # DeepSeek (priority-1 structured-stage provider, see central_llm_stage.py). Absence of
    # DEEPSEEK_API_KEY must never raise here — it only means the tier-0 attempt is skipped and
    # the previously-first provider (anthropic, else the router chain) is used unchanged.
    deepseek_api_keys = parse_api_key_pool(
        os.getenv("DEEPSEEK_API_KEYS", ""),
        os.getenv("DEEPSEEK_API_KEY", ""),
    )
    deepseek_api_key = deepseek_api_keys[0] if deepseek_api_keys else os.getenv("DEEPSEEK_API_KEY", "").strip()
    deepseek_model = os.getenv("DEEPSEEK_MODEL", "").strip() or DEFAULT_DEEPSEEK_MODEL
    deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "").strip() or DEFAULT_DEEPSEEK_BASE_URL
    deepseek_thinking_enabled = _parse_bool_env("DEEPSEEK_THINKING_ENABLED", default=True)
    deepseek_reasoning_effort = (
        os.getenv("DEEPSEEK_REASONING_EFFORT", "").strip() or DEFAULT_DEEPSEEK_REASONING_EFFORT
    )

    # DEEPSEEK-TEMP-BRIDGE-01. AI_OS_PRIMARY_PROVIDER is the operator-facing switch; the older
    # DEEPSEEK_HOST name is accepted as an alias. Anything unrecognised is a configuration error
    # rather than a silent fallback to the canonical host: a typo must not quietly send a whole
    # measurement to a different provider than the operator believes is active.
    deepseek_host = (
        os.getenv("AI_OS_PRIMARY_PROVIDER", "").strip().lower()
        or os.getenv("DEEPSEEK_HOST", "").strip().lower()
        or DEFAULT_DEEPSEEK_HOST
    )
    if deepseek_host not in DEEPSEEK_HOSTS:
        raise ConfigError(
            f"AI_OS_PRIMARY_PROVIDER must be one of {', '.join(DEEPSEEK_HOSTS)} (got {deepseek_host!r}). "
            f"{DEEPSEEK_HOST_DIRECT} is the canonical target; {DEEPSEEK_HOST_NVIDIA} is a temporary bridge."
        )
    # The bridge credential is deliberately its own variable, falling back to NVIDIA_API_KEY so an
    # existing NVIDIA credential can be reused without duplication.
    deepseek_nvidia_api_keys = parse_api_key_pool(
        os.getenv("DEEPSEEK_NVIDIA_API_KEYS", ""),
        os.getenv("DEEPSEEK_NVIDIA_API_KEY", ""),
        os.getenv("NVIDIA_API_KEY", ""),
    )
    deepseek_nvidia_api_key = deepseek_nvidia_api_keys[0] if deepseek_nvidia_api_keys else ""
    deepseek_nvidia_model = (
        os.getenv("DEEPSEEK_NVIDIA_MODEL", "").strip() or DEFAULT_DEEPSEEK_NVIDIA_MODEL
    )
    deepseek_nvidia_base_url = (
        os.getenv("DEEPSEEK_NVIDIA_BASE_URL", "").strip()
        or os.getenv("NVIDIA_BASE_URL", "").strip()
        or DEFAULT_DEEPSEEK_NVIDIA_BASE_URL
    )

    google_access_token, google_access_token_had_bearer_prefix = normalize_google_access_token(
        os.getenv("GOOGLE_ACCESS_TOKEN", "")
    )
    google_client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    google_refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN", "").strip()
    google_token_endpoint = (
        os.getenv("GOOGLE_TOKEN_ENDPOINT", DEFAULT_GOOGLE_TOKEN_ENDPOINT).strip()
        or DEFAULT_GOOGLE_TOKEN_ENDPOINT
    )
    google_oauth_scopes = _parse_google_oauth_scopes(
        os.getenv("GOOGLE_OAUTH_SCOPES", ""),
        field_name="GOOGLE_OAUTH_SCOPES",
    )
    # groq_native_model is the real Groq-hosted model slug, always resolved from GROQ_MODEL
    # regardless of llm_backend — mirrors cerebras_model/nvidia_model, which are likewise
    # resolved independent of llm_backend. groq_model (below) is backend-context-dependent
    # (it means "whatever the currently-selected single backend's model is") and must never
    # be sent to the real Groq API on its own — only groq_native_model may be.
    groq_native_model = os.getenv("GROQ_MODEL", "").strip() or DEFAULT_GROQ_MODEL
    if llm_backend == "openai_chat":
        groq_model = (
            os.getenv("OPENAI_COMPAT_MODEL", "").strip()
            or cerebras_model_env
            or os.getenv("GROQ_MODEL", "").strip()
            or "llama3.2"
        )
        groq_base_url = os.getenv("GROQ_BASE_URL", DEFAULT_GROQ_BASE_URL).strip() or DEFAULT_GROQ_BASE_URL
    else:
        groq_model = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL).strip() or DEFAULT_GROQ_MODEL
        groq_base_url = os.getenv("GROQ_BASE_URL", DEFAULT_GROQ_BASE_URL).strip() or DEFAULT_GROQ_BASE_URL
    daszek_base_url = os.getenv("DASZEK_BASE_URL", "").strip()
    daszek_login = os.getenv("DASZEK_LOGIN", "").strip()
    daszek_password = os.getenv("DASZEK_PASSWORD", "").strip()
    daszek_v2_push_enabled = _parse_bool_env("DASZEK_V2_PUSH", default=False)
    feed_auto_raw = os.getenv("DASZEK_OPERATIONAL_FEED_AUTO_PUSH", "").strip()
    if feed_auto_raw == "":
        daszek_operational_feed_auto_push_enabled = not daszek_v2_push_enabled
    else:
        daszek_operational_feed_auto_push_enabled = _parse_bool_env(
            "DASZEK_OPERATIONAL_FEED_AUTO_PUSH",
            default=not daszek_v2_push_enabled,
        )
    daszek_operational_feed_push_min_interval_sec = _parse_positive_int(
        os.getenv(
            "DASZEK_OPERATIONAL_FEED_PUSH_MIN_INTERVAL_SEC",
            str(DEFAULT_DASZEK_OPERATIONAL_FEED_PUSH_MIN_INTERVAL_SEC),
        ).strip()
        or str(DEFAULT_DASZEK_OPERATIONAL_FEED_PUSH_MIN_INTERVAL_SEC),
        field_name="DASZEK_OPERATIONAL_FEED_PUSH_MIN_INTERVAL_SEC",
    )
    daszek_operational_feed_case_limit = _parse_positive_int(
        os.getenv(
            "DASZEK_OPERATIONAL_FEED_CASE_LIMIT",
            str(DEFAULT_DASZEK_OPERATIONAL_FEED_CASE_LIMIT),
        ).strip()
        or str(DEFAULT_DASZEK_OPERATIONAL_FEED_CASE_LIMIT),
        field_name="DASZEK_OPERATIONAL_FEED_CASE_LIMIT",
    )
    daszek_operational_feed_task_limit = _parse_positive_int(
        os.getenv(
            "DASZEK_OPERATIONAL_FEED_TASK_LIMIT",
            str(DEFAULT_DASZEK_OPERATIONAL_FEED_TASK_LIMIT),
        ).strip()
        or str(DEFAULT_DASZEK_OPERATIONAL_FEED_TASK_LIMIT),
        field_name="DASZEK_OPERATIONAL_FEED_TASK_LIMIT",
    )
    daszek_v2_readback_enabled = _parse_bool_env("DASZEK_V2_READBACK_ENABLED", default=False)
    daszek_v2_desk_relax_rejected = _parse_bool_env("DASZEK_V2_DESK_RELAX_REJECTED", default=False)
    daszek_v2_desk_include_ignore = _parse_bool_env("DASZEK_V2_DESK_INCLUDE_IGNORE", default=False)
    daszek_bridge_token = os.getenv("DASZEK_BRIDGE_TOKEN", "").strip()
    daszek_node_b_service_token = os.getenv("DASZEK_NODE_B_SERVICE_TOKEN", "").strip()
    case_guidance_enabled = _parse_bool_env("CASE_GUIDANCE_ENABLED", default=False)
    case_guidance_model = os.getenv("CASE_GUIDANCE_MODEL", "").strip() or groq_model
    case_guidance_remote_state_enabled = _parse_bool_env("CASE_GUIDANCE_REMOTE_STATE", default=False)
    attachment_extraction_enabled = _parse_bool_env(
        "ATTACHMENT_EXTRACTION_ENABLED",
        default=DEFAULT_ATTACHMENT_EXTRACTION_ENABLED,
    )
    attachment_extraction_max_bytes = _parse_positive_int(
        os.getenv(
            "ATTACHMENT_EXTRACTION_MAX_BYTES",
            str(DEFAULT_ATTACHMENT_EXTRACTION_MAX_BYTES),
        ).strip()
        or str(DEFAULT_ATTACHMENT_EXTRACTION_MAX_BYTES),
        field_name="ATTACHMENT_EXTRACTION_MAX_BYTES",
    )
    mailbox_memory_database_url = os.getenv("MAILBOX_MEMORY_DATABASE_URL", "").strip()
    legacy_database_url = os.getenv("DATABASE_URL", "").strip()
    if not mailbox_memory_database_url and legacy_database_url:
        mailbox_memory_database_url = legacy_database_url
        config_sources["MAILBOX_MEMORY_DATABASE_URL"] = "DATABASE_URL"
        config_warnings.append(
            "Using legacy DATABASE_URL fallback for MAILBOX_MEMORY_DATABASE_URL. "
            "Set MAILBOX_MEMORY_DATABASE_URL in tools/gmail_audit/.env."
        )
    mailbox_memory_blob_root_raw = os.getenv("MAILBOX_MEMORY_BLOB_ROOT", "").strip()
    mailbox_memory_stage_mode = (
        os.getenv("MAILBOX_MEMORY_STAGE_MODE", DEFAULT_MAILBOX_MEMORY_STAGE_MODE).strip().lower()
        or DEFAULT_MAILBOX_MEMORY_STAGE_MODE
    )
    mailbox_memory_stage_allowlist = _parse_csv_env("MAILBOX_MEMORY_STAGE_ALLOWLIST")
    google_drive_enabled = _parse_bool_env("GOOGLE_DRIVE_ENABLED", default=False)
    google_drive_credentials_path_raw = os.getenv("GOOGLE_DRIVE_CREDENTIALS_PATH", "").strip()
    google_drive_shared_drive_id = os.getenv("GOOGLE_DRIVE_SHARED_DRIVE_ID", "").strip()
    google_drive_root_folder_id = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "").strip()
    google_drive_batch_page_size = _parse_positive_int(
        os.getenv("GOOGLE_DRIVE_BATCH_PAGE_SIZE", str(DEFAULT_GOOGLE_DRIVE_BATCH_PAGE_SIZE)).strip()
        or str(DEFAULT_GOOGLE_DRIVE_BATCH_PAGE_SIZE),
        field_name="GOOGLE_DRIVE_BATCH_PAGE_SIZE",
    )
    google_drive_max_download_bytes = _parse_positive_int(
        os.getenv("GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES", str(DEFAULT_GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES)).strip()
        or str(DEFAULT_GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES),
        field_name="GOOGLE_DRIVE_MAX_DOWNLOAD_BYTES",
    )
    google_drive_ingest_enabled = _parse_bool_env("GOOGLE_DRIVE_INGEST_ENABLED", default=google_drive_enabled)
    google_drive_graph_enabled = _parse_bool_env("GOOGLE_DRIVE_GRAPH_ENABLED", default=False)
    google_calendar_enabled = _parse_bool_env("GOOGLE_CALENDAR_ENABLED", default=False)
    google_calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary").strip() or "primary"
    neo4j_pilot_enabled = _parse_bool_env("NEO4J_PILOT_ENABLED", default=False)
    neo4j_uri = os.getenv("NEO4J_URI", "").strip()
    neo4j_username = os.getenv("NEO4J_USERNAME", "").strip()
    neo4j_password = os.getenv("NEO4J_PASSWORD", "").strip()
    neo4j_database = os.getenv("NEO4J_DATABASE", "").strip() or "neo4j"
    gmail_agent_otel_enabled = _parse_bool_env(
        "GMAIL_AGENT_OTEL_ENABLED",
        default=DEFAULT_GMAIL_AGENT_OTEL_ENABLED,
    )
    gmail_agent_otel_local_mirror_enabled = _parse_bool_env(
        "GMAIL_AGENT_OTEL_LOCAL_MIRROR_ENABLED",
        default=DEFAULT_GMAIL_AGENT_OTEL_LOCAL_MIRROR_ENABLED,
    )
    otel_service_name = os.getenv("OTEL_SERVICE_NAME", DEFAULT_OTEL_SERVICE_NAME).strip() or DEFAULT_OTEL_SERVICE_NAME
    otel_exporter_otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    otel_exporter_otlp_headers = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "").strip()
    mailbox_memory_vector_enabled = _parse_bool_env(
        "MAILBOX_MEMORY_VECTOR_ENABLED",
        default=DEFAULT_MAILBOX_MEMORY_VECTOR_ENABLED,
    )
    openai_compat_embedding_model = os.getenv("OPENAI_COMPAT_EMBEDDING_MODEL", "").strip()
    embedding_dimensions_raw = os.getenv("OPENAI_COMPAT_EMBEDDING_DIMENSIONS", "").strip()
    if embedding_dimensions_raw:
        openai_compat_embedding_dimensions = _parse_positive_int(
            embedding_dimensions_raw,
            field_name="OPENAI_COMPAT_EMBEDDING_DIMENSIONS",
        )
    else:
        openai_compat_embedding_dimensions = 0
    docling_enabled = _parse_bool_env("DOCLING_ENABLED", default=DEFAULT_DOCLING_ENABLED)
    docling_max_pages = _parse_positive_int(
        os.getenv("DOCLING_MAX_PAGES", str(DEFAULT_DOCLING_MAX_PAGES)).strip()
        or str(DEFAULT_DOCLING_MAX_PAGES),
        field_name="DOCLING_MAX_PAGES",
    )
    docling_timeout_sec = _parse_positive_int(
        os.getenv("DOCLING_TIMEOUT_SEC", str(DEFAULT_DOCLING_TIMEOUT_SEC)).strip()
        or str(DEFAULT_DOCLING_TIMEOUT_SEC),
        field_name="DOCLING_TIMEOUT_SEC",
    )
    attachment_parser_chain_raw = (
        os.getenv("ATTACHMENT_PARSER_CHAIN", DEFAULT_ATTACHMENT_PARSER_CHAIN).strip()
        or DEFAULT_ATTACHMENT_PARSER_CHAIN
    )
    from document_parse_runtime import resolve_parser_chain_from_env

    attachment_parser_chain = resolve_parser_chain_from_env(attachment_parser_chain_raw)
    unstructured_enabled = _parse_bool_env("UNSTRUCTURED_ENABLED", default=DEFAULT_UNSTRUCTURED_ENABLED)
    document_structured_facts_enabled = _parse_bool_env(
        "DOCUMENT_STRUCTURED_FACTS",
        default=DEFAULT_DOCUMENT_STRUCTURED_FACTS,
    )
    signal_runtime_mode_raw = os.getenv("SIGNAL_RUNTIME_MODE", "").strip().lower()
    signal_runtime_alias = _resolve_signal_runtime_alias()
    signal_runtime_mode = signal_runtime_mode_raw or signal_runtime_alias or DEFAULT_SIGNAL_RUNTIME_MODE
    signal_extraction_mode_raw = os.getenv("SIGNAL_EXTRACTION_MODE", DEFAULT_SIGNAL_EXTRACTION_MODE).strip().lower()
    signal_extraction_mode = signal_extraction_mode_raw or DEFAULT_SIGNAL_EXTRACTION_MODE
    skrzat_answer_mode_raw = os.getenv("SKRZAT_ANSWER_MODE", DEFAULT_SKRZAT_ANSWER_MODE).strip().lower()
    skrzat_answer_mode = skrzat_answer_mode_raw or DEFAULT_SKRZAT_ANSWER_MODE
    projection_composer_mode_raw = os.getenv(
        "PROJECTION_COMPOSER_MODE",
        DEFAULT_PROJECTION_COMPOSER_MODE,
    ).strip().lower()
    projection_composer_mode = projection_composer_mode_raw or DEFAULT_PROJECTION_COMPOSER_MODE
    signal_journal_jsonl_mirror_enabled = _parse_bool_env("SIGNAL_JOURNAL_JSONL_MIRROR_ENABLED", default=False)
    gmail_change_detection_enabled = _parse_bool_env("GMAIL_CHANGE_DETECTION_ENABLED", default=False)
    drive_change_detection_enabled = _parse_bool_env("DRIVE_CHANGE_DETECTION_ENABLED", default=False)
    signal_worker_enabled = _parse_bool_env("SIGNAL_WORKER_ENABLED", default=False)
    gmail_ingress_owner = os.getenv("GMAIL_INGRESS_OWNER", DEFAULT_GMAIL_INGRESS_OWNER).strip().lower()
    signal_runtime_compat = _parse_bool_env("SIGNAL_RUNTIME_COMPAT", default=False)
    intake_llm_before_signal = _parse_bool_env("INTAKE_LLM_BEFORE_SIGNAL", default=DEFAULT_INTAKE_LLM_BEFORE_SIGNAL)
    event_spine_processor_enabled = _parse_bool_env("EVENT_SPINE_PROCESSOR_ENABLED", default=False)
    event_spine_processor_mode_raw = os.getenv("EVENT_SPINE_PROCESSOR_MODE", "").strip().lower()
    event_spine_processor_mode = event_spine_processor_mode_raw or DEFAULT_EVENT_SPINE_PROCESSOR_MODE
    if event_spine_processor_enabled and event_spine_processor_mode == "off":
        event_spine_processor_mode = "shadow"
    event_spine_processor_poll_interval_sec = _parse_positive_int(
        os.getenv(
            "EVENT_SPINE_PROCESSOR_POLL_INTERVAL_SEC",
            str(DEFAULT_EVENT_SPINE_PROCESSOR_POLL_INTERVAL_SEC),
        ).strip()
        or str(DEFAULT_EVENT_SPINE_PROCESSOR_POLL_INTERVAL_SEC),
        field_name="EVENT_SPINE_PROCESSOR_POLL_INTERVAL_SEC",
    )
    event_spine_processor_batch_size = _parse_positive_int(
        os.getenv(
            "EVENT_SPINE_PROCESSOR_BATCH_SIZE",
            str(DEFAULT_EVENT_SPINE_PROCESSOR_BATCH_SIZE),
        ).strip()
        or str(DEFAULT_EVENT_SPINE_PROCESSOR_BATCH_SIZE),
        field_name="EVENT_SPINE_PROCESSOR_BATCH_SIZE",
    )
    case_intelligence_vnext_enabled = _parse_bool_env("CASE_INTELLIGENCE_VNEXT_ENABLED", default=False)
    understanding_output_enabled = _parse_bool_env("UNDERSTANDING_OUTPUT_ENABLED", default=False)
    decision_pipeline_enabled = _parse_bool_env("DECISION_PIPELINE_ENABLED", default=False)
    service_request_playbook_enabled = _parse_bool_env("SERVICE_REQUEST_PLAYBOOK_ENABLED", default=False)
    action_proposal_v2_enabled = _parse_bool_env("ACTION_PROPOSAL_V2_ENABLED", default=False)
    decision_pipeline_dry_run_only = _parse_bool_env("DECISION_PIPELINE_DRY_RUN_ONLY", default=True)
    gmail_history_poll_interval_sec = _parse_positive_int(
        os.getenv("GMAIL_HISTORY_POLL_INTERVAL_SEC", str(DEFAULT_GMAIL_HISTORY_POLL_INTERVAL_SEC)).strip()
        or str(DEFAULT_GMAIL_HISTORY_POLL_INTERVAL_SEC),
        field_name="GMAIL_HISTORY_POLL_INTERVAL_SEC",
    )
    agent_chat_jobs_tick_interval_sec = _parse_positive_int(
        os.getenv(
            "AGENT_CHAT_JOBS_TICK_INTERVAL_SEC",
            str(DEFAULT_AGENT_CHAT_JOBS_TICK_INTERVAL_SEC),
        ).strip()
        or str(DEFAULT_AGENT_CHAT_JOBS_TICK_INTERVAL_SEC),
        field_name="AGENT_CHAT_JOBS_TICK_INTERVAL_SEC",
    )
    agent_chat_jobs_max_per_tick = _parse_positive_int(
        os.getenv(
            "AGENT_CHAT_JOBS_MAX_PER_TICK",
            str(DEFAULT_AGENT_CHAT_JOBS_MAX_PER_TICK),
        ).strip()
        or str(DEFAULT_AGENT_CHAT_JOBS_MAX_PER_TICK),
        field_name="AGENT_CHAT_JOBS_MAX_PER_TICK",
    )
    drive_changes_poll_interval_sec = _parse_positive_int(
        os.getenv("DRIVE_CHANGES_POLL_INTERVAL_SEC", str(DEFAULT_DRIVE_CHANGES_POLL_INTERVAL_SEC)).strip()
        or str(DEFAULT_DRIVE_CHANGES_POLL_INTERVAL_SEC),
        field_name="DRIVE_CHANGES_POLL_INTERVAL_SEC",
    )
    timeout_raw = os.getenv("HTTP_TIMEOUT", str(DEFAULT_HTTP_TIMEOUT)).strip() or str(DEFAULT_HTTP_TIMEOUT)
    retries_raw = (
        os.getenv("HTTP_MAX_RETRIES", str(DEFAULT_HTTP_MAX_RETRIES)).strip()
        or str(DEFAULT_HTTP_MAX_RETRIES)
    )
    retry_base_delay_raw = (
        os.getenv("HTTP_RETRY_BASE_DELAY", str(DEFAULT_HTTP_RETRY_BASE_DELAY)).strip()
        or str(DEFAULT_HTTP_RETRY_BASE_DELAY)
    )
    llm_stage_budget_raw = (
        os.getenv("LLM_STAGE_BUDGET_SEC", str(DEFAULT_LLM_STAGE_BUDGET_SEC)).strip()
        or str(DEFAULT_LLM_STAGE_BUDGET_SEC)
    )
    llm_min_attempt_raw = (
        os.getenv("LLM_MIN_ATTEMPT_SEC", str(DEFAULT_LLM_MIN_ATTEMPT_SEC)).strip()
        or str(DEFAULT_LLM_MIN_ATTEMPT_SEC)
    )

    missing: list[str] = []
    if require_groq:
        if llm_primary_provider == "groq" and not groq_api_key:
            missing.append("GROQ_API_KEY")
        if llm_primary_provider == "openai_chat":
            if not openai_compat_base_url:
                missing.append("OPENAI_COMPAT_BASE_URL")
            else:
                _validate_openai_compat_base_url(openai_compat_base_url, field_name="OPENAI_COMPAT_BASE_URL")
                try:
                    llm_host = (urlparse(openai_compat_base_url).hostname or "").lower()
                except ValueError:
                    llm_host = ""
                if "cerebras.ai" in llm_host and not openai_compat_api_key:
                    missing.append("OPENAI_COMPAT_API_KEY or CEREBRAS_API_KEY / cerebras_api_key")
        if llm_primary_provider == "cerebras":
            if not cerebras_base_url:
                missing.append("CEREBRAS_BASE_URL")
            else:
                _validate_openai_compat_base_url(cerebras_base_url, field_name="CEREBRAS_BASE_URL")
            if not cerebras_key:
                missing.append("CEREBRAS_API_KEY or cerebras_api_key")
        if llm_primary_provider == "nvidia":
            if not nvidia_base_url:
                missing.append("NVIDIA_BASE_URL")
            else:
                _validate_openai_compat_base_url(nvidia_base_url, field_name="NVIDIA_BASE_URL")
            if not nvidia_key:
                missing.append("NVIDIA_API_KEY or nvidia_api_key")
        if "cerebras" in llm_fallback_providers and cerebras_base_url:
            _validate_openai_compat_base_url(cerebras_base_url, field_name="CEREBRAS_BASE_URL")
        if "nvidia" in llm_fallback_providers and nvidia_base_url:
            _validate_openai_compat_base_url(nvidia_base_url, field_name="NVIDIA_BASE_URL")
    refresh_missing: list[str] = []
    refresh_values = (google_client_id, google_client_secret, google_refresh_token)
    has_refresh_any = any(refresh_values)
    has_refresh_full = bool(google_client_id and google_client_secret and google_refresh_token)

    if require_google and not (google_access_token or has_refresh_full):
        if has_refresh_any:
            if not google_client_id:
                refresh_missing.append("GOOGLE_CLIENT_ID")
            if not google_client_secret:
                refresh_missing.append("GOOGLE_CLIENT_SECRET")
            if not google_refresh_token:
                refresh_missing.append("GOOGLE_REFRESH_TOKEN")
        else:
            missing.append(
                "GOOGLE_ACCESS_TOKEN or GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN"
            )
    if missing or refresh_missing:
        parts: list[str] = []
        if missing:
            parts.append(", ".join(missing))
        if refresh_missing:
            parts.append(
                "incomplete Google refresh-token flow missing: " + ", ".join(refresh_missing)
            )
        joined = "; ".join(parts)
        raise ConfigError(
            "Missing required Google/LLM configuration: "
            f"{joined}. Set them in the shell or copy .env.example to .env."
        )

    openai_compat_embedding_base_url = (
        os.getenv("OPENAI_COMPAT_EMBEDDING_BASE_URL", "").strip() or openai_compat_base_url
    )
    if openai_compat_embedding_base_url:
        _validate_openai_compat_base_url(
            openai_compat_embedding_base_url,
            field_name="OPENAI_COMPAT_EMBEDDING_BASE_URL",
        )

    openai_compat_embedding_api_key_env = os.getenv("OPENAI_COMPAT_EMBEDDING_API_KEY", "").strip()
    if openai_compat_embedding_api_key_env:
        openai_compat_embedding_api_key = openai_compat_embedding_api_key_env
    elif _openai_compat_origin(openai_compat_embedding_base_url) == _openai_compat_origin(
        openai_compat_base_url
    ):
        openai_compat_embedding_api_key = openai_compat_api_key
    else:
        openai_compat_embedding_api_key = ""

    if mailbox_memory_vector_enabled and openai_compat_embedding_model and llm_backend == "openai_chat":
        try:
            emb_host = (urlparse(openai_compat_embedding_base_url).hostname or "").lower()
        except ValueError:
            emb_host = ""
        if "cerebras.ai" in emb_host:
            config_warnings.append(
                "MAILBOX_MEMORY_VECTOR_ENABLED with embeddings routed to api.cerebras.ai will fail: "
                "Cerebras inference does not expose OpenAI-compatible /v1/embeddings. "
                "Set OPENAI_COMPAT_EMBEDDING_BASE_URL to Ollama (e.g. http://ollama:11434/v1 in Docker)."
            )

    if mailbox_memory_stage_mode not in {"disabled", "shadow", "live"}:
        raise ConfigError("MAILBOX_MEMORY_STAGE_MODE must be one of: disabled, shadow, live.")
    if signal_runtime_mode != "active":
        if signal_runtime_mode in {"legacy", "shadow"}:
            raise ConfigError(
                f"SIGNAL_RUNTIME_MODE={signal_runtime_mode!r} is not supported. "
                "gmail-agent runs signal-active only. Unset SIGNAL_RUNTIME_MODE or set active."
            )
        raise ConfigError("SIGNAL_RUNTIME_MODE must be `active`.")
    if signal_runtime_compat:
        raise ConfigError(
            "SIGNAL_RUNTIME_COMPAT is not supported. gmail-agent uses signal-active only (no legacy tail)."
        )
    if gmail_ingress_owner and gmail_ingress_owner not in {"signal_worker", "legacy_cli"}:
        raise ConfigError("GMAIL_INGRESS_OWNER must be empty, signal_worker, or legacy_cli.")
    if signal_extraction_mode not in {"regex", "llm"}:
        raise ConfigError("SIGNAL_EXTRACTION_MODE must be one of: regex, llm.")
    if skrzat_answer_mode not in {"deterministic", "llm"}:
        raise ConfigError("SKRZAT_ANSWER_MODE must be one of: deterministic, llm.")
    if projection_composer_mode not in {"adaptive", "deterministic", "llm"}:
        raise ConfigError("PROJECTION_COMPOSER_MODE must be one of: adaptive, deterministic, llm.")
    if event_spine_processor_mode not in {"off", "shadow", "active"}:
        raise ConfigError("EVENT_SPINE_PROCESSOR_MODE must be one of: off, shadow, active.")
    if mailbox_memory_database_url:
        parsed_db = urlparse(mailbox_memory_database_url)
        if not parsed_db.scheme.startswith("postgres"):
            raise ConfigError("MAILBOX_MEMORY_DATABASE_URL must use a PostgreSQL URL scheme.")
    mailbox_memory_blob_root = (
        Path(mailbox_memory_blob_root_raw).expanduser().resolve()
        if mailbox_memory_blob_root_raw
        else DEFAULT_MAILBOX_MEMORY_BLOB_ROOT
    )
    google_drive_credentials_path = (
        Path(google_drive_credentials_path_raw).expanduser().resolve()
        if google_drive_credentials_path_raw
        else None
    )
    if google_drive_credentials_path is not None and not google_drive_credentials_path.is_file():
        raise ConfigError("GOOGLE_DRIVE_CREDENTIALS_PATH must point to an existing credentials JSON file.")

    http_timeout = _parse_positive_int(timeout_raw, field_name="HTTP_TIMEOUT")
    http_max_retries = _parse_positive_int(retries_raw, field_name="HTTP_MAX_RETRIES")

    try:
        http_retry_base_delay = float(retry_base_delay_raw)
    except ValueError as exc:
        raise ConfigError("HTTP_RETRY_BASE_DELAY must be a positive number.") from exc
    if http_retry_base_delay <= 0:
        raise ConfigError("HTTP_RETRY_BASE_DELAY must be greater than zero.")

    llm_stage_budget_sec = _parse_positive_int(llm_stage_budget_raw, field_name="LLM_STAGE_BUDGET_SEC")
    llm_min_attempt_sec = _parse_positive_int(llm_min_attempt_raw, field_name="LLM_MIN_ATTEMPT_SEC")
    # A stage budget at or below one HTTP attempt reproduces the exact defect this model exists to
    # remove: the envelope expires before the retry/fallback chain it wraps can do anything.
    if llm_stage_budget_sec <= http_timeout:
        raise ConfigError(
            f"LLM_STAGE_BUDGET_SEC ({llm_stage_budget_sec}s) must exceed HTTP_TIMEOUT ({http_timeout}s); "
            "otherwise provider retry and fallback can never run inside the stage budget."
        )
    if llm_min_attempt_sec >= llm_stage_budget_sec:
        raise ConfigError(
            f"LLM_MIN_ATTEMPT_SEC ({llm_min_attempt_sec}s) must be smaller than "
            f"LLM_STAGE_BUDGET_SEC ({llm_stage_budget_sec}s)."
        )

    _validate_https_url(
        google_token_endpoint,
        field_name="GOOGLE_TOKEN_ENDPOINT",
    )

    if google_access_token_had_bearer_prefix:
        config_warnings.append(
            "GOOGLE_ACCESS_TOKEN used a Bearer prefix; runtime normalized it to the raw access token."
        )
    if (CONFIG_DIR / ".env.local").is_file():
        config_warnings.append(
            "Legacy `tools/gmail_audit/.env.local` exists on disk but is never loaded. "
            "Merge any needed values into `tools/gmail_audit/.env` and delete `.env.local`."
        )

    runtime_profile = _parse_runtime_profile()

    llm_structured_provider_alternation = _parse_bool_env(
        "LLM_STRUCTURED_PROVIDER_ALTERNATION",
        default=True,
    )
    if llm_structured_provider_alternation:
        if llm_backend != "groq":
            config_warnings.append(
                "LLM_STRUCTURED_PROVIDER_ALTERNATION is ignored unless LLM_BACKEND=groq "
                f"(current backend is {llm_backend!r})."
            )
            llm_structured_provider_alternation = False
        elif not groq_api_keys or not cerebras_api_keys:
            config_warnings.append(
                "LLM_STRUCTURED_PROVIDER_ALTERNATION requires GROQ_API_KEY(S) and CEREBRAS_API_KEY(S) "
                "(plus CEREBRAS_BASE_URL); falling back to standard LLM_PRIMARY_PROVIDER / "
                "LLM_FALLBACK_PROVIDERS routing."
            )
            llm_structured_provider_alternation = False
        elif not str(cerebras_base_url or "").strip():
            config_warnings.append(
                "LLM_STRUCTURED_PROVIDER_ALTERNATION requires CEREBRAS_BASE_URL; "
                "falling back to standard provider routing."
            )
            llm_structured_provider_alternation = False

    settings = Settings(
        llm_backend=llm_backend,
        openai_compat_base_url=openai_compat_base_url,
        openai_compat_api_key=openai_compat_api_key,
        groq_api_key=groq_api_key,
        groq_api_keys=groq_api_keys,
        google_access_token=google_access_token,
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
        google_refresh_token=google_refresh_token,
        google_token_endpoint=google_token_endpoint,
        google_oauth_scopes=google_oauth_scopes,
        groq_model=groq_model,
        groq_native_model=groq_native_model,
        groq_base_url=groq_base_url,
        daszek_base_url=daszek_base_url,
        daszek_login=daszek_login,
        daszek_password=daszek_password,
        daszek_v2_push_enabled=daszek_v2_push_enabled,
        daszek_operational_feed_auto_push_enabled=daszek_operational_feed_auto_push_enabled,
        daszek_operational_feed_push_min_interval_sec=daszek_operational_feed_push_min_interval_sec,
        daszek_operational_feed_case_limit=daszek_operational_feed_case_limit,
        daszek_operational_feed_task_limit=daszek_operational_feed_task_limit,
        daszek_v2_readback_enabled=daszek_v2_readback_enabled,
        daszek_v2_desk_relax_rejected=daszek_v2_desk_relax_rejected,
        daszek_v2_desk_include_ignore=daszek_v2_desk_include_ignore,
        daszek_bridge_token=daszek_bridge_token,
        daszek_node_b_service_token=daszek_node_b_service_token,
        case_guidance_enabled=case_guidance_enabled,
        case_guidance_model=case_guidance_model,
        case_guidance_remote_state_enabled=case_guidance_remote_state_enabled,
        attachment_extraction_enabled=attachment_extraction_enabled,
        attachment_extraction_max_bytes=attachment_extraction_max_bytes,
        mailbox_memory_database_url=mailbox_memory_database_url,
        mailbox_memory_blob_root=mailbox_memory_blob_root,
        mailbox_memory_stage_mode=mailbox_memory_stage_mode,
        mailbox_memory_stage_allowlist=mailbox_memory_stage_allowlist,
        google_drive_enabled=google_drive_enabled,
        google_drive_credentials_path=google_drive_credentials_path,
        google_drive_shared_drive_id=google_drive_shared_drive_id,
        google_drive_root_folder_id=google_drive_root_folder_id,
        google_drive_batch_page_size=google_drive_batch_page_size,
        google_drive_max_download_bytes=google_drive_max_download_bytes,
        google_drive_ingest_enabled=google_drive_ingest_enabled,
        google_drive_graph_enabled=google_drive_graph_enabled,
        google_calendar_enabled=google_calendar_enabled,
        google_calendar_id=google_calendar_id,
        neo4j_pilot_enabled=neo4j_pilot_enabled,
        neo4j_uri=neo4j_uri,
        neo4j_username=neo4j_username,
        neo4j_password=neo4j_password,
        neo4j_database=neo4j_database,
        gmail_agent_otel_enabled=gmail_agent_otel_enabled,
        gmail_agent_otel_local_mirror_enabled=gmail_agent_otel_local_mirror_enabled,
        otel_service_name=otel_service_name,
        otel_exporter_otlp_endpoint=otel_exporter_otlp_endpoint,
        otel_exporter_otlp_headers=otel_exporter_otlp_headers,
        mailbox_memory_vector_enabled=mailbox_memory_vector_enabled,
        openai_compat_embedding_base_url=openai_compat_embedding_base_url,
        openai_compat_embedding_api_key=openai_compat_embedding_api_key,
        openai_compat_embedding_model=openai_compat_embedding_model,
        openai_compat_embedding_dimensions=openai_compat_embedding_dimensions,
        docling_enabled=docling_enabled,
        docling_max_pages=docling_max_pages,
        docling_timeout_sec=docling_timeout_sec,
        attachment_parser_chain_raw=attachment_parser_chain_raw,
        attachment_parser_chain=attachment_parser_chain,
        unstructured_enabled=unstructured_enabled,
        document_structured_facts_enabled=document_structured_facts_enabled,
        signal_runtime_mode=signal_runtime_mode,
        signal_extraction_mode=signal_extraction_mode,
        skrzat_answer_mode=skrzat_answer_mode,
        projection_composer_mode=projection_composer_mode,
        signal_journal_jsonl_mirror_enabled=signal_journal_jsonl_mirror_enabled,
        gmail_change_detection_enabled=gmail_change_detection_enabled,
        drive_change_detection_enabled=drive_change_detection_enabled,
        signal_worker_enabled=signal_worker_enabled,
        gmail_ingress_owner=gmail_ingress_owner,
        signal_runtime_compat=signal_runtime_compat,
        intake_llm_before_signal=intake_llm_before_signal,
        event_spine_processor_enabled=event_spine_processor_enabled,
        event_spine_processor_mode=event_spine_processor_mode,
        event_spine_processor_poll_interval_sec=event_spine_processor_poll_interval_sec,
        event_spine_processor_batch_size=event_spine_processor_batch_size,
        case_intelligence_vnext_enabled=case_intelligence_vnext_enabled,
        understanding_output_enabled=understanding_output_enabled,
        decision_pipeline_enabled=decision_pipeline_enabled,
        service_request_playbook_enabled=service_request_playbook_enabled,
        action_proposal_v2_enabled=action_proposal_v2_enabled,
        decision_pipeline_dry_run_only=decision_pipeline_dry_run_only,
        gmail_history_poll_interval_sec=gmail_history_poll_interval_sec,
        agent_chat_jobs_tick_interval_sec=agent_chat_jobs_tick_interval_sec,
        agent_chat_jobs_max_per_tick=agent_chat_jobs_max_per_tick,
        drive_changes_poll_interval_sec=drive_changes_poll_interval_sec,
        http_timeout=http_timeout,
        http_max_retries=http_max_retries,
        http_retry_base_delay=http_retry_base_delay,
        llm_stage_budget_sec=llm_stage_budget_sec,
        llm_min_attempt_sec=llm_min_attempt_sec,
        env_path=env_path,
        config_sources=config_sources,
        config_warnings=config_warnings,
        google_access_token_had_bearer_prefix=google_access_token_had_bearer_prefix,
        runtime_profile=runtime_profile,
        case_os_runtime_profile=case_os_runtime_profile,
        llm_primary_provider=llm_primary_provider,
        llm_fallback_providers=llm_fallback_providers,
        cerebras_api_key=cerebras_key,
        cerebras_api_keys=cerebras_api_keys,
        cerebras_model=cerebras_model,
        cerebras_base_url=cerebras_base_url,
        openrouter_api_keys=openrouter_api_keys,
        openrouter_base_url=openrouter_base_url,
        openrouter_model=openrouter_model,
        nvidia_api_key=nvidia_key,
        nvidia_api_keys=nvidia_api_keys,
        nvidia_model=nvidia_model,
        nvidia_base_url=nvidia_base_url,
        llm_structured_provider_alternation=llm_structured_provider_alternation,
        anthropic_api_key=anthropic_api_key,
        anthropic_model=anthropic_model,
        deepseek_api_key=deepseek_api_key,
        deepseek_api_keys=deepseek_api_keys,
        deepseek_model=deepseek_model,
        deepseek_base_url=deepseek_base_url,
        deepseek_thinking_enabled=deepseek_thinking_enabled,
        deepseek_reasoning_effort=deepseek_reasoning_effort,
        deepseek_host=deepseek_host,
        deepseek_nvidia_api_key=deepseek_nvidia_api_key,
        deepseek_nvidia_api_keys=deepseek_nvidia_api_keys,
        deepseek_nvidia_model=deepseek_nvidia_model,
        deepseek_nvidia_base_url=deepseek_nvidia_base_url,
    )
    if runtime_profile == CANONICAL_PRODUCTION_RUNTIME_PROFILE:
        viol = canonical_production_violations(settings)
        if viol:
            raise ConfigError(
                "GMAIL_AGENT_RUNTIME_PROFILE=canonical_production does not satisfy the contract: "
                + "; ".join(viol)
            )

    from dataclasses import replace

    from daszek_engagement_feed import engagement_feed_source_enabled

    if engagement_feed_source_enabled(settings):
        if settings.daszek_v2_push_enabled:
            config_warnings.append(
                "DASZEK_V2_PUSH=1 ignored when engagement feed source is active (Move 5)."
            )
            settings = replace(settings, daszek_v2_push_enabled=False)
    return settings


def _load_env_file() -> Path | None:
    """Load the first available local env file from tool dir or current dir."""
    candidates = default_env_candidates()

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return candidate
    return None


def default_env_candidates() -> list[Path]:
    """Return env-file candidates in resolution priority order.

    First existing file wins (load_dotenv override=False):
    GMAIL_AGENT_ENV_FILE -> tools/gmail_audit/.env -> repository root .env.
    See docs/dev/ENV_LOADING.md.
    """
    candidates: list[Path] = []
    explicit_override = os.getenv(ENV_FILE_OVERRIDE_VAR, "").strip()
    if explicit_override:
        candidates.append(Path(explicit_override).expanduser())
    repo_root = CONFIG_DIR.parent.parent
    return candidates + [CONFIG_DIR / name for name in CANONICAL_ENV_FILENAMES] + [
        repo_root / name for name in CANONICAL_ENV_FILENAMES
    ]


def existing_env_candidates() -> list[Path]:
    """Return present `.env` / `.env.local` paths for manifests and operator warnings (`.env.local` is never loaded)."""
    existing: list[Path] = []
    seen: set[Path] = set()
    discoverable = [CONFIG_DIR / name for name in DISCOVERABLE_ENV_FILENAMES] + [
        Path.cwd() / name for name in DISCOVERABLE_ENV_FILENAMES
    ]
    for candidate in discoverable:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file():
            existing.append(candidate)
    return existing


def _resolve_signal_runtime_alias() -> str:
    if _env_is_falsey("USE_SIGNAL_RUNTIME") or _env_is_falsey("UNIFIED_SIGNAL_RUNTIME_ENABLED"):
        raise ConfigError(
            "USE_SIGNAL_RUNTIME=0 / UNIFIED_SIGNAL_RUNTIME_ENABLED=0 are not supported. "
            "gmail-agent requires signal-active (SIGNAL_RUNTIME_MODE=active)."
        )
    compat_value = os.getenv("SIGNAL_RUNTIME_MODE", "").strip()
    if compat_value:
        return ""
    if _env_is_truthy("USE_SIGNAL_RUNTIME") or _env_is_truthy("UNIFIED_SIGNAL_RUNTIME_ENABLED"):
        return "active"
    return ""


def normalize_google_access_token(raw_value: str) -> tuple[str, bool]:
    """Return a normalized raw access token and whether a Bearer prefix was removed."""
    token = raw_value.strip()
    had_bearer_prefix = token.lower().startswith("bearer ")
    if had_bearer_prefix:
        token = token[7:].strip()
    return token, had_bearer_prefix


def _parse_google_oauth_scopes(raw_value: str, *, field_name: str) -> tuple[str, ...]:
    text = raw_value.strip()
    if not text:
        return DEFAULT_GOOGLE_OAUTH_SCOPES

    normalized = text.replace(",", " ")
    scopes = tuple(scope.strip() for scope in normalized.split() if scope.strip())
    if not scopes:
        raise ConfigError(f"{field_name} must contain at least one OAuth scope.")
    if DEFAULT_GOOGLE_OAUTH_SCOPES[0] not in scopes:
        raise ConfigError(
            f"{field_name} must include {DEFAULT_GOOGLE_OAUTH_SCOPES[0]}."
        )
    forbidden = tuple(scope for scope in scopes if scope in FORBIDDEN_GOOGLE_WRITE_SCOPES)
    if forbidden:
        raise ConfigError(
            f"{field_name} contains forbidden Google write scopes: {', '.join(forbidden)}. "
            "Node B is read-only for Gmail and Google Calendar."
        )
    return scopes


def _capture_initial_env_values() -> dict[str, str]:
    """Capture process-env values before any dotenv file is loaded."""
    return {key: os.getenv(key, "") for key in TRACKED_CONFIG_KEYS}


def _collect_config_source_details(
    initial_env_values: dict[str, str],
    env_path: Path | None,
) -> tuple[dict[str, str], list[str]]:
    """Return a redaction-safe summary of where tracked config values came from."""
    env_values: dict[str, str] = {}
    if env_path and env_path.is_file():
        raw_env_values = dotenv_values(env_path)
        env_values = {
            key: str(value).strip()
            for key, value in raw_env_values.items()
            if value is not None
        }

    sources: dict[str, str] = {
        "_loaded_env_file": str(env_path.resolve()) if env_path else "environment_only",
    }
    warnings: list[str] = []

    for key in TRACKED_CONFIG_KEYS:
        process_value = initial_env_values.get(key, "").strip()
        file_has_key = key in env_values
        file_value = env_values.get(key, "").strip()
        if process_value:
            sources[key] = "process_env"
            if file_has_key:
                warnings.append(
                    f"Process env overrides {env_path.name} for {key}."
                )
            continue
        if file_has_key and file_value:
            sources[key] = env_path.name if env_path else "environment_only"
            continue
        if file_has_key:
            sources[key] = f"{env_path.name} (empty)" if env_path else "environment_only (empty)"
            continue
        sources[key] = "unset"

    return sources, warnings


def _parse_positive_int(raw_value: str, *, field_name: str) -> int:
    try:
        parsed = int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{field_name} must be a positive integer.") from exc
    if parsed <= 0:
        raise ConfigError(f"{field_name} must be greater than zero.")
    return parsed


def _env_is_truthy(field_name: str) -> bool:
    return os.getenv(field_name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_is_falsey(field_name: str) -> bool:
    return os.getenv(field_name, "").strip().lower() in {"0", "false", "no", "off"}


def _parse_bool_env(field_name: str, *, default: bool) -> bool:
    raw_value = os.getenv(field_name, "").strip().lower()
    if raw_value == "":
        return default
    return raw_value in {"1", "true", "yes", "on"}


def document_intelligence_promote_facts_enabled() -> bool:
    """Append bounded document-intelligence fields to mailbox facts (provenance + confidence).

    Default ON for canonical runtime cohorts; set DOCUMENT_INTELLIGENCE_PROMOTE_FACTS=0 to disable.
    """
    return _parse_bool_env("DOCUMENT_INTELLIGENCE_PROMOTE_FACTS", default=True)


def document_structured_facts_enabled() -> bool:
    """When ON, structured document parses skip regex PHONE/CITY fact extraction on attachments."""
    return _parse_bool_env("DOCUMENT_STRUCTURED_FACTS", default=DEFAULT_DOCUMENT_STRUCTURED_FACTS)


def _parse_csv_env(field_name: str) -> tuple[str, ...]:
    raw_value = os.getenv(field_name, "").strip()
    if not raw_value:
        return ()
    items = [item.strip() for item in raw_value.split(",")]
    return tuple(item for item in items if item)


def _validate_https_url(raw_value: str, *, field_name: str) -> None:
    parsed = urlparse(raw_value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ConfigError(f"{field_name} must be a valid HTTPS URL.")
