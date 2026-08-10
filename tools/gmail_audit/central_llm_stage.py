"""Unified LLM stage: ContextAssembler + Groq/Cerebras (primary) or Anthropic (optional)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Type, TypeVar, cast

from pydantic import BaseModel
from pydantic import ValidationError

from config import Settings
from context_assembler import (
    AssembledContext,
    ContextAssembler,
    CaseContextLoader,
    _facts_dict_from_active_facts,
    apply_context_token_budget,
    assembled_context_to_dict,
)
from context_pack_overlay import overlay_pack_onto_assembled
from exceptions import LLMError, LLMTimeoutError, LLMRateLimitError
from groq_client import (
    GroqClientError,
    deepseek_configured,
    deepseek_error_allows_fallback,
    extract_json_candidate,
    run_deepseek_structured_stage,
    run_structured_stage,
)
from llm_client import TopInstalLLMClient, TopInstalLLMError
from llm_deadline import (
    attempt_timeout_sec,
    remaining_sec as deadline_remaining_sec,
    resolve_min_attempt_sec,
    resolve_stage_budget_sec,
    stage_deadline,
)
from log_config import get_logger

TModel = TypeVar("TModel", bound=BaseModel)

logger = get_logger("central_llm_stage")

# ── LLM Resilience constants ──────────────────────────────────────────
# Retained for backward compatibility with callers/tests that import it. It is no longer a
# timeout anyone enforces: the Anthropic path's per-attempt bound now comes from the client's
# own HTTP timeout, derived from the stage budget (see _call_anthropic_raw_text).
LLM_HARD_TIMEOUT_SEC = 60
LLM_CLIENT_TIMEOUT_SEC = 30    # soft timeout: passed to LLM client
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2         # exponential: 2^attempt seconds

# ── Provider rate limiting (Faza 2a) ──────────────────────────────────────────
# Semafor per provider — ogranicza do 2 równoczesnych calli na tego samego providera.
# Chroni przed wyczerpaniem rate limitu API gdy wiele faz (intake, business, signal)
# woła LLM równocześnie.
_provider_semaphores: dict[str, threading.Semaphore] = {}


# ── LLM Cost estimation (bonus: sledzenie kosztow providera) ────────────────
# Ceny za 1K tokenow (USD) — orientacyjne, aktualizowac po zmianie providera.
# Zrodlo: oficjalne cenniki providerow (stan na 2026-07).
_PROVIDER_COST_PER_1K_TOKENS: dict[str, float] = {
    "groq": 0.0005,
    "cerebras": 0.0006,
    "openai": 0.002,
    "anthropic": 0.003,
    "nvidia": 0.0007,
    "openrouter": 0.0008,
}

_DEFAULT_COST_PER_1K = 0.001  # domyslna, jesli nieznany provider


def estimate_llm_cost(provider: str, tokens_used: int, total_latency_ms: int) -> float:
    """Oszacuj koszt calla LLM na podstawie providera i liczby tokenow."""
    rate = _PROVIDER_COST_PER_1K_TOKENS.get(provider.lower(), _DEFAULT_COST_PER_1K)
    return round((tokens_used * rate) / 1000, 6)


def _get_provider_semaphore(provider: str) -> threading.Semaphore:
    if provider not in _provider_semaphores:
        _provider_semaphores[provider] = threading.Semaphore(2)
    return _provider_semaphores[provider]


# ── LLM Response Cache (Krok 5) ──────────────────────────────────────────────
# Cache for structured requests that explicitly ask for temperature=0.


def _get_cache_db_url() -> str:
    """Pobierz URL bazy dla cache z env."""
    import os
    return str(os.environ.get("MAILBOX_MEMORY_DATABASE_URL", os.environ.get("MAILBOX_DB_URL", ""))).strip()


def _llm_cache_disabled() -> bool:
    import os

    if os.getenv("GMAIL_AUDIT_DISABLE_LLM_CACHE", "").strip().lower() in {"1", "true", "yes"}:
        return True
    return False


def _cache_read(cache_key: str, stage_name: str) -> str | None:
    """Odczytaj odpowiedz z cache. Zwraca JSON string lub None."""
    if _llm_cache_disabled():
        return None
    db_url = _get_cache_db_url()
    if not db_url:
        return None
    try:
        import psycopg
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT response FROM llm_response_cache WHERE query_hash = %s AND expires_at > NOW()",
                    (cache_key,)
                )
                row = cur.fetchone()
                if row:
                    logger.info("LLM_CACHE_HIT", extra={"x": {"stage": stage_name, "cache_key": cache_key[:12]}})
                    return row[0]
    except Exception as exc:
        logger.warning("LLM_CACHE_READ_FAILED", extra={"x": {"stage": stage_name, "error": str(exc)[:200]}})
    return None


def _cache_write(cache_key: str, stage_name: str, provider: str, model: str, temperature: float, response: str, ttl_hours: int = 24) -> None:
    """Zapisz odpowiedz do cache."""
    if _llm_cache_disabled():
        return
    db_url = _get_cache_db_url()
    if not db_url:
        return
    try:
        import psycopg
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO llm_response_cache (query_hash, stage_name, response, provider, model, temperature, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW() + INTERVAL '%s hours')
                    ON CONFLICT (query_hash) DO UPDATE SET
                        response = EXCLUDED.response,
                        expires_at = NOW() + INTERVAL '%s hours'""",
                    (cache_key, stage_name, response, provider, model, temperature, ttl_hours, ttl_hours)
                )
                conn.commit()
    except Exception as exc:
        logger.warning("LLM_CACHE_WRITE_FAILED", extra={"x": {"stage": stage_name, "error": str(exc)[:200]}})


