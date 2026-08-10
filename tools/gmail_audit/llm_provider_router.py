"""Bounded provider routing for structured LLM calls."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from llm_deadline import (
    DEFAULT_LLM_MIN_ATTEMPT_SEC,
    DeadlineExhausted,
    current_deadline,
    provider_budget_scope,
    provider_budget_sec,
)


ProviderCall = Callable[[], tuple[dict[str, Any], dict[str, Any]]]


@dataclass(slots=True)
class LLMProvider:
    provider: str
    backend: str
    model: str
    call: ProviderCall
    configured: bool = True
    missing_config: str = ""


@dataclass(slots=True)
class ProviderErrorInfo:
    error_class: str
    retryable: bool


class LLMRouterError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.details = details


class LLMRouter:
    """Try providers in order; fallback on transient or provider-local HTTP failures.

    Fallback is only real if it still has time to run. When a stage deadline is active
    (see ``llm_deadline``) each provider is given an even share of the *remaining* stage
    budget, so a slow primary cannot silently consume the fallback's turn -- the failure
    mode Fresh38 measured as 6/38 ``INTAKE_LLM_TIMEOUT``. Unspent time rolls forward, so a
    fast failure costs the chain nothing and the last provider gets the whole remainder.

    With no deadline active the routing behavior is exactly as before.
    """

    def __init__(self, providers: list[LLMProvider], *, min_attempt_sec: float = DEFAULT_LLM_MIN_ATTEMPT_SEC) -> None:
        self.providers = providers
        self.min_attempt_sec = float(min_attempt_sec)

    def _configured_remaining(self, from_index: int) -> int:
        return sum(1 for p in self.providers[from_index:] if p.configured)

    def run(self) -> tuple[dict[str, Any], dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        first_failure_reason = ""
        deadline = current_deadline()

        for index, provider in enumerate(self.providers):
            started = time.monotonic()

            # Budget check happens before the call, never after: starting an attempt that
            # cannot finish converts remaining budget into a guaranteed timeout and hides
            # the real reason the chain stopped.
            if deadline is not None and provider.configured and not deadline.has_room_for_attempt(self.min_attempt_sec):
                attempts.append(
                    _attempt_record(
                        provider=provider,
                        status="skipped",
                        error_class="deadline_exhausted",
                        retryable=False,
                        started=started,
                        deadline=deadline,
                    )
                )
                if index == 0:
                    first_failure_reason = "stage_deadline_exhausted_before_first_provider"
                break

            if not provider.configured:
                # An unconfigured provider is never "tried" (no call() invocation — there is
                # nothing to call), but it must not fake chain-terminal behavior either: its
                # position in the list must not silently decide whether a later, genuinely
                # usable provider ever gets a turn. Record it as skipped and continue.
                attempt = _attempt_record(
                    provider=provider,
                    status="skipped",
                    error_class="config",
                    retryable=False,
                    started=started,
                )
                attempts.append(attempt)
                if index == 0:
                    first_failure_reason = f"{provider.provider}_config_missing_{provider.missing_config}"
                continue

            share = provider_budget_sec(
                providers_remaining=self._configured_remaining(index),
                deadline=deadline,
            )
            try:
                with provider_budget_scope(provider.provider, share):
                    response_json, request_meta = provider.call()
            except Exception as exc:
                info = classify_provider_error(exc)
                attempt = _attempt_record(
                    provider=provider,
                    status="failed",
                    error_class=info.error_class,
                    retryable=info.retryable,
                    started=started,
                    deadline=deadline,
                    provider_budget_sec=share,
                )
                attempts.append(attempt)
                if index == 0:
                    first_failure_reason = f"{provider.provider}_{info.error_class}"
                if not info.retryable or index + 1 >= len(self.providers):
                    details = dict(getattr(exc, "details", {}) or {})
                    details.update(
                        _error_details(
                            attempts=attempts,
                            fallback_used=index > 0,
                            fallback_reason=first_failure_reason if index > 0 else None,
                        )
                    )
                    # Both terminal paths out of this router must describe themselves the same
                    # way. Callers key on terminal_failure_reason to tell a budget timeout from
                    # a provider fault -- gmail_intake returns a structured "central_llm_failed"
                    # for the former and re-raises the latter -- so leaving it unset here would
                    # make a budget exhausted *inside* the last provider's retry loop
                    # indistinguishable from an ordinary provider error.
                    details["terminal_failure_reason"] = (
                        "stage_deadline_exhausted"
                        if info.error_class == "deadline_exhausted"
                        else "provider_chain_failed"
                    )
                    if deadline is not None:
                        details["llm_stage_deadline"] = deadline.telemetry()
                    raise LLMRouterError(str(exc), details=details) from exc
                continue

            attempt = _attempt_record(
                provider=provider,
                status="success",
                error_class=None,
                retryable=None,
                started=started,
                deadline=deadline,
                provider_budget_sec=share,
            )
            attempts.append(attempt)
            meta = dict(request_meta or {})
            meta.update(
                {
                    "llm_provider_attempts": attempts,
                    "llm_selected_provider": provider.provider,
                    "llm_fallback_used": index > 0,
                    "llm_fallback_reason": first_failure_reason if index > 0 else None,
                    "llm_provider_backend": provider.backend,
                    "llm_fallback_index": index,
                }
            )
            if deadline is not None:
                meta["llm_stage_deadline"] = deadline.telemetry()
            return response_json, meta

        details = _error_details(
            attempts=attempts,
            fallback_used=len(attempts) > 1,
            fallback_reason=first_failure_reason or None,
        )
        deadline_terminal = any(a.get("error_class") == "deadline_exhausted" for a in attempts)
        details["terminal_failure_reason"] = (
            "stage_deadline_exhausted" if deadline_terminal else "provider_chain_exhausted"
        )
        if deadline is not None:
            details["llm_stage_deadline"] = deadline.telemetry()
        raise LLMRouterError(_exhausted_chain_message(self.providers, attempts), details=details)


def _exhausted_chain_message(providers: list[LLMProvider], attempts: list[dict[str, Any]]) -> str:
    """Build an explicit, jointly-readable summary of every provider tried or skipped.

    A generic "no provider configured" message hides whether the chain had 0 or 3
    entries, and whether the loss was a real failure or a config gap. This names each
    provider's outcome so an operator reading the raised error does not need to grep
    ``llm_provider_attempts`` separately to understand why the whole chain died.
    """
    if not attempts:
        return "No LLM provider was configured for structured output."
    parts: list[str] = []
    for attempt in attempts:
        provider_name = attempt.get("provider")
        status = attempt.get("status")
        if status == "skipped" and attempt.get("error_class") == "deadline_exhausted":
            remaining = attempt.get("remaining_budget_ms")
            budget = attempt.get("configured_stage_budget_ms")
            parts.append(
                f"{provider_name} not attempted (stage budget exhausted: "
                f"{remaining}ms left of {budget}ms)"
            )
        elif status == "skipped":
            missing = next(
                (p.missing_config for p in providers if p.provider == provider_name and not p.configured),
                "",
            )
            parts.append(f"{provider_name} unconfigured (missing {missing})")
        else:
            parts.append(f"{provider_name} failed ({attempt.get('error_class')})")
    return "All LLM providers exhausted: " + "; ".join(parts) + "."


def _is_empty_message_content_error(message: str, details: dict[str, Any]) -> bool:
    """Detect empty assistant text on structured/text paths (retryable DELIVERY).

    Matches OpenAI-compatible ``message.content`` failures and Responses-API empty
    assistant text. Does not treat malformed JSON as empty content.
    """
    if str(details.get("error_class") or "").strip().lower() == "empty_content":
        return True
    text = message.lower()
    if "message.content" in text and (
        "empty" in text or "none" in text or "null" in text or "whitespace" in text
    ):
        return True
    if "empty content" in text:
        return True
    if "empty" in text and "content" in text and (
        "message" in text or "assistant" in text or "response" in text
    ):
        return True
    if "does not contain final assistant text" in text:
        return True
    return False


def classify_provider_error(exc: Exception) -> ProviderErrorInfo:
    details = dict(getattr(exc, "details", {}) or {})
    status_code = details.get("status_code")
    message = str(exc).lower()

    # Budget exhaustion is terminal for the whole chain, never a retryable provider fault:
    # falling through to the next provider with no time left just relabels the same failure.
    if isinstance(exc, DeadlineExhausted) or str(details.get("error_class") or "") == "deadline_exhausted":
        return ProviderErrorInfo("deadline_exhausted", False)

    if status_code == 429 or "too many requests" in message or "rate limit" in message:
        return ProviderErrorInfo("rate_limit", True)
    if status_code == 400 and (
        "json_validate" in message
        or "failed_generation" in message
        or "failed to generate json" in message
    ):
        return ProviderErrorInfo("json_schema", True)
    if status_code == 402 or "requires more credits" in message or "insufficient" in message and "credit" in message:
        return ProviderErrorInfo("quota_exhausted", True)
    if status_code in {404, 408} or (
        status_code is None and (" 404 " in f" {message} " or "not found" in message)
    ):
        return ProviderErrorInfo("not_found", True)
    if isinstance(status_code, int) and status_code >= 500:
        return ProviderErrorInfo("server_error", True)
    if "timeout" in message:
        return ProviderErrorInfo("timeout", True)
    if "failed to connect" in message or "network" in message or "connection" in message:
        return ProviderErrorInfo("network", True)
    if status_code in {401, 403} or "invalid api key" in message or "unauthorized" in message or "forbidden" in message:
        return ProviderErrorInfo("auth", False)
    if "missing" in message and ("api_key" in message or "base_url" in message or "configured" in message):
        return ProviderErrorInfo("config", False)
    # Empty provider text is a transient/delivery failure, never a domain CAPABILITY result.
    # Must run before the generic contract default so router/fallback can continue the chain.
    if _is_empty_message_content_error(message, details):
        return ProviderErrorInfo("empty_content", True)
    return ProviderErrorInfo("contract", False)


def _attempt_record(
    *,
    provider: LLMProvider,
    status: str,
    error_class: str | None,
    retryable: bool | None,
    started: float,
    deadline: Any = None,
    provider_budget_sec: float | None = None,
) -> dict[str, Any]:
    """One provider's outcome plus the budget state that produced it.

    The budget fields are what make a production timeout diagnosable without
    reconstructing it from log timestamps: which provider consumed the budget, how much
    it was given, and how much was left when the chain moved on.
    """
    record: dict[str, Any] = {
        "provider": provider.provider,
        "model": provider.model,
        "status": status,
        "error_class": error_class,
        "retryable": retryable,
        "latency_ms": int(round((time.monotonic() - started) * 1000)),
    }
    if provider_budget_sec is not None:
        record["provider_budget_ms"] = int(round(float(provider_budget_sec) * 1000))
    if deadline is not None:
        record["remaining_budget_ms"] = int(round(deadline.remaining_sec() * 1000))
        record["configured_stage_budget_ms"] = int(round(deadline.budget_sec * 1000))
    return record


def _error_details(
    *,
    attempts: list[dict[str, Any]],
    fallback_used: bool,
    fallback_reason: str | None,
) -> dict[str, Any]:
    return {
        "llm_provider_attempts": attempts,
        "llm_selected_provider": None,
        "llm_fallback_used": fallback_used,
        "llm_fallback_reason": fallback_reason,
        "llm_provider_backend": None,
    }
