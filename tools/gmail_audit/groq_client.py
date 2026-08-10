"""Minimal HTTP client for Groq Responses API with safer storage and retries."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from config import (
    DEEPSEEK_HOST_DIRECT,
    DEEPSEEK_HOST_NVIDIA,
    DEFAULT_DEEPSEEK_MODEL,
    Settings,
    normalize_google_access_token,
)
from llm_deadline import (
    DeadlineExhausted,
    attempt_timeout_sec,
    resolve_min_attempt_sec,
    retry_window_sec,
)
from llm_provider_router import LLMProvider, LLMRouter, LLMRouterError
from llm_provider_router import classify_provider_error
from log_config import get_logger
from redaction import sanitize_for_storage, sanitize_text

logger = get_logger(__name__)


def _budget_allows_attempt(settings: Settings) -> bool:
    """Is there enough of the stage/provider budget left for an attempt to mean anything?

    ``retry_window_sec()`` returns None outside a stage deadline, in which case the
    provider's own ``http_max_retries`` loop governs exactly as it did before.
    """
    window = retry_window_sec()
    if window is None:
        return True
    return window >= resolve_min_attempt_sec(settings)


def _deadline_exhausted_error(settings: Settings, *, provider: str, attempt: int, mode: str) -> DeadlineExhausted:
    window = retry_window_sec()
    return DeadlineExhausted(
        f"Stage budget exhausted before attempt {attempt} on {provider} "
        f"({0.0 if window is None else round(window, 2)}s remaining, "
        f"{resolve_min_attempt_sec(settings)}s needed).",
        details={
            "error_class": "deadline_exhausted",
            "provider": provider,
            "attempt": attempt,
            "mode": mode,
            "remaining_budget_ms": 0 if window is None else int(round(window * 1000)),
            "min_attempt_ms": int(round(resolve_min_attempt_sec(settings) * 1000)),
        },
    )


def _unconfigured_structured_provider(
    *,
    provider: str,
    backend: str,
    model: str,
    missing_config: str,
) -> LLMProvider:
    def _noop_call() -> tuple[dict[str, Any], dict[str, Any]]:
        return {}, {}

    return LLMProvider(
        provider=provider,
        backend=backend,
        model=model,
        call=_noop_call,
        configured=False,
        missing_config=missing_config,
    )


IMPORTANT_MAILS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "category": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "medium", "high"]},
            "why_important": {"type": "string"},
            "sender": {"type": "string"},
            "subject": {"type": "string"},
            "date": {"type": "string"},
        },
        "required": ["title", "summary", "category", "priority", "why_important"],
        "additionalProperties": False,
    },
}

RETRYABLE_HTTP_STATUSES = {408, 424, 429, 500, 502, 503, 504}
_RUNTIME_COOLDOWNS: dict[int, float] = {}
_RUNTIME_THROTTLE_LEVELS: dict[int, int] = {}
_GROQ_KEY_ROTATION_SEQ = 0
_GROQ_KEY_ROTATION_LOCK = threading.Lock()


def reset_structured_alternation_counter_for_tests() -> None:
    """Compatibility no-op: provider selection is now correlation-keyed."""


def reset_groq_key_rotation_counter_for_tests() -> None:
    """Reset process-local Groq key-pool rotation counter (tests only)."""

    global _GROQ_KEY_ROTATION_SEQ
    with _GROQ_KEY_ROTATION_LOCK:
        _GROQ_KEY_ROTATION_SEQ = 0


def _rotate_groq_key_pool(groq_keys: tuple[str, ...]) -> tuple[str, ...]:
    """Reorder a multi-key Groq pool so consecutive calls start from a different key.

    Full fallback order is preserved (every key is still tried on 429/5xx/timeout
    within one call), only the starting point rotates call-to-call. This spreads load
    across the pool preemptively instead of pinning all traffic to keys[0] until it
    hits a rate limit.
    """
    if len(groq_keys) <= 1:
        return groq_keys
    global _GROQ_KEY_ROTATION_SEQ
    with _GROQ_KEY_ROTATION_LOCK:
        offset = _GROQ_KEY_ROTATION_SEQ % len(groq_keys)
        _GROQ_KEY_ROTATION_SEQ += 1
    return groq_keys[offset:] + groq_keys[:offset]


def reset_structured_alternation_stage_slots_for_message() -> None:
    """Compatibility no-op retained for existing intake callers."""


def _alternation_slot_for_stage(
    stage_name: str | None,
    *,
    correlation_id: str,
) -> tuple[int, str, str]:
    """Select a provider from stable request identity, independent of process history."""

    stage_key = str(stage_name or "unscoped_stage").strip() or "unscoped_stage"
    correlation_key = str(correlation_id or "uncorrelated").strip() or "uncorrelated"
    digest = hashlib.sha256(f"{correlation_key}\x1f{stage_key}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 2
    slot = "groq" if bucket == 0 else "cerebras"
    return bucket, slot, digest[:16]


class GroqClientError(RuntimeError):
    """Raised for transport, API, or response parsing failures."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


@dataclass(slots=True)
class GroqResult:
    text: str
    response_json: dict[str, Any]
    request_meta: dict[str, Any]


