"""Circuit Breaker per LLM provider — prevents cascading failures.

Usage:
    from agent_runtime.circuit_breaker import get_breaker

    breaker = get_breaker(provider_name)
    if breaker.is_open:
        continue  # skip to next provider

    try:
        result = call_llm(...)
        breaker.record_success()
    except Exception:
        breaker.record_failure()
        raise
"""

from __future__ import annotations

import time
from enum import Enum

from log_config import get_logger

logger = get_logger("circuit_breaker")


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Tracks consecutive failures per provider.

    After `failure_threshold` failures the circuit opens.
    After `recovery_timeout_sec` seconds it transitions to half-open.
    One success in half-open closes the circuit again.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout_sec: float = 30.0,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout_sec
        self._state = State.CLOSED
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._state == State.OPEN:
            if self._opened_at is not None and time.monotonic() - self._opened_at >= self.recovery_timeout:
                self._state = State.HALF_OPEN
                logger.info("CIRCUIT_HALF_OPEN", extra={"x": {"provider": self.name}})
                return False
            return True
        return False

    def record_success(self) -> None:
        if self._state == State.HALF_OPEN:
            logger.info("CIRCUIT_CLOSED", extra={"x": {"provider": self.name}})
        self._state = State.CLOSED
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = State.OPEN
            self._opened_at = time.monotonic()
            logger.warning("CIRCUIT_OPEN", extra={"x": {
                "provider": self.name,
                "failures": self._failures,
            }})


# Singleton registry per provider name
_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(provider_name: str) -> CircuitBreaker:
    """Return (or create) the circuit breaker for a given provider."""
    if provider_name not in _breakers:
        _breakers[provider_name] = CircuitBreaker(name=provider_name)
    return _breakers[provider_name]