def _build_cache_key(stage_name: str, provider: str, model: str, prompt_input: dict, task_instructions: str) -> str:
    """Zbuduj deterministyczny klucz cache dla wywolania LLM."""
    raw = json.dumps({"stage": stage_name, "provider": provider, "model": model, "prompt": prompt_input, "instructions": task_instructions}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _call_with_retry(
    fn: Any,
    *,
    stage_name: str,
    max_retries: int = MAX_RETRIES,
    model: str = "",
    provider: str = "",  # Faza 2a: nazwa providera dla semafora rate limiting
) -> Any:
    """Execute an LLM callable with exponential backoff retry + provider rate limiting.

    This is the single visible retry layer for the Anthropic path. It owns *when* to retry;
    it does not own a per-attempt bound, because it cannot enforce one -- ``fn`` is a blocking
    HTTP call and only the callee's own timeout can stop it. ``fn`` is therefore responsible
    for deriving its per-attempt timeout from the stage budget (see ``_call_anthropic_raw_text``),
    and this loop stops retrying once the budget can no longer fund a real attempt.

    It used to wrap ``fn`` in ``ThreadPoolExecutor`` with a ``hard_timeout``. That bounded
    nothing: ``future.cancel()`` cannot stop an in-flight request, and ``Executor.__exit__``
    calls ``shutdown(wait=True)``, so the block waited for the abandoned work regardless. The
    parameter is gone rather than left as a comforting no-op.
    """
    last_exc: Exception | None = None
    min_attempt = resolve_min_attempt_sec(None)
    for attempt in range(max_retries + 1):
        remaining = deadline_remaining_sec()
        if remaining is not None and remaining < min_attempt:
            logger.warning("LLM_RETRY_BUDGET_EXHAUSTED", extra={"x": {
                "stage": stage_name, "model": model, "attempt": attempt + 1,
                "terminal_failure_reason": "stage_deadline_exhausted",
                "remaining_budget_ms": int(round(remaining * 1000)),
                "min_attempt_ms": int(round(min_attempt * 1000)),
            }})
            # Exhausting the budget before any attempt ran leaves no provider error to report.
            # Falling through to the generic "unknown reason" would erase the one fact that
            # actually explains the failure, so name it here.
            last_exc = last_exc or LLMTimeoutError(
                f"Stage budget exhausted before attempt {attempt + 1} on {stage_name} "
                f"({remaining:.2f}s remaining, {min_attempt:.2f}s needed)",
                context={
                    "stage": stage_name,
                    "model": model,
                    "attempt": attempt + 1,
                    "terminal_failure_reason": "stage_deadline_exhausted",
                    "remaining_budget_ms": int(round(remaining * 1000)),
                },
            )
            break
        # Faza 2a: semafor per-provider — ogranicza równoczesne calle do tego samego API
        sem = _get_provider_semaphore(provider) if provider else None
        if sem is not None and not sem.acquire(blocking=False):
            logger.warning("PROVIDER_RATE_LIMITED", extra={"x": {
                "provider": provider, "stage": stage_name, "attempt": attempt + 1,
            }})
            last_exc = LLMRateLimitError(
                f"Provider {provider} rate limited — concurrent calls exceeded",
                context={"provider": provider, "stage": stage_name},
            )
            if attempt < max_retries:
                wait_sec = RETRY_BACKOFF_BASE ** attempt
                time.sleep(wait_sec)
            continue
        try:
            # Called directly, on this thread. The previous
            # `with ThreadPoolExecutor(...) as executor: future.result(timeout=hard_timeout)`
            # bounded nothing: `future.cancel()` cannot stop a request already in flight, and
            # `Executor.__exit__` calls `shutdown(wait=True)`, so the block waited for the
            # abandoned work anyway. It only relabelled the error and burned a thread. The real
            # per-attempt bound is the callee's own HTTP timeout, derived from the stage budget.
            result = fn()
            if attempt > 0:
                logger.info("LLM_RETRY_SUCCESS", extra={"x": {
                    "stage": stage_name, "model": model, "attempt": attempt + 1,
                }})
            return result
        except (LLMRateLimitError, LLMTimeoutError) as exc:
            last_exc = exc
            logger.warning("LLM_RETRYABLE_ERROR", extra={"x": {
                "stage": stage_name, "model": model, "attempt": attempt + 1,
                "error_type": type(exc).__name__, "error": str(exc)[:200],
            }})
        except LLMError:
            raise  # non-retryable LLM error — propagate immediately
        except Exception as exc:
            last_exc = exc
            logger.warning("LLM_UNEXPECTED_RETRY", extra={"x": {
                "stage": stage_name, "model": model, "attempt": attempt + 1,
                "error_type": type(exc).__name__, "error": str(exc)[:200],
            }})
        finally:
            if sem is not None:
                sem.release()

        if attempt < max_retries:
            wait_sec: float = RETRY_BACKOFF_BASE ** attempt
            remaining = deadline_remaining_sec()
            if remaining is not None:
                # Never sleep away the window the retry itself needs.
                wait_sec = max(0.0, min(wait_sec, remaining - min_attempt))
            logger.info("LLM_RETRY_WAIT", extra={"x": {
                "stage": stage_name, "model": model, "wait_sec": wait_sec, "attempt": attempt + 1,
            }})
            if wait_sec > 0:
                time.sleep(wait_sec)

    raise last_exc or LLMError("LLM call failed (unknown reason)")


_DEEPSEEK_DIAGNOSTIC_FIELDS = (
    "error_class",
    "finish_reason",
    "has_tool_calls",
    "has_reasoning_content",
    "content_type",
    "content_len",
    "status_code",
    "attempt",
    "mode",
)


def _deepseek_failure_diagnostics(exc: Exception) -> dict[str, Any]:
    """Response-shape facts from a failed DeepSeek attempt, safe to log.

    Only the whitelisted diagnostic fields are copied. The provider error's ``details`` can
    also carry request echoes, so an allow-list -- not a blanket copy -- is what keeps prompt
    content and credentials out of the log.
    """
    details = dict(getattr(exc, "details", {}) or {})
    out: dict[str, Any] = {}
    for field in _DEEPSEEK_DIAGNOSTIC_FIELDS:
        if field in details:
            out[field] = details[field]
    # Nested provider attempts (router-shaped errors) carry the same shape one level down.
    attempts = details.get("llm_provider_attempts")
    if isinstance(attempts, list) and attempts:
        last = attempts[-1] if isinstance(attempts[-1], dict) else {}
        for field in ("provider", "error_class", "retryable", "latency_ms"):
            if field in last:
                out.setdefault(f"last_attempt_{field}", last[field])
    return out


def anthropic_configured(settings: Settings) -> bool:
    """Check if Anthropic API key is configured."""
    return bool(str(getattr(settings, "anthropic_api_key", "") or "").strip())


def primary_llm_provider(settings: Settings) -> str:
    if deepseek_configured(settings):
        return "deepseek"
    if anthropic_configured(settings):
        return "anthropic"
    named = str(getattr(settings, "llm_primary_provider", "") or "").strip()
    if named:
        return named
    return str(getattr(settings, "llm_backend", "groq") or "groq")


def build_context_assembler(settings: Settings) -> ContextAssembler:
    loader = _mailbox_case_loader(settings) if _mailbox_memory_enabled(settings) else None
    return ContextAssembler(case_loader=loader)


def _mailbox_memory_enabled(settings: Settings) -> bool:
    return bool(str(getattr(settings, "mailbox_memory_database_url", "") or "").strip())


def _mailbox_case_loader(settings: Settings) -> CaseContextLoader | None:
    if not _mailbox_memory_enabled(settings):
        return None

    def loader(case_id: str, query_text: str, max_chunks: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        from mailbox_memory_runtime import build_mailbox_memory_runtime

        runtime = build_mailbox_memory_runtime(settings, allow_in_memory=False)
        if runtime is None:
            return {}, []
        pack = runtime.get_context_pack(case_id=case_id, query_text=query_text)
        facts = _facts_dict_from_active_facts(list(pack.active_facts or []))
        chunks = [c for c in list(pack.relevant_chunks or []) if isinstance(c, dict)]
        return facts, chunks[: max(1, int(max_chunks))]

    return loader


def resolve_case_id(
    *,
    case_id: str | None = None,
    context_bundle: dict[str, Any] | None = None,
    case_link_result: dict[str, Any] | None = None,
) -> str:
    for candidate in (
        case_id,
        (case_link_result or {}).get("case_id"),
        (context_bundle or {}).get("case_id"),
        ((context_bundle or {}).get("case_context_pack") or {}).get("case_id")
        if isinstance((context_bundle or {}).get("case_context_pack"), dict)
        else None,
    ):
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized
    return ""


def resolve_engagement_id(
    *,
    engagement_id: str | None = None,
    context_bundle: dict[str, Any] | None = None,
    case_link_result: dict[str, Any] | None = None,
) -> str:
    pack = (context_bundle or {}).get("case_context_pack")
    pack_engagement = (
        str(pack.get("engagement_id") or "").strip() if isinstance(pack, dict) else ""
    )
    for candidate in (
        engagement_id,
        (case_link_result or {}).get("engagement_id"),
        (context_bundle or {}).get("engagement_id"),
        pack_engagement,
    ):
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized
    return ""


def merge_system_prompt(assembled: AssembledContext, task_instructions: str) -> str:
    assembler = ContextAssembler()
    system = assembler.to_system_prompt(assembled)
    extra = str(task_instructions or "").strip()
    if extra:
        system += f"\n## Stage instructions\n{extra}\n"
    return system


def _anthropic_client(
    settings: Settings,
    *,
    temperature: float = 0,
    timeout_sec: float | None = None,
    max_retries: int = 1,
) -> TopInstalLLMClient:
    """Build the Anthropic client for exactly one bounded attempt by default.

    ``max_retries=1`` is deliberate: ``_call_with_retry`` above owns the retry loop, and the
    client used to run its own on top (``min(3, http_max_retries)``), so a single logical call
    could expand to 4 x 3 HTTP attempts with nothing observing the multiplication. One visible
    retry layer is the same rule the router path follows.
    """
    return TopInstalLLMClient(
        api_key=str(settings.anthropic_api_key),
        model=str(settings.anthropic_model),
        timeout_sec=float(settings.http_timeout if timeout_sec is None else timeout_sec),
        max_retries=max(1, int(max_retries)),
        temperature=temperature,
    )


def _snapshot_from_prompt_input(prompt_input: dict[str, Any] | None) -> dict[str, Any] | None:
    """Rebuild a minimal intake snapshot for schema normalization from stage payload."""
    if not isinstance(prompt_input, dict):
        return None
    source_message = prompt_input.get("source_message")
    if not isinstance(source_message, dict):
        return None
    thread_context = prompt_input.get("thread_context") if isinstance(prompt_input.get("thread_context"), dict) else {}
    return {
        "mailbox": str(prompt_input.get("mailbox") or ""),
        "observed_at": str(prompt_input.get("observed_at") or ""),
        "thread_context_quality": str(thread_context.get("quality") or "weak"),
        "source_message": source_message,
    }


def _validate_output_model(
    raw_text: str,
    output_model: Type[TModel],
    *,
    prompt_input: dict[str, Any] | None = None,
) -> tuple[TModel | None, list[dict[str, Any]] | None]:
    try:
        json_text = extract_json_candidate(raw_text)
        if json_text.lstrip().startswith("["):
            logger.error(
                "central_llm pydantic validation failed for %s: top-level JSON must be an object, got array",
                output_model.__name__,
                extra={"x": {"output_model": output_model.__name__, "error_type": "root_type_error"}},
            )
            return None, [{"type": "root_type_error", "msg": "top-level JSON must be an object, got array"}]
        if (
            output_model.__name__ == "BusinessReasoningResult"
            and str(getattr(output_model, "__module__", "")).endswith("llm_contracts.business_reasoning")
        ):
            from intake_schema import validate_business_reasoning_result

            candidate = json.loads(json_text)
            normalized = validate_business_reasoning_result(candidate)
            return output_model.model_validate(normalized), None
        return output_model.model_validate_json(json_text), None
    except ValidationError as exc:
        if output_model.__name__ == "IntakeReasoningResult":
            from intake_schema import validate_output_with_repair

            trace = validate_output_with_repair(
                raw_text,
                snapshot=_snapshot_from_prompt_input(prompt_input),
            )
            if trace.result.is_valid and trace.result.data is not None:
                try:
                    return output_model.model_validate(trace.result.data), None
                except ValidationError as repaired_exc:
                    logger.error(
                        "central_llm pydantic validation failed for %s after intake normalization: %s",
                        output_model.__name__,
                        repaired_exc,
                        extra={
                            "x": {
                                "output_model": output_model.__name__,
                                "error_type": "ValidationError",
                                "error": str(repaired_exc)[:500],
                                "normalization_notes": trace.normalization_notes,
                                "repair_notes": trace.repair_notes,
                            }
                        },
                    )
                    return None, cast(list[dict[str, Any]], repaired_exc.errors())
        logger.error(
            "central_llm pydantic validation failed for %s: %s",
            output_model.__name__,
            exc,
            extra={"x": {"output_model": output_model.__name__, "error_type": "ValidationError", "error": str(exc)[:500]}},
        )
        return None, cast(list[dict[str, Any]], exc.errors())
    except Exception as exc:
        logger.error(
            "central_llm pydantic validation failed for %s: %s",
            output_model.__name__,
            exc,
            extra={"x": {"output_model": output_model.__name__, "error_type": type(exc).__name__, "error": str(exc)[:500]}},
        )
        return None, [{"type": "unknown_error", "msg": str(exc)}]


def _call_anthropic_raw_text(
    settings: Settings,
    *,
    system: str,
    user_json: str,
    case_id: str | None,
    model: str | None,
    temperature: float,
) -> str:
    # The real bound for this attempt is the client's own HTTP timeout, clamped by whatever
    # the stage budget has left -- there is no wrapper thread that could enforce one.
    attempt_timeout = attempt_timeout_sec(float(settings.http_timeout))
    if attempt_timeout <= 0:
        raise LLMTimeoutError(
            "Stage budget exhausted before the Anthropic attempt could start",
            context={"stage": "anthropic", "model": str(model or settings.anthropic_model)},
        )
    client = _anthropic_client(settings, temperature=temperature, timeout_sec=attempt_timeout)
    payload = client.complete_json(
        system=system,
        user=user_json,
        case_id=case_id,
        model=model,
    )
    return json.dumps(payload, ensure_ascii=False)


def _call_groq_structured_stage(
    settings: Settings,
    *,
    stage_name: str,
    system: str,
    prompt_input: dict[str, Any],
    json_schema: dict[str, Any],
    schema_name: str,
    model: str | None,
    verbose: bool,
    input_variants: list[dict[str, Any]] | None,
    temperature: float,
    correlation_id: str | None,
) -> dict[str, Any]:
    return run_structured_stage(
        settings,
        stage_name=stage_name,
        instructions=system,
        prompt_input=prompt_input,
        json_schema=json_schema,
        schema_name=schema_name,
        model=model,
        verbose=verbose,
        input_variants=input_variants,
        temperature=temperature,
        correlation_id=correlation_id,
    )


def _build_pydantic_stage_result(
    *,
    stage_name: str,
    provider: str,
    model_name: str,
    latency_ms: int,
    parsed: BaseModel,
    assembled: AssembledContext,
    prompt_input: dict[str, Any],
    request_meta: dict[str, Any],
    context_budget: dict[str, Any],
    attempt_count: int = 1,
) -> dict[str, Any]:
    response_json = parsed.model_dump(exclude_none=True)
    response_text = json.dumps(response_json, ensure_ascii=False)
    assembled_payload = assembled_context_to_dict(assembled, context_budget=context_budget)
    return {
        "stage_name": stage_name,
        "model_name": model_name,
        "attempt_count": attempt_count,
        "latency_ms": latency_ms,
        "response_text": response_text,
        "parse_status": "pydantic_validated",
        "response_json": response_json,
        "request_meta": {
            **request_meta,
            "central_llm_provider": provider,
            "assembled_context": assembled_payload,
            "context_budget": context_budget,
        },
        "prompt_input": prompt_input,
        "assembled_context": assembled_payload,
        "central_llm_provider": provider,
    }


def run_central_structured_stage(
    settings: Settings,
    *,
    stage_name: str,
    task_instructions: str,
    prompt_input: dict[str, Any],
    query_text: str,
    json_schema: dict[str, Any],
    schema_name: str,
    case_id: str | None = None,
    engagement_id: str | None = None,
    model: str | None = None,
    verbose: bool = False,
    input_variants: list[dict[str, Any]] | None = None,
    output_model: Type[TModel] | None = None,
    max_chunks: int = 6,
    context_bundle: dict[str, Any] | None = None,
    client_timeout: int = LLM_CLIENT_TIMEOUT_SEC,
    max_retries: int = MAX_RETRIES,
    temperature: float = 0,  # Requested sampling value; provider/model determinism is not guaranteed.
    correlation_id: str | None = None,
) -> dict[str, Any] | None:
    """Run one structured LLM stage under a single, owned wall-clock budget.

    This is the stage boundary and therefore the one place a deadline is created. Provider
    fallback and per-attempt HTTP timeouts below this point all derive from the same budget
    instead of keeping private clocks (see ``llm_deadline``). Callers must not add their own
    outer timeout wrapper: doing so recreates the defect this boundary exists to remove --
    an envelope that expires before the retry/fallback chain it wraps can act, while leaving
    the abandoned request running.
    """
    budget_sec = resolve_stage_budget_sec(settings)
    with stage_deadline(stage_name, budget_sec) as deadline:
        try:
            return _run_central_structured_stage_bounded(
                settings,
                stage_name=stage_name,
                task_instructions=task_instructions,
                prompt_input=prompt_input,
                query_text=query_text,
                json_schema=json_schema,
                schema_name=schema_name,
                case_id=case_id,
                engagement_id=engagement_id,
                model=model,
                verbose=verbose,
                input_variants=input_variants,
                output_model=output_model,
                max_chunks=max_chunks,
                context_bundle=context_bundle,
                client_timeout=client_timeout,
                max_retries=max_retries,
                temperature=temperature,
                correlation_id=correlation_id,
            )
        except GroqClientError as exc:
            log_stage_budget_failure(stage_name, deadline, exc)
            raise


def log_stage_budget_failure(stage_name: str, deadline: Any, exc: Exception) -> dict[str, Any]:
    """Emit the provenance needed to answer 'who consumed the budget, and why no recovery?'.

    Before this, a production timeout produced a single ``INTAKE_LLM_TIMEOUT`` line with a
    constant in it -- enough to know a stage died, not enough to know which provider stalled,
    whether fallback was reached, or how much budget was left. Everything here is provider
    names, counts and durations; no prompt content, no response bodies, no credentials.
    """
    details = dict(getattr(exc, "details", {}) or {})
    provider_attempts = details.get("llm_provider_attempts") or []
    last_attempt = provider_attempts[-1] if provider_attempts else {}
    payload = {
        "stage": stage_name,
        "terminal_failure_reason": details.get("terminal_failure_reason") or "provider_chain_failed",
        "provider": last_attempt.get("provider") or details.get("provider") or "",
        "attempt": last_attempt.get("attempt") or details.get("attempt") or len(provider_attempts),
        "fallback_index": max(0, len(provider_attempts) - 1),
        "fallback_used": bool(details.get("llm_fallback_used")),
        "retryable": last_attempt.get("retryable"),
        "provider_attempts": [
            {
                "provider": item.get("provider"),
                "status": item.get("status"),
                "error_class": item.get("error_class"),
                "retryable": item.get("retryable"),
                "latency_ms": item.get("latency_ms"),
                "provider_budget_ms": item.get("provider_budget_ms"),
                "remaining_budget_ms": item.get("remaining_budget_ms"),
            }
            for item in provider_attempts
        ],
        "error": str(exc)[:300],
    }
    if deadline is not None:
        payload.update(deadline.telemetry())
    logger.error("LLM_STAGE_BUDGET_FAILURE", extra={"x": payload})
    return payload


def _run_central_structured_stage_bounded(
    settings: Settings,
    *,
    stage_name: str,
    task_instructions: str,
    prompt_input: dict[str, Any],
    query_text: str,
    json_schema: dict[str, Any],
    schema_name: str,
    case_id: str | None = None,
    engagement_id: str | None = None,
    model: str | None = None,
    verbose: bool = False,
    input_variants: list[dict[str, Any]] | None = None,
    output_model: Type[TModel] | None = None,
    max_chunks: int = 6,
    context_bundle: dict[str, Any] | None = None,
    client_timeout: int = LLM_CLIENT_TIMEOUT_SEC,
    max_retries: int = MAX_RETRIES,
    temperature: float = 0,
    correlation_id: str | None = None,
) -> dict[str, Any] | None:
    """Run structured LLM with company context; validate via Pydantic when output_model is set.

    Always called inside an active stage deadline -- see ``run_central_structured_stage``.
    """
    started = time.monotonic()
    assembler = build_context_assembler(settings)
    assembled = assembler.assemble(
        query_text,
        case_id=case_id,
        engagement_id=engagement_id,
        max_chunks=max_chunks,
    )
    assembled = overlay_pack_onto_assembled(assembled, context_bundle)
    assembled, context_budget = apply_context_token_budget(
        assembled,
        stage_name=stage_name,
        query_text=query_text,
    )
    system = merge_system_prompt(assembled, task_instructions)
    user_json = json.dumps(prompt_input, ensure_ascii=False)
    provider = primary_llm_provider(settings)
    source_message = prompt_input.get("source_message") if isinstance(prompt_input.get("source_message"), dict) else {}
    resolved_correlation_id = str(
        correlation_id
        or source_message.get("message_id")
        or prompt_input.get("message_id")
        or case_id
        or engagement_id
        or ""
    ).strip()

    # A cache hit replays an exact prior body; temperature=0 alone is not a determinism proof.
    cache_hit_raw: str | None = None
    if temperature == 0 and output_model is not None:
        db_url = _get_cache_db_url()
        if db_url:
            cache_key = _build_cache_key(stage_name, provider, str(model or settings.groq_model), prompt_input, task_instructions)
            cache_hit_raw = _cache_read(cache_key, stage_name)

    if cache_hit_raw is not None:
        raw_text = cache_hit_raw
        request_meta: dict[str, Any] = {
            "central_llm_provider": provider,
            "cache_hit": True,
            "llm_temperature_requested": float(temperature),
            "llm_determinism_guaranteed": True,
            "llm_determinism_basis": "response_cache_hit",
        }
        model_name = str(model or settings.groq_model)
        attempt_count = 1
    else:
        # DeepSeek priority-1 tier (OPERATOR_DECISIONS.md DEEPSEEK-MIGRATION-1): tried before
        # whatever was priority 1 previously (Anthropic override, else the router chain below).
        # Qualified DeepSeek upstream/runtime failures fall through to that untouched chain.
        # Permanent adapter/config errors fail fast so a bad request is not silently masked.
        deepseek_stage: dict[str, Any] | None = None
        if deepseek_configured(settings):
            try:
                deepseek_stage = run_deepseek_structured_stage(
                    settings,
                    stage_name=stage_name,
                    instructions=system,
                    prompt_input=prompt_input,
                    json_schema=json_schema,
                    schema_name=schema_name,
                    model=model,
                    verbose=verbose,
                    input_variants=input_variants,
                    temperature=temperature,
                    correlation_id=resolved_correlation_id or None,
                )
            except GroqClientError as exc:
                if not deepseek_error_allows_fallback(exc):
                    raise
                logger.error("LLM_DEEPSEEK_FAILED", extra={"x": {
                    "stage": stage_name,
                    "error": str(exc)[:300],
                    # The adapter already builds exactly the fields needed to diagnose why a
                    # response was unusable (`error_class`, `finish_reason`, `has_tool_calls`,
                    # `has_reasoning_content`, `content_type`, `content_len`) -- and this line
                    # used to discard all of them, keeping only the message. A run with 34
                    # DeepSeek fallthroughs across 28 cases could therefore not be classified
                    # afterwards: nothing recorded whether the provider returned nothing at all,
                    # returned only reasoning, or was cut off by its own token budget.
                    **_deepseek_failure_diagnostics(exc),
                }})
                logger.warning("LLM_FALLBACK_DEEPSEEK_TO_PREVIOUS_CHAIN", extra={"x": {
                    "stage": stage_name,
                }})

        if deepseek_stage is not None:
            deepseek_raw = str(deepseek_stage.get("response_text") or "").strip()
            deepseek_parse_status = str(deepseek_stage.get("parse_status") or "").strip()
            # Empty / unusable DeepSeek payload must not block the previous provider chain.
            # Exit-2 showed many business_reasoning fallbacks when DeepSeek returned a stage
            # object that later collapsed to "Business reasoning unavailable."
            if output_model is not None and (
                not deepseek_raw
                or deepseek_parse_status in {"empty_content", "parse_failed", "pydantic_failed"}
            ):
                logger.warning("LLM_FALLBACK_DEEPSEEK_EMPTY_TO_PREVIOUS_CHAIN", extra={"x": {
                    "stage": stage_name,
                    "parse_status": deepseek_parse_status or "empty_response_text",
                    "response_len": len(deepseek_raw),
                }})
                deepseek_stage = None

        if deepseek_stage is not None:
            request_meta = dict(deepseek_stage.get("request_meta") or {})
            provider = "deepseek"
            model_name = str(deepseek_stage.get("model_name") or settings.deepseek_model)
            attempt_count = int(deepseek_stage.get("attempt_count") or 1)
            if output_model is None:
                deepseek_stage["assembled_context"] = assembled_context_to_dict(assembled, context_budget=context_budget)
                deepseek_stage["central_llm_provider"] = provider
                return deepseek_stage
            raw_text = str(deepseek_stage.get("response_text") or "")
        elif anthropic_configured(settings):
            try:
                raw_text = _call_with_retry(
                    lambda: _call_anthropic_raw_text(
                        settings,
                        system=system,
                        user_json=user_json,
                        case_id=assembled.case_id_used or case_id,
                        model=model,
                        temperature=temperature,
                    ),
                    stage_name=stage_name,
                    max_retries=max_retries,
                    model=str(model or settings.anthropic_model),
                    provider="anthropic",  # Faza 2a: rate limiting per provider
                )
                request_meta = {"central_llm_provider": "anthropic"}
                request_meta["llm_temperature_requested"] = float(temperature)
                request_meta["llm_determinism_guaranteed"] = False
                request_meta["llm_determinism_basis"] = "provider_dependent"
                provider = "anthropic"
                model_name = str(model or settings.anthropic_model)
                attempt_count = 1
            except TopInstalLLMError as exc:
                logger.error("LLM_ANTHROPIC_FAILED", extra={"x": {
                    "stage": stage_name, "error": str(exc)[:300],
                }})
                logger.warning("LLM_FALLBACK_ANTHROPIC_TO_GROQ", extra={"x": {
                    "stage": stage_name,
                }})
                groq_stage = _call_groq_structured_stage(
                    settings,
                    stage_name=stage_name,
                    system=system,
                    prompt_input=prompt_input,
                    json_schema=json_schema,
                    schema_name=schema_name,
                    model=model,
                    verbose=verbose,
                    input_variants=input_variants,
                    temperature=temperature,
                    correlation_id=resolved_correlation_id or None,
                )
                request_meta = dict(groq_stage.get("request_meta") or {})
                provider = str(
                    request_meta.get("llm_selected_provider")
                    or request_meta.get("central_llm_provider")
                    or provider
                )
                model_name = str(groq_stage.get("model_name") or model or settings.groq_model)
                attempt_count = int(groq_stage.get("attempt_count") or 1)
                if output_model is None:
                    groq_stage["assembled_context"] = assembled_context_to_dict(assembled, context_budget=context_budget)
                    groq_stage["central_llm_provider"] = provider
                    return groq_stage
                raw_text = str(groq_stage.get("response_text") or "")
        else:
            groq_stage = _call_groq_structured_stage(
                settings,
                stage_name=stage_name,
                system=system,
                prompt_input=prompt_input,
                json_schema=json_schema,
                schema_name=schema_name,
                model=model,
                verbose=verbose,
                input_variants=input_variants,
                temperature=temperature,
                correlation_id=resolved_correlation_id or None,
            )
            request_meta = dict(groq_stage.get("request_meta") or {})
            provider = str(
                request_meta.get("llm_selected_provider")
                or request_meta.get("central_llm_provider")
                or provider
            )
            model_name = str(groq_stage.get("model_name") or model or settings.groq_model)
            attempt_count = int(groq_stage.get("attempt_count") or 1)

            if output_model is None:
                groq_stage["assembled_context"] = assembled_context_to_dict(assembled, context_budget=context_budget)
                groq_stage["central_llm_provider"] = provider
                return groq_stage

            raw_text = str(groq_stage.get("response_text") or "")

    latency_ms = int(round((time.monotonic() - started) * 1000))

    # Store only temperature=0 requests; the cache, not the sampling value, makes replay exact.
    if temperature == 0 and not request_meta.get("cache_hit") and raw_text:
        db_url = _get_cache_db_url()
        if db_url:
            cache_key = _build_cache_key(stage_name, provider, model_name, prompt_input, task_instructions)
            _cache_write(cache_key, stage_name, provider, model_name, temperature, raw_text)

    # Bonus: log kosztu LLM jesli znamy provider i tokeny
    tokens_from_meta = int(request_meta.get("usage", {}).get("total_tokens", 0))
    if tokens_from_meta > 0 and provider:
        _cost = estimate_llm_cost(provider, tokens_from_meta, latency_ms)
        logger.info("LLM_COST", extra={"x": {
            "stage": stage_name,
            "provider": provider,
            "model": model_name,
            "tokens": tokens_from_meta,
            "cost_usd": round(_cost, 6),
            "latency_ms": latency_ms,
        }})

    if output_model is None:
        try:
            response_json = json.loads(extract_json_candidate(raw_text))
        except json.JSONDecodeError as exc:
            logger.error("LLM_JSON_PARSE_FAILED", extra={"x": {
                "stage": stage_name, "error": str(exc)[:200],
                "raw_preview": raw_text[:200] if raw_text else "None",
            }})
            return None
        return {
            "stage_name": stage_name,
            "model_name": model_name,
            "attempt_count": attempt_count,
            "latency_ms": latency_ms,
            "response_text": raw_text,
            "parse_status": "received",
            "response_json": response_json,
            "request_meta": {
                **request_meta,
                "central_llm_provider": provider,
                "assembled_context": assembled_context_to_dict(assembled, context_budget=context_budget),
            },
            "prompt_input": prompt_input,
            "assembled_context": assembled_context_to_dict(assembled, context_budget=context_budget),
            "central_llm_provider": provider,
        }

    parsed, pydantic_errors = _validate_output_model(
        raw_text,
        output_model,
        prompt_input=prompt_input,
    )
    if parsed is None:
        try:
            response_json = json.loads(extract_json_candidate(raw_text))
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse LLM JSON response", extra={"x": {
                "error": str(exc),
                "raw_preview": raw_text[:200] if raw_text else "None",
            }})
            response_json = {}
        return {
            "stage_name": stage_name,
            "model_name": model_name,
            "attempt_count": attempt_count,
            "latency_ms": latency_ms,
            "response_text": raw_text,
            "parse_status": "pydantic_failed",
            "response_json": response_json if isinstance(response_json, dict) else {},
            "request_meta": {
                **request_meta,
                "central_llm_provider": provider,
                "assembled_context": assembled_context_to_dict(assembled, context_budget=context_budget),
                "pydantic_errors": pydantic_errors or [],
            },
            "prompt_input": prompt_input,
            "assembled_context": assembled_context_to_dict(assembled, context_budget=context_budget),
            "central_llm_provider": provider,
        }

    return _build_pydantic_stage_result(
        stage_name=stage_name,
        provider=provider,
        model_name=model_name,
        latency_ms=latency_ms,
        parsed=parsed,
        assembled=assembled,
        prompt_input=prompt_input,
        request_meta=request_meta,
        context_budget=context_budget,
        attempt_count=attempt_count,
    )


class CentralLLMStage:
    """Smoke-test facade for central LLM routing."""

    def __init__(self, settings: Settings | None = None) -> None:
        if settings is None:
            from config import load_settings

            settings = load_settings()
        self.settings = settings
        self._assembler = build_context_assembler(settings)

    @property
    def backend_name(self) -> str:
        return primary_llm_provider(self.settings)

    @property
    def has_company_context(self) -> bool:
        from context_assembler import default_company_context_path

        return default_company_context_path().is_file()

    def assemble(self, query_text: str, *, case_id: str | None = None) -> AssembledContext:
        return self._assembler.assemble(query_text, case_id=case_id)