def request_audit(
    settings: Settings,
    prompt: str,
    *,
    model: str | None = None,
    json_schema: dict[str, Any] | None = None,
    verbose: bool = False,
) -> GroqResult:
    """Send a single Responses API request with the official Gmail connector."""
    if getattr(settings, "llm_backend", "groq") == "openai_chat":
        raise GroqClientError(
            "Gmail via Groq connector (`request_audit`) requires LLM_BACKEND=groq. "
            "For LLM_BACKEND=openai_chat use --gmail-source google_api (direct Gmail API)."
        )
    from gmail_auth import GoogleOAuthError, build_google_auth_report, resolve_google_access_token

    try:
        access_token = resolve_google_access_token(
            settings,
            force_refresh=False,
        )
    except GoogleOAuthError as exc:
        raise GroqClientError(sanitize_text(str(exc))) from exc

    if verbose:
        debug_report = build_google_auth_report(settings)
        debug_report["connector_authorization_format"] = "raw_access_token"
        print(
            "[google-auth] "
            + json.dumps(debug_report, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )

    payload: dict[str, Any] = {
        "model": model or settings.groq_model,
        "input": prompt,
        "tools": [_gmail_connector_tool(access_token)],
    }

    # Groq currently rejects json_schema mode when combined with tool/MCP calling.
    _ = json_schema

    response_json, request_meta = _post_responses_payload(settings, payload)
    return _build_result(response_json, verbose=verbose, request_meta=request_meta)


def request_structured_output(
    settings: Settings,
    instructions: str,
    input_data: str | list[dict[str, Any]],
    *,
    json_schema: dict[str, Any],
    schema_name: str = "intake_output_v1",
    model: str | None = None,
    verbose: bool = False,
    input_variants: list[dict[str, Any]] | None = None,
    stage_name: str | None = None,
    providers_builder: Callable[[str, Any], list[LLMProvider]] | None = None,
    correlation_id: str | None = None,
    temperature: float = 0,
) -> GroqResult:
    """Send a schema-guided Responses API request without connector tools.

    ``providers_builder(mode, user_payload)`` replaces the normal named-chain resolution
    (primary/fallback/alternation) with an explicit provider list rebuilt fresh for each input
    variant — used by the DeepSeek priority-1 tier (``run_deepseek_structured_stage``) so
    DeepSeek is never also re-attempted as a member of the pre-existing chain it falls back to,
    while still degrading through ``input_variants`` like every other provider.
    """
    variants = input_variants or [{"mode": "default", "input": input_data, "metadata": {}}]
    resolved_correlation_id = str(correlation_id or "").strip()
    if not resolved_correlation_id:
        serialized_identity = (
            input_data
            if isinstance(input_data, str)
            else json.dumps(input_data, ensure_ascii=False, sort_keys=True)
        )
        resolved_correlation_id = "input:" + hashlib.sha256(serialized_identity.encode("utf-8")).hexdigest()
    variant_attempts: list[dict[str, Any]] = []
    degradations: list[dict[str, Any]] = []
    last_error: GroqClientError | None = None

    for index, variant in enumerate(variants):
        mode = str(variant.get("mode") or f"variant_{index + 1}")
        variant_payload = variant.get("input", input_data)
        try:
            response_json, request_meta = _post_structured_with_router(
                settings,
                instructions=instructions,
                user_payload=variant_payload,
                json_schema=json_schema,
                schema_name=schema_name,
                model=model,
                mode=mode,
                stage_name=stage_name,
                correlation_id=resolved_correlation_id,
                temperature=temperature,
                providers_override=(
                    providers_builder(mode, variant_payload) if providers_builder is not None else None
                ),
            )
            request_meta["variant_attempts"] = variant_attempts
            request_meta["degradations"] = degradations
            request_meta["final_inference_mode"] = mode
            request_meta["input_metadata"] = sanitize_for_storage(variant.get("metadata") or {})
            return _build_result(response_json, verbose=verbose, request_meta=request_meta)
        except GroqClientError as exc:
            last_error = exc
            details = dict(getattr(exc, "details", {}) or {})
            details["input_metadata"] = sanitize_for_storage(variant.get("metadata") or {})
            variant_attempts.append(
                {
                    "mode": mode,
                    "status": "failed",
                    "error": sanitize_text(str(exc)),
                    "details": sanitize_for_storage(details),
                }
            )
            should_degrade = index + 1 < len(variants) and (
                is_payload_too_large_error_message(str(exc))
                or is_rate_limit_error_message(str(exc))
            )
            if should_degrade:
                next_mode = str(variants[index + 1].get("mode") or f"variant_{index + 2}")
                degradations.append(
                    {
                        "from_mode": mode,
                        "to_mode": next_mode,
                        "reason": "payload_too_large" if is_payload_too_large_error_message(str(exc)) else "throttle_pressure",
                    }
                )
                continue
            raise

    raise last_error or GroqClientError("Unknown error while calling Groq Responses API.")


def run_structured_stage(
    settings: Settings,
    *,
    stage_name: str,
    instructions: str,
    prompt_input: dict[str, Any],
    json_schema: dict[str, Any],
    schema_name: str,
    model: str | None = None,
    verbose: bool = False,
    input_variants: list[dict[str, Any]] | None = None,
    temperature: float = 0,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Execute a structured stage call and return normalized call telemetry."""
    started = time.monotonic()
    serialized_input = json.dumps(prompt_input, indent=2, ensure_ascii=False)
    source_message = prompt_input.get("source_message") if isinstance(prompt_input.get("source_message"), dict) else {}
    resolved_correlation_id = str(
        correlation_id
        or source_message.get("message_id")
        or prompt_input.get("message_id")
        or prompt_input.get("signal_id")
        or prompt_input.get("case_id")
        or ""
    ).strip()
    result = request_structured_output(
        settings,
        instructions,
        serialized_input,
        json_schema=json_schema,
        schema_name=schema_name,
        model=model,
        verbose=verbose,
        input_variants=input_variants,
        stage_name=stage_name,
        correlation_id=resolved_correlation_id or None,
        temperature=temperature,
    )
    latency_ms = int(round((time.monotonic() - started) * 1000))
    request_meta = sanitize_for_storage(result.request_meta)
    attempts = int(request_meta.get("attempts_made") or 1)
    variant_attempts = request_meta.get("variant_attempts") or []
    fallback_used = bool(variant_attempts) or bool(request_meta.get("degradations")) or bool(request_meta.get("llm_fallback_used"))
    return {
        "stage_name": stage_name,
        "model_name": model or settings.groq_model,
        "attempt_count": attempts,
        "latency_ms": latency_ms,
        "fallback_used": fallback_used,
        "response_text": result.text,
        "parse_status": "received",
        "response_json": sanitize_for_storage(result.response_json),
        "request_meta": request_meta,
        "prompt_input": sanitize_for_storage(prompt_input),
    }


@dataclass(frozen=True, slots=True)
class DeepSeekHost:
    """Which host currently serves the DeepSeek tier, and how to reach it.

    The tier expresses a logical model intent ("use DeepSeek"). *Who hosts it* is a separate,
    operator-controlled decision, so that a billing outage on one host does not force a change
    to prompts, schema contracts or business logic.

    ``provider`` is the telemetry identity and is deliberately distinct per host: a measurement
    produced through NVIDIA NIM must never be indistinguishable from one produced by DeepSeek
    Direct.
    """

    host: str            # deepseek_direct | deepseek_nvidia
    # Telemetry identity. The canonical host deliberately keeps the long-standing "deepseek"
    # label: renaming it would make future canonical-mode measurements non-comparable with the
    # historical baselines (32/38, 38/38) that were recorded under it. Only the bridge takes a
    # new identity, which is what makes the two distinguishable.
    provider: str
    base_url: str
    model: str
    api_keys: tuple[str, ...]
    missing_config: str
    role: str            # CANONICAL_TARGET | TEMPORARY_BRIDGE

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_keys)


def resolve_deepseek_host(settings: Settings) -> DeepSeekHost:
    """Resolve the active DeepSeek host from configuration alone.

    Note what this does *not* consult: ``LLM_BACKEND``. The DeepSeek tier passes an explicit
    ``base_url``/``api_key``/``model`` to the OpenAI-compatible adapter, so host selection is
    independent of the backend variable and its URL/model-resolution coupling.
    """
    host = str(getattr(settings, "deepseek_host", "") or DEEPSEEK_HOST_DIRECT).strip().lower()

    if host == DEEPSEEK_HOST_NVIDIA:
        keys = tuple(getattr(settings, "deepseek_nvidia_api_keys", ()) or ())
        if not keys and str(getattr(settings, "deepseek_nvidia_api_key", "") or "").strip():
            keys = (str(settings.deepseek_nvidia_api_key).strip(),)
        return DeepSeekHost(
            host=DEEPSEEK_HOST_NVIDIA,
            provider=DEEPSEEK_HOST_NVIDIA,
            base_url=str(getattr(settings, "deepseek_nvidia_base_url", "") or "").strip(),
            model=str(getattr(settings, "deepseek_nvidia_model", "") or "").strip(),
            api_keys=keys,
            missing_config="DEEPSEEK_NVIDIA_API_KEY",
            role="TEMPORARY_BRIDGE",
        )

    keys = tuple(getattr(settings, "deepseek_api_keys", ()) or ())
    if not keys and str(getattr(settings, "deepseek_api_key", "") or "").strip():
        keys = (str(settings.deepseek_api_key).strip(),)
    return DeepSeekHost(
        host=DEEPSEEK_HOST_DIRECT,
        provider="deepseek",
        base_url=str(getattr(settings, "deepseek_base_url", "") or "").strip(),
        model=str(getattr(settings, "deepseek_model", "") or "").strip(),
        api_keys=keys,
        missing_config="DEEPSEEK_API_KEY",
        role="CANONICAL_TARGET",
    )


def deepseek_configured(settings: Settings) -> bool:
    """Is the DeepSeek tier usable on its currently selected host?"""
    return resolve_deepseek_host(settings).configured


def _deepseek_thinking_payload(settings: Settings) -> dict[str, Any]:
    if not bool(getattr(settings, "deepseek_thinking_enabled", True)):
        return {}
    effort = str(getattr(settings, "deepseek_reasoning_effort", "") or "high").strip().lower() or "high"
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}


def deepseek_error_allows_fallback(exc: Exception) -> bool:
    """Return True for DeepSeek provider failures, False for permanent adapter/config bugs."""
    info = classify_provider_error(exc)
    if info.retryable:
        return True
    text = str(exc).lower()
    if "invalid request" in text or "unsupported parameter" in text or "tool_choice" in text:
        return False
    if "401" in text or "403" in text or "invalid api key" in text or "unauthorized" in text or "forbidden" in text:
        return True
    # Empty assistant text / empty message.content is delivery, not a permanent adapter bug.
    # Match both spaced and backtick forms ("empty content" vs "empty `message.content`").
    if info.error_class == "empty_content":
        return True
    if "json" in text and "parse" in text:
        return True
    return False


def _deepseek_providers(
    settings: Settings,
    *,
    instructions: str,
    user_payload: Any,
    json_schema: dict[str, Any],
    schema_name: str,
    model: str | None,
    mode: str | None,
    temperature: float,
) -> list[LLMProvider]:
    """Single-provider chain for the DeepSeek priority-1 tier (see run_deepseek_structured_stage).

    The endpoint, credential and model come from the currently selected host
    (``deepseek_direct`` or the temporary ``deepseek_nvidia`` bridge). Everything else about the
    tier -- prompts, schema contract, thinking payload, deadline, retry/fallback -- is identical
    across hosts by design, so switching hosts changes where the call goes and nothing else.
    """
    host = resolve_deepseek_host(settings)
    provider_model = str(model or host.model or "").strip() or DEFAULT_DEEPSEEK_MODEL
    deepseek_base_url = host.base_url
    deepseek_keys = host.api_keys
    if not deepseek_keys or not deepseek_base_url:
        return [
            _unconfigured_structured_provider(
                provider=host.provider,
                backend="openai_compatible",
                model=provider_model,
                missing_config=host.missing_config,
            )
        ]
    thinking_payload = _deepseek_thinking_payload(settings)
    providers: list[LLMProvider] = []
    for key_index, deepseek_api_key in enumerate(deepseek_keys):
        def call_deepseek(
            provider_model: str = provider_model,
            _api_key: str = deepseek_api_key,
            _key_index: int = key_index,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            synthetic, request_meta = _post_openai_chat_structured(
                settings,
                instructions=instructions,
                user_payload=user_payload,
                json_schema=json_schema,
                model=provider_model,
                mode=mode,
                schema_name=schema_name,
                base_url=deepseek_base_url,
                api_key=_api_key,
                extra_payload=thinking_payload,
                temperature=temperature,
            )
            request_meta["llm_backend"] = "deepseek"
            request_meta["llm_api_key_slot"] = _key_index + 1
            request_meta["llm_deepseek_thinking_enabled"] = bool(thinking_payload)
            request_meta["llm_temperature_effective"] = (
                "provider_managed" if thinking_payload else float(temperature)
            )
            # Provenance: which host actually served this call, and in what role. Without these
            # a bridge measurement and a canonical measurement look identical after the fact.
            request_meta["llm_logical_model_intent"] = "deepseek"
            request_meta["llm_deepseek_host"] = host.host
            request_meta["llm_provider_role"] = host.role
            request_meta["llm_canonical_target"] = DEEPSEEK_HOST_DIRECT
            return synthetic, request_meta

        providers.append(
            LLMProvider(
                provider=host.provider,
                backend="openai_compatible",
                model=provider_model,
                call=call_deepseek,
                configured=bool(deepseek_base_url and deepseek_api_key),
                missing_config=host.missing_config,
            )
        )
    return providers


def run_deepseek_structured_stage(
    settings: Settings,
    *,
    stage_name: str,
    instructions: str,
    prompt_input: dict[str, Any],
    json_schema: dict[str, Any],
    schema_name: str,
    model: str | None = None,
    verbose: bool = False,
    input_variants: list[dict[str, Any]] | None = None,
    temperature: float = 0,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Priority-1 structured-stage attempt via DeepSeek only.

    Mirrors ``run_structured_stage``'s return shape exactly so callers (central_llm_stage.py)
    can treat a DeepSeek result identically to a router-chain result. Raises GroqClientError on
    any failure (including "not configured") — the caller is responsible for falling back to
    the pre-existing chain, unchanged, exactly as it already does for the Anthropic tier.
    """
    started = time.monotonic()
    serialized_input = json.dumps(prompt_input, indent=2, ensure_ascii=False)

    def _build_deepseek_providers(mode: str, user_payload: Any) -> list[LLMProvider]:
        return _deepseek_providers(
            settings,
            instructions=instructions,
            user_payload=user_payload,
            json_schema=json_schema,
            schema_name=schema_name,
            model=model,
            mode=mode,
            temperature=temperature,
        )

    result = request_structured_output(
        settings,
        instructions,
        serialized_input,
        json_schema=json_schema,
        schema_name=schema_name,
        model=model,
        verbose=verbose,
        input_variants=input_variants,
        stage_name=stage_name,
        providers_builder=_build_deepseek_providers,
        correlation_id=correlation_id,
        temperature=temperature,
    )
    latency_ms = int(round((time.monotonic() - started) * 1000))
    request_meta = sanitize_for_storage(result.request_meta)
    attempts = int(request_meta.get("attempts_made") or 1)
    return {
        "stage_name": stage_name,
        "model_name": model or getattr(settings, "deepseek_model", "") or "deepseek-v4-flash",
        "attempt_count": attempts,
        "latency_ms": latency_ms,
        "fallback_used": False,
        "response_text": result.text,
        "parse_status": "received",
        "response_json": sanitize_for_storage(result.response_json),
        "request_meta": request_meta,
        "prompt_input": sanitize_for_storage(prompt_input),
    }


def extract_response_text(response_json: dict[str, Any]) -> str:
    """Extract assistant text from several possible Responses API shapes."""
    direct = response_json.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct

    chunks: list[str] = []
    output = response_json.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue

            if item.get("type") == "message":
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") in {"output_text", "text"}:
                            text = part.get("text")
                            if isinstance(text, str):
                                chunks.append(text)
                continue

            content = item.get("content")
            if isinstance(content, str):
                chunks.append(content)

    return "\n".join(chunk.strip() for chunk in chunks if chunk and chunk.strip()).strip()


def extract_mcp_output(response_json: dict[str, Any], *, tool_name: str) -> str:
    """Return raw output for a specific mcp_call item."""
    output = response_json.get("output")
    if not isinstance(output, list):
        raise GroqClientError("Missing `output` in Groq response.")

    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "mcp_call":
            continue
        if str(item.get("name") or "").strip() != tool_name:
            continue
        raw = item.get("output")
        if isinstance(raw, str) and raw.strip():
            return raw

    raise GroqClientError(f"Missing raw mcp_call output for tool `{tool_name}`.")


def parse_important_mails_json(text: str) -> list[dict[str, str]]:
    """Parse and validate the strict JSON array returned by important-mails."""
    candidate = extract_json_candidate(text)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise GroqClientError(f"Model did not return valid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise GroqClientError("Model returned JSON, but it is not an array of objects.")

    validated: list[dict[str, str]] = []
    required = {"title", "summary", "category", "priority", "why_important"}
    optional = {"sender", "subject", "date"}
    allowed = required | optional

    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise GroqClientError(f"Element #{index} is not a JSON object.")

        keys = set(item.keys())
        missing = required - keys
        extra = keys - allowed
        if missing:
            joined = ", ".join(sorted(missing))
            raise GroqClientError(f"Element #{index} is missing required fields: {joined}.")
        if extra:
            joined = ", ".join(sorted(extra))
            raise GroqClientError(f"Element #{index} contains forbidden fields: {joined}.")

        normalized: dict[str, str] = {}
        for field in sorted(allowed):
            if field not in item:
                continue
            value = item[field]
            if not isinstance(value, str):
                raise GroqClientError(f"Field `{field}` in element #{index} must be a string.")
            normalized[field] = value.strip()

        if normalized["priority"] not in {"low", "medium", "high"}:
            raise GroqClientError(
                f"Field `priority` in element #{index} must be low, medium, or high."
            )

        validated.append(normalized)

    return validated


def summarize_output_types(response_json: dict[str, Any]) -> str:
    """Return a compact summary of top-level output item types."""
    output = response_json.get("output")
    if not isinstance(output, list):
        return ""

    types: list[str] = []
    for item in output:
        if isinstance(item, dict):
            item_type = item.get("type")
            if isinstance(item_type, str):
                types.append(item_type)
    return ", ".join(types)


def extract_json_candidate(text: str) -> str:
    """Extract the most likely JSON object or array from raw model text."""
    stripped = _strip_markdown_fences(text).strip()
    if not stripped:
        return stripped

    if stripped[0] in "[{":
        fragment = _extract_balanced_json_fragment(stripped, 0)
        return fragment or stripped

    starts = [index for index in (stripped.find("{"), stripped.find("[")) if index >= 0]
    if not starts:
        return stripped

    for start in sorted(starts):
        fragment = _extract_balanced_json_fragment(stripped, start)
        if fragment:
            return fragment

    return stripped[min(starts):].strip()


def is_auth_error_message(message: str) -> bool:
    """Return True when an error message looks like config/auth failure."""
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "401 unauthorized",
            "403 forbidden",
            "invalid credentials",
            "token expired",
            "access token expired",
            "refresh token",
            "invalid_grant",
            "invalid_rapt",
            "invalid_client",
            "invalid api key",
            "missing permissions",
            "gmail.readonly scope",
            "google oauth",
            "token refresh",
            "csrf",
            "daszek login failed",
        )
    )


def is_rate_limit_error_message(message: str) -> bool:
    """Return True when an error is caused by rate limiting or throttling."""
    lowered = message.lower()
    return "429" in lowered or "too many requests" in lowered or "rate limit" in lowered


def is_payload_too_large_error_message(message: str) -> bool:
    """Return True when an error indicates the request payload is too large."""
    lowered = message.lower()
    return "413" in lowered or "request too large" in lowered or "reduce your message size" in lowered


def format_connector_tool_error(raw_text: str, *, tool_name: str) -> str:
    """Translate raw connector tool errors into operator-friendly guidance."""
    message = sanitize_text(raw_text.strip())
    lowered = message.lower()
    prefix = f"{tool_name} failed:"
    if (
        ("401" in lowered and "invalid credentials" in lowered)
        or "token expired" in lowered
        or "access token expired" in lowered
    ):
        return (
            f"{prefix} Gmail access token is invalid or expired. "
            "Either refresh GOOGLE_ACCESS_TOKEN manually or configure GOOGLE_CLIENT_ID "
            "and GOOGLE_REFRESH_TOKEN, plus GOOGLE_CLIENT_SECRET when your OAuth client uses one, then rerun "
            "`python tools/gmail_audit/gmail_intake.py doctor`."
        )
    if "missing required authentication credential" in lowered or "unauthorized" in lowered:
        return (
            f"{prefix} Gmail access token is missing, invalid, or expired. "
            "Provide a fresh GOOGLE_ACCESS_TOKEN with gmail.readonly scope or enable refresh-token flow, "
            "then rerun doctor."
        )
    if "403" in lowered:
        return (
            f"{prefix} Gmail connector refused access. "
            "Check that the active Google OAuth token has "
            "https://www.googleapis.com/auth/gmail.readonly scope."
        )
    return f"{prefix} {message}"


def _raise_empty_assistant_text(response_json: dict[str, Any], *, surface: str) -> None:
    """Raise a typed empty-content failure for Responses-shaped provider payloads."""
    output_types = summarize_output_types(response_json)
    raise GroqClientError(
        f"{surface} response does not contain final assistant text. "
        f"Detected output types: {output_types or 'none'}.",
        details={
            "error_class": "empty_content",
            "output_types": output_types,
            "has_reasoning_content": False,
        },
    )


def _ensure_nonempty_assistant_text(response_json: dict[str, Any], *, surface: str) -> str:
    text = extract_response_text(response_json)
    if not text.strip():
        _raise_empty_assistant_text(response_json, surface=surface)
    return text


def _build_result(
    response_json: dict[str, Any],
    *,
    verbose: bool,
    request_meta: dict[str, Any] | None = None,
) -> GroqResult:
    text = _ensure_nonempty_assistant_text(response_json, surface="Groq")

    if verbose:
        output_types = summarize_output_types(response_json)
        if output_types:
            print(f"[verbose] Response output types: {output_types}", file=sys.stderr, flush=True)

    return GroqResult(
        text=text,
        response_json=response_json,
        request_meta=request_meta or {},
    )


def _post_structured_with_router(
    settings: Settings,
    *,
    instructions: str,
    user_payload: Any,
    json_schema: dict[str, Any],
    schema_name: str,
    model: str | None,
    mode: str | None,
    stage_name: str | None = None,
    correlation_id: str,
    temperature: float,
    providers_override: list[LLMProvider] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    providers = (
        providers_override
        if providers_override is not None
        else _structured_providers(
            settings,
            instructions=instructions,
            user_payload=user_payload,
            json_schema=json_schema,
            schema_name=schema_name,
            model=model,
            mode=mode,
            stage_name=stage_name,
            correlation_id=correlation_id,
            temperature=temperature,
        )
    )
    router = LLMRouter(providers)
    try:
        response_json, request_meta = router.run()
        request_meta["llm_temperature_requested"] = float(temperature)
        request_meta["llm_determinism_guaranteed"] = False
        request_meta["llm_determinism_basis"] = "provider_dependent"
        return response_json, request_meta
    except LLMRouterError as exc:
        raise GroqClientError(str(exc), details=sanitize_for_storage(exc.details)) from exc


def _structured_provider_plan(
    settings: Settings,
    *,
    stage_name: str | None = None,
    correlation_id: str,
) -> tuple[list[str], dict[str, Any] | None]:
    """Return ordered provider names for one structured LLM request.

    When ``llm_structured_provider_alternation`` is enabled, the alternation slot is the
    first provider for this stage; ``LLM_FALLBACK_PROVIDERS`` still apply on transient
    failures (429, 5xx, timeout) so structured stages use the same multi-key chain as
    the agent planner.
    """

    primary = getattr(settings, "llm_primary_provider", "") or _legacy_primary_provider(settings)
    fallback_providers = tuple(getattr(settings, "llm_fallback_providers", ()) or ())
    alt_meta: dict[str, Any] | None = None
    ordered: tuple[str, ...]
    if getattr(settings, "llm_structured_provider_alternation", False):
        idx, slot, selection_key = _alternation_slot_for_stage(
            stage_name,
            correlation_id=correlation_id,
        )
        alt_meta = {
            "llm_structured_provider_alternation": True,
            "llm_alternation_index": idx,
            "llm_alternation_slot": slot,
            "llm_alternation_key": selection_key,
            "llm_alternation_strategy": "stable_correlation_hash_v1",
        }
        ordered = (slot, primary, *fallback_providers)
    else:
        ordered = (primary, *fallback_providers)
    names: list[str] = []
    for name in ordered:
        provider = str(name or "").strip().lower()
        if provider and provider not in names:
            names.append(provider)
    return names, alt_meta


def _structured_model_for_provider(
    settings: Settings,
    provider: str,
    model: str | None,
) -> str:
    """Resolve the model id for one structured provider.

    Callers often pass ``settings.groq_model`` (e.g. ``openai/gpt-oss-120b``). That slug is
    valid on Groq but returns 404 on Cerebras/NVIDIA — map to each provider's native model env.
    """
    explicit = str(model or "").strip()
    groq_model = str(settings.groq_model or "").strip()

    if provider == "openai_chat":
        return explicit or groq_model

    native = ""
    if provider == "groq":
        # groq_model is backend-context-dependent (settings.groq_model can be resolved from
        # OPENAI_COMPAT_MODEL when llm_backend=openai_chat — see config.py) and must never be
        # sent to the real Groq API directly. groq_native_model is always resolved from
        # GROQ_MODEL regardless of llm_backend, same pattern as cerebras/nvidia below.
        native = str(getattr(settings, "groq_native_model", "") or "").strip()
    elif provider == "cerebras":
        native = str(getattr(settings, "cerebras_model", "") or "").strip()
    elif provider == "nvidia":
        native = str(getattr(settings, "nvidia_model", "") or "").strip()

    if explicit and explicit != groq_model and not explicit.startswith("openai/"):
        return explicit
    return native or groq_model


def _structured_providers(
    settings: Settings,
    *,
    instructions: str,
    user_payload: Any,
    json_schema: dict[str, Any],
    schema_name: str,
    model: str | None,
    mode: str | None,
    stage_name: str | None = None,
    correlation_id: str,
    temperature: float,
) -> list[LLMProvider]:
    names, alt_meta = _structured_provider_plan(
        settings,
        stage_name=stage_name,
        correlation_id=correlation_id,
    )
    providers: list[LLMProvider] = []
    for provider in names:
        if provider == "groq":
            provider_model = _structured_model_for_provider(settings, "groq", model)
            groq_keys = tuple(getattr(settings, "groq_api_keys", ()) or ())
            if not groq_keys and str(settings.groq_api_key or "").strip():
                groq_keys = (str(settings.groq_api_key).strip(),)
            if not groq_keys:
                providers.append(
                    _unconfigured_structured_provider(
                        provider="groq",
                        backend="groq_responses",
                        model=provider_model,
                        missing_config="GROQ_API_KEY",
                    )
                )
                continue
            groq_keys = _rotate_groq_key_pool(groq_keys)
            for key_index, groq_key in enumerate(groq_keys):
                def call_groq(
                    provider_model: str = provider_model,
                    _groq_key: str = groq_key,
                    _key_index: int = key_index,
                ) -> tuple[dict[str, Any], dict[str, Any]]:
                    payload: dict[str, Any] = {
                        "model": provider_model,
                        "instructions": instructions,
                        "input": user_payload,
                        "temperature": float(temperature),
                        "text": {
                            "format": {
                                "type": "json_schema",
                                "name": schema_name,
                                "schema": json_schema,
                            }
                        },
                    }
                    response_json, request_meta = _post_responses_payload(
                        settings,
                        payload,
                        mode=mode,
                        api_key=_groq_key,
                    )
                    # Fail inside the provider call so empty Responses text is retryable
                    # via LLMRouter instead of escaping as a post-router contract error.
                    _ensure_nonempty_assistant_text(response_json, surface="Groq")
                    request_meta["llm_backend"] = "groq"
                    request_meta["llm_api_key_slot"] = _key_index + 1
                    if alt_meta:
                        request_meta.update(alt_meta)
                    return response_json, request_meta

                providers.append(
                    LLMProvider(
                        provider="groq",
                        backend="groq_responses",
                        model=provider_model,
                        call=call_groq,
                        configured=bool(groq_key),
                        missing_config="GROQ_API_KEY",
                    )
                )
            continue

        if provider == "openai_chat":
            provider_model = _structured_model_for_provider(settings, "openai_chat", model)

            def call_openai_compat(provider_model: str = provider_model) -> tuple[dict[str, Any], dict[str, Any]]:
                synthetic, request_meta = _post_openai_chat_structured(
                    settings,
                    instructions=instructions,
                    user_payload=user_payload,
                    json_schema=json_schema,
                    model=provider_model,
                    mode=mode,
                    schema_name=schema_name,
                    temperature=temperature,
                )
                if alt_meta:
                    request_meta.update(alt_meta)
                return synthetic, request_meta

            providers.append(
                LLMProvider(
                    provider="openai_chat",
                    backend="openai_compatible",
                    model=provider_model,
                    call=call_openai_compat,
                    configured=bool(str(settings.openai_compat_base_url or "").strip()),
                    missing_config="OPENAI_COMPAT_BASE_URL",
                )
            )
            continue

        if provider == "cerebras":
            provider_model = _structured_model_for_provider(settings, "cerebras", model)
            cerebras_base_url = str(getattr(settings, "cerebras_base_url", "") or "").strip()
            cerebras_keys = tuple(getattr(settings, "cerebras_api_keys", ()) or ())
            if not cerebras_keys and str(getattr(settings, "cerebras_api_key", "") or "").strip():
                cerebras_keys = (str(settings.cerebras_api_key).strip(),)
            if not cerebras_keys:
                providers.append(
                    _unconfigured_structured_provider(
                        provider="cerebras",
                        backend="openai_compatible",
                        model=provider_model,
                        missing_config="CEREBRAS_API_KEY",
                    )
                )
                continue
            for key_index, cerebras_api_key in enumerate(cerebras_keys):
                def call_cerebras(
                    provider_model: str = provider_model,
                    _api_key: str = cerebras_api_key,
                    _key_index: int = key_index,
                ) -> tuple[dict[str, Any], dict[str, Any]]:
                    synthetic, request_meta = _post_openai_chat_structured(
                        settings,
                        instructions=instructions,
                        user_payload=user_payload,
                        json_schema=json_schema,
                        model=provider_model,
                        mode=mode,
                        schema_name=schema_name,
                        base_url=cerebras_base_url,
                        api_key=_api_key,
                        temperature=temperature,
                    )
                    request_meta["llm_api_key_slot"] = _key_index + 1
                    if alt_meta:
                        request_meta.update(alt_meta)
                    return synthetic, request_meta

                providers.append(
                    LLMProvider(
                        provider="cerebras",
                        backend="openai_compatible",
                        model=provider_model,
                        call=call_cerebras,
                        configured=bool(cerebras_base_url and cerebras_api_key),
                        missing_config="CEREBRAS_API_KEY",
                    )
                )
            continue

        if provider == "openrouter":
            provider_model = _structured_model_for_provider(settings, "openai_chat", model)
            openrouter_base_url = str(getattr(settings, "openrouter_base_url", "") or "").strip()
            openrouter_model = str(getattr(settings, "openrouter_model", "") or "").strip() or provider_model
            openrouter_keys = tuple(getattr(settings, "openrouter_api_keys", ()) or ())
            if not openrouter_keys and str(getattr(settings, "openai_compat_api_key", "") or "").strip():
                openrouter_keys = (str(settings.openai_compat_api_key).strip(),)
            if not openrouter_keys:
                providers.append(
                    _unconfigured_structured_provider(
                        provider="openrouter",
                        backend="openai_compatible",
                        model=openrouter_model,
                        missing_config="OPENROUTER_API_KEY",
                    )
                )
                continue
            for key_index, openrouter_api_key in enumerate(openrouter_keys):
                def call_openrouter(
                    provider_model: str = openrouter_model,
                    _api_key: str = openrouter_api_key,
                    _key_index: int = key_index,
                ) -> tuple[dict[str, Any], dict[str, Any]]:
                    synthetic, request_meta = _post_openai_chat_structured(
                        settings,
                        instructions=instructions,
                        user_payload=user_payload,
                        json_schema=json_schema,
                        model=provider_model,
                        mode=mode,
                        schema_name=schema_name,
                        base_url=openrouter_base_url,
                        api_key=_api_key,
                        temperature=temperature,
                    )
                    request_meta["llm_api_key_slot"] = _key_index + 1
                    if alt_meta:
                        request_meta.update(alt_meta)
                    return synthetic, request_meta

                providers.append(
                    LLMProvider(
                        provider="openrouter",
                        backend="openai_compatible",
                        model=openrouter_model,
                        call=call_openrouter,
                        configured=bool(openrouter_base_url and openrouter_api_key),
                        missing_config="OPENROUTER_API_KEY",
                    )
                )
            continue

        if provider == "nvidia":
            provider_model = _structured_model_for_provider(settings, "nvidia", model)
            nvidia_base_url = str(getattr(settings, "nvidia_base_url", "") or "").strip()
            nvidia_keys = tuple(getattr(settings, "nvidia_api_keys", ()) or ())
            if not nvidia_keys and str(getattr(settings, "nvidia_api_key", "") or "").strip():
                nvidia_keys = (str(settings.nvidia_api_key).strip(),)
            if not nvidia_keys:
                providers.append(
                    _unconfigured_structured_provider(
                        provider="nvidia",
                        backend="openai_compatible",
                        model=provider_model,
                        missing_config="NVIDIA_API_KEY",
                    )
                )
                continue
            for key_index, nvidia_api_key in enumerate(nvidia_keys):
                def call_nvidia(
                    provider_model: str = provider_model,
                    _api_key: str = nvidia_api_key,
                    _key_index: int = key_index,
                ) -> tuple[dict[str, Any], dict[str, Any]]:
                    synthetic, request_meta = _post_openai_chat_structured(
                        settings,
                        instructions=instructions,
                        user_payload=user_payload,
                        json_schema=json_schema,
                        model=provider_model,
                        mode=mode,
                        schema_name=schema_name,
                        base_url=nvidia_base_url,
                        api_key=_api_key,
                        temperature=temperature,
                    )
                    request_meta["llm_api_key_slot"] = _key_index + 1
                    if alt_meta:
                        request_meta.update(alt_meta)
                    return synthetic, request_meta

                providers.append(
                    LLMProvider(
                        provider="nvidia",
                        backend="openai_compatible",
                        model=provider_model,
                        call=call_nvidia,
                        configured=bool(nvidia_base_url and nvidia_api_key),
                        missing_config="NVIDIA_API_KEY",
                    )
                )
            continue
    return providers


def _legacy_primary_provider(settings: Settings) -> str:
    if getattr(settings, "llm_backend", "groq") != "openai_chat":
        return "groq"
    base = str(getattr(settings, "openai_compat_base_url", "") or "").lower()
    if "cerebras.ai" in base:
        return "cerebras"
    return "openai_chat"


def _synthetic_responses_shape_from_chat_text(content: str) -> dict[str, Any]:
    """Normalize OpenAI Chat Completions content into a Responses-shaped dict for shared parsing."""
    return {
        "output_text": content,
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": content}],
            }
        ],
    }


