"""Agent runtime configuration validation and doctor checks (PR-C)."""

from __future__ import annotations

from agent_runtime.primary_cutover import validate_primary_cutover_settings
from agent_runtime.settings import AgentRuntimeSettings


class AgentRuntimeConfigError(ValueError):
    """Raised when agent runtime cannot start safely."""


def validate_agent_runtime_settings(settings: AgentRuntimeSettings) -> list[str]:
    issues: list[str] = []
    mode = str(settings.mode or "").strip().lower()
    if mode == "primary":
        issues.append(
            "AGENT_RUNTIME_MODE=primary jest trwale wyłączony (prep = jedyny tryb agenta; HITL + guardrails)"
        )
    if mode not in {"prep", "primary", "legacy"}:
        issues.append(f"AGENT_RUNTIME_MODE invalid: {mode!r}")
    if not settings.enabled:
        return issues
    if mode == "legacy":
        issues.append("AGENT_RUNTIME_ENABLED=1 with mode=legacy is inconsistent (agent must be off in legacy)")
    if mode in {"prep", "primary"}:
        if not str(settings.openai_api_key or "").strip():
            issues.append("AGENT_OPENAI_API_KEY (or OPENAI_COMPAT_API_KEY) required when agent enabled")
        if settings.max_rounds < 1 or settings.max_rounds > 32:
            issues.append(f"AGENT_MAX_ROUNDS out of range (1–32): {settings.max_rounds}")
    if settings.kalk_top_base_url and not settings.kalk_top_agent_key:
        issues.append("KALK_TOP_AGENT_KEY recommended when KALK_TOP_BASE_URL is set")
    base_url = str(settings.openai_base_url or "").lower()
    if "openrouter.ai" in base_url and not str(settings.openai_api_key or "").strip():
        issues.append("AGENT_OPENAI_API_KEY required when AGENT_OPENAI_BASE_URL points to OpenRouter")
    issues.extend(validate_primary_cutover_settings(settings))
    return issues


def assert_agent_run_ready(settings: AgentRuntimeSettings) -> None:
    issues = validate_agent_runtime_settings(settings)
    if issues:
        raise AgentRuntimeConfigError("; ".join(issues))


def build_agent_doctor_check(settings: AgentRuntimeSettings) -> dict:
    issues = validate_agent_runtime_settings(settings)
    enabled = bool(settings.enabled)
    mode = str(settings.mode or "prep")
    if not enabled:
        status = "skipped"
    elif issues:
        status = "failed"
    else:
        status = "ok"
    from agent_runtime.primary_cutover import build_primary_cutover_doctor_check

    primary_check = build_primary_cutover_doctor_check(settings)
    if primary_check.get("status") == "failed":
        status = "failed"
        issues = list(dict.fromkeys([*issues, *primary_check.get("issues", [])]))
    from agent_runtime.mcp_service import build_agent_mcp_doctor_check

    mcp_check = build_agent_mcp_doctor_check()
    warnings: list[str] = []
    if settings.model_fallback and ":free" in settings.model_fallback.lower():
        warnings.append("AGENT_MODEL_FALLBACK uses a free-tier model (dev/OpenRouter)")
    if "openrouter.ai" in str(settings.openai_base_url or "").lower():
        warnings.append("AGENT_OPENAI_BASE_URL targets OpenRouter")
    return {
        "status": status,
        "warnings": warnings,
        "enabled": enabled,
        "mode": mode,
        "model": settings.model,
        "model_fallback": settings.model_fallback,
        "max_rounds": settings.max_rounds,
        "openai_configured": bool(str(settings.openai_api_key or "").strip()),
        "kalk_top_configured": bool(str(settings.kalk_top_base_url or "").strip()),
        "rag_enabled": bool(settings.rag_enabled),
        "primary_cutover": primary_check,
        "mcp": mcp_check,
        "issues": issues,
    }
