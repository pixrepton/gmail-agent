"""Central Anthropic HTTP client for structured TOP-INSTAL LLM calls."""

from __future__ import annotations

import json
import os
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Type, TypeVar

import requests
from pydantic import BaseModel, ValidationError

from groq_client import extract_json_candidate
from log_config import get_logger

TModel = TypeVar("TModel", bound=BaseModel)

RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_RETRIES = 3
ANTHROPIC_MESSAGES_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1/messages")
ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "2023-06-01")

logger = get_logger(__name__)


class TopInstalLLMError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


@dataclass(slots=True)
class LLMCallLog:
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    case_id: str


class TopInstalLLMClient:
    """Anthropic Messages API client with retries, logging, and Pydantic validation."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        base_url: str = ANTHROPIC_MESSAGES_URL,
        timeout_sec: float = 60.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        temperature: float = DEFAULT_TEMPERATURE,
        session: requests.Session | None = None,
    ) -> None:
        key = str(api_key or "").strip()
        if not key:
            raise TopInstalLLMError("ANTHROPIC_API_KEY is not configured.")
        self.api_key = key
        self.model = model
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/v1/messages"):
            self.messages_url = self.base_url
        else:
            self.messages_url = f"{self.base_url}/v1/messages"
        self.timeout_sec = float(timeout_sec)
        self.max_retries = max(1, int(max_retries))
        self.temperature = float(temperature)
        self._session = session or requests.Session()

    def complete_structured(
        self,
        *,
        system: str,
        user: str,
        output_model: Type[TModel],
        case_id: str | None = None,
        model: str | None = None,
    ) -> TModel:
        response_json, usage, latency_ms = self._post_messages(
            system=system,
            user=user,
            case_id=case_id,
            model=model,
            schema_hint=json.dumps(output_model.model_json_schema(), ensure_ascii=False),
        )
        text = _extract_message_text(response_json)
        try:
            payload = json.loads(extract_json_candidate(text))
        except json.JSONDecodeError as exc:
            raise TopInstalLLMError(f"LLM response was not valid JSON: {exc}") from exc
        try:
            parsed = output_model.model_validate(payload)
        except ValidationError as exc:
            raise TopInstalLLMError(f"LLM JSON failed schema validation: {exc}") from exc

        self._log_call(model=model, usage=usage, latency_ms=latency_ms, case_id=case_id)
        return parsed

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        case_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        response_json, usage, latency_ms = self._post_messages(
            system=system,
            user=user,
            case_id=case_id,
            model=model,
            schema_hint="Return one JSON object only. No markdown fences.",
        )
        text = _extract_message_text(response_json)
        try:
            payload = json.loads(extract_json_candidate(text))
        except json.JSONDecodeError as exc:
            raise TopInstalLLMError(f"LLM response was not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise TopInstalLLMError("LLM JSON root must be an object.")
        self._log_call(model=model, usage=usage, latency_ms=latency_ms, case_id=case_id)
        return payload

    def _log_call(
        self,
        *,
        model: str | None,
        usage: dict[str, int],
        latency_ms: int,
        case_id: str | None,
    ) -> None:
        log_row = LLMCallLog(
            model=str(model or self.model),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=latency_ms,
            case_id=str(case_id or ""),
        )
        logger.info(
            "topinstal_llm_call model=%s input_tokens=%s output_tokens=%s latency_ms=%s case_id=%s",
            log_row.model,
            log_row.input_tokens,
            log_row.output_tokens,
            log_row.latency_ms,
            log_row.case_id,
        )

    def _post_messages(
        self,
        *,
        system: str,
        user: str,
        case_id: str | None,
        model: str | None,
        schema_hint: str,
    ) -> tuple[dict[str, Any], dict[str, int], int]:
        user_payload = (
            f"{user.strip()}\n\n"
            "Respond with a single JSON object only (no markdown fences) matching this schema:\n"
            f"{schema_hint}"
        )
        body: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": 4096,
            "temperature": self.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user_payload}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            started = time.monotonic()
            try:
                response = self._session.post(
                    self.messages_url,
                    headers=headers,
                    json=body,
                    timeout=self.timeout_sec,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise TopInstalLLMError(
                        f"Anthropic transport error after {attempt} attempts: {exc}"
                    ) from exc
                _sleep_backoff(attempt)
                continue

            latency_ms = int(round((time.monotonic() - started) * 1000))
            if response.status_code >= 400:
                retryable = response.status_code in RETRYABLE_HTTP_STATUSES
                last_error = TopInstalLLMError(
                    f"Anthropic HTTP {response.status_code}: {response.text[:500]}",
                    details={"status_code": response.status_code, "latency_ms": latency_ms},
                )
                if not retryable or attempt >= self.max_retries:
                    raise last_error
                _sleep_backoff(attempt)
                continue

            response_json = response.json()
            usage = response_json.get("usage") if isinstance(response_json.get("usage"), dict) else {}
            normalized_usage = {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
            }
            return response_json, normalized_usage, latency_ms

        raise TopInstalLLMError(
            f"Anthropic call failed after {self.max_retries} attempts.",
            details={"last_error": str(last_error)},
        )


def _extract_message_text(response_json: dict[str, Any]) -> str:
    content = response_json.get("content")
    if not isinstance(content, list):
        raise TopInstalLLMError("Anthropic response missing content blocks.")
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text") or "")
            if text:
                parts.append(text)
    if not parts:
        raise TopInstalLLMError("Anthropic response has no text blocks.")
    return "\n".join(parts).strip()


def _sleep_backoff(attempt: int) -> None:
    delay = min(8.0, 0.5 * (2 ** (attempt - 1)))
    time.sleep(delay + random.uniform(0.0, 0.25))