def _extract_openai_chat_message_text(
    response_json: dict[str, Any],
    *,
    require_text_content: bool = True,
) -> str:
    """Extract business text from an OpenAI-compatible chat completion.

    Structured/text paths (``require_text_content=True``) treat None / empty /
    whitespace-only ``message.content`` as an invalid provider response. Planner /
    tool paths may pass ``require_text_content=False`` when ``tool_calls`` are the
    expected output. ``reasoning_content`` is diagnostic only and is never copied
    into the returned business text.
    """
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        raise GroqClientError("OpenAI-compatible response missing `choices`.")
    first = choices[0]
    if not isinstance(first, dict):
        raise GroqClientError("OpenAI-compatible response has invalid `choices[0]`.")
    msg = first.get("message")
    if not isinstance(msg, dict):
        raise GroqClientError("OpenAI-compatible response missing `message`.")
    content = msg.get("content")
    tool_calls = msg.get("tool_calls")
    has_tool_calls = isinstance(tool_calls, list) and len(tool_calls) > 0
    reasoning = msg.get("reasoning_content")
    has_reasoning_content = isinstance(reasoning, str) and bool(reasoning.strip())
    finish_reason = first.get("finish_reason")
    empty_details: dict[str, Any] = {
        "error_class": "empty_content",
        "finish_reason": finish_reason,
        "has_tool_calls": has_tool_calls,
        "has_reasoning_content": has_reasoning_content,
        "content_type": type(content).__name__ if content is not None else "NoneType",
        "content_len": len(content) if isinstance(content, str) else 0,
    }
    if isinstance(content, str) and content.strip():
        return content.strip()
    if not require_text_content and has_tool_calls:
        return ""
    # Never promote reasoning_content to business output.
    raise GroqClientError(
        "OpenAI-compatible response has empty `message.content`.",
        details=empty_details,
    )


def _openai_chat_completions_url(base_url: str | None) -> str:
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.endswith("/v1"):
        return f"{raw}/chat/completions"
    return f"{raw}/v1/chat/completions"


def _format_openai_chat_http_error(status_code: int, response_json: dict[str, Any]) -> str:
    err = response_json.get("error")
    if isinstance(err, dict):
        message = sanitize_text(str(err.get("message") or "").strip())
        err_type = sanitize_text(str(err.get("type") or "").strip())
        if message:
            return f"{status_code} OpenAI-compatible API: {message}" + (f" ({err_type})" if err_type else "")
    return f"{status_code} OpenAI-compatible Chat Completions error."


def _post_openai_chat_structured(
    settings: Settings,
    *,
    instructions: str,
    user_payload: Any,
    json_schema: dict[str, Any] | None = None,
    model: str | None,
    mode: str | None,
    schema_name: str,
    base_url: str | None = None,
    api_key: str | None = None,
    extra_payload: dict[str, Any] | None = None,
    temperature: float = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """POST Chat Completions and return Responses-compatible JSON for `_build_result`.

    ``extra_payload`` merges additional top-level request fields (e.g. DeepSeek's
    ``thinking``/``reasoning_effort``) without changing the request shape for existing
    providers, which never pass it.
    """
    if isinstance(user_payload, list):
        user_content = json.dumps(user_payload, ensure_ascii=False)
    else:
        user_content = str(user_payload)
    schema_instruction = ""
    if json_schema:
        schema_instruction = (
            f"\n\nJSON Schema contract for {schema_name}:\n"
            f"{json.dumps(json_schema, ensure_ascii=False, sort_keys=True)}\n"
            "Return exactly one JSON value that validates against this schema. "
            "Use the schema field names and JSON types exactly."
        )
    system_content = (
        instructions.strip()
        + schema_instruction
        + f"\n\nSchema name: {schema_name}. "
        "Reply with a single JSON value (object or array only). No markdown fences, no text outside JSON."
    )
    body: dict[str, Any] = {
        "model": model or settings.groq_model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
        "temperature": float(temperature),
        "stream": False,
    }
    if extra_payload:
        body.update(extra_payload)
    response_json, request_meta = _post_openai_chat_payload(
        settings,
        body,
        mode=mode,
        base_url=base_url,
        api_key=api_key,
    )
    # DeepSeek thinking mode returns chain-of-thought reasoning in a separate
    # `reasoning_content` field alongside `content` — only `content` (the final answer) is
    # ever extracted here, so reasoning never leaks into the structured JSON business output.
    text = _extract_openai_chat_message_text(response_json)
    synthetic = _synthetic_responses_shape_from_chat_text(text)
    return synthetic, request_meta


def _post_openai_chat_payload(
    settings: Settings,
    body: dict[str, Any],
    *,
    mode: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    url = _openai_chat_completions_url(base_url) if base_url is not None else settings.openai_chat_completions_url
    if not url:
        raise GroqClientError("OPENAI_COMPAT_BASE_URL is not configured.")

    headers: dict[str, str] = {"Content-Type": "application/json"}
    key = (settings.openai_compat_api_key if api_key is None else api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    last_error: GroqClientError | None = None
    attempts = max(1, settings.http_max_retries)
    retry_events: list[dict[str, Any]] = []
    mode_name = mode or "default"

    for attempt in range(1, attempts + 1):
        # The budget is checked before every attempt, not only before the first: a long
        # first attempt plus backoff can consume the room a later attempt needs, and
        # starting it anyway would guarantee a timeout while hiding the real reason.
        if not _budget_allows_attempt(settings):
            if attempt == 1:
                raise _deadline_exhausted_error(settings, provider="openai_chat", attempt=attempt, mode=mode_name)
            break
        _wait_for_runtime_cooldown(settings)
        attempt_timeout = attempt_timeout_sec(settings.http_timeout)
        # Re-checked after the cooldown, which itself spends budget: a zero-length timeout
        # would be issued as an immediate, uninformative failure.
        if attempt_timeout <= 0:
            if attempt == 1:
                raise _deadline_exhausted_error(settings, provider="openai_chat", attempt=attempt, mode=mode_name)
            break
        try:
            response = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=attempt_timeout,
            )
        except requests.Timeout as exc:
            last_error = GroqClientError(
                f"HTTP timeout ({attempt_timeout:.1f}s of {settings.http_timeout}s configured) "
                "while calling OpenAI-compatible chat endpoint.",
                details={
                    "mode": mode_name,
                    "attempt": attempt,
                    "attempt_timeout_ms": int(round(attempt_timeout * 1000)),
                    "retry_events": retry_events,
                },
            )
            if attempt >= attempts:
                raise last_error from exc
            _sleep_before_retry(settings, attempt, reason="timeout", retry_events=retry_events)
            continue
        except requests.RequestException as exc:
            last_error = GroqClientError(
                f"Failed to connect to OpenAI-compatible chat endpoint: {sanitize_text(str(exc))}",
                details={
                    "mode": mode_name,
                    "attempt": attempt,
                    "retry_events": retry_events,
                },
            )
            if attempt >= attempts:
                raise last_error from exc
            _sleep_before_retry(settings, attempt, reason="network", retry_events=retry_events)
            continue

        try:
            response_json = _parse_http_json(response)
        except GroqClientError as exc:
            last_error = exc
            if response.status_code in RETRYABLE_HTTP_STATUSES and attempt < attempts:
                if _should_fast_fallback_status(response.status_code):
                    raise exc
                _sleep_before_retry(
                    settings,
                    attempt,
                    reason=f"http-{response.status_code}",
                    response=response,
                    retry_events=retry_events,
                )
                continue
            raise

        if response.status_code >= 400:
            message = _format_openai_chat_http_error(response.status_code, response_json)
            last_error = GroqClientError(
                message,
                details={
                    "mode": mode_name,
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "retry_events": retry_events,
                },
            )
            if _should_fast_fallback_status(response.status_code):
                raise last_error
            if _should_retry_status(response.status_code) and attempt < attempts:
                _sleep_before_retry(
                    settings,
                    attempt,
                    reason=f"http-{response.status_code}",
                    response=response,
                    message=message,
                    retry_events=retry_events,
                )
                continue
            raise last_error

        _decay_runtime_throttle(settings)
        return response_json, {
            "mode": mode_name,
            "attempts_made": attempt,
            "retry_events": retry_events,
            "llm_backend": "openai_chat",
        }

    raise last_error or GroqClientError("Unknown error while calling OpenAI-compatible Chat Completions API.")


def _gmail_connector_tool(access_token: str) -> dict[str, Any]:
    normalized_access_token, _ = normalize_google_access_token(access_token)
    return {
        "type": "mcp",
        "server_label": "Gmail",
        "server_description": (
            "Read-only Gmail connector for profiling, searching, and reading mailbox messages. "
            "Use it to audit mailbox contents safely and never modify email state."
        ),
        "connector_id": "connector_gmail",
        "authorization": normalized_access_token,
        "require_approval": "never",
    }


def _post_responses_payload(
    settings: Settings,
    payload: dict[str, Any],
    *,
    mode: str | None = None,
    api_key: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    key = str(api_key or settings.groq_api_key or "").strip()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    last_error: GroqClientError | None = None
    attempts = max(1, settings.http_max_retries)
    retry_events: list[dict[str, Any]] = []
    mode_name = mode or "default"

    for attempt in range(1, attempts + 1):
        # Same budget contract as the OpenAI-compatible loop: never start an attempt that
        # the remaining stage/provider budget cannot let finish.
        if not _budget_allows_attempt(settings):
            if attempt == 1:
                raise _deadline_exhausted_error(settings, provider="groq", attempt=attempt, mode=mode_name)
            break
        _wait_for_runtime_cooldown(settings)
        attempt_timeout = attempt_timeout_sec(settings.http_timeout)
        # Re-checked after the cooldown, which itself spends budget.
        if attempt_timeout <= 0:
            if attempt == 1:
                raise _deadline_exhausted_error(settings, provider="groq", attempt=attempt, mode=mode_name)
            break
        try:
            response = requests.post(
                settings.responses_url,
                headers=headers,
                json=payload,
                timeout=attempt_timeout,
            )
        except requests.Timeout as exc:
            last_error = GroqClientError(
                f"HTTP timeout ({attempt_timeout:.1f}s of {settings.http_timeout}s configured) "
                "while calling Groq.",
                details={
                    "mode": mode_name,
                    "attempt": attempt,
                    "attempt_timeout_ms": int(round(attempt_timeout * 1000)),
                    "retry_events": retry_events,
                },
            )
            if attempt >= attempts:
                raise last_error from exc
            _sleep_before_retry(settings, attempt, reason="timeout", retry_events=retry_events)
            continue
        except requests.RequestException as exc:
            last_error = GroqClientError(
                f"Failed to connect to Groq Responses API: {sanitize_text(str(exc))}",
                details={
                    "mode": mode_name,
                    "attempt": attempt,
                    "retry_events": retry_events,
                },
            )
            if attempt >= attempts:
                raise last_error from exc
            _sleep_before_retry(settings, attempt, reason="network", retry_events=retry_events)
            continue

        try:
            response_json = _parse_http_json(response)
        except GroqClientError as exc:
            last_error = exc
            if response.status_code in RETRYABLE_HTTP_STATUSES and attempt < attempts:
                if _should_fast_fallback_status(response.status_code):
                    raise exc
                _sleep_before_retry(
                    settings,
                    attempt,
                    reason=f"http-{response.status_code}",
                    response=response,
                    retry_events=retry_events,
                )
                continue
            raise

        if response.status_code >= 400:
            message = _format_api_error(response.status_code, response_json)
            last_error = GroqClientError(
                message,
                details={
                    "mode": mode_name,
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "retry_events": retry_events,
                },
            )
            if _should_fast_fallback_status(response.status_code):
                raise last_error
            if _should_retry_status(response.status_code) and attempt < attempts:
                _sleep_before_retry(
                    settings,
                    attempt,
                    reason=f"http-{response.status_code}",
                    response=response,
                    message=message,
                    retry_events=retry_events,
                )
                continue
            raise last_error

        _decay_runtime_throttle(settings)
        return response_json, {
            "mode": mode_name,
            "attempts_made": attempt,
            "retry_events": retry_events,
        }

    raise last_error or GroqClientError("Unknown error while calling Groq Responses API.")


def _parse_http_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        body_preview = sanitize_text(response.text[:500].strip())
        raise GroqClientError(
            "Groq returned a response that could not be parsed as JSON. "
            f"HTTP status: {response.status_code}. Body: {body_preview!r}",
            details={"status_code": response.status_code},
        ) from exc

    if not isinstance(data, dict):
        raise GroqClientError(
            "Groq returned an unexpected JSON root; expected an object.",
            details={"status_code": response.status_code},
        )

    return data


def _format_api_error(status_code: int, response_json: dict[str, Any]) -> str:
    error = response_json.get("error")
    message = ""
    error_type = ""
    error_code = ""

    if isinstance(error, dict):
        message = sanitize_text(str(error.get("message") or "").strip())
        error_type = sanitize_text(str(error.get("type") or "").strip())
        error_code = sanitize_text(str(error.get("code") or "").strip())

    details = " | ".join(part for part in [message, error_type, error_code] if part)
    suffix = f" Details: {details}" if details else ""

    if status_code == 401:
        lowered = f"{message} {error_type} {error_code}".lower()
        if "invalid api key" in lowered or "invalid_api_key" in lowered:
            return "401 Unauthorized: GROQ_API_KEY was rejected by Groq." + suffix
        if "invalid credentials" in lowered or "gmail" in lowered or "connector" in lowered:
            return (
                "401 Unauthorized: Google OAuth token for the Gmail connector is invalid or expired. "
                "Refresh GOOGLE_ACCESS_TOKEN manually or configure refresh-token flow, then rerun doctor."
            ) + suffix
        return "401 Unauthorized: check GROQ_API_KEY or Google OAuth token validity." + suffix
    if status_code == 403:
        return (
            "403 Forbidden: missing permissions or Google token lacks gmail.readonly scope."
        ) + suffix
    if status_code == 424:
        lowered = f"{message} {error_type} {error_code}".lower()
        if "401" in lowered and "invalid credentials" in lowered:
            return (
                "424 Failed Dependency: Gmail connector could not read the mailbox because "
                "the active Google OAuth token is invalid or expired."
            ) + suffix
        if "403" in lowered:
            return (
                "424 Failed Dependency: Gmail connector access was denied. "
                "Check Google OAuth scope and mailbox permissions."
            ) + suffix
        return "424 Failed Dependency: Groq could not execute the Gmail connector call." + suffix
    if status_code == 429:
        return "429 Too Many Requests: rate limit or connector quota exceeded." + suffix
    if status_code >= 500:
        return f"{status_code} Server Error: Groq or connector-side problem." + suffix
    return f"{status_code} API Error." + suffix


def _sleep_before_retry(
    settings: Settings,
    attempt: int,
    *,
    reason: str,
    response: requests.Response | None = None,
    message: str | None = None,
    retry_events: list[dict[str, Any]] | None = None,
) -> None:
    delay = _compute_retry_delay(settings, attempt, response=response, message=message)
    delay = min(delay, 600.0)
    # Backoff must never consume the time the retry it is waiting for would need. Without
    # this clamp a long Retry-After silently converts "we will retry" into "we will time out
    # while sleeping", which is the same class of defect as an outer envelope preempting an
    # inner retry. Outside a stage deadline the window is None and backoff is unchanged.
    window = retry_window_sec()
    if window is not None:
        delay = max(0.0, min(delay, window - resolve_min_attempt_sec(settings)))
    if response is not None and response.status_code == 429:
        _increase_runtime_throttle(settings, delay)
    elif reason in {"timeout", "network"}:
        _increase_runtime_throttle(settings, delay / 2)
    if retry_events is not None:
        retry_events.append(
            {
                "attempt": attempt,
                "reason": reason,
                "delay_seconds": round(delay, 5),
                "status_code": response.status_code if response is not None else None,
            }
        )
    print(
        f"[retry] Groq call failed ({reason}). Sleeping {delay:.1f}s before retry {attempt + 1}.",
        file=sys.stderr,
        flush=True,
    )
    time.sleep(delay)


def _compute_retry_delay(
    settings: Settings,
    attempt: int,
    *,
    response: requests.Response | None = None,
    message: str | None = None,
) -> float:
    retry_after = None
    if response is not None:
        header_value = response.headers.get("Retry-After")
        if header_value:
            try:
                retry_after = float(header_value)
            except ValueError:
                retry_after = None

    if retry_after is None and message:
        retry_after = _extract_retry_after_seconds(message)

    if retry_after is not None and retry_after > 0:
        # Groq may return multi-hour Retry-After; cap so Gate B / sequential ingress can finish.
        capped = min(float(retry_after), 600.0)
        return round(max(1.0, capped) + random.uniform(0.1, 0.8), 1)

    exponent = max(0, attempt - 1)
    base_delay = settings.http_retry_base_delay * (4 ** exponent)
    return round(base_delay + random.uniform(0.2, base_delay * 0.35 + 0.2), 1)


def _extract_retry_after_seconds(message: str) -> float | None:
    match = re.search(r"try again in ([0-9]+(?:\.[0-9]+)?)s", message, flags=re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1)) + 0.5


def _should_retry_status(status_code: int) -> bool:
    return status_code in RETRYABLE_HTTP_STATUSES


def _should_fast_fallback_status(status_code: int) -> bool:
    """Rate/quota limits: skip long in-provider sleep; let LLMRouter try next provider."""
    return status_code in {402, 429}


def _runtime_key(settings: Settings) -> int:
    return id(settings)


def _wait_for_runtime_cooldown(settings: Settings) -> None:
    remaining = _RUNTIME_COOLDOWNS.get(_runtime_key(settings), 0.0) - time.monotonic()
    if remaining <= 0:
        return
    # The cooldown is a sleep inside the retry path, so it spends the same budget the attempt
    # after it needs. Left unbounded it silently converts remaining budget into a zero-length
    # attempt -- the same defect class as an outer envelope preempting an inner retry, just
    # one layer down. Outside a stage deadline the window is None and the wait is unchanged.
    window = retry_window_sec()
    if window is not None:
        remaining = max(0.0, min(remaining, window - resolve_min_attempt_sec(settings)))
        if remaining <= 0:
            return
    print(
        f"[cooldown] Groq runtime cooldown active. Sleeping {remaining:.1f}s before next request.",
        file=sys.stderr,
        flush=True,
    )
    time.sleep(remaining)


def _increase_runtime_throttle(settings: Settings, delay: float) -> None:
    key = _runtime_key(settings)
    level = min(_RUNTIME_THROTTLE_LEVELS.get(key, 0) + 1, 5)
    _RUNTIME_THROTTLE_LEVELS[key] = level
    cooldown = max(delay, settings.http_retry_base_delay * (1 + level * 0.5))
    _RUNTIME_COOLDOWNS[key] = time.monotonic() + cooldown


def _decay_runtime_throttle(settings: Settings) -> None:
    key = _runtime_key(settings)
    level = _RUNTIME_THROTTLE_LEVELS.get(key, 0)
    if level <= 1:
        _RUNTIME_THROTTLE_LEVELS.pop(key, None)
        _RUNTIME_COOLDOWNS.pop(key, None)
        return
    _RUNTIME_THROTTLE_LEVELS[key] = level - 1


def _strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _extract_balanced_json_fragment(text: str, start_index: int) -> str:
    opening = text[start_index]
    if opening not in "{[":
        return ""

    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escape = False

    for index in range(start_index, len(text)):
        char = text[index]

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start_index:index + 1].strip()

    return ""
